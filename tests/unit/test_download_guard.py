"""SSRF guard for media download URLs.

The URL arrives inside an untrusted provider payload and the worker sits
on the internal Docker network next to PostgreSQL and Redis, so a
crafted payload must not be able to make us fetch them.
"""

from __future__ import annotations

import pytest

from app.whatsapp.download_guard import DownloadNotAllowedError, check_download_url

HOSTS = ("green-api.com", "greenapi.com")


def test_provider_url_is_allowed() -> None:
    assert check_download_url("https://media.green-api.com/file/x.ogg", HOSTS) == (
        "media.green-api.com"
    )
    # The apex domain itself is fine too.
    assert check_download_url("https://green-api.com/x.ogg", HOSTS) == "green-api.com"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://media.green-api.com/x.ogg", "scheme"),  # plaintext
        ("file:///etc/passwd", "scheme"),
        ("https://evil.example.com/x.ogg", "host"),
        # The classic near-miss: a lookalike domain that merely CONTAINS
        # the allowed one.
        ("https://green-api.com.evil.example/x.ogg", "host"),
        ("https://notgreen-api.com/x.ogg", "host"),
    ],
)
def test_rejected_urls(url: str, reason: str) -> None:
    with pytest.raises(DownloadNotAllowedError) as excinfo:
        check_download_url(url, HOSTS)
    assert reason in str(excinfo.value)


def test_internal_services_are_unreachable_even_if_allowlisted() -> None:
    """If an attacker could get a private address past the allowlist, the
    resolved-address check is the second wall: postgres:5432 and
    redis:6379 live on the same network as the worker."""
    with pytest.raises(DownloadNotAllowedError) as excinfo:
        check_download_url("https://localhost/x.ogg", ("localhost",))
    assert "private_address" in str(excinfo.value)

    with pytest.raises(DownloadNotAllowedError) as excinfo:
        check_download_url("https://127.0.0.1/x.ogg", ("127.0.0.1",))
    assert "private_address" in str(excinfo.value)


def test_error_never_leaks_the_url() -> None:
    """A signed provider URL is a credential: it must not reach logs or
    error messages (same rule as the GREEN-API token)."""
    secret = "https://evil.example.com/file?token=super-secret-value"
    with pytest.raises(DownloadNotAllowedError) as excinfo:
        check_download_url(secret, HOSTS)
    assert "super-secret-value" not in str(excinfo.value)
    assert "evil.example.com" not in str(excinfo.value)
