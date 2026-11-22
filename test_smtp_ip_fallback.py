"""The IPv4 retry in the SMTP ladder must use aiosmtplib's real signature.

On 2026-11-22 five vendor emails walked the SMTP ladder on the owner's Mac.
Every hostname attempt was refused (Zoho: "550 unusual sending activity"), so
the code fell through to its IPv4 sweep and died on

    TypeError: send() got an unexpected keyword argument 'server_hostname'

which buried the real refusal and made the failure look like a bug. aiosmtplib
5.x `send()` has no such kwarg -- TLS hostname pinning is not part of its
signature at all. This pins a stub with the real signature so the extra kwarg
cannot come back.
"""
import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiosmtplib  # noqa: E402
import documents_service as ds  # noqa: E402

CFG = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_TLS": "starttls",
    "SMTP_USER": "someone@example.com",
    "SMTP_PASS": "app-password",
    "SMTP_FROM": "hello@example.com",
    "SMTP_NAME": "Example",
}

REAL_GETADDRINFO = socket.getaddrinfo
REAL_SEND = aiosmtplib.send


def test_ipv4_retry_does_not_pass_server_hostname():
    seen = []

    async def strict_send(message, *, hostname=None, port=None, username=None,
                          password=None, timeout=None, use_tls=False,
                          start_tls=None, validate_certs=True):
        # Mirrors aiosmtplib 5.1.3's keyword-only surface for send(). No **kw:
        # an unknown keyword must fail exactly as it does in the real library.
        seen.append((hostname, port, start_tls, use_tls))
        if hostname != "127.0.0.1":
            raise OSError("connection refused")
        return ({}, "250 OK")

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if family == socket.AF_INET:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return REAL_GETADDRINFO(host, port, family, type, proto, flags)

    saved = {k: os.environ.get(k) for k in ("EMAIL_TRANSPORT", "SMTP_CONNECT_TIMEOUT")}
    os.environ["EMAIL_TRANSPORT"] = "legacy"  # skip Gmail, go straight to the ladder
    os.environ["SMTP_CONNECT_TIMEOUT"] = "2"
    aiosmtplib.send = strict_send
    socket.getaddrinfo = fake_getaddrinfo
    try:
        ok = asyncio.run(ds.send_email(CFG, "rcpt@example.com", "Subject", "Body"))
    finally:
        aiosmtplib.send = REAL_SEND
        socket.getaddrinfo = REAL_GETADDRINFO
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    assert ok is True, f"ladder never reached the working IPv4 attempt: {seen}"
    assert any(host == "127.0.0.1" for host, *_ in seen), "IPv4 retry was not attempted"
    print(f"PASS  IPv4 retry used aiosmtplib's real signature; attempts={len(seen)}")


if __name__ == "__main__":
    test_ipv4_retry_does_not_pass_server_hostname()
    print("ALL smtp IPv4 fallback checks PASSED")
