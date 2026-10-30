"""Zoho Mail API inbox reader: shape, dedupe, own-mail skip, failure reporting.

    venv/bin/python -m pytest test_zoho_mail_inbox.py -v

No network: zoho_client.api_request is stubbed.
"""
import time

import pytest

import zoho_mail as zm


NOW_MS = int(time.time() * 1000)


class _Resp:
    def __init__(self, status, body, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        return self._body


def _fake_api(calls):
    def api(method, url, **kw):
        calls.append((method, url, kw.get("params")))
        if url.endswith("/accounts"):
            return _Resp(200, {"data": [{"accountId": "acc1", "primaryEmailAddress": "hello@bizstackperks.com",
                                        "emailAddress": []}]})
        if url.endswith("/folders"):
            return _Resp(200, {"data": [{"folderId": "f-sent", "folderName": "Sent", "folderType": "Sent"},
                                        {"folderId": "f-in", "folderName": "Inbox", "folderType": "Inbox"}]})
        if "/messages/view" in url:
            return _Resp(200, {"data": [
                {"messageId": "m1", "fromAddress": "barbara@example.com", "toAddress": "hello@bizstackperks.com",
                 "subject": "Re: your walkthrough", "receivedTime": str(NOW_MS - 3600_000), "summary": "Yes please"},
                {"messageId": "m2", "fromAddress": "hello@bizstackperks.com", "toAddress": "x@y.com",
                 "subject": "our own outbound", "receivedTime": str(NOW_MS - 60_000), "summary": "..."},
                {"messageId": "m3", "fromAddress": "old@example.com", "toAddress": "hello@bizstackperks.com",
                 "subject": "ancient", "receivedTime": str(NOW_MS - 40 * 86400_000), "summary": "..."},
                {"messageId": "m4", "fromAddress": "carl@example.com", "toAddress": "hello@bizstackperks.com",
                 "subject": "STOP", "receivedTime": str(NOW_MS - 120_000), "summary": "STOP"},
            ]})
        if "/content" in url:
            mid = url.rsplit("/", 2)[-2]
            return _Resp(200, {"data": {"content": f"<div>Body of <b>{mid}</b><br>Tuesday works. STOP</div>"}})
        return _Resp(404, {}, "nope")
    return api


@pytest.fixture
def wired(monkeypatch):
    calls = []
    monkeypatch.setattr(zm.zoho_client, "api_request", _fake_api(calls))
    monkeypatch.setattr(zm.zoho_client, "is_configured", lambda: True)
    monkeypatch.setattr(zm, "available", lambda: True)
    monkeypatch.setenv("SMTP_FROM", "hello@bizstackperks.com")
    zm._account_cache.update({"id": "", "address": "", "at": 0.0})
    zm._folder_cache.update({"account": "", "id": "", "at": 0.0})
    return calls


class _Cur:
    def __init__(self, seen): self.seen = seen; self.executed = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.executed.append((sql, params))
    def fetchall(self): return [{"external_id": s} for s in self.seen]
    def fetchone(self): return None


class _Conn:
    def __init__(self, seen=()): self.cur = _Cur(list(seen)); self.committed = 0
    def cursor(self): return self.cur
    def commit(self): self.committed += 1
    def rollback(self): pass


def test_inbox_status_finds_the_inbox_folder(wired):
    st = zm.inbox_status()
    assert st["ok"] and st["mailbox"] == "hello@bizstackperks.com" and st["via"] == "Zoho Mail API"
    assert zm._folder_cache["id"] == "f-in", "Inbox, not Sent"


def test_list_recent_drops_old_mail_and_keeps_the_shape(wired):
    rows = zm.list_recent(days=7, limit=30)
    ids = [r["id"] for r in rows]
    assert "m3" not in ids, "40-day-old mail is outside the window"
    assert ids == ["m1", "m2", "m4"]
    assert rows[0]["from"] == "barbara@example.com" and rows[0]["date"].endswith("+0000")


def test_poll_skips_our_own_mail_and_already_seen_messages(wired, monkeypatch):
    ingested = {}
    import inbound_email
    monkeypatch.setattr(inbound_email, "_ingest", lambda db, ck, msgs, mb: ingested.update(msgs=msgs, mailbox=mb) or
                        {"matched": 0, "inserted": len(msgs), "deduped": 0, "skipped": 0, "unmatched": 0})
    res = zm.poll_inbox(_Conn(seen=["zoho|m1"]))
    ext = [m["external_id"] for m in ingested["msgs"]]
    assert ext == ["zoho|m4"], "m1 already in comms_logs, m2 is our own outbound, m3 too old"
    assert ingested["mailbox"] == "hello@bizstackperks.com"
    assert "Tuesday works" in ingested["msgs"][0]["text"] and "<" not in ingested["msgs"][0]["text"], "HTML flattened"
    assert res["inserted"] == 1 and res["checked"] == 2, "checked counts inbound candidates, not our own mail"
    content_calls = [c for c in wired if "/content" in c[1]]
    assert len(content_calls) == 1, "only genuinely new messages cost a content call"


def test_poll_reports_instead_of_raising_when_the_api_fails(wired, monkeypatch):
    monkeypatch.setattr(zm.zoho_client, "api_request", lambda *a, **k: _Resp(500, {}, "boom"))
    zm._account_cache.update({"id": "", "address": "", "at": 0.0})
    res = zm.poll_inbox(_Conn())
    assert res["checked"] == 0 and res["error"]


def test_poll_is_off_without_the_mail_scope(monkeypatch):
    monkeypatch.setattr(zm, "available", lambda: False)
    res = zm.poll_inbox(_Conn())
    assert "scope" in res["error"]
