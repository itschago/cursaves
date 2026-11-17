import subprocess

import pytest

from cursor_saves import cli, importer, paths

from chat_fixtures import all_chat_ids, write_snapshot
from conftest import make_workspace


def git_repo(path, remote=None):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    return str(path)


@pytest.fixture(autouse=True)
def _clear_identity_cache():
    paths.remote_project_identifier.cache_clear()
    yield
    paths.remote_project_identifier.cache_clear()


def test_different_repo_with_same_folder_name_is_rejected(cursor_env, tmp_path):
    # pushed from machine B: github.com/org-a/api checked out at /b/api
    source = git_repo(tmp_path / "b" / "api", "git@github.com:org-a/api.git")
    write_snapshot("c1", ["m"], source)
    # this machine: an unrelated repo also called api
    local = git_repo(tmp_path / "a" / "api", "git@github.com:org-b/api.git")
    make_workspace(cursor_env["user_dir"], "ws1", local)

    [project] = importer.list_snapshot_projects()
    assert project["name"] == "github.com-org-a-api"
    assert cli._matching_workspaces_for_project(project) == []
    assert importer.find_snapshot_dir_for_project(local) is None

    assert cli._pull_behind(None) == 0
    assert all_chat_ids() == set()


def test_same_repo_at_different_path_is_accepted(cursor_env, tmp_path):
    source = git_repo(tmp_path / "b" / "api", "https://github.com/org-a/api.git")
    write_snapshot("c1", ["m"], source)
    local = git_repo(tmp_path / "a" / "api", "git@github.com:org-a/api.git")
    make_workspace(cursor_env["user_dir"], "ws1", local)

    assert cli._pull_behind(None) == 1
    assert all_chat_ids() == {"c1"}


def test_ssh_pushed_snapshot_still_imports_into_local_clone(cursor_env, tmp_path):
    # pushed from an SSH workspace: path doesn't exist locally, so the
    # project was identified by folder name
    write_snapshot("c1", ["m"], "/home/remote-user/api")
    local = git_repo(tmp_path / "a" / "api", "git@github.com:org-b/api.git")
    make_workspace(cursor_env["user_dir"], "ws1", local)

    [project] = importer.list_snapshot_projects()
    assert project["name"] == "api"
    assert [w["path"] for w in cli._matching_workspaces_for_project(project)] == [local]


def test_local_folder_without_git_accepts_folder_name_match(cursor_env, tmp_path):
    source = git_repo(tmp_path / "b" / "notes", "git@github.com:me/notes.git")
    write_snapshot("c1", ["m"], source)
    local = tmp_path / "a" / "notes"
    local.mkdir(parents=True)
    make_workspace(cursor_env["user_dir"], "ws1", str(local))

    [project] = importer.list_snapshot_projects()
    assert len(cli._matching_workspaces_for_project(project)) == 1
