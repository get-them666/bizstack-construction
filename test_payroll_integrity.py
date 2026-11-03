"""Payroll must not say paid, or post cash, before PNC actually settles it.

No network, Stripe, or real database. Route boundaries and ledger posting are faked.
"""

import asyncio

import pytest
from fastapi import HTTPException

import construction_main
import ledger_bridge
import ledger_sync


class _Cursor:
    def __init__(self, db):
        self.db = db
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.db.queries.append((normalized, params))
        self.rowcount = self.db.update_rowcount if normalized.startswith("UPDATE") else 0
        if self.db.responses and (normalized.startswith("SELECT") or " RETURNING " in normalized):
            rows = self.db.responses.pop(0)
            self.rows = list(rows) if isinstance(rows, list) else [rows]
        else:
            self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Db:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.queries = []
        self.update_rowcount = 1

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def test_ach_refuses_to_skip_an_unbanked_worker(monkeypatch):
    db = _Db(responses=[{"id": 3, "status": "closed"}])
    monkeypatch.setattr(construction_main.direct_deposit, "ensure_schema", lambda conn: None)
    monkeypatch.setattr(
        construction_main.direct_deposit, "run_lines",
        lambda conn, run_id: [
            {"name": "Banked", "has_bank": True, "gross_cents": 10000},
            {"name": "No Bank", "has_bank": False, "gross_cents": 5000},
        ],
    )
    with pytest.raises(HTTPException) as exc:
        construction_main._payroll_ach_ready(db, 3)
    assert exc.value.status_code == 409
    assert "No Bank" in exc.value.detail


def test_ach_requires_a_finalized_run(monkeypatch):
    db = _Db(responses=[{"id": 4, "status": "open"}])
    monkeypatch.setattr(construction_main.direct_deposit, "ensure_schema", lambda conn: None)
    with pytest.raises(HTTPException) as exc:
        construction_main._payroll_ach_ready(db, 4)
    assert exc.value.status_code == 409
    assert "Finalize" in exc.value.detail


def test_copilot_finalizes_for_pnc_without_calling_stripe(monkeypatch):
    class _StripeBomb:
        def transfer_to_worker(self, *args, **kwargs):
            raise AssertionError("Stripe payroll must never be called")

    db = _Db(responses=[{
        "id": 8, "status": "open", "period_start": "2026-10-01", "period_end": "2026-10-14",
    }])
    accrued = []
    monkeypatch.setattr(construction_main.ledger_sync, "ensure_schema", lambda conn: None)
    monkeypatch.setattr(
        construction_main.ledger_sync, "sync_payroll_accrual",
        lambda conn, run_id, company: accrued.append(run_id),
    )
    handlers = construction_main.build_tool_handlers(db, _StripeBomb())
    result = handlers["run_payroll"]()
    assert result["ok"] is True and result["money_moved"] is False
    assert result["status"] == "closed" and result["run_id"] == 8
    assert accrued == [8]
    assert not any("sync_payroll_paid" in q[0] for q in db.queries)


def test_business_summary_does_not_count_payroll_as_customer_deposits():
    db = _Db(responses=[{"c": 10}, {"c": 4}, {"c": 2, "amt": 5000}])
    handlers = construction_main.build_tool_handlers(db, object())
    result = handlers["get_business_summary"]()
    assert result["open_leads"] == 4 and result["deposit_amount_cents"] == 5000
    assert "'won'" in db.queries[1][0]
    assert "lead_id IS NOT NULL" in db.queries[2][0]


def test_accounting_summary_uses_paid_payroll_and_customer_cash_only():
    db = _Db(responses=[
        {"amt": 10000, "n": 1}, {"amt": 3000, "n": 1},
        {"amt": 25000}, {"amt": 1250000, "n": 2},
    ])
    handlers = construction_main.build_tool_handlers(db, object())
    result = handlers["get_accounting_summary"]()
    assert result["total_collections_cents"] == 10000
    assert result["payroll_paid_total_cents"] == 25000
    assert result["open_job_feed_value_cents"] == 1250000
    assert "lead_id IS NOT NULL" in db.queries[0][0]
    assert "pr.status = 'paid'" in db.queries[2][0]
    assert "estimated_value * 100" in db.queries[3][0]


def test_payroll_summary_includes_finalized_unpaid_run_and_pnc_readiness(monkeypatch):
    import datetime as dt
    db = _Db(responses=[
        {"id": 5, "status": "closed", "period_start": dt.date(2026, 10, 1),
         "period_end": dt.date(2026, 10, 14)},
        [{"crew_id": 2, "crew_name": "ACH Worker", "hours": 40, "overtime_hours": 2,
          "gross_cents": 90000, "bank_status": "connected", "direct_deposit_ready": True}],
    ])
    monkeypatch.setattr(construction_main.direct_deposit, "ensure_schema", lambda conn: None)
    handlers = construction_main.build_tool_handlers(db, object())
    result = handlers["get_payroll_summary"]()
    assert result["open_run"]["status"] == "closed"
    assert result["lines"][0]["direct_deposit_ready"] is True
    assert "crew_bank_accounts" in db.queries[1][0]


def test_accrual_uses_stored_gross_for_overtime_and_salary(monkeypatch):
    db = _Db(responses=[
        {"t": "payroll_lines"}, {"t": "crew"},
        [{"crew_id": 1, "name": "Hourly", "pay_rate": 10, "hours": 45, "gross_cents": 95000},
         {"crew_id": 2, "name": "Salary", "pay_rate": 500, "hours": 0, "gross_cents": 50000}],
    ])
    lines = ledger_sync._payroll_accrual_lines(db, {"id": 7}, "construction")
    assert [l["gross_cents"] for l in lines] == [95000, 50000]
    assert lines[1]["hours"] == 0

    posted = []
    monkeypatch.setattr(
        ledger_bridge.ledger, "post",
        lambda conn, entry_lines, **kwargs: posted.append(entry_lines) or {"posted": True},
    )
    result = ledger_bridge.accrue_payroll(None, {"id": 7}, lines)
    assert result["posted"] is True
    entry = posted[0]
    assert sum(l.get("debit", 0) for l in entry) == 145000
    assert sum(l.get("credit", 0) for l in entry) == 145000
    assert any(l["account"] == "5020" and l["debit"] == 50000 for l in entry)


def test_accrual_allocates_stored_gross_to_the_runs_bids(monkeypatch):
    db = _Db(responses=[
        {"t": "payroll_lines"}, {"t": "crew"},
        [{"crew_id": 1, "name": "Hourly", "pay_rate": 10, "hours": 40, "gross_cents": 10001}],
        {"t": "timesheets"}, {"t": "projects"},
        [{"bid_id": 7, "hours": 30}, {"bid_id": 8, "hours": 10}],
    ])
    lines = ledger_sync._payroll_accrual_lines(db, {"id": 7}, "construction")
    assert sum(l["gross_cents"] for l in lines) == 10001
    assert {(l["bid_id"], l["gross_cents"]) for l in lines} == {(7, 7501), (8, 2500)}

    posted = []
    monkeypatch.setattr(
        ledger_bridge.ledger, "post",
        lambda conn, entry_lines, **kwargs: posted.append(entry_lines) or {"posted": True},
    )
    ledger_bridge.accrue_payroll(None, {"id": 7}, lines)
    assert {l["bid_id"] for l in posted[0] if l["account"] == "5020"} == {7, 8}


def test_run_creation_locks_approved_sheets_without_marking_them_paid(monkeypatch):
    import datetime as dt
    db = _Db(responses=[
        None,
        [{"crew_id": 1, "pay_type": "hourly", "pay_rate": 20}],
        {"id": 11},
        [{"work_date": dt.date(2026, 10, 1), "hours": 40}],
    ])
    monkeypatch.setattr(construction_main, "require_admin", lambda request: None)
    monkeypatch.setattr(construction_main.ledger_sync, "ensure_schema", lambda conn: None)
    monkeypatch.setattr(
        construction_main.ledger_sync, "sync_payroll_accrual",
        lambda conn, run_id, company: None,
    )
    response = asyncio.run(construction_main.payroll_run_create(
        object(), period_start="2026-10-01", period_end="2026-10-14", db=db,
    ))
    assert response.status_code == 303
    joined = "\n".join(q[0] for q in db.queries)
    assert "t.status = 'approved' AND t.payroll_run_id IS NULL" in joined
    assert "status IN ('submitted','approved')" not in joined
    assert "UPDATE timesheets SET payroll_run_id = %s" in joined
    assert "UPDATE timesheets SET status = 'paid'" not in joined


def test_paid_or_assigned_timesheet_cannot_be_approved_again(monkeypatch):
    db = _Db()
    db.update_rowcount = 0
    monkeypatch.setattr(construction_main, "require_admin", lambda request: None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(construction_main.payroll_sheet_approve(4, object(), db))
    assert exc.value.status_code == 409
    assert "status = 'submitted' AND payroll_run_id IS NULL" in db.queries[0][0]
