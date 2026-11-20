"""Scheduled AI calls must survive a restart and stay inside calling hours."""

import datetime as dt
from zoneinfo import ZoneInfo

import scheduled_calls


class _FakeCursor:
    def __init__(self):
        self.rows = []

    def execute(self, *args, **kwargs):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def commit(self):
        return None


def test_outside_calling_hours_nothing_is_dialed(monkeypatch):
    monkeypatch.setattr(scheduled_calls, "ensure_schema", lambda conn: None)

    class _Fixed(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 11, 20, 23, 30, tzinfo=ZoneInfo("America/New_York"))

    monkeypatch.setattr(scheduled_calls, "dt", type("dtmod", (), {"datetime": _Fixed,
                                                                 "timezone": dt.timezone,
                                                                 "timedelta": dt.timedelta}))
    result = scheduled_calls.run_due(_FakeConn())
    assert result["due"] == 0
    assert result["dialed"] == 0


def test_calling_hours_are_the_business_ones():
    assert scheduled_calls.CALL_START_HOUR == 8
    assert scheduled_calls.CALL_END_HOUR == 19
    assert scheduled_calls.CALL_TZ.key == "America/New_York"
