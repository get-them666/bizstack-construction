"""The incremental permit window must survive a feed gap.

The scan only looks back PERMIT_INCREMENTAL_DAYS after the first boot-time
backfill. With a 2-day window, a weekend, a holiday or a stale feed longer than
two days permanently skipped every permit issued in the gap. Thanksgiving 2026
left Nov 21-26 with no new rows, and the scanner could never look back far
enough to pick them up.

Seven days is the small, free overlap: the municipal feeds are free, and the
paid city is capped per geo, so the extra window does not increase Shovels
spend.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

import construction_main as cm  # noqa: E402


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def rollback(self):
        pass


def _scan_days(monkeypatch, increment_env):
    seen = []
    monkeypatch.setattr(cm, "_permit_backfilled", True)
    monkeypatch.setenv("PERMIT_CITIES", "Virginia Beach")
    monkeypatch.delenv("PERMIT_INCREMENTAL_DAYS", raising=False)
    if increment_env is not None:
        monkeypatch.setenv("PERMIT_INCREMENTAL_DAYS", str(increment_env))
    monkeypatch.setattr(cm.permit_service, "fetch_permits",
                        lambda city, state, days, limit: seen.append(days) or [])
    monkeypatch.setattr(cm.permit_service, "backfill_classification", lambda conn: None)
    monkeypatch.setattr(cm.psycopg, "connect", lambda *a, **k: _Connection())
    monkeypatch.setattr(cm, "ingest_permits", lambda conn, permits: (0, 0))

    cm.run_permit_import()
    assert seen, "the scan never called fetch_permits"
    return seen[0]


def test_incremental_window_is_a_full_week(monkeypatch):
    assert _scan_days(monkeypatch, None) == 7


def test_incremental_window_can_be_overridden(monkeypatch):
    assert _scan_days(monkeypatch, 10) == 10
