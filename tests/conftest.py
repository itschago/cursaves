"""Shared fixtures: a fake Cursor user dir and HOME so tests never touch real data."""

import json
import sqlite3
from pathlib import Path

import pytest

from cursor_saves import db, paths


def make_state_db(path: Path, item_table: dict | None = None, disk_kv: dict | None = None) -> Path:
    """Create a state.vscdb with Cursor's two key-value tables."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE, value BLOB)")
    conn.execute("CREATE TABLE IF NOT EXISTS cursorDiskKV (key TEXT UNIQUE, value BLOB)")
    for table, items in (("ItemTable", item_table or {}), ("cursorDiskKV", disk_kv or {})):
        for k, v in items.items():
            if not isinstance(v, (str, bytes)):
                v = json.dumps(v)
            conn.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def cursor_env(tmp_path, monkeypatch):
    """Isolated Cursor user dir + HOME, with Cursor reported as not running."""
    home = tmp_path / "home"
    user_dir = tmp_path / "Cursor" / "User"
    (user_dir / "globalStorage").mkdir(parents=True)
    (user_dir / "workspaceStorage").mkdir(parents=True)
    home.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(paths, "get_cursor_user_dir", lambda: user_dir)
    monkeypatch.setattr(paths, "is_cursor_running", lambda: False)
    monkeypatch.setattr(paths, "_global_headers_cache", None)
    db.allow_writes_while_cursor_running(False)

    make_state_db(user_dir / "globalStorage" / "state.vscdb")
    return {"home": home, "user_dir": user_dir}


def make_workspace(user_dir: Path, ws_hash: str, folder: str, composer_data: dict | None = None) -> Path:
    """Create a workspaceStorage/<hash>/ dir pointing at `folder`."""
    ws_dir = user_dir / "workspaceStorage" / ws_hash
    ws_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(json.dumps({"folder": Path(folder).as_uri()}))
    items = {"composer.composerData": composer_data} if composer_data is not None else {}
    make_state_db(ws_dir / "state.vscdb", item_table=items)
    return ws_dir
