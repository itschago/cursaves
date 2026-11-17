import pytest

from cursor_saves import cli, importer, sync_state

from chat_fixtures import (
    add_chat,
    all_chat_ids,
    local_chat,
    register_in_workspace,
    write_snapshot,
)
from conftest import make_workspace


@pytest.mark.parametrize(
    "local, snap, expected",
    [
        (["a", "b"], ["a", "b"], "up_to_date"),
        (["a", "b", "c"], ["a", "b"], "local_ahead"),
        (["a", "b"], ["a", "b", "c"], "behind"),
        (["a", "b", "c"], ["a", "b", "d"], "diverged"),
        # more local messages, but the snapshot has one we don't: not "ahead"
        (["a", "b", "c", "e"], ["a", "b", "d"], "diverged"),
    ],
)
def test_compare_message_ids(local, snap, expected):
    assert importer.compare_message_ids(local, snap, len(snap)) == expected


def test_compare_falls_back_to_counts_without_ids():
    assert importer.compare_message_ids(["a", "b", "c"], None, 2) == "local_ahead"
    assert importer.compare_message_ids(["a"], None, 2) == "behind"


@pytest.fixture
def project(cursor_env, tmp_path):
    proj = tmp_path / "work" / "myproj"
    proj.mkdir(parents=True)
    ws_dir = make_workspace(cursor_env["user_dir"], "ws1", str(proj))
    return {"path": str(proj), "ws_dir": ws_dir}


def test_diverged_import_creates_one_stable_copy(project):
    add_chat("c1", ["a", "b", "c"], name="Design")
    register_in_workspace(project["ws_dir"], ["c1"])
    snap = write_snapshot("c1", ["a", "b", "d"], project["path"], name="Design")

    assert importer.import_snapshot(snap, project["path"], project["ws_dir"])
    copy_id = importer.diverged_copy_id("c1", "machine-b")
    assert all_chat_ids() == {"c1", copy_id}
    assert local_chat(copy_id)["name"] == "Design (from machine-b)"
    assert len(local_chat("c1")["fullConversationHeadersOnly"]) == 3  # original untouched

    # importing the same snapshot again doesn't create another duplicate
    assert importer.import_snapshot(snap, project["path"], project["ws_dir"])
    assert all_chat_ids() == {"c1", copy_id}

    # the remote grows: the existing copy is updated in place
    snap = write_snapshot("c1", ["a", "b", "d", "e"], project["path"], name="Design")
    assert importer.import_snapshot(snap, project["path"], project["ws_dir"])
    assert all_chat_ids() == {"c1", copy_id}
    assert len(local_chat(copy_id)["fullConversationHeadersOnly"]) == 4

    assert copy_id in sync_state.diverged_copy_ids()


def test_snapshot_meta_records_message_ids(project):
    snap = write_snapshot("c1", ["a", "b"], project["path"])
    meta = importer.read_snapshot_meta(snap)
    assert meta["headerIds"] == ["a", "b"]


def _names(items):
    return sorted(i["composerId"] for i in items)


def test_push_selection(project, cursor_env, tmp_path):
    # c_ahead: pushed before, gained a message locally
    add_chat("c_ahead", ["a", "b", "c"])
    write_snapshot("c_ahead", ["a", "b"], project["path"])
    # c_new: never pushed, has content, project already opted in
    add_chat("c_new", ["x"])
    # c_empty: never pushed, no messages and no name
    add_chat("c_empty", [], name="")
    # c_div: changed on both sides
    add_chat("c_div", ["a", "c"])
    write_snapshot("c_div", ["a", "d"], project["path"])
    # c_synced: identical
    add_chat("c_synced", ["a"])
    write_snapshot("c_synced", ["a"], project["path"])
    # c_copy: local copy of another machine's diverged version
    add_chat("c_copy", ["q"])
    sync_state.record_diverged_copy("c_copy")
    register_in_workspace(
        project["ws_dir"], ["c_ahead", "c_new", "c_empty", "c_div", "c_synced", "c_copy"],
    )

    # a second project that has never been pushed: its chats stay local
    other = tmp_path / "work" / "other"
    other.mkdir()
    other_ws = make_workspace(cursor_env["user_dir"], "ws2", str(other))
    add_chat("c_other", ["z"])
    register_in_workspace(other_ws, ["c_other"])

    to_push, diverged = cli._find_ahead_conversations()

    assert _names(to_push) == ["c_ahead", "c_new"]
    assert _names(diverged) == ["c_div"]


def test_pull_behind_end_to_end(project):
    add_chat("c_behind", ["a"])
    add_chat("c_div", ["a", "c"], name="Div")
    register_in_workspace(project["ws_dir"], ["c_behind", "c_div"])
    write_snapshot("c_behind", ["a", "b"], project["path"])
    write_snapshot("c_new", ["n"], project["path"], name="New")
    write_snapshot("c_div", ["a", "d"], project["path"], name="Div")

    assert cli._pull_behind(None) == 3

    copy_id = importer.diverged_copy_id("c_div", "machine-b")
    assert all_chat_ids() == {"c_behind", "c_new", "c_div", copy_id}
    assert len(local_chat("c_behind")["fullConversationHeadersOnly"]) == 2
    assert local_chat("c_div")["fullConversationHeadersOnly"][-1]["bubbleId"] == "c"

    # a second sync with nothing new on the remote imports nothing
    assert cli._pull_behind(None) == 0
    assert all_chat_ids() == {"c_behind", "c_new", "c_div", copy_id}
