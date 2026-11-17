"""Current Cursor ties chats to workspaces via composerData.workspaceIdentifier."""

import json
import sqlite3

import pytest

from cursor_saves import export, importer, paths

from chat_fixtures import composer_data, local_chat, write_snapshot
from conftest import make_workspace


@pytest.fixture
def project(cursor_env, tmp_path):
    proj = tmp_path / "work" / "myproj"
    proj.mkdir(parents=True)
    ws_dir = make_workspace(cursor_env["user_dir"], "ws-here", str(proj))
    return {"path": str(proj), "ws_dir": ws_dir}


def put_chat(cid, data):
    conn = sqlite3.connect(str(paths.get_global_db_path()))
    conn.execute("INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)", (f"composerData:{cid}", json.dumps(data)))
    conn.commit()
    conn.close()


def test_import_points_chat_at_this_machines_workspace(project):
    snap = write_snapshot("c1", ["a"], project["path"])
    data = importer.read_snapshot_file(snap)
    data["composerData"]["workspaceIdentifier"] = {"id": "ws-on-other-machine"}
    snap.unlink()
    snap = export.save_snapshot(data, paths.get_snapshots_dir())

    assert importer.import_snapshot(snap, project["path"], project["ws_dir"], skip_backup=True)
    wi = local_chat("c1")["workspaceIdentifier"]
    assert wi["id"] == "ws-here"
    assert wi["uri"]["fsPath"] == project["path"]


def test_chat_found_by_workspace_identifier_alone(project):
    """No central index and not in the workspace's own lists."""
    data = composer_data("c1", ["a"], name="Only via identifier")
    data["workspaceIdentifier"] = {"id": "ws-here"}
    put_chat("c1", data)

    convs = export.get_workspace_conversations(project["path"])
    assert [(c["composerId"], c["name"]) for c in convs] == [("c1", "Only via identifier")]
    assert "c1" in paths.get_workspace_composer_ids(project["ws_dir"] / "state.vscdb")


def test_multi_root_workspace_uses_config_path(cursor_env, tmp_path):
    ws_dir = cursor_env["user_dir"] / "workspaceStorage" / "multi"
    ws_dir.mkdir()
    cfg = tmp_path / "proj.code-workspace"
    (ws_dir / "workspace.json").write_text(json.dumps({"workspace": cfg.as_uri()}))

    wi = importer._build_workspace_identifier(ws_dir)
    assert set(wi) == {"id", "configPath"}
    assert wi["configPath"]["path"] == str(cfg)
