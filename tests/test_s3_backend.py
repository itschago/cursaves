"""S3Backend: three-way sync per chat, deletions propagate, no blind overwrites."""

import hashlib
import io
from pathlib import Path

import pytest

from cursor_saves import backends
from cursor_saves.backends import S3Backend


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    @staticmethod
    def _etag(body: bytes) -> str:
        return f'"{hashlib.md5(body).hexdigest()}"'

    def get_paginator(self, name):
        s3 = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [
                    {"Key": k, "ETag": s3._etag(v), "Size": len(v)}
                    for k, v in sorted(s3.objects.items()) if k.startswith(Prefix)
                ]}

        return Paginator()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body
        return {"ETag": self._etag(Body)}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


@pytest.fixture
def s3():
    return FakeS3()


class Machine:
    """One machine: its own snapshots dir and its own manifest."""

    def __init__(self, root: Path, s3: FakeS3, monkeypatch):
        self.root = root
        self.snapshots = root / "snapshots"
        self.snapshots.mkdir(parents=True)
        self.backend = S3Backend("bucket")
        self.backend._client = s3
        self._monkeypatch = monkeypatch

    def _use(self):
        self._monkeypatch.setattr(backends, "_CONFIG_PATH", self.root / "config" / "config.json")

    def pull(self):
        self._use()
        return self.backend.pull(self.snapshots)

    def push(self):
        self._use()
        return self.backend.push(self.snapshots)

    def write(self, cid, content, project="proj"):
        d = self.snapshots / project
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{cid}.json.gz").write_text(content)
        (d / f"{cid}.meta.json").write_text(f'{{"v": "{content}"}}')

    def read(self, cid, project="proj"):
        p = self.snapshots / project / f"{cid}.json.gz"
        return p.read_text() if p.exists() else None

    def delete(self, cid, project="proj"):
        for f in (self.snapshots / project).glob(f"{cid}.*"):
            f.unlink()


@pytest.fixture
def machines(tmp_path, s3, monkeypatch):
    return (
        Machine(tmp_path / "a", s3, monkeypatch),
        Machine(tmp_path / "b", s3, monkeypatch),
    )


def remote(s3, cid, project="proj"):
    body = s3.objects.get(f"snapshots/{project}/{cid}.json.gz")
    return body.decode() if body is not None else None


def test_push_then_pull_round_trip(machines, s3):
    a, b = machines
    a.write("c1", "v1")
    assert a.push()
    assert remote(s3, "c1") == "v1"
    assert b.pull()
    assert b.read("c1") == "v1"


def test_local_delete_propagates(machines, s3):
    a, b = machines
    a.write("c1", "v1")
    a.push()
    b.pull()

    a.delete("c1")
    assert a.push()
    assert remote(s3, "c1") is None
    assert not any(k.startswith("snapshots/proj/c1.") for k in s3.objects)

    assert b.pull()
    assert b.read("c1") is None


def test_older_local_file_not_overwritten_unless_remote_changed(machines, s3):
    """mtime no longer matters: an unsynced local edit is never clobbered by a stale remote."""
    a, b = machines
    a.write("c1", "v1")
    a.push()
    b.pull()

    b.write("c1", "b-edit")
    assert b.pull()  # remote unchanged since last sync
    assert b.read("c1") == "b-edit"
    assert b.push()
    assert remote(s3, "c1") == "b-edit"


def test_push_does_not_overwrite_remote_changed_since_pull(machines, s3, capsys):
    a, b = machines
    a.write("c1", "v1")
    a.push()
    b.pull()

    a.write("c1", "a-edit")
    a.push()
    b.write("c1", "b-edit")
    assert b.push()
    assert remote(s3, "c1") == "a-edit"
    assert "Skipped 1 chat(s)" in capsys.readouterr().err


def test_pull_conflict_keeps_remote_whole_chat(machines, s3):
    a, b = machines
    a.write("c1", "v1")
    a.push()
    b.pull()

    a.write("c1", "a-edit")
    a.push()
    b.write("c1", "b-edit")
    assert b.pull()
    assert b.read("c1") == "a-edit"
    assert "a-edit" in (b.snapshots / "proj/c1.meta.json").read_text()


def test_new_local_chat_not_deleted_by_pull(machines, s3):
    a, b = machines
    a.write("c1", "v1")
    a.push()
    b.write("mine", "local-only")
    assert b.pull()
    assert b.read("mine") == "local-only"
    assert b.read("c1") == "v1"


def test_first_sync_with_identical_content_is_quiet(machines, s3):
    """Upgrading with no manifest: matching files are recorded, not re-transferred."""
    a, b = machines
    a.write("c1", "v1")
    a.push()
    b.write("c1", "v1")
    s3_before = dict(s3.objects)
    assert b.pull() and b.push()
    assert s3.objects == s3_before
    a.delete("c1")
    a.push()
    assert b.pull()
    assert b.read("c1") is None
