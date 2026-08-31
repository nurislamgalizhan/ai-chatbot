import pytest

from app.services.phone import PhoneValidationError, normalize_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+77011234567", "+77011234567"),
        ("+7 701 123 45 67", "+77011234567"),
        ("8 701 123-45-67", "+77011234567"),
        ("+7 (701) 123.45.67", "+77011234567"),
    ],
)
def test_normalize_accepts_common_formats(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "12345", "+0123456789", "hello", "+7701", "7701123456789012345", "'+77011234567; --"],
)
def test_normalize_rejects_garbage(raw: str) -> None:
    with pytest.raises(PhoneValidationError):
        normalize_phone(raw)
