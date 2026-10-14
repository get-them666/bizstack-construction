"""A dead Gmail transport must not be a dead end for outbound mail.

Background: the Google Cloud project behind this app's OAuth client was deleted,
so every Gmail API call returns 403 CONSUMER_INVALID. send_email() treated that
as terminal and returned False, so lead replies, bid inquiries and owner
notifications were silently dropped. This pins the degraded behaviour:

  * Gmail fails + SMTP configured  -> walk the SMTP ladder (do not return False)
  * Gmail fails + EMAIL_FALLBACK_SMTP=0 -> stay fatal, as before
  * Gmail fails + no SMTP config -> stay fatal
  * Gmail succeeds -> unchanged, SMTP never touched

No network calls. Gmail and aiosmtplib are both stubbed.
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# documents_service imports aiosmtplib at module scope; stub before import so
# this runs with no network stack at all.
calls = {"gmail": 0, "smtp": 0}


def _install_stubs():
    import aiosmtplib

    async def fake_send(msg, **kw):
        calls["smtp"] += 1
        return ("250 OK", "sent")

    aiosmtplib.send = fake_send


_install_stubs()

import documents_service as ds  # noqa: E402

CFG = {
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_PORT": "587",
    "SMTP_TLS": "starttls",
    "SMTP_USER": "someone@example.com",
    "SMTP_PASS": "app-password",
    "SMTP_FROM": "hello@example.com",
    "SMTP_NAME": "Example",
}


def _gmail(result):
    async def _f(*a, **kw):
        calls["gmail"] += 1
        return result
    return _f


def run(cfg=CFG, gmail_ok=False, env=None):
    calls["gmail"] = calls["smtp"] = 0
    saved = {k: os.environ.get(k) for k in ("EMAIL_TRANSPORT", "EMAIL_FALLBACK_SMTP", "SMTP_CONNECT_TIMEOUT")}
    os.environ["EMAIL_TRANSPORT"] = "gmail"
    os.environ.pop("SMTP_CONNECT_TIMEOUT", None)
    for k in ("EMAIL_FALLBACK_SMTP",):
        os.environ.pop(k, None)
    os.environ.update(env or {})
    ds._send_via_gmail = _gmail(gmail_ok)
    try:
        return asyncio.run(ds.send_email(cfg, "rcpt@example.com", "Subject", "Body")), dict(calls)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- 1. the bug: Gmail dead must NOT silently drop the message --------------
ok, c = run(gmail_ok=False)
print(f"gmail fails, smtp configured -> returned {ok}, gmail={c['gmail']} smtp={c['smtp']}")
assert ok is True, "Gmail failure dropped the message instead of falling back to SMTP"
assert c["smtp"] >= 1, "never attempted SMTP"
print("PASS  Gmail failure degrades to SMTP instead of dropping the message")

# --- 2. explicit opt-out keeps the old fatal behaviour ----------------------
ok, c = run(gmail_ok=False, env={"EMAIL_FALLBACK_SMTP": "0"})
print(f"\ngmail fails, EMAIL_FALLBACK_SMTP=0 -> returned {ok}, smtp={c['smtp']}")
assert ok is False, "opt-out must stay fatal"
assert c["smtp"] == 0, "opt-out must not touch SMTP"
print("PASS  EMAIL_FALLBACK_SMTP=0 preserves the old hard stop")

# --- 3. no SMTP configured -> nothing to fall back to, stay fatal ----------
ok, c = run(cfg={"SMTP_FROM": "hello@example.com"}, gmail_ok=False)
print(f"\ngmail fails, no smtp host -> returned {ok}, smtp={c['smtp']}")
assert ok is False, "must stay fatal when there is no fallback configured"
print("PASS  stays fatal when no SMTP host is configured")

# --- 4. healthy Gmail is untouched ----------------------------------------
ok, c = run(gmail_ok=True)
print(f"\ngmail succeeds -> returned {ok}, gmail={c['gmail']} smtp={c['smtp']}")
assert ok is True and c["gmail"] == 1 and c["smtp"] == 0, "healthy Gmail path changed"
print("PASS  healthy Gmail path unchanged, SMTP never contacted")

print("\nALL email fallback checks PASSED")