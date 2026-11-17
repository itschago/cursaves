"""Import reads the DB in place and writes each chat atomically."""

import pytest

from cursor_saves import db, importer, paths

from chat_fixtures import all_chat_ids, local_chat, write_snapshot
from conftest import make_workspace


@pytest.fixture
def project(cursor_env, tmp_path):
    proj = tmp_path / "work" / "myproj"
    proj.mkdir(parents=True)
    ws_dir = make_workspace(cursor_env["user_dir"], "ws1", str(proj))
    return {"path": str(proj), "ws_dir": ws_dir}


def test_import_never_copies_the_global_db(project, monkeypatch):
    copied = []
    real = db.snapshot_sqlite_db

    def tracking(src, dest):
        copied.append(src)
        real(src, dest)

    monkeypatch.setattr(db, "snapshot_sqlite_db", tracking)
    snap = write_snapshot("c1", ["a", "b"], project["path"], name="Chat")
    assert importer.import_snapshot(snap, project["path"], project["ws_dir"], skip_backup=True)
    assert local_chat("c1")["name"] == "Chat"
    assert paths.get_global_db_path() not in copied


def test_failed_write_leaves_no_partial_chat(project, monkeypatch):
    real_batch = db.CursorDB.write_batch
    calls = {"n": 0}

    def failing_batch(self, items, table="cursorDiskKV"):
        calls["n"] += 1
        if calls["n"] == 2:  # after composerData, during the bubbles
            raise RuntimeError("disk full")
        return real_batch(self, items, table)

    monkeypatch.setattr(db.CursorDB, "write_batch", failing_batch)
    snap = write_snapshot("c1", ["a", "b"], project["path"], name="Chat")
    with pytest.raises(RuntimeError):
        importer.import_snapshot(snap, project["path"], project["ws_dir"], skip_backup=True)

    assert "c1" not in all_chat_ids()
    with db.CursorDB(paths.get_global_db_path()) as cdb:
        assert cdb.list_keys("bubbleId:c1:") == []


def test_list_keys_prefix_is_literal(cursor_env):
    with db.CursorDB(paths.get_global_db_path()) as cdb:
        cdb.write_batch([
            ("bubbleId:c1:x", "1"), ("bubbleId:c10:y", "2"),
            ("BUBBLEID:c1:z", "3"), ("bubbleId:c1_:w", "4"),
        ])
    with db.CursorDB(paths.get_global_db_path()) as cdb:
        assert cdb.list_keys("bubbleId:c1:") == ["bubbleId:c1:x"]
