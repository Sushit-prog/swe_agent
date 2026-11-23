from swe_agent.core.sanitize import sanitize_untrusted, truncate_after_sanitize


def test_strips_ansi_escape_bytes_and_control_characters() -> None:
    cleaned = sanitize_untrusted("\x1b[2J\x1b[1;31mhello\x1b[0m")
    assert "\x1b" not in cleaned
    assert "hello" in cleaned
    assert sanitize_untrusted("a\rb\x00c\x7fd") == "abcd"
    assert sanitize_untrusted("a\x85b") == "ab"


def test_strips_bidi_format_and_separator_characters() -> None:
    assert sanitize_untrusted("safe\u202etxt.exe\u200b") == "safetxt.exe"
    assert sanitize_untrusted("left\u2028right\u2029end") == "leftrightend"
    assert sanitize_untrusted("x\ue000y") == "xy"


def test_preserves_tab_and_newline() -> None:
    assert sanitize_untrusted("a\tb\nc") == "a\tb\nc"


def test_truncation_happens_after_sanitizing() -> None:
    payload = "\u200b" * 20 + "A" * 10

    assert truncate_after_sanitize(payload, limit=4) == "AAAA… [truncated]"

    ansi_result = truncate_after_sanitize("\x1b[31m" * 20 + "A" * 10, limit=4)
    assert "\x1b" not in ansi_result


def test_no_marker_when_within_limit() -> None:
    assert truncate_after_sanitize("short", limit=10) == "short"


def test_default_limit_is_500() -> None:
    result = truncate_after_sanitize("x" * 501)

    assert result == "x" * 500 + "… [truncated]"
