import os

from app.observability.logging import _redact, mask_phone


def test_mask_phone() -> None:
    assert mask_phone("+77019481555") == "+7701***1555"
    assert mask_phone("+123") == "***"


def test_redact_hides_sensitive_keys() -> None:
    event = {
        "password": "hunter2hunter2",
        "session_token": "abc",
        "Authorization": "Bearer x",
        "api_key": "sk-123",
        "message": "ok",
    }
    result = _redact(None, "", dict(event))
    assert result["password"] == "[redacted]"
    assert result["session_token"] == "[redacted]"
    assert result["Authorization"] == "[redacted]"
    assert result["api_key"] == "[redacted]"
    assert result["message"] == "ok"


def test_redact_masks_phone_values() -> None:
    result = _redact(None, "", {"phone": "+77019481555"})
    assert result["phone"] == "+7701***1555"


def test_tracebacks_do_not_carry_local_variables(capsys) -> None:
    """The secret leak this guards against is not hypothetical.

    structlog's default traceback renderer serialises every frame's
    locals. In this codebase those frames hold the GREEN-API token and
    the customer's message text with their phone number, and they would
    be written out in full on every logger.exception — invisible to the
    key-based redaction, which only inspects event keys.
    """
    import structlog

    from app.config import Settings
    from app.observability.logging import configure_logging

    configure_logging(
        Settings(
            app_env="test",
            database_url="postgresql+asyncpg://u:p@localhost/db",
            redis_url="redis://localhost:6379/0",
            encryption_key=os.environ["ENCRYPTION_KEY"],
            cors_origins="",
        )
    )
    logger = structlog.get_logger("test")

    def failing_call() -> None:
        green_api_token = "SECRET-TOKEN-123456"  # noqa: F841 - the point of the test
        customer_text = "Здравствуйте, мой номер +77019481555"  # noqa: F841
        raise RuntimeError("boom")

    try:
        failing_call()
    except RuntimeError:
        logger.exception("poll_unexpected_error")

    written = capsys.readouterr().out + capsys.readouterr().err
    assert "SECRET-TOKEN-123456" not in written
    assert "+77019481555" not in written
    assert "locals" not in written
    # The traceback itself must still be there — this is about locals,
    # not about hiding failures.
    assert "RuntimeError" in written
