"""SSRF guard for incoming-media URLs.

The download URL arrives inside an untrusted provider payload, and the
worker sits on the internal Docker network next to PostgreSQL and Redis.
Without this check a crafted payload could make the worker fetch
http://postgres:5432 or the cloud metadata endpoint and store the
response as "customer media".

Rules: https only, host must match the configured provider suffixes, and
the resolved address must be public. Redirects are refused at the client
level (follow_redirects=False), so a permitted host cannot bounce us.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import structlog

logger = structlog.get_logger(__name__)


class DownloadNotAllowedError(Exception):
    """Rejected URL. The message carries a REASON CODE, never the URL —
    a signed provider URL is a credential and must not reach logs."""


def _is_public(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_reserved
        or parsed.is_multicast
        or parsed.is_unspecified
    )


def check_download_url(url: str, allowed_host_suffixes: tuple[str, ...]) -> str:
    """Return the hostname if the URL may be fetched, else raise."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        msg = "download_rejected:scheme"
        raise DownloadNotAllowedError(msg)
    host = (parts.hostname or "").lower()
    if not host:
        msg = "download_rejected:no_host"
        raise DownloadNotAllowedError(msg)
    if allowed_host_suffixes and not any(
        host == suffix or host.endswith(f".{suffix}") for suffix in allowed_host_suffixes
    ):
        logger.warning("media_download_host_rejected")
        msg = "download_rejected:host"
        raise DownloadNotAllowedError(msg)
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        msg = "download_rejected:dns"
        raise DownloadNotAllowedError(msg) from exc
    # Every resolved address must be public: one private answer is enough
    # for a DNS-rebinding attempt.
    for info in infos:
        address = info[4][0]
        if not _is_public(str(address)):
            logger.warning("media_download_private_address_rejected")
            msg = "download_rejected:private_address"
            raise DownloadNotAllowedError(msg)
    return host
