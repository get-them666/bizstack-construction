"""Tests for the owner inbox (mail_inbox).

Run: venv/bin/python test_mail_inbox.py   (pytest also collects it)

What is under test is the part that can go wrong quietly:
  - grouping two customers with the same subject into one thread,
  - stacking "Re: Re:" on a subject that already has one,
  - replying to the wrong message in a thread,
  - sending without the owner pressing Send.

No network and no database: the Zoho module and the model are both faked.
"""

from __future__ import annotations

import asyncio
import os


def _fake_zoho(heads, bodies=None):
    """Stand-in with only the surface mail_inbox uses."""
    bodies = bodies or {}

    class Fake:
        @staticmethod
        def available():
            return True

        @staticmethod
        def account(sender=""):
            return "ACC1", "hello@bizstackperks.com"

        @staticmethod
        def list_recent(days=14, limit=30):
            return list(heads)

        @staticmethod
        def message_text(account_id, folder_id, message_id):
            return bodies.get(message_id, "body of " + message_id)

        @staticmethod
        def reply(account_id, message_id, to, subject, body):
            Fake.last_reply = {"account_id": account_id, "message_id": message_id,
                               "to": to, "subject": subject, "body": body}
            return True

    Fake.last_reply = None
    return Fake


def _head(mid, sender, subject, received_ms, folder="1", summary="summary"):
    return {
        "id": mid, "folder_id": folder, "account_id": "ACC1",
        "from": sender, "to": "hello@bizstackperks.com",
        "subject": subject, "received_ms": received_ms,
        "date": "Mon, 02 Nov 2026 10:00:00 +0000", "summary": summary,
    }


def _sent(mid, to, subject, received_ms, folder="2"):
    h = _head(mid, "hello@bizstackperks.com", subject, received_ms, folder)
    h["to"] = to
    return h


# ── subject handling ────────────────────────────────────────────────────────

def test_subject_key_strips_reply_prefixes():
    key = __import__("mail_inbox").subject_key
    for raw in ("Re: Roof quote", "RE: Roof quote", "Re: Re: FW: Roof quote",
                "  Fwd:  roof   quote ", "roof quote"):
        assert key(raw) == "roof quote", raw


def test_subject_key_keeps_distinct_subjects_apart():
    key = __import__("mail_inbox").subject_key
    assert key("Re: Roof quote") != key("Re: Deck quote")


def test_subject_key_handles_no_subject():
    key = __import__("mail_inbox").subject_key
    assert key("") == "(no subject)"
    assert key("Re:") == "(no subject)"


def test_reply_subject_does_not_stack():
    rs = __import__("mail_inbox").reply_subject
    assert rs("Roof quote") == "Re: Roof quote"
    assert rs("Re: Roof quote") == "Re: Roof quote"
    assert rs("") == "Re: BuildStack Construction"


# ── grouping ────────────────────────────────────────────────────────────────

def test_conversations_group_by_counterpart_and_subject(monkeypatch):
    import mail_inbox

    heads = [
        _head("m1", "Alice <alice@example.com>", "Roof quote", 1_000, summary="first"),
        _head("m2", "Alice <alice@example.com>", "Re: Roof quote", 2_000, summary="second"),
        # Same subject, different person: must NOT join Alice's thread.
        _head("m3", "Bob <bob@example.com>", "Re: Roof quote", 3_000, summary="bob"),
        # Our own reply belongs to Alice's conversation, not a new one.
        _sent("m4", "alice@example.com", "Re: Roof quote", 4_000),
        # Our sent mail to an address we never heard from still shows as a thread.
        _sent("m5", "carol@example.com", "Material list", 5_000),
    ]
    monkeypatch.setattr(mail_inbox, "_zoho", lambda: _fake_zoho(heads))
    mail_inbox._cache_clear()

    try:
        data = mail_inbox.conversations(days=14, limit=50)
        assert data["ok"] is True, data
        by_party = {c["party"]: c for c in data["items"]}

        assert set(by_party) == {"alice@example.com", "bob@example.com", "carol@example.com"}
        assert by_party["alice@example.com"]["messages"] == 3
        assert by_party["bob@example.com"]["messages"] == 1
        assert by_party["carol@example.com"]["messages"] == 1

        # Newest first, and the last message decides who owes a reply.
        assert data["items"][0]["party"] == "carol@example.com"
        assert by_party["alice@example.com"]["last_direction"] == "out"
        assert by_party["alice@example.com"]["needs_reply"] is False
        assert by_party["bob@example.com"]["needs_reply"] is True
    finally:
        mail_inbox._cache_clear()


def test_conversations_do_not_group_two_customers_with_same_subject():
    """The failure this guards: one thread and a reply to the wrong person."""
    import mail_inbox

    heads = [
        _head("m1", "alice@example.com", "Re: Your project", 1_000),
        _head("m2", "bob@example.com", "Re: Your project", 2_000),
    ]
    original = mail_inbox._zoho
    mail_inbox._zoho = lambda: _fake_zoho(heads)
    mail_inbox._cache_clear()
    try:
        items = mail_inbox.conversations()["items"]
        assert len(items) == 2
        assert {i["party"] for i in items} == {"alice@example.com", "bob@example.com"}
    finally:
        mail_inbox._zoho = original
        mail_inbox._cache_clear()


# ── thread ──────────────────────────────────────────────────────────────────

def test_thread_returns_oldest_first_and_targets_latest_inbound(monkeypatch):
    import mail_inbox

    heads = [
        _head("m1", "alice@example.com", "Roof quote", 1_000),
        _sent("m2", "alice@example.com", "Re: Roof quote", 2_000),
        _head("m3", "alice@example.com", "Re: Roof quote", 3_000),
    ]
    monkeypatch.setattr(mail_inbox, "_zoho", lambda: _fake_zoho(heads))
    mail_inbox._cache_clear()
    try:
        data = mail_inbox.thread("alice@example.com", "roof quote")
        assert data["ok"] is True, data
        assert [m["id"] for m in data["messages"]] == ["m1", "m2", "m3"]
        assert [m["direction"] for m in data["messages"]] == ["in", "out", "in"]
        # m3 is the one to answer, NOT our own m2.
        assert data["reply_to"] == "m3"
        assert data["reply_subject"] == "Re: Roof quote"
        assert data["messages"][0]["body"] == "body of m1"
    finally:
        mail_inbox._cache_clear()


def test_thread_refuses_without_usable_address():
    import mail_inbox
    data = mail_inbox.thread("not-an-address", "x")
    assert data["ok"] is False
    assert "address" in data["reason"].lower()


# ── draft ───────────────────────────────────────────────────────────────────

def test_clean_draft_strips_wrappers():
    import mail_inbox
    raw = "```text\nSubject: Re: Roof quote\n\nHi Alice,\n\nTuesday works.\n```"
    cleaned = mail_inbox._clean_draft(raw)
    assert cleaned == "Hi Alice,\n\nTuesday works."


def test_draft_never_sends_and_includes_the_instruction(monkeypatch):
    import mail_inbox
    import con_ai_agent

    captured = {}

    class Resp:
        class _C:
            class _M:
                content = "Hi Alice,\n\nWe can start Tuesday.\n\n— BuildStack"

            message = _M()

        choices = [_C()]

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["subset"] = kwargs.get("subset")

        def _complete(self, messages, max_tokens):
            captured["messages"] = messages
            return Resp()

    monkeypatch.setattr(con_ai_agent, "BusinessAIAgent", FakeAgent)
    monkeypatch.setattr(mail_inbox, "thread", lambda party, key, days=90: {
        "ok": True, "party": party, "party_name": "Alice", "subject": "Re: Roof quote",
        "reply_subject": "Re: Roof quote", "reply_to": "m3", "account_id": "ACC1",
        "messages": [
            {"direction": "in", "from_name": "Alice", "date": "Mon", "body": "Can you start Tuesday?"},
        ],
    })

    result = mail_inbox.draft("alice@example.com", "roof quote",
                              instruction="we can start Tuesday", tone="friendly")
    assert result["ok"] is True, result
    assert result["subject"] == "Re: Roof quote"
    assert "Tuesday" in result["body"]
    user_turn = captured["messages"][-1]["content"]
    assert "we can start Tuesday" in user_turn
    assert "Can you start Tuesday?" in user_turn


def test_draft_refuses_a_thread_with_nothing_inbound(monkeypatch):
    import mail_inbox
    monkeypatch.setattr(mail_inbox, "thread", lambda party, key, days=90: {
        "ok": True, "messages": [
            {"direction": "out", "from_name": "You", "body": "Hello"},
        ],
    })
    result = mail_inbox.draft("alice@example.com", "roof quote")
    assert result["ok"] is False
    assert "inbound" in result["reason"].lower()


# ── send ────────────────────────────────────────────────────────────────────

def test_send_reply_validates_before_touching_a_transport():
    import mail_inbox

    async def run():
        bad = await mail_inbox.send_reply(None, party="nope", key="x", subject="s", body="b")
        assert bad["ok"] is False
        empty = await mail_inbox.send_reply(None, party="a@b.com", key="x", subject="s", body="   ")
        assert empty["ok"] is False
        assert "empty" in empty["reason"].lower()

    asyncio.run(run())


def test_send_reply_uses_zoho_and_logs_only_on_success(monkeypatch):
    import mail_inbox

    sent = {}

    def fake_zoho(party, subject, body, key):
        return {"ok": True, "transport": "Zoho Mail API", "in_reply_to": "m3"}

    monkeypatch.setattr(mail_inbox, "_send_via_zoho", fake_zoho)
    monkeypatch.setattr(mail_inbox, "_recent_duplicate", lambda *a, **k: False)
    monkeypatch.setattr(mail_inbox, "_lookup_lead_id", lambda db, party: 42)
    monkeypatch.setattr(mail_inbox, "_log_sent", lambda db, p, s, b, lead: sent.update(
        {"party": p, "subject": s, "body": b, "lead": lead}))

    async def run():
        result = await mail_inbox.send_reply(
            None, party="Alice@Example.com", key="roof quote",
            subject="Re: Roof quote", body="Tuesday works.")
        assert result["ok"] is True, result
        assert result["to"] == "alice@example.com"
        assert result["lead_id"] == 42

    asyncio.run(run())
    assert sent == {"party": "alice@example.com", "subject": "Re: Roof quote",
                    "body": "Tuesday works.", "lead": 42}


def test_send_reply_double_click_is_refused(monkeypatch):
    import mail_inbox

    called = {"transport": False}

    def fake_zoho(party, subject, body, key):
        called["transport"] = True
        return {"ok": True}

    monkeypatch.setattr(mail_inbox, "_send_via_zoho", fake_zoho)
    monkeypatch.setattr(mail_inbox, "_recent_duplicate", lambda *a, **k: True)

    async def run():
        result = await mail_inbox.send_reply(
            None, party="alice@example.com", key="x", subject="Re: x", body="same text")
        assert result["ok"] is False
        assert "exact reply" in result["reason"].lower()

    asyncio.run(run())
    assert called["transport"] is False


if __name__ == "__main__":
    import sys
    import types

    FAILS = []

    def check(name, fn):
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            print(f"  FAIL  {name}\n        {exc}")
            FAILS.append(name)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {name}\n        {type(exc).__name__}: {exc}")
            FAILS.append(name)

    class _Patch:
        """Tiny monkeypatch stand-in so this file runs without pytest."""

        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value):
            old = getattr(obj, name)
            self._undo.append((obj, name, old))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)
            self._undo.clear()

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and isinstance(f, types.FunctionType)]
    print(f"running {len(tests)} mail_inbox tests (no network, no db)")
    for name, fn in tests:
        if "monkeypatch" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
            patch = _Patch()
            check(name, lambda fn=fn, patch=patch: (fn(patch), patch.undo())[0])
        else:
            check(name, fn)
    print(f"\n{len(tests) - len(FAILS)}/{len(tests)} pass")
    sys.exit(1 if FAILS else 0)
