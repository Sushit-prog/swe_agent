"""Structured logging setup with secret redaction.

Never logs secrets: a redaction filter strips values of anything that looks
like an API key or token before a record is emitted. Verbosity is
configurable via `configure_logging(verbosity)` or the SWE_LOG_LEVEL env var.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

_REDACTED = "[REDACTED]"

# Patterns that indicate secret-bearing content. Values matching these are redacted.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),          # Anthropic API keys
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),          # GitHub tokens
    re.compile(r"(?i)(authorization\s*[:=]\s*)\S+"),    # Authorization headers
    re.compile(
        r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)\S+"
    ),                                                  # key=value style
)


class SecretRedactionFilter(logging.Filter):
    """Redact secret-looking substrings from log messages and args."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(str(record.msg))
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._redact(v) for k, v in record.args.items()}
            else:
                record.args = tuple(self._redact(a) for a in record.args)
        return True

    @staticmethod
    def _redact(text: str) -> str:
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(_REDACTED, text)
        return text


def configure_logging(verbosity: int = 0) -> None:
    """Install the root logger handler and redaction filter.

    verbosity: 0=WARNING, 1=INFO, 2+=DEBUG. SWE_LOG_LEVEL env var overrides.
    """
    env_level = os.environ.get("SWE_LOG_LEVEL", "").upper()
    if env_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level = getattr(logging, env_level)
    elif verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(SecretRedactionFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Get a namespaced logger with the redaction filter attached."""
    logger = logging.getLogger(name)
    if not any(isinstance(f, SecretRedactionFilter) for f in logger.filters):
        logger.addFilter(SecretRedactionFilter())
    return logger


def redact_text(text: str) -> str:
    """Apply the secret redaction patterns to arbitrary text (e.g. trace lines)."""
    return SecretRedactionFilter._redact(text)


def safe_log_fields(**fields: Any) -> dict[str, Any]:
    """Return fields safe for logging: secret-named values are redacted."""
    secret_key = re.compile(r"(?i)key|token|secret|password|authorization")
    return {
        k: (_REDACTED if secret_key.search(k) else v) for k, v in fields.items()
    }
