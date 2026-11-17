import pytest

from cursor_saves import paths


@pytest.mark.parametrize(
    "url, expected",
    [
        ("git@github.com:user/repo.git", "github.com-user-repo"),
        ("https://github.com/user/repo.git", "github.com-user-repo"),
        ("https://github.com/user/repo/", "github.com-user-repo"),
        ("ssh://git@github.com/user/repo", "github.com-user-repo"),
        ("https://user@github.com/user/repo", "github.com-user-repo"),
        ("https://x-access-token:ghp_SECRET@github.com/user/repo.git", "github.com-user-repo"),
        ("https://oauth2:glpat-SECRET@gitlab.example.com/group/sub/repo.git", "gitlab.example.com-group-sub-repo"),
        ("ssh://git@gitlab.example.com:2222/team/repo.git", "gitlab.example.com-team-repo"),
        ("git@gitlab.example.com:team/repo.git", "gitlab.example.com-team-repo"),
        ("git@github.com-work:user/repo.git", "github.com-work-user-repo"),
        ("https://GitHub.com/User/Repo", "github.com-User-Repo"),
        ("file:///srv/git/repo.git", "srv-git-repo"),
        ("/srv/git/repo.git", "srv-git-repo"),
    ],
)
def test_normalize_remote_url(url, expected):
    assert paths._normalize_remote_url(url) == expected


def test_ssh_and_https_forms_agree():
    forms = [
        "git@github.com:user/repo.git",
        "https://github.com/user/repo.git",
        "ssh://git@github.com:22/user/repo.git",
        "https://token:x@github.com/user/repo",
    ]
    assert len({paths._normalize_remote_url(f) for f in forms}) == 1
