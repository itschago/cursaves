import json
import types

import pytest

from cursor_saves import cli, importer, interactive, paths

from chat_fixtures import add_chat, register_in_workspace, write_snapshot
from conftest import make_state_db, make_workspace


@pytest.mark.parametrize(
    "uri, expected",
    [
        ("file:///Users/me/My%20Projects/caf%C3%A9%23v2", "/Users/me/My Projects/café#v2"),
        ("file:///home/me/app", "/home/me/app"),
        ("vscode-remote://ssh-remote%2Bcore/home/me/my%20app", "/home/me/my app"),
        ("vscode-vfs://github/org/repo", None),
        ("", None),
    ],
)
def test_uri_to_path(uri, expected):
    assert paths.uri_to_path(uri) == expected


def test_workspace_with_special_characters_is_found(cursor_env, tmp_path):
    proj = tmp_path / "My Projects" / "café#v2"
    proj.mkdir(parents=True)
    ws_dir = make_workspace(cursor_env["user_dir"], "ws1", str(proj))
    assert paths.find_workspace_dirs_for_project(str(proj)) == [ws_dir]


def test_ssh_workspace_host_and_path(cursor_env):
    ws_dir = cursor_env["user_dir"] / "workspaceStorage" / "ssh1"
    ws_dir.mkdir()
    host = json.dumps({"hostName": "core"}).encode().hex()
    (ws_dir / "workspace.json").write_text(
        json.dumps({"folder": f"vscode-remote://ssh-remote%2B{host}/home/me/my%20app"})
    )
    make_state_db(ws_dir / "state.vscdb")
    [ws] = paths.list_all_workspaces()
    assert (ws["type"], ws["host"], ws["path"]) == ("ssh", "core", "/home/me/my app")
    ident = importer._build_workspace_identifier(ws_dir)
    assert ident["uri"]["authority"] == f"ssh-remote+{host}"
    assert ident["uri"]["fsPath"] == "/home/me/my app"


def test_created_workspace_uri_is_percent_encoded(cursor_env, tmp_path):
    proj = tmp_path / "My Projects" / "app"
    ws_dir = importer.find_or_create_workspace(str(proj))
    folder = json.loads((ws_dir / "workspace.json").read_text())["folder"]
    assert "%20" in folder and " " not in folder
    assert paths.find_workspace_dirs_for_project(str(proj)) == [ws_dir]


@pytest.fixture
def three_workspaces(cursor_env, tmp_path):
    out = {}
    for name, ws_hash in (("api", "bbbb1111"), ("api-legacy", "aaaa2222"), ("web", "cccc3333")):
        proj = tmp_path / name
        proj.mkdir()
        ws_dir = make_workspace(cursor_env["user_dir"], ws_hash, str(proj))
        add_chat(f"chat-{name}", ["m"])
        register_in_workspace(ws_dir, [f"chat-{name}"])
        out[name] = str(proj)
    return out


def test_workspace_numbers_are_stable(three_workspaces):
    order = [w["path"] for w in paths.list_workspaces_with_conversations()]
    assert order == sorted(three_workspaces.values())


def test_resolve_workspace_prefers_exact_path(three_workspaces):
    assert paths.resolve_workspace(three_workspaces["api"])["path"] == three_workspaces["api"]


def test_resolve_workspace_rejects_ambiguous_substring(three_workspaces, capsys):
    assert paths.resolve_workspace("api") is None
    assert "matches 2 workspaces" in capsys.readouterr().err
    assert paths.resolve_workspace("legacy")["path"] == three_workspaces["api-legacy"]


def test_delete_select_completes_and_pushes(cursor_env, tmp_path, monkeypatch):
    write_snapshot("c1", ["a"], str(tmp_path / "proj"))
    pushed = []

    class Backend:
        def has_remote(self):
            return True

        def pull(self, d):
            return True

        def push(self, d, message=None):
            pushed.append(d)
            return True

    monkeypatch.setattr(cli, "get_backend", lambda: Backend())
    monkeypatch.setattr(interactive, "select_many", lambda choices, **kw: choices)
    monkeypatch.setattr(interactive, "confirm", lambda msg: True)
    args = types.SimpleNamespace(all_projects=False, select=True, project=None, all=False, id=None, yes=True)

    cli.cmd_delete(args)

    assert pushed
    assert not list(paths.get_snapshots_dir().iterdir())
