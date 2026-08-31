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
