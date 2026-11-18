"""Owner outbound calling: the routes, and the telephony under them.

    venv/bin/python -m pytest test_outbound_routes.py -q

Why this file exists
--------------------
The batch campaign in ``outbound_campaign.py`` is the only thing that dials an
existing lead. The owner needs a single deliberate call from a lead page, and
that path must not become a way around the rules the campaign already enforces:
suppression/DNC, the calling window, the cold-call scrub and the daily cap.

It also pins the two telephony defects that made a call look placed when it was
not: a number that was not E.164, and an instruction URL the carrier could not
fetch (relative, or http). Both are refused before SignalWire is asked to dial.

No network and no database: ``requests.post``, the SignalWire client and
``get_db`` are all replaced. Nothing here can place a real call.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import construction_main as main
import outbound_campaign
import signalwire_service as sw
import vapi_service


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeCursor:
    """Just enough SQL surface for the routes and outbound_campaign._record."""

    def __init__(self, db):
        self.db = db
        self._one = None
        self._many = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(str(sql).lower().split())
        params = tuple(params or ())
        self.db.statements.append((text, params))
        self._one = None
        self._many = []

        if text.startswith("create table"):
            return

        if text.startswith("select"):
            if "from leads where phone is not null" in text:
                self._many = list(self.db.leads.values())
                return
            if "from leads where id" in text:
                lead = self.db.leads.get(int(params[0])) if params else None
                # The route asks for the whole lead (company filter); the
                # opt-out helper asks only for the phone.
                self._one = lead if "company" in text else (
                    {"phone": (lead or {}).get("phone")} if lead else None)
                return
            if "from bot_calls where lead_id" in text:
                self._one = self.db.latest_for_lead(int(params[0]))
                return
            if "from bot_calls where call_sid" in text:
                self._one = self.db.by_sid().get(str(params[0]))
                return
            if "from comms_logs" in text:
                self._one = {"n": 0, "c": 0, "last_at": None}
                return
            if "from outbound_dnc_scrub" in text:
                self._one = None
                return
            self._one = None
            return

        if text.startswith("insert into bot_calls"):
            self.db.bot_calls.append({
                "call_sid": params[0],
                "lead_id": params[1],
                "direction": "outbound",
                "status": "dialed",
                "turns": 0,
                "created_at": datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc),
            })
            return
        if text.startswith("insert into comms_logs"):
            self.db.comms_logs.append(params)
            return
        if text.startswith("update bot_calls"):
            sid = params[-1]
            for row in self.db.bot_calls:
                if str(row["call_sid"]) == str(sid):
                    row["status"] = "opted_out"
            return

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class FakeDB:
    def __init__(self, leads=(), bot_calls=()):
        self.leads = {int(row["id"]): dict(row) for row in leads}
        self.bot_calls = [dict(row) for row in bot_calls]
        self.comms_logs = []
        self.statements = []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def latest_for_lead(self, lead_id):
        rows = [r for r in self.bot_calls
                if int(r.get("lead_id") or 0) == int(lead_id)]
        return rows[-1] if rows else None

    def by_sid(self):
        return {str(r.get("call_sid")): r for r in self.bot_calls}


class FakeResponse:
    def __init__(self, ok=True, status_code=200, payload=None, text=""):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


LEAD = {
    "id": 42,
    "name": "Dana Homeowner",
    "phone": "757-555-0142",
    "email": "dana@example.com",
    "source": "quotes_page",
    "status": "new",
    "address": "Norfolk, VA 23510",
    "project_type": "Kitchen",
}


@pytest.fixture
def db():
    return FakeDB(leads=[LEAD])


@pytest.fixture
def gates(monkeypatch, db):
    """All the campaign gates on, plus a carrier that accepts the call."""
    monkeypatch.delenv("DISABLE_OUTBOUND_CAMPAIGN", raising=False)
    monkeypatch.setenv("APP_BASE_URL", "https://construction.example.test")
    monkeypatch.setattr(outbound_campaign, "in_call_window", lambda now=None: (True, ""))
    monkeypatch.setattr(outbound_campaign, "calls_made_today", lambda conn: 0)
    monkeypatch.setattr(outbound_campaign, "daily_limit", lambda: 30)
    monkeypatch.setattr(outbound_campaign, "cold_calling_allowed",
                        lambda conn: (True, "DNC scrub 1 day(s) old"))
    monkeypatch.setattr(outbound_campaign, "enabled", lambda: True)
    monkeypatch.setattr(main.suppression, "check_outbound", lambda *a, **k: (True, ""))
    monkeypatch.setattr(main.signalwire, "is_configured", lambda: True)
    monkeypatch.setattr(main.signalwire, "missing", lambda: [])

    dialed = []

    def fake_dial(to, swml_url, raise_on_error=False):
        dialed.append({"to": to, "swml_url": swml_url})
        return "CA_test_123"

    monkeypatch.setattr(main.signalwire, "create_ai_outbound_call", fake_dial)
    return dialed


@pytest.fixture
def client(db, gates, monkeypatch):
    monkeypatch.setattr(main, "require_admin", lambda request: None)
    main.app.dependency_overrides[main.get_db] = lambda: db
    try:
        yield TestClient(main.app, raise_server_exceptions=True)
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)


@pytest.fixture
def anonymous_client(db):
    main.app.dependency_overrides[main.get_db] = lambda: db
    try:
        yield TestClient(main.app, raise_server_exceptions=True)
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)


# --------------------------------------------------------------------------
# the routes are owner-only
# --------------------------------------------------------------------------

def test_outbound_routes_require_an_owner_session(anonymous_client):
    for method, path in (("post", "/api/outbound/call"),
                         ("get", "/api/outbound/status"),
                         ("get", "/api/outbound/campaign")):
        response = getattr(anonymous_client, method)(path)
        assert response.status_code == 401, path


# --------------------------------------------------------------------------
# POST /api/outbound/call
# --------------------------------------------------------------------------

def test_places_one_call_and_records_it_like_the_campaign(client, db, gates):
    response = client.post("/api/outbound/call", json={"lead_id": 42})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "call_sid": "CA_test_123"}

    assert len(gates) == 1
    assert gates[0]["to"] == "+17575550142"
    url = gates[0]["swml_url"]
    assert url.startswith("https://construction.example.test/comms/outbound-voice.swml?")
    assert "lead_id=42" in url

    assert len(db.bot_calls) == 1
    assert db.bot_calls[0]["call_sid"] == "CA_test_123"
    assert db.bot_calls[0]["lead_id"] == 42
    assert db.bot_calls[0]["status"] == "dialed"
    assert len(db.comms_logs) == 1
    assert db.commits >= 1


def test_refuses_a_suppressed_number(client, gates, monkeypatch):
    """A spoken stop has to survive as a suppression entry; this is the dial path."""
    monkeypatch.setattr(
        main.suppression, "check_outbound",
        lambda *a, **k: (False, "UNSUBSCRIBE: asked to stop on a call"))

    response = client.post("/api/outbound/call", json={"lead_id": 42})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "UNSUBSCRIBE" in body["reason"]
    assert gates == []          # nothing was dialled


def test_refuses_closed_won_and_lost_leads(client, gates, db):
    for status_value in ("won", "lost", "closed", "cancelled"):
        db.leads[42]["status"] = status_value
        body = client.post("/api/outbound/call", json={"lead_id": 42}).json()
        assert body["ok"] is False, status_value
        assert status_value in body["reason"]
    assert gates == []


def test_refuses_a_placeholder_or_missing_phone(client, gates, db):
    for phone in ("", "12345", "555-555-5555", "000-000-0000"):
        db.leads[42]["phone"] = phone
        body = client.post("/api/outbound/call", json={"lead_id": 42}).json()
        assert body["ok"] is False, phone
        assert "phone" in body["reason"].lower()
    assert gates == []


def test_refuses_outside_the_calling_window(client, gates, monkeypatch):
    monkeypatch.setattr(outbound_campaign, "in_call_window",
                        lambda now=None: (False, "Sunday — no calling"))
    body = client.post("/api/outbound/call", json={"lead_id": 42}).json()
    assert body["ok"] is False
    assert "Sunday" in body["reason"]
    assert gates == []


def test_refuses_when_the_daily_cap_is_reached(client, gates, monkeypatch):
    monkeypatch.setattr(outbound_campaign, "calls_made_today", lambda conn: 30)
    body = client.post("/api/outbound/call", json={"lead_id": 42}).json()
    assert body["ok"] is False
    assert "cap" in body["reason"].lower()
    assert gates == []


def test_refuses_a_cold_lead_without_a_dnc_scrub(client, gates, monkeypatch, db):
    db.leads[42]["source"] = "permit_finder"
    monkeypatch.setattr(outbound_campaign, "cold_calling_allowed",
                        lambda conn: (False, "no DNC scrub on file"))
    body = client.post("/api/outbound/call", json={"lead_id": 42}).json()
    assert body["ok"] is False
    assert "DNC scrub" in body["reason"]
    assert gates == []


def test_refuses_when_the_kill_switch_is_on(client, gates, monkeypatch):
    monkeypatch.setenv("DISABLE_OUTBOUND_CAMPAIGN", "true")
    body = client.post("/api/outbound/call", json={"lead_id": 42}).json()
    assert body["ok"] is False
    assert "DISABLE_OUTBOUND_CAMPAIGN" in body["reason"]
    assert gates == []


def test_refuses_an_unknown_lead(client, gates):
    body = client.post("/api/outbound/call", json={"lead_id": 999}).json()
    assert body["ok"] is False
    assert "999" in body["reason"]
    assert gates == []


def test_refuses_a_bad_lead_id(client, gates):
    for payload in ({}, {"lead_id": "abc"}, {"lead_id": None}):
        body = client.post("/api/outbound/call", json=payload).json()
        assert body["ok"] is False
        assert "lead_id" in body["reason"]
    assert gates == []


def test_carrier_failure_is_reported_not_swallowed(client, db, monkeypatch):
    def reject(*args, **kwargs):
        raise sw.SignalWireError("SignalWire rejected the AI call: HTTP 422 inactive_campaign")

    monkeypatch.setattr(main.signalwire, "create_ai_outbound_call", reject)
    body = client.post("/api/outbound/call", json={"lead_id": 42}).json()
    assert body["ok"] is False
    assert "422" in body["reason"]
    assert db.bot_calls == []


def test_unconfigured_signalwire_names_the_missing_variable(client, monkeypatch):
    monkeypatch.setattr(main.signalwire, "is_configured", lambda: False)
    monkeypatch.setattr(main.signalwire, "missing", lambda: ["SIGNALWIRE_API_TOKEN"])
    body = client.post("/api/outbound/call", json={"lead_id": 42}).json()
    assert body["ok"] is False
    assert "SIGNALWIRE_API_TOKEN" in body["reason"]


# --------------------------------------------------------------------------
# GET /api/outbound/status
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stored,expected", [
    ("dialed", "dialed"),
    ("in_progress", "in_progress"),
    ("answering", "answered"),
    ("answered", "answered"),
    ("opted_out", "opted_out"),
])
def test_status_maps_the_carrier_vocabulary(client, db, stored, expected):
    db.bot_calls.append({
        "call_sid": "CA_one", "lead_id": 42, "direction": "outbound",
        "status": stored, "turns": 1,
        "created_at": datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc),
    })
    body = client.get("/api/outbound/status", params={"lead_id": 42}).json()
    assert body["ok"] is True
    assert body["status"] == expected
    assert body["last_at"].startswith("2026-11-18T15:00")


def test_status_is_not_called_before_any_row(client):
    body = client.get("/api/outbound/status", params={"lead_id": 42}).json()
    assert body == {"ok": True, "lead_id": 42, "status": "not_called", "last_at": None}


def test_status_requires_a_lead_id(client):
    body = client.get("/api/outbound/status").json()
    assert body["ok"] is False


# --------------------------------------------------------------------------
# GET /api/outbound/campaign
# --------------------------------------------------------------------------

def test_campaign_status_comes_from_the_enforcing_helpers(client, monkeypatch):
    monkeypatch.setattr(outbound_campaign, "calls_made_today", lambda conn: 7)
    monkeypatch.setattr(outbound_campaign, "daily_limit", lambda: 25)
    monkeypatch.setattr(outbound_campaign, "enabled", lambda: False)
    monkeypatch.setattr(outbound_campaign, "cold_calling_allowed",
                        lambda conn: (False, "no DNC scrub"))

    body = client.get("/api/outbound/campaign").json()
    assert body == {
        "ok": True,
        "dialed_today": 7,
        "daily_cap": 25,
        "enabled": False,
        "cold_calling": False,
    }


# --------------------------------------------------------------------------
# a spoken "stop" must survive the SWML call
# --------------------------------------------------------------------------

def test_transcript_stop_detection_reads_only_the_caller():
    assert main._transcript_says_stop([
        {"role": "assistant", "content": "You can say stop at any time and I'll remove your number."},
    ]) is False
    assert main._transcript_says_stop([
        {"role": "user", "content": "can you stop by Tuesday?"},
    ]) is False
    assert main._transcript_says_stop([
        {"role": "assistant", "content": "Sure."},
        {"role": "user", "content": "Stop."},
    ]) is True


def test_post_prompt_suppresses_a_spoken_stop(monkeypatch, db):
    db.bot_calls.append({
        "call_sid": "CA_stop", "lead_id": 42, "direction": "outbound",
        "status": "in_progress", "turns": 2,
        "created_at": datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc),
    })
    captured = {}

    def fake_honor(conn, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(main.suppression, "honor_opt_out", fake_honor)
    main.app.dependency_overrides[main.get_db] = lambda: db
    try:
        response = TestClient(main.app).post("/api/voice/debug", json={
            "call_id": "CA_stop",
            "direction": "outbound",
            "to": "+17575550142",
            "summary": "",
            "call_log": [
                {"role": "assistant", "content": "You can say stop at any time."},
                {"role": "user", "content": "please take me off your list"},
            ],
        })
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)

    assert response.status_code == 204
    assert captured["phone"] == "+17575550142"
    assert "stop" in captured["reason"].lower()
    assert db.bot_calls[0]["status"] == "opted_out"


def test_post_prompt_uses_the_lead_phone_on_the_real_payload_shape(monkeypatch, db):
    """SignalWire's post-prompt report has call_id and caller_id_number, not from/to."""
    monkeypatch.setattr(main, "SIGNALWIRE_NUMBER", "+17578469275")
    db.bot_calls.append({
        "call_sid": "CA_real", "lead_id": 42, "direction": "outbound",
        "status": "in_progress", "turns": 1,
        "created_at": datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc),
    })
    captured = {}

    def fake_honor(conn, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(main.suppression, "honor_opt_out", fake_honor)
    main.app.dependency_overrides[main.get_db] = lambda: db
    try:
        response = TestClient(main.app).post("/api/voice/debug", json={
            "action": "post_conversation",
            "conversation_type": "voice",
            "call_id": "CA_real",
            "caller_id_number": "+17578469275",   # our line, because we dialled them
            "call_log": [{"role": "user", "content": "do not call me again"}],
        })
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)

    assert response.status_code == 204
    assert captured["phone"] == "757-555-0142"


def test_post_prompt_does_not_suppress_a_plan_to_visit(monkeypatch, db):
    monkeypatch.setattr(main.suppression, "honor_opt_out", lambda *a, **k: pytest.fail(
        "a scheduling sentence is not an opt-out"))
    main.app.dependency_overrides[main.get_db] = lambda: db
    try:
        response = TestClient(main.app).post("/api/voice/debug", json={
            "call_id": "CA_visit",
            "direction": "outbound",
            "to": "+17575550142",
            "call_log": [{"role": "user", "content": "can you stop by Thursday?"}],
        })
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)
    assert response.status_code == 204


# --------------------------------------------------------------------------
# SignalWire: validate before the carrier
# --------------------------------------------------------------------------

def _service(monkeypatch):
    monkeypatch.setenv("SIGNALWIRE_PROJECT_ID", "project-1")
    monkeypatch.setenv("SIGNALWIRE_API_TOKEN", "token-1")
    monkeypatch.setenv("SIGNALWIRE_SPACE_URL", "bizstack-perks.signalwire.com")
    monkeypatch.setenv("SIGNALWIRE_PHONE", "+17578469275")
    return sw.SignalWireService()


def test_space_url_accepts_a_full_url_and_trailing_slash(monkeypatch):
    monkeypatch.setenv("SIGNALWIRE_PROJECT_ID", "project-1")
    monkeypatch.setenv("SIGNALWIRE_API_TOKEN", "token-1")
    monkeypatch.setenv("SIGNALWIRE_SPACE_URL", "https://bizstack-perks.signalwire.com/")
    monkeypatch.setenv("SIGNALWIRE_PHONE", "+17578469275")
    service = sw.SignalWireService()
    assert service.is_configured() is True
    assert service.host == "bizstack-perks.signalwire.com"


def test_ai_call_sends_e164_and_returns_the_call_id(monkeypatch):
    service = _service(monkeypatch)
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["body"] = kwargs["json"]
        return FakeResponse(payload={"result": {"call_id": "call-123"}})

    monkeypatch.setattr(sw.requests, "post", fake_post)
    sid = service.create_ai_outbound_call(
        "757-555-0142",
        "https://construction.example.test/comms/outbound-voice.swml")
    assert sid == "call-123"
    assert seen["body"]["params"]["to"] == "+17575550142"
    assert seen["body"]["params"]["from"] == "+17578469275"
    assert seen["url"] == "https://bizstack-perks.signalwire.com/api/calling/calls"


@pytest.mark.parametrize("bad_number", ["", "12345", "not-a-number", "757-555"])
def test_ai_call_refuses_a_number_that_is_not_e164(monkeypatch, bad_number):
    service = _service(monkeypatch)

    def no_post(*args, **kwargs):
        pytest.fail("the carrier must not be called with a malformed number")

    monkeypatch.setattr(sw.requests, "post", no_post)
    assert service.create_ai_outbound_call(
        bad_number, "https://construction.example.test/x.swml") == ""
    with pytest.raises(sw.SignalWireError):
        service.create_ai_outbound_call(
            bad_number, "https://construction.example.test/x.swml", raise_on_error=True)


@pytest.mark.parametrize("bad_url", [
    "",
    "/comms/outbound-voice.swml",
    "http://construction.example.test/comms/outbound-voice.swml",
    "https:///comms/outbound-voice.swml",
])
def test_ai_call_refuses_a_url_the_carrier_cannot_fetch(monkeypatch, bad_url):
    service = _service(monkeypatch)

    def no_post(*args, **kwargs):
        pytest.fail("the carrier must not be called without an absolute https SWML URL")

    monkeypatch.setattr(sw.requests, "post", no_post)
    assert service.create_ai_outbound_call("+17575550142", bad_url) == ""
    with pytest.raises(sw.SignalWireError):
        service.create_ai_outbound_call("+17575550142", bad_url, raise_on_error=True)


def test_missing_credentials_are_named_and_never_post(monkeypatch):
    monkeypatch.setenv("SIGNALWIRE_PROJECT_ID", "")
    monkeypatch.setenv("SIGNALWIRE_API_TOKEN", "")
    monkeypatch.setenv("SIGNALWIRE_SPACE_URL", "yourspace.signalwire.com")
    monkeypatch.setenv("SIGNALWIRE_PHONE", "")
    service = sw.SignalWireService()

    def no_post(*args, **kwargs):
        pytest.fail("nothing may be posted without credentials")

    monkeypatch.setattr(sw.requests, "post", no_post)
    assert service.is_configured() is False
    assert set(service.missing()) == {
        "SIGNALWIRE_PROJECT_ID", "SIGNALWIRE_API_TOKEN",
        "SIGNALWIRE_SPACE_URL", "SIGNALWIRE_PHONE",
    }
    assert service.create_ai_outbound_call(
        "+17575550142", "https://construction.example.test/x.swml") == ""
    with pytest.raises(sw.SignalWireError):
        service.create_ai_outbound_call(
            "+17575550142", "https://construction.example.test/x.swml", raise_on_error=True)


def test_http_rejection_carries_the_status_and_body(monkeypatch):
    service = _service(monkeypatch)
    monkeypatch.setattr(sw.requests, "post", lambda *a, **k: FakeResponse(
        ok=False, status_code=422, text='{"error":"inactive_campaign"}'))

    assert service.create_ai_outbound_call(
        "+17575550142", "https://construction.example.test/x.swml") == ""
    with pytest.raises(sw.SignalWireError) as caught:
        service.create_ai_outbound_call(
            "+17575550142", "https://construction.example.test/x.swml",
            raise_on_error=True)
    assert "422" in str(caught.value)
    assert "inactive_campaign" in str(caught.value)


def test_legacy_twiml_call_uses_the_same_validation(monkeypatch):
    service = _service(monkeypatch)
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["body"] = kwargs["data"]
        return FakeResponse(payload={"sid": "CA_legacy"})

    monkeypatch.setattr(sw.requests, "post", fake_post)
    assert service.create_outbound_call(
        "757-555-0142", "https://construction.example.test/comms/voice") == "CA_legacy"
    assert seen["body"]["To"] == "+17575550142"
    assert "/api/laml/2010-04-01/Accounts/project-1/Calls" in seen["url"]

    with pytest.raises(sw.SignalWireError):
        service.create_outbound_call(
            "+17575550142", "http://construction.example.test/comms/voice",
            raise_on_error=True)


# --------------------------------------------------------------------------
# Vapi: same pre-validation, before it creates anything billable
# --------------------------------------------------------------------------

def test_vapi_normalises_a_us_number_and_rejects_a_placeholder():
    assert vapi_service.VapiService._e164("757-555-0142") == "+17575550142"
    assert vapi_service.VapiService._e164("+1 (757) 555-0142") == "+17575550142"
    assert vapi_service.VapiService._e164("12345") == ""
    assert vapi_service.VapiService._e164("") == ""


def test_vapi_refuses_a_bad_number_before_creating_an_assistant(monkeypatch):
    monkeypatch.setenv("VAPI_API_KEY", "key")
    service = vapi_service.VapiService()

    def must_not_run(*args, **kwargs):
        pytest.fail("the number must be validated before anything is created or bought")

    monkeypatch.setattr(service, "_ensure_assistant", must_not_run)
    monkeypatch.setattr(service, "_ensure_phone", must_not_run)
    with pytest.raises(ValueError):
        service.create_ai_outbound_call("12345")


# --------------------------------------------------------------------------
# the tuned outbound SWML is intact
# --------------------------------------------------------------------------

def test_swml_keeps_the_tuned_turn_taking(monkeypatch):
    monkeypatch.delenv("VOICE_AI_MODEL", raising=False)
    swml = main._build_voice_swml("prompt")
    ai = swml["sections"]["main"][1]["ai"]
    params = ai["params"]

    assert params["ai_model"] == "gpt-4.1-mini"
    assert params["end_of_speech_timeout"] == 500
    assert params["interrupt_on_noise"] is False
    assert params["enable_barge"] == "complete"
    assert params["barge_min_words"] == 2
    assert params["wait_for_user"] is False
    assert params["transparent_barge"] is True
    assert ai["languages"][0]["voice"] == "elevenlabs.rachel"


def test_outbound_swml_keeps_the_greeting_and_legal_disclosure(db):
    main.app.dependency_overrides[main.get_db] = lambda: db
    try:
        response = TestClient(main.app).get(
            "/comms/outbound-voice.swml", params={"to": "+17575550142"})
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)

    assert response.status_code == 200
    ai = response.json()["sections"]["main"][1]["ai"]
    greeting = ai["params"]["static_greeting"]
    assert "Dana" in greeting
    assert "automated assistant" in greeting
    assert "okay time" in greeting

    prompt = ai["prompt"]["text"]
    assert "LEGAL REQUIRED BEHAVIOUR FOR THIS OUTBOUND CALL" in prompt
    assert "automated AI" in prompt
    assert "say 'stop'" in prompt
    assert ai["params"]["direction"] == "outbound"
    assert ai["params"]["wait_for_user"] is False


def test_swml_callbacks_are_absolute_https_even_without_app_base(monkeypatch):
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    monkeypatch.setenv("COMPANY_DOMAIN", "construction.example.test")
    ai = main._build_voice_swml("prompt")["sections"]["main"][1]["ai"]
    assert ai["post_prompt_url"] == "https://construction.example.test/api/voice/debug"
    assert ai["SWAIG"]["defaults"]["web_hook_url"] == \
        "https://construction.example.test/api/voice/tool"
