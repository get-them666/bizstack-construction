"""Inbound follow-up schedule, booking tokens, opt-out detection.

    venv/bin/python -m pytest test_followups.py -v

No database for the unit tests; the sweep is exercised against a stub.
"""
import datetime as dt

import pytest

import followups as fu


# --- opt-out -----------------------------------------------------------------------

@pytest.mark.parametrize("text", ["STOP", "stop", "Stop.", "Unsubscribe", "please remove me",
                                  "Take me off your list", "opt out", "Not interested", "STOP ALL"])
def test_whole_message_opt_outs_are_recognised(text):
    assert fu.is_opt_out(text)


@pytest.mark.parametrize("text", ["Can you stop by Tuesday?", "Please don't stop the work, it looks great",
                                  "Yes, let's do it", "", "I'd like a quote for the roof"])
def test_ordinary_replies_are_not_opt_outs(text):
    assert not fu.is_opt_out(text)


def test_email_reply_tail_is_ignored():
    assert fu.is_opt_out("STOP\n\nOn Tue, Oct 28, Shaun wrote:\n> Hi there, a permit was filed...")


# --- booking token --------------------------------------------------------------------

def test_booking_token_round_trips_and_rejects_tampering(monkeypatch):
    monkeypatch.setenv("APP_SECRET", "test-secret")
    tok = fu.booking_token(1462)
    assert fu.verify_booking_token(tok) == 1462
    assert fu.verify_booking_token(tok[:-2] + "zz") is None
    assert fu.verify_booking_token("garbage") is None
    monkeypatch.setenv("APP_SECRET", "another-secret")
    assert fu.verify_booking_token(tok) is None, "a token minted under one secret must not verify under another"


def test_booking_url_prefers_an_external_scheduler(monkeypatch):
    monkeypatch.delenv("BOOKING_URL", raising=False)
    monkeypatch.setenv("COMPANY_DOMAIN", "construction.bizstackperks.com")
    assert fu.booking_url(5).startswith("https://construction.bizstackperks.com/book/")
    monkeypatch.setenv("BOOKING_URL", "https://cal.example.com/shaun")
    assert fu.booking_url(5) == "https://cal.example.com/shaun"


# --- slots ------------------------------------------------------------------------------

def test_slots_are_weekday_windows_starting_tomorrow():
    now = dt.datetime(2026, 10, 30, 15, 0, tzinfo=fu.LOCAL_TZ)   # a Friday
    s = fu.slots(days_ahead=3, now=now)
    dates = sorted({x["date"] for x in s})
    assert dates == ["2026-11-02", "2026-11-03", "2026-11-04"], "skips the weekend"
    assert {x["window"] for x in s} == {"morning", "midday", "afternoon"}
    assert fu.parse_slot(s[0]["iso"]) is not None


def test_only_offered_windows_parse():
    assert fu.parse_slot("2099-06-02T08:00:00-04:00") is not None     # a Tuesday, morning
    assert fu.parse_slot("2099-06-02T09:30:00-04:00") is None          # not a window start
    assert fu.parse_slot("2099-06-06T08:00:00-04:00") is None          # Saturday
    assert fu.parse_slot("2001-06-05T08:00:00-04:00") is None          # the past
    assert fu.parse_slot("not a date") is None


# --- messages -------------------------------------------------------------------------------

def test_every_message_carries_the_link_and_an_opt_out():
    lead = {"id": 7, "name": "Barbara Mastic", "project_type": "Kitchen"}
    for step in (0, 1, 2):
        subject, body = fu.followup_email(lead, step, "https://x/book/abc")
        assert "https://x/book/abc" in body and "STOP" in body and "Barbara" in body
        assert "BuildStack Construction" in body
        sms = fu.followup_sms(lead, step, "https://x/book/abc")
        assert "https://x/book/abc" in sms and "STOP" in sms and len(sms) <= 320


def test_schedule_is_configurable_and_sane(monkeypatch):
    monkeypatch.delenv("INBOUND_FOLLOWUP_DAYS", raising=False)
    assert fu.schedule_days() == [1, 3, 7]
    monkeypatch.setenv("INBOUND_FOLLOWUP_DAYS", "2, 5, x, -1, 5")
    assert fu.schedule_days() == [2, 5]


# --- the sweep, against a stub -----------------------------------------------------------------

class _Cur:
    def __init__(self, rows): self.rows = rows; self.executed = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.executed.append((sql, params))
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, rows): self.rows = rows; self.cur = _Cur(rows)
    def cursor(self): return self.cur
    def commit(self): pass
    def rollback(self): pass


def test_due_followups_respects_the_schedule(monkeypatch):
    monkeypatch.setattr(fu, "ensure_schema", lambda conn, force=False: None)
    now = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc)
    rows = [
        {"id": 1, "name": "A", "email": "a@x.com", "phone": "", "project_type": "", "source": "website", "status": "new",
         "created_at": now - dt.timedelta(days=1, hours=1), "last_step": -1},   # day-1 touch due
        {"id": 2, "name": "B", "email": "b@x.com", "phone": "", "project_type": "", "source": "website", "status": "new",
         "created_at": now - dt.timedelta(hours=5), "last_step": -1},            # too new
        {"id": 3, "name": "C", "email": "c@x.com", "phone": "", "project_type": "", "source": "website", "status": "contacted",
         "created_at": now - dt.timedelta(days=4), "last_step": 0},              # day-3 touch due
        {"id": 4, "name": "D", "email": "d@x.com", "phone": "", "project_type": "", "source": "website", "status": "contacted",
         "created_at": now - dt.timedelta(days=30), "last_step": 2},             # sequence finished
    ]
    due = fu.due_followups(_Conn(rows), now=now)
    assert [(d["id"], d["step"]) for d in due] == [(1, 0), (3, 1)]


def test_review_mode_and_kill_switch_send_nothing(monkeypatch):
    monkeypatch.setattr(fu, "due_followups", lambda conn, now=None: [{"id": 1, "step": 0, "email": "a@x.com", "name": "A"}])
    calls = []
    res = fu.run_once(_Conn([]), send_email=lambda *a: calls.append(a) or True, send_sms=None,
                      channel_allowed=lambda ch: (True, ""), review_mode=True)
    assert res["sent"] == 0 and not calls and "review mode" in res["skipped"][0]
    monkeypatch.setenv("DISABLE_FOLLOWUPS", "1")
    res = fu.run_once(_Conn([]), send_email=lambda *a: calls.append(a) or True, send_sms=None,
                      channel_allowed=lambda ch: (True, ""), review_mode=False)
    assert res["sent"] == 0 and not calls


def test_a_touch_goes_out_once_and_honours_the_daily_cap(monkeypatch):
    monkeypatch.setattr(fu, "ensure_schema", lambda conn, force=False: None)
    monkeypatch.delenv("DISABLE_FOLLOWUPS", raising=False)
    conn = _Conn([])
    lead = {"id": 9, "name": "Eve", "email": "eve@x.com", "phone": "(757) 555-0199", "project_type": "Deck"}
    sent = []
    res = fu.send_followup(conn, lead, 0, send_email=lambda to, s, b: sent.append(("email", to)) or True,
                           send_sms=lambda to, b: sent.append(("sms", to)) or True,
                           channel_allowed=lambda ch: (ch == "email", "" if ch == "email" else "daily text cap reached"))
    assert res["email"] is True and res["sms"] is False
    assert sent == [("email", "eve@x.com")]
    assert any("lead_followups" in sql for sql, _ in conn.cur.executed)
    assert any("comms_logs" in sql for sql, _ in conn.cur.executed)
