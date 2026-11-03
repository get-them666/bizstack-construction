"""Office-entered hours: add, correct, delete, and clearing a stuck punch.

A crew member who forgets to clock in, loses their phone, or has no phone at
all still has to be paid, and their hours still have to land on the job's cost.
Only the office can enter that time. Once a sheet is in a pay run it is locked,
because that run's gross, its accrual and the ACH file were built from it.

No network, no real database: the cursor is faked and require_admin is patched.
"""

import asyncio
from datetime import date, timedelta

from construction_main import (
    payroll_add_hours,
    payroll_clock_discard,
    payroll_sheet_delete,
    payroll_sheet_edit,
)


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
        # Both UPDATE and DELETE report how many rows they touched.
        writes = normalized.startswith("UPDATE") or normalized.startswith("DELETE")
        self.rowcount = self.db.write_rowcount if writes else 0
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
    def __init__(self, responses=None, write_rowcount=1):
        self.responses = list(responses or [])
        self.queries = []
        self.write_rowcount = write_rowcount

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def _admin(monkeypatch):
    monkeypatch.setattr("construction_main.require_admin", lambda request: None)


def test_owner_adds_hours_for_a_worker_with_no_phone(monkeypatch):
    _admin(monkeypatch)
    db = _Db(responses=[{"id": 5}])
    response = asyncio.run(payroll_add_hours(
        object(), crew_id=5, project_id=0, work_date="2026-11-02", hours="7.5",
        work_type="regular", notes="no phone", period_start="2026-11-01",
        period_end="2026-11-14", db=db,
    ))
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]

    insert = next(q for q in db.queries if q[0].startswith("INSERT INTO timesheets"))
    assert "'owner','approved'" in insert[0]
    crew_id, project_id, work_date, hours, work_type, notes = insert[1]
    assert (crew_id, project_id, hours, work_type) == (5, None, 7.5, "regular")
    assert work_date == date(2026, 11, 2)
    assert notes == "no phone"


def test_hours_can_be_attached_to_the_job_for_that_jobs_cost(monkeypatch):
    _admin(monkeypatch)
    db = _Db(responses=[{"id": 5}, {"id": 11}])
    response = asyncio.run(payroll_add_hours(
        object(), crew_id=5, project_id=11, work_date="2026-11-02", hours="8",
        work_type="regular", notes="", period_start="2026-11-01",
        period_end="2026-11-14", db=db,
    ))
    assert response.status_code == 303
    insert = next(q for q in db.queries if q[0].startswith("INSERT INTO timesheets"))
    assert insert[1][1] == 11


def test_future_date_and_bad_hours_are_refused_before_any_write(monkeypatch):
    _admin(monkeypatch)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    db = _Db()
    response = asyncio.run(payroll_add_hours(
        object(), crew_id=5, project_id=0, work_date=tomorrow, hours="8",
        work_type="regular", notes="", period_start="", period_end="", db=db,
    ))
    assert response.status_code == 303
    assert "future" in response.headers["location"]
    assert db.queries == []

    response = asyncio.run(payroll_add_hours(
        object(), crew_id=5, project_id=0, work_date="2026-11-02", hours="25",
        work_type="regular", notes="", period_start="", period_end="", db=db,
    ))
    assert response.status_code == 303
    assert "24" in response.headers["location"]
    assert db.queries == []


def test_a_sheet_already_in_a_pay_run_cannot_be_edited(monkeypatch):
    _admin(monkeypatch)
    db = _Db(responses=[{"id": 5}], write_rowcount=0)
    response = asyncio.run(payroll_sheet_edit(
        4, object(), crew_id=5, project_id=0, work_date="2026-11-02", hours="9",
        work_type="regular", notes="", period_start="2026-11-01",
        period_end="2026-11-14", db=db,
    ))
    assert response.status_code == 303
    assert "pay+run" in response.headers["location"]

    update = next(q for q in db.queries if q[0].startswith("UPDATE timesheets"))
    assert "payroll_run_id IS NULL" in update[0]


def test_a_sheet_already_in_a_pay_run_cannot_be_deleted(monkeypatch):
    _admin(monkeypatch)
    db = _Db(write_rowcount=0)
    response = asyncio.run(payroll_sheet_delete(
        4, object(), period_start="2026-11-01", period_end="2026-11-14", db=db,
    ))
    assert response.status_code == 303
    assert "pay+run" in response.headers["location"]

    delete = next(q for q in db.queries if q[0].startswith("DELETE FROM timesheets"))
    assert "payroll_run_id IS NULL" in delete[0]
    assert "status <> 'paid'" in delete[0]


def test_clearing_a_stuck_punch_creates_no_hours(monkeypatch):
    _admin(monkeypatch)
    db = _Db()
    response = asyncio.run(payroll_clock_discard(
        9, object(), period_start="2026-11-01", period_end="2026-11-14", db=db,
    ))
    assert response.status_code == 303
    delete = db.queries[0]
    assert delete[0].startswith("DELETE FROM clock_events")
    assert "punch_out_at IS NULL" in delete[0]
    assert not any(q[0].startswith("INSERT INTO timesheets") for q in db.queries)


def test_payroll_page_exposes_the_office_entry_controls():
    from pathlib import Path
    html = Path("templates/construction/payroll.html").read_text(encoding="utf-8")
    for action in (
        "/api/payroll/timesheets/add",
        "/api/payroll/timesheets/{{ t.id }}/edit",
        "/api/payroll/timesheets/{{ t.id }}/delete",
        "/api/payroll/clock/{{ p.id }}/discard",
    ):
        assert action in html, action
    assert "Open clock-ins" in html
    assert "What each job made" in html
    # Payroll file links must use the singular route the app actually serves.
    assert "/api/payroll/run/{{ run_id }}/nacha" in html
    assert "/api/payroll/run/{{ run_id }}/csv" in html
    assert "/api/payroll/run/{{ run_id }}/finalize" in html
    assert "/api/payroll/run/{{ run_id }}/ach-sent" in html
    assert "/api/payroll/runs/" not in html


def test_the_bid_number_is_the_job_number_everywhere_the_crew_look():
    from pathlib import Path
    src = Path("construction_main.py").read_text(encoding="utf-8")
    app = Path("templates/construction/app.html").read_text(encoding="utf-8")
    payroll = Path("templates/construction/payroll.html").read_text(encoding="utf-8")
    # The crew app payload carries the bid's id and the printed label.
    assert "p.bid_id" in src
    assert '"job_number"' in src
    assert "Bid #" in src
    # The number is rendered where crew choose a job and where hours land.
    assert "j.job_number" in app
    assert "Bid #{{ p.bid_id }}" in payroll
    assert "Bid #{{ t.project_bid_id }}" in payroll
