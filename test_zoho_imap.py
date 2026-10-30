"""Zoho IMAP: flag semantics, message flattening, and the enable-IMAP message.

    venv/bin/python -m pytest test_zoho_imap.py -v

No network: _connect is replaced with a stub.
"""
import email.message

import pytest

import zoho_imap as zi


RAW = (
    b"From: Barbara Mastic <barbara@example.com>\r\n"
    b"To: hello@bizstackperks.com\r\n"
    b"Subject: Re: your walkthrough\r\n"
    b"Date: Wed, 29 Oct 2026 09:12:00 -0400\r\n"
    b"Message-ID: <abc123@mail.example.com>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Yes please, Tuesday morning works. STOP\r\n"
)


class _FakeIMAP:
    def __init__(self, raw=RAW, search_result=b"1", login_error=None):
        self.raw = raw
        self.search_result = search_result
        self.login_error = login_error
        self.logged_out = False

    def select(self, box, readonly=False):
        return "OK", [b"1"]

    def search(self, charset, criteria):
        return "OK", [self.search_result]

    def fetch(self, num, spec):
        return "OK", [(b"1 (RFC822 {123}", self.raw), b")"]

    def logout(self):
        self.logged_out = True


# --- configuration ------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [("on", True), ("1", True), ("", True),
                                            ("false", False), ("off", False), ("0", False)])
def test_kill_switch_parses_explicitly(monkeypatch, value, expected):
    """Railway stores an unset flag as the string "false", which is truthy."""
    monkeypatch.setenv("SMTP_USER", "hello@bizstackperks.com")
    monkeypatch.setenv("SMTP_PASS", "x")
    monkeypatch.setenv("ZOHO_IMAP_INBOUND", value)
    assert zi.enabled() is expected


def test_without_credentials_it_is_disabled(monkeypatch):
    monkeypatch.delenv("ZOHO_IMAP_USER", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    assert zi.enabled() is False


def test_credentials_fall_back_to_the_smtp_mailbox(monkeypatch):
    monkeypatch.delenv("ZOHO_IMAP_USER", raising=False)
    monkeypatch.delenv("ZOHO_IMAP_PASS", raising=False)
    monkeypatch.setenv("SMTP_USER", "hello@bizstackperks.com")
    monkeypatch.setenv("SMTP_PASS", "secret")
    assert zi._user() == "hello@bizstackperks.com"
    assert zi._password() == "secret"


# --- reading ------------------------------------------------------------------

def test_messages_flatten_into_the_ingest_shape(monkeypatch):
    fake = _FakeIMAP()
    monkeypatch.setattr(zi, "_connect", lambda: fake)
    msgs = zi.fetch_recent(days=7, limit=5)
    assert len(msgs) == 1
    m = msgs[0]
    assert m["from"] == "barbara@example.com", "the display name is stripped for matching"
    assert m["subject"] == "Re: your walkthrough"
    assert "Tuesday morning" in m["text"]
    assert m["external_id"] == "zoho|<abc123@mail.example.com>"
    assert fake.logged_out, "the connection is always closed"


def test_dedupe_key_is_stable_across_reads(monkeypatch):
    monkeypatch.setattr(zi, "_connect", lambda: _FakeIMAP())
    a = zi.fetch_recent()[0]["external_id"]
    b = zi.fetch_recent()[0]["external_id"]
    assert a == b, "re-reading a message must not create a second comms row"


def test_a_message_without_message_id_still_gets_a_key(monkeypatch):
    raw = RAW.replace(b"Message-ID: <abc123@mail.example.com>\r\n", b"")
    monkeypatch.setattr(zi, "_connect", lambda: _FakeIMAP(raw=raw))
    assert zi.fetch_recent()[0]["external_id"].startswith("zoho|")


def test_empty_inbox_returns_nothing(monkeypatch):
    monkeypatch.setattr(zi, "_connect", lambda: _FakeIMAP(search_result=b""))
    assert zi.fetch_recent() == []


# --- status -------------------------------------------------------------------

def test_status_reports_the_enable_imap_message_verbatim(monkeypatch):
    def boom():
        raise zi.imaplib.IMAP4.error(
            b"[ALERT] You are yet to enable IMAP for your account. Please contact your administrator (Failure)")
    monkeypatch.setenv("SMTP_USER", "hello@bizstackperks.com")
    monkeypatch.setenv("SMTP_PASS", "x")
    monkeypatch.setattr(zi, "_connect", boom)
    st = zi.status(force=True)
    assert st["configured"] and not st["ok"] and not st["imap_enabled"]
    assert "enable imap" in st["reason"].lower(), "the owner needs to see the real reason"
    assert not st["reason"].startswith("b'"), "the bytes repr is stripped for display"


def test_status_is_cached_between_calls(monkeypatch):
    calls = []
    monkeypatch.setenv("SMTP_USER", "hello@bizstackperks.com")
    monkeypatch.setenv("SMTP_PASS", "x")
    monkeypatch.setattr(zi, "_connect", lambda: calls.append(1) or _FakeIMAP())
    zi.status(force=True)
    zi.status()
    assert len(calls) == 1


def test_poll_reports_unavailable_instead_of_raising(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "hello@bizstackperks.com")
    monkeypatch.setenv("SMTP_PASS", "x")
    monkeypatch.setattr(zi, "_connect", lambda: (_ for _ in ()).throw(zi.imaplib.IMAP4.error("enable IMAP")))
    res = zi.poll_zoho_inbox(conn_stub())
    assert res["checked"] == 0 and "IMAP" in res["error"]


def conn_stub():
    class C:
        def cursor(self):
            raise AssertionError("ingest must not be reached when IMAP is unavailable")
        def commit(self):
            pass
    return C()
