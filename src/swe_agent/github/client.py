"""Thin wrapper over PyGithub. No raw PyGithub exceptions leak from here."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

from swe_agent.github.errors import (
    AuthError,
    IssueIsPullRequestError,
    NetworkError,
    NotFoundError,
    RateLimitError,
)
from swe_agent.github.urls import IssueRef

logger = logging.getLogger(__name__)

_REQUESTS_TIMEOUT = 15


class IssueData(BaseModel):
    """Structured issue metadata (untrusted content: title/body are data)."""

    model_config = ConfigDict(extra="forbid")

    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    author: str
    url: str
    created_at: datetime
    is_pull_request: bool


class GitHubClient:
    """Fetch issues via the GitHub API.

    The token is never logged and never appears in repr().
    An empty-string token is treated as None (unauthenticated).
    """

    def __init__(self, token: str | None = None) -> None:
        self._token: str | None = token.strip() if token else None

    def __repr__(self) -> str:
        mode = "authenticated" if self._token else "unauthenticated"
        return f"GitHubClient({mode})"

    def _build_github(self) -> Any:
        # Imported here so tests can monkeypatch swe_agent.github.client.Github.
        from github import Auth, Github

        if self._token:
            return Github(
                auth=Auth.Token(self._token),
                timeout=_REQUESTS_TIMEOUT,
                retry=None,
            )
        logger.warning(
            "GitHub access is unauthenticated; the rate limit is low "
            "(60 requests/hour). Set GITHUB_TOKEN for higher limits."
        )
        return Github(timeout=_REQUESTS_TIMEOUT, retry=None)

    def fetch_issue(self, ref: IssueRef) -> IssueData:
        """Fetch issue metadata. Maps all failures to typed swe-agent errors."""
        full_name = f"{ref.owner}/{ref.repo}"
        try:
            gh = self._build_github()
            issue = gh.get_repo(full_name).get_issue(ref.number)
            is_pr = getattr(issue, "pull_request", None) is not None
            labels = [getattr(label, "name", str(label)) for label in issue.labels]
            author = getattr(issue.user, "login", "") if issue.user else ""
            data = IssueData(
                number=issue.number,
                title=issue.title or "",
                body=issue.body or "",
                state=issue.state,
                labels=labels,
                author=author,
                url=issue.html_url,
                created_at=self._as_utc(issue.created_at),
                is_pull_request=is_pr,
            )
        except Exception as exc:
            raise self._map_exception(exc, ref) from exc

        if data.is_pull_request:
            raise IssueIsPullRequestError(
                f"{full_name}#{ref.number} is a pull request, not an issue"
            )
        return data

    @staticmethod
    def _as_utc(dt: datetime | None) -> datetime:
        if dt is None:
            return datetime.now(timezone.utc)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def _map_exception(self, exc: Exception, ref: IssueRef) -> Exception:
        """Translate a PyGithub/network exception into a typed swe-agent error."""
        try:
            from github import GithubException
        except ImportError:  # pragma: no cover
            GithubException = ()  # type: ignore[assignment]

        full_name = f"{ref.owner}/{ref.repo}"

        if isinstance(exc, GithubException):
            status = exc.status
            headers = dict(getattr(exc, "headers", None) or {})
            if status == 401:
                return AuthError("authentication failed: check GITHUB_TOKEN")
            if status == 404:
                if self._token is None:
                    return NotFoundError(
                        f"issue {full_name}#{ref.number} not found, or private "
                        "(a token is required)"
                    )
                return NotFoundError(f"issue {full_name}#{ref.number} not found")
            if status == 429:
                return RateLimitError(
                    "GitHub API rate limit exceeded",
                    reset_at=_parse_reset(headers),
                )
            if status == 403:
                remaining = headers.get("x-ratelimit-remaining")
                message = str(getattr(exc, "data", "") or "").lower()
                if (
                    remaining == "0"
                    or "rate limit" in message
                    or "secondary rate limit" in message
                ):
                    return RateLimitError(
                        "GitHub API rate limit exceeded",
                        reset_at=_parse_reset(headers),
                    )
                return AuthError(
                    "token lacks permission for this repository "
                    "(403 forbidden, not a rate limit)"
                )
            return NetworkError(f"GitHub API error (status {status})")

        # requests-layer failures surface as ConnectionError/Timeout/HTTPError.
        if isinstance(exc, (TimeoutError, OSError)):
            return NetworkError(f"network error contacting GitHub: {exc}")
        return NetworkError(f"unexpected error contacting GitHub: {exc!r}")


def _parse_reset(headers: dict[str, str]) -> datetime | None:
    raw = headers.get("x-ratelimit-reset")
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (ValueError, OSError):
        return None
