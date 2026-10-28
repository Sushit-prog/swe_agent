"""Typed errors for GitHub operations. No raw PyGithub exceptions escape."""

from __future__ import annotations

from datetime import datetime


class GitHubError(Exception):
    """Base class for all swe-agent GitHub errors."""


class InvalidIssueURL(GitHubError, ValueError):
    """The provided URL is not a valid https://github.com/{owner}/{repo}/issues/{n} URL."""


class AuthError(GitHubError):
    """Authentication failed (401) or the token lacks permission (403 non-rate-limit)."""


class NotFoundError(GitHubError):
    """The issue or repository does not exist (404), or is private without a token."""


class RateLimitError(GitHubError):
    """GitHub API rate limit exceeded (429, or 403 with x-ratelimit-remaining == 0)."""

    def __init__(self, message: str, reset_at: datetime | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class NetworkError(GitHubError):
    """Network-level failure reaching the GitHub API (timeout, connection error)."""


class IssueIsPullRequestError(GitHubError):
    """The referenced number resolves to a pull request, not an issue."""
