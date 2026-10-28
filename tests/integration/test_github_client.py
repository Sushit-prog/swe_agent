"""GitHubClient tests with a mocked PyGithub. No network, no real token."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from github import Auth, GithubException

from swe_agent.github.client import GitHubClient, IssueData
from swe_agent.github.errors import (
    AuthError,
    IssueIsPullRequestError,
    NetworkError,
    NotFoundError,
    RateLimitError,
)
from swe_agent.github.urls import IssueRef

REF = IssueRef(owner="o", repo="r", number=7)


class _Label:
    def __init__(self, name: str) -> None:
        self.name = name


class _User:
    def __init__(self, login: str) -> None:
        self.login = login


class _Issue:
    def __init__(self, *, is_pr: bool = False) -> None:
        self.number = 7
        self.title = "A title"
        self.body = "A body"
        self.state = "open"
        self.labels = [_Label("bug"), _Label("help wanted")]
        self.user = _User("octocat")
        self.html_url = "https://github.com/o/r/issues/7"
        self.created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.pull_request = object() if is_pr else None


class _Repo:
    def __init__(self, issue: _Issue, exc: Exception | None = None) -> None:
        self._issue = issue
        self._exc = exc

    def get_issue(self, number: int) -> _Issue:
        if self._exc:
            raise self._exc
        return self._issue


class _FakeGithub:
    """Stand-in for github.Github capturing constructor kwargs."""

    last_init_kwargs: dict = {}
    last_init_args: tuple = ()
    repo: _Repo | None = None
    raise_on_init: Exception | None = None

    def __init__(self, *args, **kwargs) -> None:
        _FakeGithub.last_init_args = args
        _FakeGithub.last_init_kwargs = kwargs
        if _FakeGithub.raise_on_init:
            raise _FakeGithub.raise_on_init
        assert _FakeGithub.repo is not None
        self._repo = _FakeGithub.repo

    def get_repo(self, full_name: str) -> _Repo:
        assert full_name == "o/r"
        return self._repo


@pytest.fixture
def fake_github(monkeypatch):
    _FakeGithub.last_init_args = ()
    _FakeGithub.last_init_kwargs = {}
    _FakeGithub.raise_on_init = None
    _FakeGithub.repo = _Repo(_Issue())
    monkeypatch.setattr("github.Github", _FakeGithub)
    return _FakeGithub


def test_success_fetches_issue_data(fake_github) -> None:
    data = GitHubClient(token="ghp_FAKE").fetch_issue(REF)
    assert isinstance(data, IssueData)
    assert data.number == 7
    assert data.title == "A title"
    assert data.body == "A body"
    assert data.state == "open"
    assert data.labels == ["bug", "help wanted"]
    assert data.author == "octocat"
    assert data.url == "https://github.com/o/r/issues/7"
    assert data.created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert data.is_pull_request is False


def test_authenticated_uses_auth_token_api_with_timeout_and_no_retry(
    fake_github,
) -> None:
    GitHubClient(token="ghp_FAKE").fetch_issue(REF)
    kwargs = fake_github.last_init_kwargs
    assert isinstance(kwargs.get("auth"), Auth.Token) or hasattr(
        kwargs.get("auth"), "token"
    )
    assert kwargs.get("timeout") == 15
    assert kwargs.get("retry") is None


def test_empty_string_token_is_unauthenticated(fake_github) -> None:
    GitHubClient(token="   ").fetch_issue(REF)
    assert "auth" not in fake_github.last_init_kwargs


def test_unauthenticated_no_auth_kwarg(fake_github) -> None:
    GitHubClient(token=None).fetch_issue(REF)
    assert "auth" not in fake_github.last_init_kwargs
    assert fake_github.last_init_kwargs.get("timeout") == 15
    assert fake_github.last_init_kwargs.get("retry") is None


def test_repr_never_contains_token() -> None:
    client = GitHubClient(token="ghp_SUPERSECRET")
    assert "ghp_SUPERSECRET" not in repr(client)
    assert "authenticated" in repr(client)


def test_401_maps_to_auth_error(fake_github) -> None:
    fake_github.repo = _Repo(_Issue(), GithubException(401, {"message": "Bad credentials"}))
    with pytest.raises(AuthError, match="authentication failed"):
        GitHubClient(token="bad").fetch_issue(REF)


def test_404_with_token(fake_github) -> None:
    fake_github.repo = _Repo(_Issue(), GithubException(404, {"message": "Not Found"}))
    with pytest.raises(NotFoundError, match="not found$"):
        GitHubClient(token="tok").fetch_issue(REF)


def test_404_without_token_mentions_private(fake_github) -> None:
    fake_github.repo = _Repo(_Issue(), GithubException(404, {"message": "Not Found"}))
    with pytest.raises(NotFoundError, match="not found, or private"):
        GitHubClient(token=None).fetch_issue(REF)


def test_429_maps_to_rate_limit_with_reset(fake_github) -> None:
    fake_github.repo = _Repo(
        _Issue(),
        GithubException(
            429,
            {"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"},
        ),
    )
    with pytest.raises(RateLimitError) as excinfo:
        GitHubClient(token="tok").fetch_issue(REF)
    assert excinfo.value.reset_at == datetime(
        2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc
    )


def test_403_rate_limit_when_remaining_zero(fake_github) -> None:
    fake_github.repo = _Repo(
        _Issue(),
        GithubException(
            403,
            {"message": "API rate limit exceeded for user"},
            headers={"x-ratelimit-remaining": "0"},
        ),
    )
    with pytest.raises(RateLimitError):
        GitHubClient(token="tok").fetch_issue(REF)


def test_403_plain_is_auth_error_not_rate_limit(fake_github) -> None:
    """A 403 without rate-limit signals means 'forbidden', not 'rate limited'."""
    fake_github.repo = _Repo(
        _Issue(),
        GithubException(
            403,
            {"message": "Resource not accessible by integration"},
            headers={"x-ratelimit-remaining": "4999"},
        ),
    )
    with pytest.raises(AuthError, match="lacks permission"):
        GitHubClient(token="tok").fetch_issue(REF)


def test_network_error_maps_to_network_error(fake_github) -> None:
    import requests

    fake_github.raise_on_init = requests.exceptions.ConnectionError("boom")
    with pytest.raises(NetworkError):
        GitHubClient(token="tok").fetch_issue(REF)


def test_timeout_maps_to_network_error(fake_github) -> None:
    import requests

    fake_github.raise_on_init = requests.exceptions.Timeout("too slow")
    with pytest.raises(NetworkError):
        GitHubClient(token="tok").fetch_issue(REF)


def test_pull_request_issue_is_rejected(fake_github) -> None:
    fake_github.repo = _Repo(_Issue(is_pr=True))
    with pytest.raises(IssueIsPullRequestError, match="pull request"):
        GitHubClient(token="tok").fetch_issue(REF)


def test_no_raw_pygithub_exception_escapes(fake_github) -> None:
    from swe_agent.github.errors import GitHubError

    for exc in (
        GithubException(401, {}),
        GithubException(404, {}),
        GithubException(403, {"message": "nope"}),
        GithubException(500, {}),
    ):
        fake_github.repo = _Repo(_Issue(), exc)
        with pytest.raises(GitHubError):
            GitHubClient(token="tok").fetch_issue(REF)
