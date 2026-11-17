import pytest

from cursor_saves import db, paths


@pytest.mark.parametrize(
    "line, expected",
    [
        ("/Applications/Cursor.app/Contents/MacOS/Cursor", True),
        ("/Users/me/Applications/Cursor.app/Contents/MacOS/Cursor --foo", True),
        ("/Applications/Cursor.app/Contents/Frameworks/Cursor Helper (Renderer).app/Contents/MacOS/Cursor Helper (Renderer) --type=renderer", False),
        ("/System/Library/PrivateFrameworks/CursorUIViewService.app/Contents/MacOS/CursorUIViewService", False),
        ("/usr/bin/python3 -m cursaves sync", False),
    ],
)
def test_macos_process_detection(line, expected):
    assert paths._is_cursor_main_process(line, "Darwin") is expected


@pytest.mark.parametrize(
    "line, expected",
    [
        ("/usr/share/cursor/cursor", True),
        ("/tmp/.mount_CursorAbc123/cursor --no-sandbox", True),
        ("/home/me/Applications/Cursor-1.2.3-x86_64.AppImage", True),
        ("/usr/share/cursor/cursor --type=renderer --enable-crash-reporter", False),
        ("/usr/share/cursor/cursor --type=zygote", False),
        ("/home/me/.cursor-server/bin/abc/node server.js", False),
        ("cursor-agent chat", False),
        ("", False),
    ],
)
def test_linux_process_detection(line, expected):
    assert paths._is_cursor_main_process(line, "Linux") is expected


def test_writes_blocked_while_cursor_running(cursor_env, monkeypatch):
    monkeypatch.setattr(paths, "is_cursor_running", lambda: True)
    with db.CursorDB(paths.get_global_db_path()) as cdb:
        with pytest.raises(db.CursorRunningError):
            cdb.write_item("k", "v")


def test_force_allows_writes_while_cursor_running(cursor_env, monkeypatch):
    monkeypatch.setattr(paths, "is_cursor_running", lambda: True)
    db.allow_writes_while_cursor_running()
    with db.CursorDB(paths.get_global_db_path()) as cdb:
        cdb.write_item("k", "v")
        assert cdb.get_item("k") == "v"


def test_reads_allowed_while_cursor_running(cursor_env, monkeypatch):
    monkeypatch.setattr(paths, "is_cursor_running", lambda: True)
    with db.CursorDB(paths.get_global_db_path()) as cdb:
        assert cdb.get_item("missing") is None
