"""Secret redaction: fake keys/tokens must never appear in log output."""

import logging

from swe_agent.tracing.logging import configure_logging, get_logger


def test_redaction_strips_anthropic_key_and_github_token(
    monkeypatch, caplog
) -> None:
    monkeypatch.delenv("SWE_LOG_LEVEL", raising=False)
    configure_logging(verbosity=2)  # DEBUG, redaction filter installed

    fake_key = "sk-ant-FAKEKEY123456"
    fake_token = "ghp_FAKETOKEN123456"

    logger = get_logger("swe_agent.test_redaction")
    with caplog.at_level(logging.DEBUG):
        logger.warning("leaking key %s and token %s", fake_key, fake_token)
        logger.error(f"raw message with {fake_key} inline and {fake_token} inline")

    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert fake_key not in all_text
    assert fake_token not in all_text
    assert "[REDACTED]" in all_text
