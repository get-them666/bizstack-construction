"""Bid actuals must exclude rejected time and never double-enter labor."""

import bid_costing


class _Cursor:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.db.queries.append((" ".join(sql.split()), params))
        rows = self.db.responses.pop(0) if self.db.responses else []
        self.rows = list(rows) if isinstance(rows, list) else [rows]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Db:
    def __init__(self, responses):
        self.responses = list(responses)
        self.queries = []

    def cursor(self):
        return _Cursor(self)


def test_labor_actual_excludes_rejected_time_and_prices_explicit_overtime():
    db = _Db([
        {"t": "timesheets"}, {"t": "crew"}, {"t": "projects"},
        {"n": 2, "hours": 42, "hourly_hours": 42, "labor_cost": 900,
         "salaried_hours": 0},
    ])
    result = bid_costing.labor_actual(db, 7)
    assert result["labor_cost"] == 900
    sql = db.queries[-1][0]
    assert "t.status <> 'rejected'" in sql
    assert "work_type" in sql and "1.5" in sql


def test_board_numbers_do_not_double_count_manual_labor():
    db = _Db([
        {"t": "timesheets"}, {"t": "crew"}, {"t": "projects"},
        [{"bid_id": 7, "labor": 900}],
        [{"bid_id": 7, "actual": 50, "estimated": 1000}],
    ])
    result = bid_costing.board_numbers(db)
    assert result[7]["labor"] == 900
    assert result[7]["other"] == 50
    assert result[7]["actual"] == 950
    cost_sql = db.queries[-1][0]
    assert "SUM(actual) FILTER (WHERE category <> 'labor')" in cost_sql
    labor_sql = db.queries[-2][0]
    assert "t.status <> 'rejected'" in labor_sql
