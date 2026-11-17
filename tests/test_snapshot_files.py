import os

from cursor_saves import export, importer

from chat_fixtures import write_snapshot


def _files(project_dir):
    return {p.name: p.read_bytes() for p in sorted(project_dir.iterdir())}


def test_compression_is_deterministic():
    snap = {"composerId": "x", "data": "hello" * 100}
    assert export._compress_snapshot(snap) == export._compress_snapshot(dict(reversed(snap.items())))


def test_resaving_unchanged_chat_leaves_files_untouched(cursor_env, tmp_path):
    proj = str(tmp_path / "myproj")
    path = write_snapshot("c1", ["a", "b"], proj, exported_at="2026-01-01T00:00:00+00:00")
    before = _files(path.parent)
    mtime = os.stat(path).st_mtime_ns

    write_snapshot("c1", ["a", "b"], proj, exported_at="2026-06-01T00:00:00+00:00")

    assert _files(path.parent) == before
    assert os.stat(path).st_mtime_ns == mtime
    assert importer.read_snapshot_meta(path)["exportedAt"] == "2026-01-01T00:00:00+00:00"


def test_changed_chat_is_rewritten(cursor_env, tmp_path):
    proj = str(tmp_path / "myproj")
    path = write_snapshot("c1", ["a", "b"], proj)
    write_snapshot("c1", ["a", "b", "c"], proj, exported_at="2026-06-01T00:00:00+00:00")

    data = importer.read_snapshot_file(path)
    assert len(data["composerData"]["fullConversationHeadersOnly"]) == 3
    assert importer.read_snapshot_meta(path)["headerIds"] == ["a", "b", "c"]


def test_missing_snapshot_file_is_rewritten_even_if_meta_matches(cursor_env, tmp_path):
    proj = str(tmp_path / "myproj")
    path = write_snapshot("c1", ["a"], proj)
    path.unlink()
    write_snapshot("c1", ["a"], proj)
    assert path.exists()
