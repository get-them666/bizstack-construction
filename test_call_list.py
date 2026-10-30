"""Call list: the token boundary, the outcome whitelist, and the frozen list.

    venv/bin/python -m pytest test_call_list.py -v

No database. The SQL is exercised against a cursor that answers by statement
shape, so what is pinned here is the behaviour a wrong answer would silently
break: a link that reads another crew member's list, an outcome that is not a
real call result, and a list that changes under the person working it.
"""
import datetime as dt

import pytest

import call_list as cl


DAY = dt.date(2026, 10, 31)


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append((sql, params))
        s = " ".join(sql.split()).lower()
        self.rowcount = 0
        if s.startswith("create table"):
            self._rows = []
        elif "insert into call_assignments" in s:
            company, crew_id, lead_id, day, pos = params
            key = (crew_id, lead_id, str(day))
            if key in self.conn.assigned:
                self.rowcount = 0
            else:
                self.conn.assigned.add(key)
                self.conn.rows.append({"id": len(self.conn.rows) + 1, "assignment_id": len(self.conn.rows) + 1,
                                       "state": "assigned", "note": None, "position": pos,
                                       "lead_id": lead_id, "name": f"Lead {lead_id}",
                                       "phone": "(757) 555-0100", "address": "1 Main St, Norfolk, VA",
                                       "email": "", "source": "permit_finder", "status": "new",
                                       "project_type": ""})
                self.rowcount = 1
        elif "from call_assignments a join leads" in s:
            self._rows = [r for r in self.conn.rows]
        elif "update call_assignments set state" in s:
            state, note, aid = params
            hit = [r for r in self.conn.rows if r["assignment_id"] == aid]
            if hit:
                hit[0]["state"], hit[0]["note"] = state, note
                self._rows = [(hit[0]["lead_id"],)]
                self.rowcount = 1
            else:
                self._rows = []
        elif "from leads l where l.company" in s:
            self._rows = [{"id": 501, "name": "Ada Lovelace", "phone": "757-555-0101",
                           "address": "1 Main St", "email": "", "source": "permit_finder",
                           "status": "new", "project_type": "", "created_at": None,
                           "last_reply": None, "touches": 0, "last_touch": None, "reason": "new"}]
        elif "from crew where id" in s:
            self._rows = [{"id": 7, "name": "Justin Ryan", "phone": "(540) 748-2748"}]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self):
        self.sql = []
        self.rows = []
        self.assigned = set()
        self.commits = 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_a_token_is_scoped_to_one_crew_member_and_one_day():
    tok = cl.call_token(7, DAY)
    assert cl.verify_call_token(tok) == (7, DAY)
    # The same link must not open another person's list.
    assert cl.verify_call_token(tok) != (8, DAY)
    assert cl.verify_call_token(cl.call_token(8, DAY)) == (8, DAY)


def test_a_tampered_or_truncated_token_is_refused():
    tok = cl.call_token(7, DAY)
    assert cl.verify_call_token(tok[:-1] + ("a" if tok[-1] != "a" else "b")) is None
    assert cl.verify_call_token("not-a-token") is None
    assert cl.verify_call_token("") is None


def test_yesterdays_link_does_not_open_todays_list():
    yesterday = cl.call_token(7, dt.date(2026, 10, 30))
    assert cl.verify_call_token(yesterday) == (7, dt.date(2026, 10, 30))
    assert cl.verify_call_token(yesterday) != (7, DAY)


def test_only_real_outcomes_are_accepted():
    conn = _Conn()
    assert cl.mark(conn, 1, "sounded_like_a_nice_man")["ok"] is False
    assert "unknown outcome" in cl.mark(conn, 1, "nonsense")["error"]


def test_marking_a_call_that_is_not_on_the_list_is_refused():
    conn = _Conn()
    assert cl.mark(conn, 999, "called")["ok"] is False


def test_assign_freezes_a_list_and_a_second_run_does_not_duplicate_it():
    conn = _Conn()
    first = cl.assign(conn, 7, "construction", limit=5, for_date=DAY)
    assert first["added"] >= 1
    second = cl.assign(conn, 7, "construction", limit=5, for_date=DAY)
    assert second["added"] == 0, "re-running must not reshuffle or duplicate the list"
    assert second["total"] == first["total"]


def test_the_list_hands_the_phone_as_a_dialable_number():
    conn = _Conn()
    cl.assign(conn, 7, "construction", limit=5, for_date=DAY)
    rows = cl.list_for(conn, 7, "construction", DAY)
    assert rows and rows[0]["dial"] == "7575550100"


def test_progress_counts_what_is_left_not_what_was_asked():
    conn = _Conn()
    cl.assign(conn, 7, "construction", limit=5, for_date=DAY)
    rows = cl.list_for(conn, 7, "construction", DAY)
    cl.mark(conn, rows[0]["assignment_id"], "booked", "Tuesday 9am")
    p = cl.progress(conn, 7, "construction", DAY)
    assert p["total"] == len(rows) and p["done"] == 1 and p["booked"] == 1


def test_nobody_is_auto_texted_until_the_owner_names_them(monkeypatch):
    monkeypatch.delenv("CALL_LIST_CREW_IDS", raising=False)
    assert cl.crew_ids_from_env() == []
    assert cl.auto_plan(_Conn(), for_date=DAY)["skipped"] == "CALL_LIST_CREW_IDS is not set"
    monkeypatch.setenv("CALL_LIST_CREW_IDS", "7, 9;11")
    assert cl.crew_ids_from_env() == [7, 9, 11]


def test_the_text_carries_the_link_and_the_count():
    body = cl.sms_text("Justin Ryan", 7, 22, DAY)
    assert body.startswith("Justin,")
    assert "22 people" in body
    assert "/calllist/" in body and cl.call_token(7, DAY) in body


@pytest.mark.parametrize("state", sorted(cl.OUTCOMES))
def test_every_advertised_outcome_is_writable(state):
    conn = _Conn()
    cl.assign(conn, 7, "construction", limit=5, for_date=DAY)
    rows = cl.list_for(conn, 7, "construction", DAY)
    assert cl.mark(conn, rows[0]["assignment_id"], state)["ok"] is True
