"""Queue discipline for outbound AI calling, and DNC numbers on crew call lists.

    venv/bin/python -m pytest test_outbound_queue.py -q

No network, no database. Two fakes:

* ``_Store`` / ``_FakeDB`` model the handful of tables ``outbound_campaign``
  touches -- including the ``ON CONFLICT ... WHERE`` semantics of the claim row
  -- so two fake connections behave like two Postgres sessions against one
  database. That is the point of the exercise: the scheduler thread and a manual
  run must not dial the same lead.

* ``_CallListDB`` answers the call-list queries by shape and applies the
  suppression list in Python, but only when the emitted SQL actually contains a
  suppression match. Dropping the clause from ``call_list`` therefore fails
  these tests instead of quietly putting a DNC number back in a crew member's
  hand.
"""

import sys
import time
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import call_list as cl
import outbound_campaign as oc

MONDAY_9 = datetime(2026, 11, 16, 9, 5, tzinfo=ZoneInfo("America/New_York"))
DAY = MONDAY_9.date()


# --------------------------------------------------------------------------
# outbound campaign fakes
# --------------------------------------------------------------------------

def _lead(i, phone, status="new", source="quotes_page", company="construction"):
    return {"id": i, "name": f"Lead {i}", "phone": phone, "email": "",
            "source": source, "status": status, "project_type": "",
            "address": "Norfolk, VA 23510", "company": company,
            "created_at": time.time() - 3600}


class _Store:
    def __init__(self, leads=()):
        self.leads = [dict(lead) for lead in leads]
        self.claims = {}          # lead_id -> (worker, epoch seconds)
        self.bot_calls = []
        self.comms = []
        self.runs = set()
        self.scrubs = []
        self.counter_broken = False
        self.sql = []


class _Cur:
    def __init__(self, store):
        self.store = store
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.store.sql.append((sql, params))
        s = " ".join(sql.split()).lower()
        self._rows = []
        self.rowcount = 0

        if s.startswith("create"):
            return
        if "insert into outbound_call_claims" in s:
            lead_id, worker, ttl = params
            now = time.time()
            held = self.store.claims.get(lead_id)
            if held is None or held[1] < now - float(ttl):
                self.store.claims[lead_id] = (worker, now)
                self._rows = [{"lead_id": lead_id}]
            return
        if "delete from outbound_call_claims" in s:
            lead_id, worker = params
            held = self.store.claims.get(lead_id)
            if held and held[0] == worker:
                del self.store.claims[lead_id]
            self.rowcount = 1
            return
        if "insert into bot_calls" in s:
            sid, lead_id = params
            if all(c["call_sid"] != sid for c in self.store.bot_calls):
                self.store.bot_calls.append({
                    "call_sid": sid, "lead_id": int(lead_id), "direction": "outbound",
                    "status": "dialed", "created_at": time.time()})
                self.rowcount = 1
            return
        if "insert into comms_logs" in s:
            self.store.comms.append({"direction": "outbound", "channel": "voice",
                                     "recipient": params[0]})
            return
        if "select count(*) as n from bot_calls" in s:
            if self.store.counter_broken:
                raise RuntimeError("counter unavailable")
            failed = set(params[-1])
            self._rows = [{"n": sum(
                1 for c in self.store.bot_calls
                if c["direction"] == "outbound"
                and str(c.get("status") or "").lower() not in failed)}]
            return
        if "select 1 from bot_calls where lead_id" in s:
            lead_id = int(params[0])
            self._rows = [{"one": 1}] if any(
                c["lead_id"] == lead_id for c in self.store.bot_calls) else []
            return
        if "select 1 from outbound_campaign_runs" in s:
            self._rows = [{"one": 1}] if "today" in self.store.runs else []
            return
        if "select scrubbed_at from outbound_dnc_scrub" in s:
            self._rows = list(self.store.scrubs)
            return
        if "insert into outbound_campaign_runs" in s:
            self.store.runs.add("today")
            return
        if "from leads l" in s:
            self._rows = self._candidates(params)
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def _candidates(self, params):
        p = params if isinstance(params, dict) else {}
        closed = set(p.get("closed") or ())
        sources = p.get("sources") or []
        ttl = float(p.get("claim_ttl") or oc._CLAIM_TTL_SECONDS)
        fresh = float(p.get("fresh_seconds") or 0)
        now = time.time()
        out = []
        for lead in self.store.leads:
            digits = "".join(ch for ch in str(lead.get("phone") or "") if ch.isdigit())
            if len(digits) < 10:
                continue
            if str(lead.get("status") or "").lower() in closed:
                continue
            created = lead.get("created_at")
            if created is not None and created >= now - fresh:
                continue
            if any(c["lead_id"] == lead["id"] for c in self.store.bot_calls):
                continue
            if any(c["channel"] == "voice" and c["direction"] == "outbound"
                   and str(c.get("recipient") or "")[-10:] == digits[-10:]
                   for c in self.store.comms):
                continue
            held = self.store.claims.get(lead["id"])
            if held and held[1] >= now - ttl:
                continue
            if sources and str(lead.get("source") or "").lower() not in sources:
                continue
            out.append({k: lead.get(k) for k in
                        ("id", "name", "phone", "email", "source", "status",
                         "project_type", "address")})
        out.sort(key=lambda r: (0 if r["status"] in ("engaged", "replied", "contacted")
                                else 1 if r["status"] == "new" else 2, -r["id"]))
        return out[: int(p.get("limit") or 5)]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """One connection. Several of these share a ``_Store`` like sessions do."""

    def __init__(self, store):
        self.store = store
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cur(self.store)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Wire:
    def __init__(self, reject=False):
        self.calls = []
        self.reject = reject

    def is_configured(self):
        return True

    def create_ai_outbound_call(self, to, url):
        self.calls.append((to, url))
        return "" if self.reject else f"CA{len(self.calls):04d}"


def _install(monkeypatch, wire, allow=True):
    """Opt the campaign in and freeze out SignalWire and the suppression gate."""
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_ENABLED", "1")
    monkeypatch.delenv("DISABLE_OUTBOUND_CAMPAIGN", raising=False)
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_MAX", "5")
    monkeypatch.setattr(
        oc.suppression, "check_outbound",
        lambda *a, **k: (allow, "" if allow else "SUPPRESSED: do not call"))
    monkeypatch.setitem(sys.modules, "signalwire_service",
                        types.SimpleNamespace(SignalWireService=lambda: wire))


# --------------------------------------------------------------------------
# one dial per lead, even with two workers
# --------------------------------------------------------------------------

def test_two_workers_cannot_hold_the_same_lead():
    store = _Store([_lead(1, "757-908-7121")])
    first, second = _FakeDB(store), _FakeDB(store)

    assert oc._claim(first, 1, "worker-a") is True
    assert oc._claim(second, 1, "worker-b") is False
    assert store.claims[1][0] == "worker-a"

    # Only the holder can release it.
    oc._release(second, 1, "worker-b")
    assert 1 in store.claims
    oc._release(first, 1, "worker-a")
    assert oc._claim(second, 1, "worker-b") is True


def test_a_lead_being_dialled_is_not_a_candidate_again():
    store = _Store([_lead(1, "757-908-7121"), _lead(2, "757-908-7122")])
    store.claims[1] = ("other-worker", time.time())

    assert [r["id"] for r in oc.candidates(_FakeDB(store), limit=5)] == [2]


def test_a_lead_created_moments_ago_is_left_to_the_immediate_follow_up():
    old = _lead(1, "757-908-7121")
    fresh = _lead(2, "757-908-7122")
    fresh["created_at"] = time.time() - 60        # one minute old

    store = _Store([old, fresh])

    assert [r["id"] for r in oc.candidates(_FakeDB(store), limit=5)] == [1]


def test_the_second_run_dials_nothing_while_the_first_holds_the_lead(monkeypatch):
    store = _Store([_lead(1, "757-908-7121")])
    first, second = _FakeDB(store), _FakeDB(store)
    sids = []
    nested = []

    def dial(wire, to, lead_id, base):
        # The scheduler's copy fires while the first worker is inside the
        # carrier call. It is a separate connection to the same database.
        nested.append(oc.run(second, limit=5, apply=True, force_window=True))
        sids.append(f"CA{len(sids) + 1}")
        return sids[-1]

    monkeypatch.setattr(oc, "_dial", dial)
    _install(monkeypatch, _Wire())

    report = oc.run(first, limit=5, apply=True, force_window=True)

    assert [d["lead_id"] for d in report["dialed"]] == [1]
    assert len(sids) == 1, "the lead was dialled twice"
    assert nested[0]["dialed"] == []
    assert nested[0]["considered"] == 0
    assert len(store.bot_calls) == 1


def test_a_lead_another_worker_claimed_is_skipped_even_if_selected(monkeypatch):
    store = _Store()
    store.claims[1] = ("other-worker", time.time())
    monkeypatch.setattr(oc, "candidates", lambda *a, **k: [_lead(1, "757-908-7121")])
    _install(monkeypatch, _Wire())

    report = oc.run(_FakeDB(store), limit=5, apply=True, force_window=True)

    assert report["dialed"] == []
    assert report["skipped"][0]["reason"] == "another worker is already dialling this lead"
    assert store.bot_calls == []


def test_a_stale_claim_is_taken_over_but_a_recorded_call_still_blocks(monkeypatch):
    store = _Store()
    store.claims[1] = ("dead-worker", time.time() - oc._CLAIM_TTL_SECONDS - 60)
    store.bot_calls.append({"call_sid": "CA9", "lead_id": 1, "direction": "outbound",
                            "status": "dialed", "created_at": time.time()})
    monkeypatch.setattr(oc, "candidates", lambda *a, **k: [_lead(1, "757-908-7121")])
    _install(monkeypatch, _Wire())

    report = oc.run(_FakeDB(store), limit=5, apply=True, force_window=True)

    assert report["dialed"] == []
    assert report["skipped"][0]["reason"] == "already dialled by another worker"
    assert len(store.bot_calls) == 1


# --------------------------------------------------------------------------
# configuration cannot talk its way past the cold-calling hold
# --------------------------------------------------------------------------

def test_the_env_source_list_can_only_narrow_the_inbound_list(monkeypatch):
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_SOURCES", "permit_finder,Quotes_Page")

    assert oc._sources(False) == ["quotes_page"]
    assert oc._sources(True) == ["permit_finder", "quotes_page"]

    monkeypatch.delenv("OUTBOUND_CAMPAIGN_SOURCES")
    assert oc._sources(False) == list(oc._INBOUND_SOURCES)
    assert oc._sources(True) == []


def test_candidates_ignore_a_configured_permit_source_when_permits_are_held(monkeypatch):
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_SOURCES", "permit_finder,quotes_page")
    store = _Store([_lead(21, "757-908-7121", source="permit_finder")])

    # include_permits=False is what run() passes once the scrub check downgrades.
    assert oc.candidates(_FakeDB(store), limit=5, include_permits=False) == []


def test_a_configured_permit_source_cannot_bypass_the_cold_calling_hold(monkeypatch):
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_SOURCES", "permit_finder,quotes_page")
    store = _Store([_lead(21, "757-908-7121", source="permit_finder"),
                    _lead(22, "757-908-7122", source="quotes_page")])
    _install(monkeypatch, _Wire())

    # No scrub is on file, so run() downgrades include_permits. The configured
    # permit source must not survive that downgrade.
    report = oc.run(_FakeDB(store), limit=5, apply=True,
                    include_permits=True, force_window=True)

    assert report.get("cold_calling_blocked"), "the hold must be reported"
    assert [d["lead_id"] for d in report["dialed"]] == [22]
    assert [c["lead_id"] for c in store.bot_calls] == [22]


def test_only_cold_sources_configured_dials_nothing_while_permits_are_held(monkeypatch):
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_SOURCES", "permit_finder")
    store = _Store([_lead(21, "757-908-7121", source="permit_finder")])

    assert oc._sources(False) == [oc._NO_SOURCE]
    assert oc.candidates(_FakeDB(store), limit=5, include_permits=False) == []


# --------------------------------------------------------------------------
# a refused dial is not a call
# --------------------------------------------------------------------------

def test_a_rejected_dial_is_not_recorded_and_does_not_use_the_cap(monkeypatch):
    store = _Store([_lead(1, "757-908-7121")])
    db = _FakeDB(store)
    _install(monkeypatch, _Wire(reject=True))

    refused = oc.run(db, limit=5, apply=True, force_window=True)

    assert refused["dialed"] == []
    assert refused["skipped"][0]["reason"] == "carrier did not accept the call"
    assert store.bot_calls == [], "a rejected dial must not look like a dialled lead"
    assert oc.calls_made_today(db) == 0

    # Nothing was recorded, so the same lead is still eligible next time.
    assert [r["id"] for r in oc.candidates(db, limit=5)] == [1]

    _install(monkeypatch, _Wire(reject=False))
    accepted = oc.run(db, limit=5, apply=True, force_window=True)

    assert [d["lead_id"] for d in accepted["dialed"]] == [1]
    assert oc.calls_made_today(db) == 1


def test_only_placed_calls_count_against_the_daily_cap():
    store = _Store()
    now = time.time()
    for status in ("failed", "rejected", "error", "undialed", "dialed", "completed"):
        store.bot_calls.append({"call_sid": f"CA-{status}", "lead_id": 1,
                                "direction": "outbound", "status": status,
                                "created_at": now})

    # dialed + completed were real calls; the rest were never placed.
    assert oc.calls_made_today(_FakeDB(store)) == 2


def test_a_call_that_could_not_be_recorded_keeps_its_claim(monkeypatch):
    store = _Store()
    monkeypatch.setattr(oc, "candidates", lambda *a, **k: [_lead(1, "757-908-7121")])
    monkeypatch.setattr(oc, "_record", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("disk full")))
    _install(monkeypatch, _Wire())

    report = oc.run(_FakeDB(store), limit=5, apply=True, force_window=True)

    assert report["ok"] is False
    assert report["unrecorded"][0]["lead_id"] == 1
    assert 1 in store.claims, "releasing would allow an immediate second dial"


def test_a_dry_run_takes_no_claim_and_places_no_call(monkeypatch):
    store = _Store([_lead(1, "757-908-7121")])
    wire = _Wire()
    _install(monkeypatch, wire)

    report = oc.run(_FakeDB(store), limit=5, apply=False)

    assert [r["lead_id"] for r in report["would_call"]] == [1]
    assert store.claims == {}
    assert wire.calls == []


def test_an_unreadable_counter_stops_the_run(monkeypatch):
    store = _Store([_lead(1, "757-908-7121")])
    store.counter_broken = True
    monkeypatch.setattr(oc, "candidates", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not select anyone when the cap cannot be read")))
    _install(monkeypatch, _Wire())

    report = oc.run(_FakeDB(store), limit=5, apply=True, force_window=True)

    assert oc.calls_made_today(_FakeDB(store)) == -1
    assert report["ok"] is False
    assert "fail closed" in report["reason"]
    assert report["dialed"] == []


def test_the_daily_cap_is_enforced_and_stops_the_batch(monkeypatch):
    store = _Store([_lead(1, "757-908-7121"), _lead(2, "757-908-7122")])
    now = time.time()
    for i in range(3):
        store.bot_calls.append({"call_sid": f"CA{i}", "lead_id": 100 + i,
                                "direction": "outbound", "status": "dialed",
                                "created_at": now})
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_DAILY", "3")
    _install(monkeypatch, _Wire())

    report = oc.run(_FakeDB(store), limit=5, apply=True, force_window=True)

    assert report["ok"] is False
    assert "daily call cap reached" in report["reason"]
    assert report["dialed"] == []


def test_part_of_a_budget_is_still_spendable(monkeypatch):
    store = _Store([_lead(1, "757-908-7121"), _lead(2, "757-908-7122")])
    store.bot_calls.append({"call_sid": "CA0", "lead_id": 100, "direction": "outbound",
                            "status": "dialed", "created_at": time.time()})
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_DAILY", "2")
    _install(monkeypatch, _Wire())

    report = oc.run(_FakeDB(store), limit=5, apply=True, force_window=True)

    assert len(report["dialed"]) == 1
    assert oc.calls_made_today(_FakeDB(store)) == 2


# --------------------------------------------------------------------------
# the daily scheduler
# --------------------------------------------------------------------------

def test_the_scheduler_loop_survives_a_failing_cycle(monkeypatch):
    seen = []

    def flaky():
        seen.append("cycle")
        if len(seen) == 1:
            raise RuntimeError("carrier 500")

    monkeypatch.setattr(oc, "_scheduler_cycle", flaky)
    oc._scheduler_loop(interval=0, max_cycles=3)   # must not raise, must continue

    assert len(seen) == 3


def test_the_scheduler_rechecks_the_kill_switch_every_cycle(monkeypatch):
    store = _Store()
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_ENABLED", "1")
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_SCHEDULED", "1")
    monkeypatch.setenv("DISABLE_OUTBOUND_CAMPAIGN", "1")

    def explode(*a, **k):
        raise AssertionError("must not open a connection while the kill switch is on")

    assert oc._scheduler_cycle(now=MONDAY_9, connect=explode) == {
        "ran": False, "reason": "disabled"}

    # Clearing it resumes without a restart -- the loop re-reads it each cycle.
    monkeypatch.delenv("DISABLE_OUTBOUND_CAMPAIGN")
    monkeypatch.setattr(oc, "run", lambda *a, **k: {
        "ok": True, "considered": 1, "dialed": [], "skipped": []})
    monkeypatch.setattr(oc, "_log_run", lambda *a, **k: None)

    assert oc._scheduler_cycle(now=MONDAY_9, connect=lambda: _FakeDB(store))["ran"] is True


def test_a_refused_daily_run_does_not_burn_the_day(monkeypatch):
    store = _Store()
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_ENABLED", "1")
    monkeypatch.setenv("OUTBOUND_CAMPAIGN_SCHEDULED", "1")
    monkeypatch.setattr(oc, "run", lambda *a, **k: {
        "ok": False, "considered": 0, "dialed": [],
        "reason": "SignalWire is not configured; no call placed."})
    logged = []
    monkeypatch.setattr(oc, "_log_run", lambda *a, **k: logged.append(a))

    out = oc._scheduler_cycle(now=MONDAY_9, connect=lambda: _FakeDB(store))

    assert out == {"ran": True, "ok": False, "dialed": 0, "considered": 0,
                   "reason": "SignalWire is not configured; no call placed."}
    assert logged == [], "a refused run must stay retryable inside the hour"
    assert store.runs == set()

    monkeypatch.setattr(oc, "run", lambda *a, **k: {
        "ok": True, "considered": 0, "dialed": [], "skipped": []})
    oc._scheduler_cycle(now=MONDAY_9, connect=lambda: _FakeDB(store))
    assert logged, "a real run must mark the day"


# --------------------------------------------------------------------------
# the crew call list must not surface a suppressed number
# --------------------------------------------------------------------------

# One entry stored the way this app writes it, one the way an imported scrub
# file does: the comparison has to hold in both directions.
_SUPPRESSED = ("+1 (757) 555-0101", "757-555-0103")

_CALL_LIST_LEADS = [
    {"id": 1, "name": "Clean", "phone": "(757) 555-0102", "address": "1 Main St",
     "email": "", "source": "quotes_page", "status": "new", "project_type": "",
     "created_at": None, "last_reply": None, "touches": 0, "last_touch": None,
     "reason": "new"},
    # Same number as the E.164 entry, written without punctuation.
    {"id": 2, "name": "Do not call (E.164 entry)", "phone": "7575550101",
     "address": "2 Main St", "email": "", "source": "quotes_page", "status": "new",
     "project_type": "", "created_at": None, "last_reply": None, "touches": 0,
     "last_touch": None, "reason": "new"},
    # Same number as the bare scrub entry, written as E.164.
    {"id": 3, "name": "Do not call (scrub entry)", "phone": "+1 757-555-0103",
     "address": "3 Main St", "email": "", "source": "quotes_page", "status": "new",
     "project_type": "", "created_at": None, "last_reply": None, "touches": 0,
     "last_touch": None, "reason": "new"},
]


def _last_ten(phone):
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return digits[-10:]


_SUPPRESSED_KEYS = {_last_ten(p) for p in _SUPPRESSED}


def _visible(rows, sql):
    """The behaviour under test, keyed on the SQL actually emitted.

    If the suppression clause is missing the fake returns everything, which is
    precisely the defect these tests exist to catch.
    """
    if "suppression_list" not in sql:
        return [dict(r) for r in rows]
    return [dict(r) for r in rows if _last_ten(r.get("phone")) not in _SUPPRESSED_KEYS]


class _CallListCur:
    def __init__(self, store):
        self.store = store
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.store.sql.append((sql, params))
        s = " ".join(sql.split()).lower()
        self._rows = []
        self.rowcount = 0
        if s.startswith("create table"):
            return
        if "from leads l where l.company" in s:
            self._rows = _visible(self.store.leads, s)
        elif "from call_assignments a join leads" in s:
            self._rows = _visible(self.store.assigned, s)
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return list(self._rows)


class _CallListDB:
    def __init__(self):
        self.leads = [dict(r) for r in _CALL_LIST_LEADS]
        self.assigned = [dict(r, assignment_id=r["id"], state="assigned", note=None,
                              position=i, lead_id=r["id"])
                         for i, r in enumerate(_CALL_LIST_LEADS)]
        self.sql = []

    def cursor(self):
        return _CallListCur(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def test_the_crew_list_never_shows_a_suppressed_number():
    rows = cl.candidates(_CallListDB(), "construction", 25, DAY)

    assert [r["id"] for r in rows] == [1]
    shown = {_last_ten(r["phone"]) for r in rows}
    assert shown.isdisjoint(_SUPPRESSED_KEYS)


def test_the_frozen_list_drops_a_number_suppressed_after_assignment():
    rows = cl.list_for(_CallListDB(), 7, "construction", DAY)

    assert [r["lead_id"] for r in rows] == [1]
    assert {_last_ten(r["phone"]) for r in rows}.isdisjoint(_SUPPRESSED_KEYS)


def test_every_read_path_carries_the_suppression_match():
    db = _CallListDB()
    cl.candidates(db, "construction", 25, DAY)
    cl.list_for(db, 7, "construction", DAY)

    checked = 0
    for sql, _ in db.sql:
        s = " ".join(sql.split()).lower()
        if "from leads l where l.company" in s or "from call_assignments a join leads" in s:
            checked += 1
            assert "suppression_list" in s
            assert "s.value_type = 'phone'" in s
            assert "'+1' ||" in s
            assert "right(regexp_replace" in s
            assert "'any', 'phone', 'voice'" in s
    assert checked == 2


def test_the_callable_fragment_matches_normalised_and_last_ten_digits():
    sql = " ".join(cl._CALLABLE.split()).lower()

    assert "suppression_list" in sql
    # +1 E.164 form (what suppression.normalize_phone writes) and the last ten
    # digits (what an imported scrub can hold).
    assert "'+1' ||" in sql
    assert "right(regexp_replace(s.value, '[^0-9]', '', 'g'), 10)" in sql
    assert "right(regexp_replace(l.phone, '[^0-9]', '', 'g'), 10)" in sql
    # A voice call is blocked by an `any`, `phone` or `voice` entry; an
    # email-only entry must not hide a callable number.
    assert "'any', 'phone', 'voice'" in sql
    assert "s.active" in sql
