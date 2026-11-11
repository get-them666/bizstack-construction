"""The one-button owner backfill: selection and single-flight behaviour."""
import owner_backfill_job as job


class _Cur:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    def fetchall(self):
        return self.rows


def test_candidates_keep_only_placeholder_names():
    cur = _Cur([
        {"id": 1, "name": "Virginia Beach", "address": "1 A St"},
        {"id": 2, "name": "Jones, Brandon", "address": "2 B St"},
        {"id": 3, "name": "", "address": "3 C St"},
        {"id": 4, "name": "Permit · 415 Carlisle Way", "address": "4 D St"},
        {"id": 5, "name": "Smith, Henry Jr & Lynette", "address": "5 E St"},
    ])
    got = job._candidates(cur, "construction")
    assert [r["id"] for r in got] == [1, 3, 4]


def test_candidates_exclude_dead_statuses_in_sql():
    cur = _Cur([])
    job._candidates(cur, "construction")
    assert "NOT IN" in cur.sql
    assert set(job.DEAD) == {"won", "lost", "closed", "cancelled", "canceled", "archived"}
    # company is bound, never interpolated
    assert cur.params[0] == "construction"


def test_a_second_click_does_not_start_a_duplicate_pass(monkeypatch):
    monkeypatch.setattr(job, "_run", lambda *a: None)
    job._STATE.update(running=True, total=40, examined=12)
    try:
        out = job.start("construction")
        assert out["started"] is False
        assert out["reason"] == "already running"
        assert out["examined"] == 12
    finally:
        job._STATE.update(running=False, total=0, examined=0)


def test_status_is_a_copy(monkeypatch):
    job._STATE.update(running=False, total=7)
    snap = job.status()
    snap["total"] = 999
    assert job.status()["total"] == 7
