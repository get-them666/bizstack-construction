"""Uploading paperwork makes the job card, or finds the one that exists.

Before this, documents could sit on a bid with no job to work them against, so
hours could not be logged until somebody remembered to press *Start project*.
The card is keyed on the bid id, which is also the job number, so the paperwork,
the crew's clock-ins and the profit report all point at the same job.

No network or database: the cursor is scripted.
"""

from pathlib import Path

import bid_costing


class _Cursor:
    def __init__(self, script):
        self.script = list(script)
        self.executed = []
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if not self.script:
            self.rows = []
            return
        row = self.script.pop(0)
        if isinstance(row, dict) and row.get("__none__"):
            self.rows = []
        elif isinstance(row, list):
            self.rows = row
        else:
            self.rows = [row]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, script):
        self.cur = _Cursor(script)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def _setup(monkeypatch):
    monkeypatch.setattr(bid_costing, "ensure_accounting_schema", lambda conn: None)
    monkeypatch.setattr(bid_costing, "_table_exists", lambda conn, name: True)


def _bid(**extra):
    bid = {"id": 18350, "name": "520 King George", "location": "520 King George Rd",
           "bid_amount": 2591, "trade": "renovation", "status": "drafting",
           "start_date": None}
    bid.update(extra)
    return bid


def test_an_upload_creates_the_job_card_when_there_is_none(monkeypatch):
    _setup(monkeypatch)
    conn = _Conn([_bid(), {"__none__": True}, {"id": 42, "name": "520 King George"}])
    result = bid_costing.ensure_job_for_bid(conn, 18350)
    assert result == {"id": 42, "name": "520 King George", "created": True}
    insert = [q for q in conn.cur.executed if q[0].startswith("INSERT INTO projects")][0]
    assert "bid_id" in insert[0]
    assert insert[1][0] == 18350
    # Not a won job yet, so the card is planned, not an active worksite.
    assert insert[1][4] == "planned"


def test_a_won_bid_creates_an_active_card(monkeypatch):
    _setup(monkeypatch)
    conn = _Conn([_bid(status="won"), {"__none__": True}, {"id": 42, "name": "Job"}])
    bid_costing.ensure_job_for_bid(conn, 18350)
    insert = [q for q in conn.cur.executed if q[0].startswith("INSERT INTO projects")][0]
    assert insert[1][4] == "active"


def test_an_existing_card_is_found_and_only_blank_fields_are_filled(monkeypatch):
    _setup(monkeypatch)
    conn = _Conn([_bid(), {"id": 9, "name": "My name", "status": "planned"},
                  {"id": 9, "name": "My name"}])
    result = bid_costing.ensure_job_for_bid(conn, 18350)
    assert result == {"id": 9, "name": "My name", "created": False}
    assert not any(q[0].startswith("INSERT INTO projects") for q in conn.cur.executed)
    update = [q for q in conn.cur.executed if q[0].startswith("UPDATE projects")][0]
    assert "COALESCE(NULLIF(name, '')" in update[0]
    assert "COALESCE(NULLIF(address, '')" in update[0]
    # A finished job must never be reopened by an upload.
    assert "status IN ('completed', 'done', 'closed', 'cancelled')" in update[0]


def test_winning_promotes_but_does_not_reopen(monkeypatch):
    _setup(monkeypatch)
    conn = _Conn([_bid(status="won"), {"id": 9, "name": "Job", "status": "planned"},
                  {"id": 9, "name": "Job"}])
    bid_costing.ensure_job_for_bid(conn, 18350)
    update = [q for q in conn.cur.executed if q[0].startswith("UPDATE projects")][0]
    assert update[1][-3] == "active"
    assert "WHEN %s = 'active' THEN 'active'" in update[0]


def test_a_lost_bid_does_not_get_a_new_job_card(monkeypatch):
    _setup(monkeypatch)
    conn = _Conn([_bid(status="lost"), {"__none__": True}])
    assert bid_costing.ensure_job_for_bid(conn, 18350) is None
    assert not any(q[0].startswith("INSERT INTO projects") for q in conn.cur.executed)


def test_the_upload_route_asks_for_the_job_card():
    src = Path("construction_main.py").read_text(encoding="utf-8")
    assert "ensure_job_for_bid(db, bid_id" in src
    # A failure to make the card must not swallow the upload.
    assert "job card unavailable" in src
