"""GitHub integration (issue ingestion in M2; PR creation later)."""

from swe_agent.github.client import GitHubClient, IssueData
from swe_agent.github.errors import (
    AuthError,
    GitHubError,
    IssueIsPullRequestError,
    InvalidIssueURL,
    NetworkError,
    NotFoundError,
    RateLimitError,
)
from swe_agent.github.urls import IssueRef, parse_issue_url

__all__ = [
    "AuthError",
    "GitHubClient",
    "GitHubError",
    "IssueData",
    "IssueIsPullRequestError",
    "IssueRef",
    "InvalidIssueURL",
    "NetworkError",
    "NotFoundError",
    "RateLimitError",
    "parse_issue_url",
]
