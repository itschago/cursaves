import sqlite3

from cursor_saves import db, paths

from conftest import make_state_db


def test_read_copy_includes_uncheckpointed_wal_writes(tmp_path):
    src = make_state_db(tmp_path / "live" / "state.vscdb")
    writer = sqlite3.connect(str(src))
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO cursorDiskKV VALUES ('composerData:1', '{\"name\": \"x\"}')")
    writer.commit()
    try:
        assert (src.parent / "state.vscdb-wal").stat().st_size > 0
        with db.CursorDB(src) as cdb:
            assert cdb.get_json("composerData:1") == {"name": "x"}
    finally:
        writer.close()


def test_read_copy_while_write_transaction_open_sees_last_commit(tmp_path):
    src = make_state_db(tmp_path / "live" / "state.vscdb", disk_kv={"a": "committed"})
    writer = sqlite3.connect(str(src), isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE cursorDiskKV SET value='uncommitted' WHERE key='a'")
    try:
        with db.CursorDB(src) as cdb:
            assert cdb.get_disk_kv("a") == "committed"
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_backup_is_single_file_outside_cursor_dirs(cursor_env):
    gdb = paths.get_global_db_path()
    with db.CursorDB(gdb) as cdb:
        cdb.write_item("k", "v", table="cursorDiskKV")

    backup = db.backup_db(gdb)

    assert backup.parent == cursor_env["home"] / ".local/share/cursaves/backups/global"
    assert not backup.with_name(backup.name + "-wal").exists()
    conn = sqlite3.connect(str(backup))
    assert conn.execute("SELECT value FROM cursorDiskKV WHERE key='k'").fetchone() == ("v",)
    conn.close()
    assert not list(gdb.parent.glob("*backup*"))


def test_workspace_backups_are_labelled_by_hash(cursor_env):
    ws_db = make_state_db(cursor_env["user_dir"] / "workspaceStorage" / "abc123" / "state.vscdb")
    assert db.backup_db(ws_db).parent.name == "workspace-abc123"


def test_prune_keeps_recent_and_first_of_each_recent_day(tmp_path):
    names = [
        "state_20260101_090000_000000.vscdb",  # day 1 first
        "state_20260101_100000_000000.vscdb",
        "state_20260102_090000_000000.vscdb",  # day 2 first
        "state_20260103_090000_000000.vscdb",  # day 3 first
        "state_20260103_100000_000000.vscdb",
        "state_20260104_090000_000000.vscdb",  # day 4 first
        "state_20260104_100000_000000.vscdb",
        "state_20260104_110000_000000.vscdb",
    ]
    for n in names:
        (tmp_path / n).write_text("")

    db._prune_backups(tmp_path, keep_recent=2, keep_daily=3)

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "state_20260102_090000_000000.vscdb",
        "state_20260103_090000_000000.vscdb",
        "state_20260104_090000_000000.vscdb",
        "state_20260104_100000_000000.vscdb",
        "state_20260104_110000_000000.vscdb",
    ]


def test_repeated_backups_never_drop_earlier_days(cursor_env):
    gdb = paths.get_global_db_path()
    backup_dir = cursor_env["home"] / ".local/share/cursaves/backups/global"
    backup_dir.mkdir(parents=True)
    good = backup_dir / "state_20200101_000000_000000.vscdb"
    good.write_text("")

    for _ in range(5):
        db.backup_db(gdb)

    assert good.exists()
    # the 2020 backup, today's first, and the two most recent
    assert len(list(backup_dir.glob("state_*.vscdb"))) == 4
