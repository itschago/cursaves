"""Chats whose message headers live in composerConvHeaders chunks (composerData _v 18+)."""

import json

import pytest

from cursor_saves import db, export, importer, paths

from chat_fixtures import (
    add_chat,
    all_chat_ids,
    composer_data,
    header_chunks,
    local_header_chunk_keys,
    local_header_ids,
    register_in_workspace,
    write_snapshot,
)
from conftest import make_workspace


@pytest.fixture
def project(cursor_env, tmp_path):
    proj = tmp_path / "work" / "myproj"
    proj.mkdir(parents=True)
    ws_dir = make_workspace(cursor_env["user_dir"], "ws1", str(proj))
    return {"path": str(proj), "ws_dir": ws_dir}


def test_resolve_headers_inline_and_chunked():
    ids = ["a", "b", "c", "d", "e"]
    inline = composer_data("c1", ids)
    chunked = composer_data("c1", ids, chunked=True)
    chunks = header_chunks(ids)

    assert [h["bubbleId"] for h in db.resolve_headers(inline, chunks.get)] == ids
    assert [h["bubbleId"] for h in db.resolve_headers(chunked, chunks.get)] == ids
    assert db.message_count(inline) == db.message_count(chunked) == 5
    assert db.resolve_headers(chunked, {}.get) == []


def test_export_includes_chunks_and_meta_ids(project):
    add_chat("c1", ["a", "b", "c"], chunked=True)
    register_in_workspace(project["ws_dir"], ["c1"])

    snap = export.export_conversation(project["path"], "c1")
    assert snap["headerChunks"] == header_chunks(["a", "b", "c"])

    path = export.save_snapshot(snap, paths.get_snapshots_dir())
    meta = json.loads(path.with_name("c1.meta.json").read_text())
    assert meta["messageCount"] == 3
    assert meta["headerIds"] == ["a", "b", "c"]


def test_new_messages_in_chunked_chat_are_detected_as_ahead(project):
    """Previously both sides looked empty, so a grown chat was never pushed again."""
    add_chat("c1", ["a", "b", "c"], chunked=True)
    register_in_workspace(project["ws_dir"], ["c1"])
    export.save_snapshot(export.export_conversation(project["path"], "c1"), paths.get_snapshots_dir())
    project_id = paths.get_project_identifier(project["path"])

    assert importer.get_push_status_for_conversation("c1", project_id) == "up_to_date"
    add_chat("c1", ["a", "b", "c", "d"], chunked=True)
    assert importer.get_push_status_for_conversation("c1", project_id) == "local_ahead"


def test_import_chunked_snapshot_writes_chunks(project):
    snap = write_snapshot("c1", ["a", "b", "c"], project["path"], chunked=True)
    assert importer.import_snapshot(snap, project["path"], project["ws_dir"], skip_backup=True)

    assert local_header_ids("c1") == ["a", "b", "c"]
    assert local_header_chunk_keys("c1") == [
        "composerConvHeaders:c1:000000", "composerConvHeaders:c1:000001",
    ]


def test_import_update_replaces_chunks(project):
    add_chat("c1", ["a", "b", "c"], chunked=True)
    register_in_workspace(project["ws_dir"], ["c1"])
    snap = write_snapshot("c1", ["a", "b", "c", "d", "e"], project["path"], chunked=True)
    assert importer.import_snapshot(snap, project["path"], project["ws_dir"], skip_backup=True)
    assert local_header_ids("c1") == ["a", "b", "c", "d", "e"]
    assert len(local_header_chunk_keys("c1")) == 3


def test_diverged_chunked_chat_copies_chunks_under_new_id(project):
    add_chat("c1", ["a", "b", "c"], chunked=True)
    register_in_workspace(project["ws_dir"], ["c1"])
    snap = write_snapshot("c1", ["a", "b", "x"], project["path"], chunked=True)
    assert importer.import_snapshot(snap, project["path"], project["ws_dir"], skip_backup=True)

    copy_id = importer.diverged_copy_id("c1", "machine-b")
    assert all_chat_ids() == {"c1", copy_id}
    assert local_header_ids("c1") == ["a", "b", "c"]
    assert local_header_ids(copy_id) == ["a", "b", "x"]


def test_import_rewrites_paths_in_header_chunks(project, tmp_path):
    src = "/home/other/work/myproj"
    snapshot = {
        "version": 3,
        "exportedAt": "2026-01-01T00:00:00+00:00",
        "sourceMachine": "machine-b",
        "sourceProjectPath": src,
        "projectIdentifier": paths.get_project_identifier(project["path"]),
        "composerId": "c1",
        "composerData": composer_data("c1", ["a"], chunked=True),
        "bubbleEntries": {"a": {"bubbleId": "a"}},
        "headerChunks": {"000000": [{
            "bubbleId": "a", "type": 2,
            "grouping": {"toolDisplayPath": f"{src}/src/app.py"},
        }]},
    }
    path = export.save_snapshot(snapshot, paths.get_snapshots_dir())
    assert importer.import_snapshot(path, project["path"], project["ws_dir"], skip_backup=True)
    with db.CursorDB(paths.get_global_db_path()) as cdb:
        h = cdb.get_conversation_headers("c1")
    assert h[0]["grouping"]["toolDisplayPath"] == f"{project['path']}/src/app.py"


def test_old_snapshot_without_chunks_is_not_imported(project, capsys):
    add_chat("c1", ["a", "b"], chunked=True)
    register_in_workspace(project["ws_dir"], ["c1"])
    snap = write_snapshot("c1", ["a", "b", "c"], project["path"], chunked=True, include_chunks=False)

    assert not importer.import_snapshot(snap, project["path"], project["ws_dir"], skip_backup=True)
    assert "Push it again from machine-b" in capsys.readouterr().err
    assert local_header_ids("c1") == ["a", "b"]  # local chat untouched
