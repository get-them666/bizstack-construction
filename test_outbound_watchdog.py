"""Outbound must not be allowed to go silent without telling the owner.

The 2026-11-23 to 2026-11-26 outage was invisible: no send errored, because no
send was attempted. These tests pin the watchdog that turns "nothing has gone
out for 48 hours" into the same alert the transport monitor already sends.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

import lead_enrich_loop  # noqa: E402
import outbound_watchdog as watchdog  # noqa: E402

NOW = datetime(2026, 11, 26, 19, 0, tzinfo=timezone.utc)


def test_recent_email_keeps_watchdog_quiet():
    result = watchdog.evaluate(
        last_email=NOW - timedelta(hours=5),
        last_call=NOW - timedelta(hours=80),
        now=NOW, hours=48)
    assert result["ok"] is True


def test_recent_call_keeps_watchdog_quiet():
    result = watchdog.evaluate(
        last_email=NOW - timedelta(hours=80),
        last_call=NOW - timedelta(hours=3),
        now=NOW, hours=48)
    assert result["ok"] is True


def test_both_stale_alerts_with_the_gap_in_the_detail():
    result = watchdog.evaluate(
        last_email=NOW - timedelta(hours=56),
        last_call=NOW - timedelta(hours=78),
        now=NOW, hours=48)
    assert result["ok"] is False
    assert "56h" in result["detail"]
    assert "last call" in result["detail"]


def test_never_sent_anything_alerts():
    result = watchdog.evaluate(now=NOW, hours=48)
    assert result["ok"] is False
    assert "no outbound email or call" in result["detail"]


def test_campaign_zero_streak_is_named_in_the_alert():
    runs = [{"run_date": NOW.date(), "considered": 4, "dialed": 0} for _ in range(3)]
    result = watchdog.evaluate(
        last_email=NOW - timedelta(hours=56),
        last_call=NOW - timedelta(hours=78),
        now=NOW, hours=48, runs=runs)
    assert result["ok"] is False
    assert result["zero_streak"] == 3
    assert "dialled 0" in result["detail"]


def test_a_recent_dial_breaks_the_zero_streak():
    runs = [
        {"run_date": NOW.date(), "considered": 4, "dialed": 0},
        {"run_date": NOW.date(), "considered": 4, "dialed": 2},
        {"run_date": NOW.date(), "considered": 4, "dialed": 0},
    ]
    result = watchdog.evaluate(
        last_email=NOW - timedelta(hours=56),
        last_call=NOW - timedelta(hours=78),
        now=NOW, hours=48, runs=runs)
    assert result["zero_streak"] == 1


def test_silence_hours_zero_disables_the_watchdog():
    result = watchdog.evaluate(last_email=None, now=NOW, hours=0)
    assert result["ok"] is True
    assert result["disabled"] is True


class _Cursor:
    def __init__(self):
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        if "comms_logs" in self.sql:
            return [
                {"channel": "email", "last_at": NOW - timedelta(hours=56)},
                {"channel": "voice", "last_at": NOW - timedelta(hours=78)},
            ]
        if "outbound_campaign_runs" in self.sql:
            return [{"run_date": NOW.date(), "considered": 4, "dialed": 0}]
        return []


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _Cursor()

    def rollback(self):
        pass


def test_check_and_record_writes_the_outbound_state(monkeypatch):
    recorded = []
    monkeypatch.setattr(watchdog, "_connect", lambda: _Connection())
    monkeypatch.setattr(
        watchdog.transport_health, "record",
        lambda channel, ok, detail="": recorded.append((channel, ok, detail)))

    result = watchdog.check_and_record()

    assert result["ok"] is False
    assert recorded and recorded[0][0] == "outbound"
    assert recorded[0][1] is False
    assert "no outbound email or call" in recorded[0][2]


def test_enrichment_quota_is_recorded_as_a_transport_failure(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        lead_enrich_loop.transport_health, "record",
        lambda channel, ok, detail="": recorded.append((channel, ok, detail)))

    lead_enrich_loop.record_health(
        True, False, "Tracerfy is out of lookup credits (402)")

    # Two records so the shared two-consecutive-failures alert fires on the
    # same daily pass rather than a day later.
    assert recorded == [
        ("enrichment", False, "Tracerfy is out of lookup credits (402)"),
        ("enrichment", False, "Tracerfy is out of lookup credits (402)"),
    ]


def test_enrichment_recovery_is_recorded(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        lead_enrich_loop.transport_health, "record",
        lambda channel, ok, detail="": recorded.append((channel, ok, detail)))

    lead_enrich_loop.record_health(False, True)

    assert recorded == [("enrichment", True, "")]
