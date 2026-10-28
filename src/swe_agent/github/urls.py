"""Strict parsing of GitHub issue URLs.

Accepts only: https://github.com/{owner}/{repo}/issues/{n}
with an optional trailing slash, query string, or fragment.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from swe_agent.github.errors import InvalidIssueURL

# GitHub username/org rules: alphanumeric and single (not consecutive, not
# leading/trailing) hyphens, max 39 chars.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
# GitHub repo name allowed set (letters, digits, dot, underscore, hyphen).
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Issue numbers are positive base-10 integers with no leading zeros.
_NUMBER_RE = re.compile(r"^[1-9][0-9]*$")
_ALLOWED_NETLOCS = frozenset({"github.com", "www.github.com"})


class IssueRef(BaseModel, frozen=True):
    """A validated reference to a GitHub issue."""

    model_config = ConfigDict(extra="forbid")

    owner: str
    repo: str
    number: int


def parse_issue_url(url: str) -> IssueRef:
    """Parse a GitHub issue URL into an IssueRef.

    Raises InvalidIssueURL with a specific message for every rejection.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidIssueURL("URL must be a non-empty string")

    raw = url.strip()
    parts = urlsplit(raw)

    if parts.scheme != "https":
        raise InvalidIssueURL(
            f"only https:// URLs are accepted, got scheme {parts.scheme!r}"
        )
    if parts.username or parts.password:
        raise InvalidIssueURL("URL must not contain credentials")
    netloc = parts.netloc.lower()
    if netloc not in _ALLOWED_NETLOCS:
        raise InvalidIssueURL(
            f"only github.com URLs are accepted, got host {parts.netloc!r}"
        )

    # Path: /{owner}/{repo}/issues/{n} with optional trailing slash.
    # Query and fragment are dropped by urlsplit and play no part in matching.
    segments = [s for s in parts.path.split("/") if s != ""]
    if len(segments) != 4 or segments[2] != "issues":
        raise InvalidIssueURL(
            "path must be /{owner}/{repo}/issues/{number} "
            f"(got /{parts.path.lstrip('/')})"
        )

    owner, repo, _, number_str = segments

    # '.' and '..' are the dangerous path-traversal names; reject them in the
    # owner/repo positions. (Deeper '../' sequences land here too, because
    # urlsplit's path is split on '/' — e.g. '/o/../issues/5' gives repo='..'.)
    if owner in (".", "..") or repo in (".", ".."):
        raise InvalidIssueURL("owner and repo must not be '.' or '..'")

    if not _OWNER_RE.match(owner):
        raise InvalidIssueURL(f"invalid owner name {owner!r}")
    if not _REPO_RE.match(repo) or repo.endswith(".git"):
        raise InvalidIssueURL(f"invalid repo name {repo!r}")
    if not _NUMBER_RE.match(number_str):
        raise InvalidIssueURL(
            f"issue number must be a positive integer, got {number_str!r}"
        )

    return IssueRef(owner=owner, repo=repo, number=int(number_str))
