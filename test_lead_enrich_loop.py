"""The daily enrichment autopilot spends only what it is allowed to.

    python3 -m pytest test_lead_enrich_loop.py -v

What this pins, and why each one costs real money if it drifts:

  - the kill switch parses Railway's string "false" as OFF, not ON
  - the once-per-day marker is consulted before any provider is reached
  - Skip Sherpa is never called without a full ZIP (a billed wrong-door match)
  - the PDL step is gone from the waterfall (that account is not in use)
  - a gained contact stages a draft and nothing else -- never a send

No network and no database: providers and cursors are stubs.
"""

import os

import pytest

import data_enrichment
import lead_enrich_loop as loop


class _Cur:
    def __init__(self, rows=None, one=None):
        self.rows = rows or []
        self.one = one
        self.executed = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.one


class _Db:
    def __init__(self, cursor=None):
        self._cur = cursor or _Cur()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


# --- flags and caps ---------------------------------------------------------

def test_railway_string_false_does_not_disable(monkeypatch):
    """Railway stores an unset flag as 'false'. Bare truthiness would read that
    as ON and the autopilot would never start -- the exact bug test_flag_semantics
    exists for elsewhere."""
    monkeypatch.setenv("DISABLE_LEAD_ENRICH", "false")
    assert loop._env_flag("DISABLE_LEAD_ENRICH") is False
    monkeypatch.setenv("DISABLE_LEAD_ENRICH", "true")
    assert loop._env_flag("DISABLE_LEAD_ENRICH") is True
    monkeypatch.setenv("DISABLE_LEAD_ENRICH", "1")
    assert loop._env_flag("DISABLE_LEAD_ENRICH") is True


def test_daily_max_defaults_to_ten_and_survives_garbage(monkeypatch):
    monkeypatch.delenv("LEAD_ENRICH_MAX", raising=False)
    assert loop._daily_max() == 10
    monkeypatch.setenv("LEAD_ENRICH_MAX", "garbage")
    assert loop._daily_max() == 10
    monkeypatch.setenv("LEAD_ENRICH_MAX", "0")
    assert loop._daily_max() == 0


def test_run_once_stops_on_the_kill_switch_before_touching_anything(monkeypatch):
    monkeypatch.setenv("DISABLE_LEAD_ENRICH", "true")
    result = loop.run_once(db=_Db())
    assert result["examined"] == 0
    assert "DISABLE_LEAD_ENRICH" in result["skipped"]


def test_run_once_stops_when_cap_is_zero(monkeypatch):
    monkeypatch.delenv("DISABLE_LEAD_ENRICH", raising=False)
    monkeypatch.setenv("LEAD_ENRICH_MAX", "0")
    result = loop.run_once(db=_Db())
    assert result["examined"] == 0
    assert "0" in result["skipped"]


def test_already_ran_today_short_circuits_before_any_lookup():
    """The marker is read (and spending prevented) before the first provider
    call, so a redeploy mid-day cannot re-spend the cap."""
    today = loop.date.today().isoformat()
    db = _Db(cursor=_Cur(one={"value": today}))
    result = loop.run_once(db=db)
    assert result["skipped"] == "already ran today"
    assert result["examined"] == 0
    # Only the marker read ran -- no SELECT batch, no provider.
    statements = " ".join(sql for sql, _ in db._cur.executed)
    assert "FROM leads" not in statements


def test_force_bypasses_the_daily_marker(monkeypatch):
    """The owner asking by name (the Copilot's enrich_leads tool) must not be
    refused because the nightly pass already ran. force buys timing, not
    credits: the batch is still capped."""
    monkeypatch.delenv("DISABLE_LEAD_ENRICH", raising=False)
    today = loop.date.today().isoformat()
    db = _Db(cursor=_Cur(one={"value": today}, rows=[]))
    result = loop.run_once(db=db, force=True)
    assert result["skipped"] == ""
    statements = " ".join(sql for sql, _ in db._cur.executed)
    assert "FROM leads" in statements


def test_limit_overrides_the_daily_cap_and_clamps(monkeypatch):
    """A model-supplied 1000 must not become a 1000-credit bill; 0 must not
    mean 'skip the batch cursor entirely' either."""
    monkeypatch.delenv("DISABLE_LEAD_ENRICH", raising=False)
    db = _Db(cursor=_Cur(rows=[]))
    loop.run_once(db=db, force=True, limit=1000)
    selects = [p for sql, p in db._cur.executed if "FROM leads" in sql]
    assert selects and selects[0][-1] == 25

    db = _Db(cursor=_Cur(rows=[]))
    loop.run_once(db=db, force=True, limit=0)
    selects = [p for sql, p in db._cur.executed if "FROM leads" in sql]
    assert selects and selects[0][-1] == 1


def test_select_batch_never_queries_at_zero(monkeypatch):
    class _BoomDb(_Db):
        def cursor(self):
            raise AssertionError("limit 0 must not open a cursor")

    assert loop.select_batch(_BoomDb(), 0) == []


def test_select_batch_orders_newest_first_and_filters_unreachable():
    db = _Db(cursor=_Cur(rows=[]))
    loop.select_batch(db, 5)
    sql, params = db._cur.executed[0]
    assert "ORDER BY id DESC" in sql
    assert "@lead.local" in sql
    assert "LIMIT %s" in sql
    assert params[-1] == 5
    dead = ("cancelled", "canceled", "closed", "lost", "won", "archived")
    assert params[:-1] == dead


# --- the waterfall ----------------------------------------------------------

def test_waterfall_never_bills_sherpa_without_a_zip(monkeypatch):
    """One credit, possibly on a neighbour's door. Refusal is the only answer."""
    calls = []

    def _boom(*a, **kw):
        calls.append(a)
        return {"found": True}

    monkeypatch.setattr(data_enrichment.skip_sherpa_service, "configured", lambda: True)
    monkeypatch.setattr(data_enrichment.skip_sherpa_service, "trace", _boom)
    monkeypatch.setattr(data_enrichment.skiptrace_service, "lookup",
                        lambda *a, **kw: {})
    # The free ZIP resolver is down too, so no ZIP can be resolved.
    import zip_lookup
    monkeypatch.setattr(zip_lookup, "lookup_zip",
                        lambda *a, **kw: (_ for _ in ()).throw(zip_lookup.ZipLookupError("offline")))

    out = data_enrichment.lookup_address(_Db(), "123 Main Street, Norfolk, VA", None)
    assert not calls, "Skip Sherpa must not be reached without a ZIP"
    assert any(s.startswith("skip_sherpa: skipped") for s in out["steps"])


def test_waterfall_uses_the_resolved_zip_key(monkeypatch):
    """parse_address returns 'zipcode'. The old code read 'zip'/'zip_code' and
    always sent an empty ZIP -- this pins the real key end to end."""
    seen = {}

    def _trace(street, city, state, zipcode):
        seen["zipcode"] = zipcode
        return {"found": True, "emails": ["a@b.com"], "preferred_phone": "+17575551212"}

    monkeypatch.setattr(data_enrichment.skip_sherpa_service, "configured", lambda: True)
    monkeypatch.setattr(data_enrichment.skip_sherpa_service, "trace", _trace)
    monkeypatch.setattr(data_enrichment.skiptrace_service, "lookup",
                        lambda *a, **kw: {})

    out = data_enrichment.lookup_address(
        _Db(), "123 Main Street, Norfolk, VA 23510", None)
    assert seen["zipcode"] == "23510"
    assert out["email"] == "a@b.com"
    assert out["phone"] == "+17575551212"


def test_pdl_is_out_of_the_waterfall():
    assert "pdl" not in data_enrichment.providers()
    assert not hasattr(data_enrichment, "enrichment")


# --- drafting, not sending --------------------------------------------------

def test_first_touch_draft_carries_the_business_line_only():
    subject, body = loop.first_touch_draft("Bruce Oleary", "123 Main St")
    assert subject == loop.SUBJECT
    assert "Hi Bruce," in body
    assert "+1 (757) 908-7121" in body
    # The personal mobile belongs to lending material only.
    assert "252-665-5891" not in body
    assert "2526655891" not in body


def test_stage_is_an_update_that_cannot_overwrite_a_draft(handlers=None):
    """run_once only stages into an empty draft_reply. Pin the SQL guard."""
    import inspect
    src = inspect.getsource(loop.run_once)
    assert "draft_reply IS NULL OR TRIM(draft_reply) = ''" in src
    assert "fire_lead_draft" not in src, "the loop must never send"
