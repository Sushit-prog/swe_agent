"""Sanitization of untrusted text (issue titles/bodies, labels, authors).

Removes terminal-control content (C0/C1 controls, \r, ANSI/OSC escapes) and
Unicode format/bidi characters (category Cf, e.g. U+202E, zero-width) so
untrusted text cannot manipulate the terminal or spoof reading order.

The result is plain text. For Rich rendering, also wrap in rich.text.Text()
or rich.markup.escape() — sanitizing does not stop Rich markup like [link=...].
"""

from __future__ import annotations

import unicodedata

# C0 controls (minus \t and \n), DEL, C1 controls, and \r.
_KEEP = frozenset("\t\n")


def sanitize_untrusted(text: str) -> str:
    """Return `text` with terminal-control and format/bidi characters removed."""
    out: list[str] = []
    for ch in text:
        if ch in _KEEP:
            out.append(ch)
            continue
        category = unicodedata.category(ch)
        if category in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp"):
            continue  # control, format, private-use, surrogate, line/para separator
        out.append(ch)
    return "".join(out)


def truncate_after_sanitize(text: str, limit: int = 500) -> str:
    """Sanitize, then truncate to `limit` chars, appending a marker if cut."""
    clean = sanitize_untrusted(text)
    if len(clean) <= limit:
        return clean
    return clean[:limit] + "… [truncated]"
