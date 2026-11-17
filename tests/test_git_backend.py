"""GitBackend pull/push: local snapshot work survives syncing with the remote."""

import subprocess
from pathlib import Path

import pytest

from cursor_saves.backends import GitBackend


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for kind in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{kind}_NAME", "test")
        monkeypatch.setenv(f"GIT_{kind}_EMAIL", "test@example.com")


@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(bare), str(seed))
    write_chat(seed, "seed", "v1")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "seed")
    git(seed, "push", "origin", "HEAD:main")
    return bare


def clone(remote: Path, dest: Path) -> GitBackend:
    git(dest.parent, "clone", str(remote), str(dest))
    return GitBackend(dest)


def write_chat(repo: Path, cid: str, content: str, project: str = "proj") -> None:
    d = repo / "snapshots" / project
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{cid}.json.gz").write_text(content)
    (d / f"{cid}.meta.json").write_text(f'{{"v": "{content}"}}')


def read_chat(repo: Path, cid: str, project: str = "proj"):
    p = repo / "snapshots" / project / f"{cid}.json.gz"
    return p.read_text() if p.exists() else None


def remote_chat(remote: Path, cid: str, project: str = "proj"):
    r = subprocess.run(
        ["git", "show", f"main:snapshots/{project}/{cid}.json.gz"],
        cwd=str(remote), capture_output=True, text=True,
    )
    return r.stdout if r.returncode == 0 else None


def snaps(b: GitBackend) -> Path:
    return b.sync_dir / "snapshots"


def test_unpushed_commit_survives_pull(remote, tmp_path):
    a = clone(remote, tmp_path / "a")
    b = clone(remote, tmp_path / "b")

    write_chat(a.sync_dir, "mine", "local")
    a._commit_local_changes()  # committed but never pushed (e.g. push failed)

    write_chat(b.sync_dir, "theirs", "remote")
    assert b.push(snaps(b))

    assert a.pull(snaps(a))
    assert read_chat(a.sync_dir, "mine") == "local"
    assert read_chat(a.sync_dir, "theirs") == "remote"

    assert a.push(snaps(a))
    assert remote_chat(remote, "mine") == "local"
    assert remote_chat(remote, "theirs") == "remote"


def test_uncommitted_change_survives_pull(remote, tmp_path):
    a = clone(remote, tmp_path / "a")
    b = clone(remote, tmp_path / "b")

    write_chat(a.sync_dir, "mine", "local")
    write_chat(b.sync_dir, "theirs", "remote")
    assert b.push(snaps(b))

    assert a.pull(snaps(a))
    assert read_chat(a.sync_dir, "mine") == "local"
    assert read_chat(a.sync_dir, "theirs") == "remote"
    assert git(a.sync_dir, "status", "--porcelain") == ""


def test_rejected_push_resyncs_and_retries(remote, tmp_path):
    a = clone(remote, tmp_path / "a")
    b = clone(remote, tmp_path / "b")

    write_chat(b.sync_dir, "theirs", "remote")
    assert b.push(snaps(b))

    write_chat(a.sync_dir, "mine", "local")
    assert a.push(snaps(a))  # first attempt is rejected as non-fast-forward

    assert remote_chat(remote, "mine") == "local"
    assert remote_chat(remote, "theirs") == "remote"


def test_same_chat_changed_on_both_sides_keeps_remote(remote, tmp_path, capsys):
    a = clone(remote, tmp_path / "a")
    b = clone(remote, tmp_path / "b")

    write_chat(b.sync_dir, "seed", "from-b")
    assert b.push(snaps(b))

    write_chat(a.sync_dir, "seed", "from-a")
    write_chat(a.sync_dir, "other", "a-only")
    assert a.pull(snaps(a))

    assert read_chat(a.sync_dir, "seed") == "from-b"
    meta = (a.sync_dir / "snapshots/proj/seed.meta.json").read_text()
    assert "from-b" in meta  # whole chat comes from one side
    assert read_chat(a.sync_dir, "other") == "a-only"
    assert "1 chat(s) changed both here and on the remote" in capsys.readouterr().err


def test_local_delete_survives_pull(remote, tmp_path):
    a = clone(remote, tmp_path / "a")
    b = clone(remote, tmp_path / "b")

    for f in (snaps(a) / "proj").glob("seed.*"):
        f.unlink()
    write_chat(b.sync_dir, "theirs", "remote")
    assert b.push(snaps(b))

    assert a.pull(snaps(a))
    assert read_chat(a.sync_dir, "seed") is None
    assert a.push(snaps(a))
    assert remote_chat(remote, "seed") is None


def test_remote_delete_applies_when_untouched_locally(remote, tmp_path):
    a = clone(remote, tmp_path / "a")
    b = clone(remote, tmp_path / "b")

    for f in (snaps(b) / "proj").glob("seed.*"):
        f.unlink()
    assert b.push(snaps(b))

    assert a.pull(snaps(a))
    assert read_chat(a.sync_dir, "seed") is None


def test_first_push_to_empty_remote(tmp_path):
    bare = tmp_path / "empty.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    a = clone(bare, tmp_path / "a")

    assert a.pull(snaps(a))
    write_chat(a.sync_dir, "mine", "local")
    assert a.push(snaps(a))
    assert remote_chat(bare, "mine") == "local"


def test_unrelated_local_history_merges_with_remote(remote, tmp_path):
    """A second machine that ran `init` on its own before adding the remote."""
    local = tmp_path / "local"
    local.mkdir()
    git(local, "init", "-b", "main")
    write_chat(local, "mine", "local")
    write_chat(local, "seed", "local-version")
    git(local, "add", "-A")
    git(local, "commit", "-m", "local init")
    git(local, "remote", "add", "origin", str(remote))
    a = GitBackend(local)

    assert a.pull(snaps(a))
    assert read_chat(local, "mine") == "local"
    assert read_chat(local, "seed") == "v1"  # remote wins where both have the chat

    assert a.push(snaps(a))
    assert remote_chat(remote, "mine") == "local"
