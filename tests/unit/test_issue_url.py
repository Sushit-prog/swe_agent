"""Unit tests for strict GitHub issue URL parsing."""

import pytest

from swe_agent.github.errors import InvalidIssueURL
from swe_agent.github.urls import parse_issue_url


@pytest.mark.parametrize(
    "url, owner, repo, number",
    [
        ("https://github.com/o/r/issues/1", "o", "r", 1),
        ("https://github.com/o/r/issues/12345", "o", "r", 12345),
        ("https://github.com/o/r/issues/1/", "o", "r", 1),  # trailing slash
        ("https://github.com/o/r/issues/1?foo=bar", "o", "r", 1),  # query
        ("https://github.com/o/r/issues/1#comment-2", "o", "r", 1),  # fragment
        (
            "https://github.com/o/r/issues/1/?x=1#f",
            "o",
            "r",
            1,
        ),  # slash + query + fragment
        (
            "https://github.com/Sushit-prog/swe_agent/issues/7",
            "Sushit-prog",
            "swe_agent",
            7,
        ),
        ("https://github.com/my-org/my.repo/issues/9", "my-org", "my.repo", 9),
        ("  https://github.com/o/r/issues/3  ", "o", "r", 3),  # surrounding space
        ("https://www.github.com/o/r/issues/4", "o", "r", 4),
    ],
)
def test_valid_urls(url, owner, repo, number) -> None:
    ref = parse_issue_url(url)
    assert (ref.owner, ref.repo, ref.number) == (owner, repo, number)


@pytest.mark.parametrize(
    "url",
    [
        # pull request URL, not issues
        "https://github.com/o/r/pull/5",
        "https://github.com/o/r/pull/5/files",
        # other hosts
        "https://gitlab.com/o/r/issues/5",
        "https://gist.github.com/o/5",
        "https://evil-github.com/o/r/issues/5",
        "https://github.com.evil.test/o/r/issues/5",
        # non-https
        "http://github.com/o/r/issues/5",
        "ftp://github.com/o/r/issues/5",
        "git@github.com:o/r.git#5",
        "https://github.com:o/r/issues/5",  # port form of ssh-ish
        # missing / bad number
        "https://github.com/o/r/issues/",
        "https://github.com/o/r/issues",
        "https://github.com/o/r/issues/42abc",
        "https://github.com/o/r/issues/0",
        "https://github.com/o/r/issues/-1",
        "https://github.com/o/r/issues/007",
        # extra path segments
        "https://github.com/o/r/issues/5/comments",
        "https://github.com/o/r/issues/5/extra",
        "https://github.com/o/r/extra/issues/5",
        # path traversal in owner/repo positions
        "https://github.com/o/../issues/5",  # repo is '..'
        "https://github.com/./o/issues/5",  # owner is '.'
        "https://github.com/.../o/issues/5",
        # invalid owner/repo character sets
        "https://github.com/-bad/r/issues/5",  # leading hyphen
        "https://github.com/bad-/r/issues/5",  # trailing hyphen
        "https://github.com/bad--owner/r/issues/5",  # consecutive hyphens
        "https://github.com/" + "a" * 40 + "/r/issues/5",  # owner > 39
        "https://github.com/o/repo.git/issues/5",
        "https://github.com/o/r%2Fissues/issues/5",  # encoded slash
        # credentials
        "https://user:pass@github.com/o/r/issues/5",
        # garbage
        "",
        "not-a-url",
        "https://github.com",
    ],
)
def test_invalid_urls_rejected(url) -> None:
    with pytest.raises(InvalidIssueURL):
        parse_issue_url(url)


def test_error_message_is_specific() -> None:
    with pytest.raises(InvalidIssueURL, match="only https://"):
        parse_issue_url("http://github.com/o/r/issues/1")
    with pytest.raises(InvalidIssueURL, match="only github.com"):
        parse_issue_url("https://gitlab.com/o/r/issues/1")
    with pytest.raises(InvalidIssueURL, match="pull"):
        # /pull/ fails the issues-path shape; message mentions expected shape
        parse_issue_url("https://github.com/o/r/pull/1")
