"""End-to-end proof of the outbound calling lifecycle with no network and no real calls.

Everything here is a fake: :class:`FakeDB` is a SQL-recording stand-in for a
psycopg connection (it answers only the queries the campaign, suppression and
cadence code actually issues), and ``signalwire_service.SignalWireService`` is
replaced with an object that records the dial instead of placing it. No test
opens a database connection and no test places a call.

The sequence pinned here is the real one:

    candidates -> suppression gate -> dial -> bot_calls + comms_logs
    -> (on a spoken stop) suppression.honor_opt_out

Run:

    venv/bin/python -m pytest test_outbound_e2e.py -q
"""

from __future__ import annotations

import re
from datetime import date

import pytest

import followups
import outbound_campaign as oc
import signalwire_service
import suppression

PHONE = "757-908-7121"
E164 = "+17579087121"


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeCursor:
    """Minimal psycopg v3 cursor: records SQL, answers from FakeDB."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.db.executed.append((sql, params))
        self.db._route(sql, params)

    def fetchall(self):
        return self.db._rows

    def fetchone(self):
        return self.db._one

    @property
    def rowcount(self):
        return 1


class FakeDB:
    """A connection-shaped double that records every statement it is given.

    It emulates just enough of Postgres to make the code under test take its
    real branches: the source allow-list in ``candidates``, the suppression
    lookup, the DNC scrub date, the daily call counter and the two inserts a
    placed call writes. Nothing outside those statements is interpreted.
    """

    def __init__(self, leads=(), suppressions=(), scrub_date=None, calls_today=0):
        self.leads = [dict(row) for row in leads]
        self.suppressions = [dict(row) for row in suppressions]
        self.scrub_date = scrub_date
        self.calls_today = calls_today

        self.executed = []            # [(sql, params), ...] in call order
        self.commits = 0
        self.rollbacks = 0
        self.bot_calls = []           # rows a real bot_calls insert would write
        self.comms_logs = []          # rows a real comms_logs insert would write
        self.suppression_inserts = []
        self.audit_inserts = []
        self.candidates_params = None
        self._rows = []
        self._one = None
        self._next_id = 1

    # -- connection surface ------------------------------------------------
    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass

    # -- query routing -----------------------------------------------------
    def _route(self, sql, params):
        q = " ".join(str(sql).split()).lower()
        self._rows = []
        self._one = None

        # --- writes -------------------------------------------------------
        if "insert into suppression_list" in q:
            kind, value_type, value, channel, reason, source, actor = params
            row = next((r for r in self.suppressions
                        if r.get("value_type") == value_type and r.get("value") == value), None)
            if row is None:
                row = {"id": self._next_id, "value_type": value_type, "value": value}
                self._next_id += 1
                self.suppressions.append(row)
            row.update({"kind": kind, "channel": channel, "reason": reason,
                        "source": source, "actor": actor, "active": True})
            self.suppression_inserts.append(dict(row))
            self._one = {"id": row["id"]}
            return
        if "insert into suppression_audit" in q:
            self.audit_inserts.append(params)
            return
        if "insert into bot_calls" in q:
            sid, lead_id = params
            self.bot_calls.append({"call_sid": sid, "lead_id": int(lead_id),
                                   "direction": "outbound", "status": "dialed"})
            return
        if "insert into comms_logs" in q:
            self.comms_logs.append({"direction": "outbound", "channel": "voice",
                                    "sender": "system", "recipient": params[0],
                                    "message_body": params[1]})
            return

        # --- reads --------------------------------------------------------
        if "from leads l" in q:
            self.candidates_params = params if isinstance(params, dict) else {}
            self._rows = self._candidates(self.candidates_params)
            return
        if "from suppression_list" in q:
            self._rows = self._suppression_matches(params)
            return
        if "from outbound_dnc_scrub" in q:
            self._one = {"scrubbed_at": self.scrub_date} if self.scrub_date else None
            return
        if "from outbound_campaign_runs" in q:
            return
        if "from bot_calls" in q:
            self._one = {"n": self.calls_today}
            return
        if "from comms_logs" in q:
            # Used by both contact_policy_allows (n/last_at) and
            # _outbound_sent_today (c). A fresh lead has no touches.
            self._one = {"n": 0, "c": 0, "last_at": None}
            return
        if "from leads" in q:
            # followups.opted_out -> leads.opted_out_at
            self._one = {"opted_out_at": None}
            return

    def _candidates(self, params):
        """Emulate the SELECT in outbound_campaign.candidates (not its policy)."""
        rows = [dict(r) for r in self.leads]
        company = params.get("company")
        if company:
            rows = [r for r in rows if r.get("company", company) == company]
        closed = {str(s).lower() for s in (params.get("closed") or [])}
        if closed:
            rows = [r for r in rows
                    if str(r.get("status") or "new").lower() not in closed]
        sources = {str(s).lower() for s in (params.get("sources") or [])}
        if sources:
            rows = [r for r in rows
                    if str(r.get("source") or "").lower() in sources]
        rows = [r for r in rows
                if len(re.sub(r"\D", "", str(r.get("phone") or ""))) >= 10]
        dialed = {int(c["lead_id"]) for c in self.bot_calls
                  if c.get("lead_id") is not None}
        rows = [r for r in rows if int(r["id"]) not in dialed]
        limit = params.get("limit")
        if limit:
            rows = rows[: int(limit)]
        return rows

    def _suppression_matches(self, params):
        pairs = []
        values = list(params or [])
        for i in range(0, len(values) - 1, 2):
            pairs.append((values[i], values[i + 1]))
        return [dict(r) for r in self.suppressions
                if (r.get("value_type"), r.get("value")) in pairs]


class FakeSignalWire:
    """Records dials; never opens a socket."""

    def __init__(self, sid="CA_fake_0001", configured=True):
        self.sid = sid
        self.configured = configured
        self.calls = []

    def is_configured(self):
        return self.configured

    def create_ai_outbound_call(self, to, swml_url):
        self.calls.append({"to": to, "swml_url": swml_url})
        return self.sid


class ExplodingSignalWire:
    """Constructing or dialling this fails the test loudly."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("SignalWire must not be constructed when the campaign is off")

    def create_ai_outbound_call(self, *args, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("SignalWire must not be dialled")


def _forbidden(message):
    def _raise(*args, **kwargs):
        raise AssertionError(message)
    return _raise


def _lead(lead_id=11, phone=PHONE, source="quotes_page", status="new"):
    return {
        "id": lead_id,
        "company": "construction",
        "name": "Dana Homeowner",
        "phone": phone,
        "email": "",
        "source": source,
        "status": status,
        "project_type": "kitchen",
        "address": "Norfolk, VA 23510",
    }


def _install_wire(monkeypatch, wire):
    monkeypatch.setattr(signalwire_service, "SignalWireService",
                        lambda *a, **k: wire)
    return wire


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_campaign_env(monkeypatch):
    """No ambient flag may leak in from the developer's shell."""
    for name in (
        "OUTBOUND_CAMPAIGN_ENABLED",
        "DISABLE_OUTBOUND_CAMPAIGN",
        "OUTBOUND_CAMPAIGN_SCHEDULED",
        "OUTBOUND_CAMPAIGN_SOURCES",
        "OUTBOUND_CAMPAIGN_MAX",
        "OUTBOUND_CAMPAIGN_DAILY",
        "OUTBOUND_CAMPAIGN_INCLUDE_PERMITS",
    ):
        monkeypatch.delenv(name, raising=False)
    # Do not let the fake DB's DDL flip a module-global that a real-DB test
    # later in the same session would then skip. monkeypatch restores it.
    monkeypatch.setattr(followups, "_SCHEMA_READY", True, raising=False)


# --------------------------------------------------------------------------
# 1. selection + dry run
# --------------------------------------------------------------------------

def test_fresh_lead_is_selected_then_passes_dry_run(monkeypatch):
    db = FakeDB(leads=[_lead()])
    wire = _install_wire(monkeypatch, FakeSignalWire(configured=False))
    monkeypatch.setattr(oc, "_dial", _forbidden("a dry run must never dial"))

    selected = oc.candidates(db, limit=5)
    assert [row["id"] for row in selected] == [11]
    assert selected[0]["phone"] == PHONE
    assert db.candidates_params["sources"], "dry runs must use the inbound source allow-list"

    result = oc.run(db, limit=5, apply=False)

    assert result["ok"] is True
    assert result["applied"] is False
    assert result["considered"] == 1
    assert result["skipped"] == []
    assert [row["lead_id"] for row in result["would_call"]] == [11]
    assert result["would_call"][0]["phone"] == E164
    assert result["would_call"][0]["source"] == "quotes_page"
    assert result["dialed"] == []
    assert wire.calls == []
    assert db.bot_calls == []
    assert db.comms_logs == []


# --------------------------------------------------------------------------
# 2. the enable flag is a real gate
# --------------------------------------------------------------------------

def test_apply_without_the_enable_flag_refuses_before_signalwire(monkeypatch):
    monkeypatch.delenv("OUTBOUND_CAMPAIGN_ENABLED", raising=False)
    monkeypatch.delenv("DISABLE_OUTBOUND_CAMPAIGN", raising=False)
    assert oc.enabled() is False

    db = FakeDB(leads=[_lead()])
    monkeypatch.setattr(signalwire_service, "SignalWireService", ExplodingSignalWire)
    monkeypatch.setattr(oc, "_dial", _forbidden("a disabled campaign must never dial"))

    result = oc.run(db, limit=5, apply=True)

    assert result["ok"] is False
    assert result["applied"] is False
    assert "OUTBOUND_CAMPAIGN_ENABLED" in result["reason"]
    assert result["dialed"] == []
    assert result["would_call"] == []
    # It refused before even querying for candidates.
    assert db.executed == []
    assert db.bot_calls == []
    assert db.comms_logs == []

    # "false" is not a disabling value (the documented truthiness trap).
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_ENABLED", "1")
    monkeypatch.setenv("DISABLE_OUTBOUND_CAMPAIGN", "false")
    assert oc.enabled() is True


# --------------------------------------------------------------------------
# 3. the live path: dial, then both audit tables
# --------------------------------------------------------------------------

def test_apply_dials_inside_the_window_and_records_bot_calls_and_comms_logs(monkeypatch):
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_ENABLED", "1")
    monkeypatch.setattr(oc, "in_call_window", lambda now=None: (True, ""))

    db = FakeDB(leads=[_lead()])
    wire = _install_wire(monkeypatch, FakeSignalWire(sid="CA_fake_sid_123"))

    result = oc.run(db, limit=1, apply=True)

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["configured"] is True
    assert result["considered"] == 1
    assert result["skipped"] == []
    assert [row["lead_id"] for row in result["dialed"]] == [11]
    assert result["dialed"][0]["call_sid"] == "CA_fake_sid_123"
    assert result["dialed"][0]["phone"] == E164

    # The dial itself.
    assert len(wire.calls) == 1
    call = wire.calls[0]
    assert call["to"] == E164
    assert "/comms/outbound-voice.swml" in call["swml_url"]
    assert "lead_id=11" in call["swml_url"]

    # bot_calls: one row, keyed by the carrier sid.
    bot_inserts = [(sql, params) for sql, params in db.executed
                   if "insert into bot_calls" in sql.lower()]
    assert len(bot_inserts) == 1
    assert bot_inserts[0][1][0] == "CA_fake_sid_123"
    assert bot_inserts[0][1][1] == 11
    assert [row["call_sid"] for row in db.bot_calls] == ["CA_fake_sid_123"]

    # comms_logs: the voice/outbound row the contact policy counts next time.
    comm_inserts = [(sql, params) for sql, params in db.executed
                    if "insert into comms_logs" in sql.lower()]
    assert len(comm_inserts) == 1
    assert comm_inserts[0][1][0] == E164
    assert "lead 11" in comm_inserts[0][1][1]
    assert db.comms_logs[0]["channel"] == "voice"
    assert db.comms_logs[0]["direction"] == "outbound"
    assert db.commits >= 1


# --------------------------------------------------------------------------
# 4. suppression is absolute
# --------------------------------------------------------------------------

def test_suppressed_number_is_skipped_with_the_reason_and_never_dialled(monkeypatch):
    dnc = {
        "id": 41,
        "kind": "dnc",
        "value_type": "phone",
        "value": E164,
        "channel": "phone",
        "reason": "on the national DNC registry",
        "source": "dnc-scrub",
        "created_at": "2026-11-01T00:00:00Z",
    }
    db = FakeDB(leads=[_lead()], suppressions=[dnc])
    wire = _install_wire(monkeypatch, FakeSignalWire())
    monkeypatch.setattr(oc, "_dial", _forbidden("a suppressed number must never be dialled"))

    result = oc.run(db, limit=5, apply=False)

    assert result["ok"] is True
    assert result["would_call"] == []
    assert result["dialed"] == []
    assert len(result["skipped"]) == 1
    skip = result["skipped"][0]
    assert skip["lead_id"] == 11
    assert skip["phone"] == E164
    assert "suppressed" in skip["reason"].lower()
    assert "DNC" in skip["reason"]
    assert "national DNC registry" in skip["reason"]
    assert wire.calls == []
    assert db.bot_calls == []
    assert db.comms_logs == []
    assert not any("insert into bot_calls" in sql.lower() for sql, _ in db.executed)


# --------------------------------------------------------------------------
# 5. a spoken stop -> honor_opt_out (the helper contract, not the handler)
# --------------------------------------------------------------------------

def test_spoken_stop_is_detected_and_honor_opt_out_is_the_required_follow_up():
    for said in ("stop", "Stop.", "please stop", "stop calling me",
                 "do not call me again", "take me off your list"):
        assert oc.wants_stop(said) is True, said
    # A scheduling sentence is not an opt-out.
    assert oc.wants_stop("stop by Tuesday") is False

    db = FakeDB()
    result = suppression.honor_opt_out(
        db, phone=PHONE, reason="said stop on an outbound call", actor="voice-agent")

    assert result["ok"] is True
    phone_entry = result["results"]["phone"]
    assert phone_entry["ok"] is True
    assert phone_entry["value"] == E164
    assert phone_entry["kind"] == suppression.KIND_UNSUBSCRIBE
    assert db.suppression_inserts[-1]["kind"] == "unsubscribe"
    assert db.suppression_inserts[-1]["channel"] == "any"
    assert db.suppression_inserts[-1]["value"] == E164

    # The next call to check_outbound must see the stop, not a clean list.
    # check_outbound's contract is (allowed, reason) -- False means blocked.
    allowed, why = suppression.check_outbound(db, suppression.CHANNEL_VOICE, E164)
    assert allowed is False
    assert "UNSUBSCRIBE" in why
    assert "said stop on an outbound call" in why
    assert db.audit_inserts, "an opt-out must leave an audit row"


# --------------------------------------------------------------------------
# 6. cold permit calling is held without a recent DNC scrub
# --------------------------------------------------------------------------

def test_cold_permit_calling_is_held_without_a_scrub_while_inbound_is_dialled(monkeypatch):
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_ENABLED", "1")
    monkeypatch.setattr(oc, "in_call_window", lambda now=None: (True, ""))

    permit = _lead(lead_id=21, phone="757-623-0142", source="permit_finder")
    inbound = _lead(lead_id=22, phone=PHONE, source="quotes_page")
    db = FakeDB(leads=[permit, inbound])
    wire = _install_wire(monkeypatch, FakeSignalWire(sid="CA_inbound_only"))

    allowed, why = oc.cold_calling_allowed(db)
    assert allowed is False
    assert "DNC scrub" in why
    assert "import-dnc" in why

    result = oc.run(db, limit=5, apply=True, include_permits=True)

    assert result["ok"] is True
    assert result["applied"] is True
    assert "cold_calling_blocked" in result
    assert "DNC scrub" in result["cold_calling_blocked"]
    assert [row["lead_id"] for row in result["dialed"]] == [22]
    assert [call["to"] for call in wire.calls] == [E164]
    assert [row["lead_id"] for row in db.bot_calls] == [22]

    # The permit list was never put into the candidate query.
    sources = [str(s).lower() for s in db.candidates_params["sources"]]
    assert "permit_finder" not in sources
    assert "quotes_page" in sources

    # Positive control: a fresh scrub unlocks cold calling.
    fresh = FakeDB(leads=[permit], scrub_date=date.today())
    ok, fresh_why = oc.cold_calling_allowed(fresh)
    assert ok is True
    assert "DNC scrub" in fresh_why


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN DEFECT (outbound_campaign.run -> _sources): when "
        "OUTBOUND_CAMPAIGN_SOURCES names a permit source, the configured "
        "allow-list overrides the include_permits=False downgrade performed "
        "after cold_calling_allowed() denies, so a permit lead is dialled on "
        "a campaign whose report says cold calling is held."
    ),
)
def test_configured_source_allowlist_cannot_bypass_the_cold_calling_hold(monkeypatch):
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_ENABLED", "1")
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_SOURCES", "permit_finder,quotes_page")
    monkeypatch.setattr(oc, "in_call_window", lambda now=None: (True, ""))

    permit = _lead(lead_id=21, phone="757-623-0142", source="permit_finder")
    inbound = _lead(lead_id=22, phone=PHONE, source="quotes_page")
    db = FakeDB(leads=[permit, inbound])
    wire = _install_wire(monkeypatch, FakeSignalWire(sid="CA_permit_must_not_dial"))

    result = oc.run(db, limit=5, apply=True, include_permits=True)

    # The report says the cold segment is held...
    assert "cold_calling_blocked" in result
    # ...so only the inbound lead may be dialled.
    assert [row["lead_id"] for row in result["dialed"]] == [22]
    assert [call["to"] for call in wire.calls] == [E164]


# --------------------------------------------------------------------------
# 7. the SWML builder's latency/turn-taking contract
# --------------------------------------------------------------------------

def test_outbound_swml_keeps_latency_turn_taking_and_greeting_settings():
    try:
        import construction_main as m
    except ImportError as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"construction_main is not importable here: {exc}")

    greeting = "Hi Dana, this is Rachel, the automated assistant at BuildStack Construction."
    swml = m._build_voice_swml("test prompt", static_greeting=greeting)
    ai = swml["sections"]["main"][1]["ai"]
    params = ai["params"]

    assert params["wait_for_user"] is False
    assert params["interrupt_on_noise"] is False
    assert params["end_of_speech_timeout"] <= 500
    assert params["ai_model"] != "gpt-4.1"
    assert params["ai_model"]
    assert params["static_greeting"] == greeting
    assert params["static_greeting_no_barge"] is False

    # No greeting passed -> no static_greeting emitted at all.
    plain = m._build_voice_swml("test prompt")
    assert "static_greeting" not in plain["sections"]["main"][1]["ai"]["params"]
