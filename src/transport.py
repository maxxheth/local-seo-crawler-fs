"""Structured classification of HTTP transport failures (TLS, DNS, timeouts, ...)."""

from __future__ import annotations

import httpx


TRANSPORT_ERROR_TYPES = (
    "tls_hostname_mismatch",
    "tls_verify_failed",
    "dns_failure",
    "timeout",
    "connection_refused",
    "connection_reset",
    "other",
)


def classify_transport_error(error: BaseException | str | None) -> str:
    """Map a transport exception (or its message) onto a structured error type."""
    if error is None or error == "":
        return ""
    if isinstance(error, BaseException):
        if isinstance(error, httpx.TimeoutException):
            return "timeout"
        msg = f"{type(error).__name__}: {error}"
    else:
        msg = str(error)
    low = msg.lower()
    # Order matters: specific certificate failures first, then failure modes whose messages
    # often mention "_ssl.c" (timeouts during the handshake, resets), then generic TLS.
    if "hostname mismatch" in low or "doesn't match" in low or "does not match" in low or "not valid for" in low:
        return "tls_hostname_mismatch"
    if "certificate_verify_failed" in low or "certificate verify failed" in low or "certificate" in low:
        return "tls_verify_failed"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if (
        "name or service not known" in low
        or "nodename nor servname" in low
        or "getaddrinfo" in low
        or "temporary failure in name resolution" in low
        or "no address associated" in low
        or "name resolution" in low
        or "dns" in low
    ):
        return "dns_failure"
    if "connection refused" in low or "errno 111" in low:
        return "connection_refused"
    if "connection reset" in low or "errno 104" in low or "remote end closed" in low or "server disconnected" in low:
        return "connection_reset"
    if "ssl" in low or "tls" in low:
        return "tls_verify_failed"
    return "other"
