"""Old permits are spent, not work.

Cities keep a permit on the public record for years, so "listed" is not "live".
The owner's line is three months: after that either the job is done or the
homeowner has moved on. The rows are kept, but they must stay off the boards and
the importer must not readmit them on the next scan.
"""
import datetime as dt
import os

import pytest

import permit_stats


def test_recent_window_defaults_to_ninety_days(monkeypatch):
    monkeypatch.delenv("PERMIT_MAX_AGE_DAYS", raising=False)
    today = dt.date(2026, 11, 4)
    assert permit_stats.is_recent("2026-11-03", today) is True
    assert permit_stats.is_recent("2026-08-07", today) is True   # 89 days
    assert permit_stats.is_recent("2026-08-05", today) is False  # 91 days
    assert permit_stats.is_recent("2025-04-01", today) is False


def test_the_window_is_configurable(monkeypatch):
    monkeypatch.setenv("PERMIT_MAX_AGE_DAYS", "30")
    today = dt.date(2026, 11, 4)
    assert permit_stats.is_recent("2026-10-10", today) is True
    assert permit_stats.is_recent("2026-09-01", today) is False


def test_a_missing_or_unreadable_date_is_kept():
    """No date is not evidence that the work is spent."""
    assert permit_stats.is_recent("") is True
    assert permit_stats.is_recent(None) is True
    assert permit_stats.is_recent("not a date") is True


def test_the_sql_fragment_is_cast_safe(monkeypatch):
    """issue_date is a VARCHAR: a bare ::date would raise on one bad row and
    take the whole board down, so the cast is behind a regex CASE."""
    monkeypatch.setenv("PERMIT_MAX_AGE_DAYS", "90")
    sql = permit_stats.recent_sql()
    assert "CASE WHEN issue_date ~" in sql
    assert "INTERVAL '90 days'" in sql
    assert "issue_date::date >=" in sql


def test_the_permit_board_query_is_age_filtered():
    """The lane adds the predicate on every load; without it a 2-year-old
    permit renders as work to do."""
    import inspect
    import construction_main as main
    src = inspect.getsource(main.leads_page)
    assert "permit_stats.recent_sql()" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
