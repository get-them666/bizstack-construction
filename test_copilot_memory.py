"""Tests for Copilot conversation memory, task retention, and the math sandbox.

No network and no database: the connection is faked with an in-memory store.
"""

import unittest

import copilot_memory as cm
import copilot_tasks as ct
from copilot_ops import calculate


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.db.queries.append((s, params))
        if s.startswith("CREATE") or s.startswith("ALTER") or s.startswith("DELETE FROM copilot_messages WHERE id IN"):
            self.rowcount = 0
            return
        if s.startswith("INSERT INTO copilot_messages"):
            kind, key, role, content, _exp = params
            self.db.messages.append({"kind": kind, "key": key, "role": role, "content": content})
        elif s.startswith("DELETE FROM copilot_messages WHERE kind"):
            kind, key = params
            before = len(self.db.messages)
            self.db.messages = [
                m for m in self.db.messages if not (m["kind"] == kind and m["key"] == key)
            ]
            self.rowcount = before - len(self.db.messages)
        elif s.startswith("SELECT role, content FROM copilot_messages"):
            kind, key, limit = params
            rows = [m for m in self.db.messages if m["kind"] == kind and m["key"] == key][-limit:]
            self._rows = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
        elif s.startswith("INSERT INTO copilot_tasks"):
            owner, title, detail, category, etype, eid, due = params
            self.db.tasks.append({
                "id": len(self.db.tasks) + 1,
                "owner_email": owner, "title": title, "detail": detail,
                "category": category, "entity_type": etype, "entity_id": eid,
                "due_at": due, "status": "open", "result": "",
            })
            self._rows = [{"id": self.db.tasks[-1]["id"]}]
        elif s.startswith("UPDATE copilot_tasks SET status"):
            status, result, _again, task_id, owner = params
            for t in self.db.tasks:
                if t["id"] == task_id and t["owner_email"] == owner:
                    t["status"], t["result"] = status, result
                    self._rows = [{"id": task_id}]
                    return
            self._rows = []
        elif s.startswith("SELECT id, title, detail"):
            # params vary: (owner, status, [category], limit). status='all' drops
            # the status filter, and category is conditional.
            owner = params[0]
            limit = params[-1]
            status = params[1] if len(params) >= 3 else "all"
            category = params[2] if len(params) == 4 else ""
            rows = [t for t in self.db.tasks if t["owner_email"] == owner]
            if status and status != "all":
                rows = [t for t in rows if t["status"] == status]
            if category:
                rows = [t for t in rows if t["category"] == category]
            self._rows = rows[-limit:]
        else:  # purge queries
            self.rowcount = 0

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self):
        self.messages, self.tasks, self.queries = [], [], []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConn()
        cm._ensure_schema(self.conn)
        ct._ensure_schema(self.conn)

    def check(self, ok, detail=""):
        self.assertTrue(ok, detail)

    def test_round_trip_preserves_order(self):
        cm.append_turn(self.conn, "copilot", "o@x.com", "user", "what's lead 12?")
        cm.append_turn(self.conn, "copilot", "o@x.com", "assistant", "Kelley, roof, quoted.")
        cm.append_turn(self.conn, "copilot", "o@x.com", "user", "and 13?")
        hist = cm.load_history(self.conn, "copilot", "o@x.com")
        self.check(len(hist) == 3, f"expected 3 turns, got {len(hist)}")
        self.check([h["role"] for h in hist] == ["user", "assistant", "user"], "role/order wrong")
        self.check(hist[1]["content"].startswith("Kelley"), "assistant content lost")

    def test_history_is_scoped_per_visitor(self):
        cm.append_turn(self.conn, "webchat", "visitor-a", "user", "my address is 1 A St")
        cm.append_turn(self.conn, "webchat", "visitor-b", "user", "my address is 2 B St")
        a = cm.load_history(self.conn, "webchat", "visitor-a")
        b = cm.load_history(self.conn, "webchat", "visitor-b")
        self.check(len(a) == 1 and "1 A St" in a[0]["content"], "visitor A leaked/missing")
        self.check("2 B St" not in a[0]["content"], "visitor A saw visitor B's address")

    def test_orphan_leading_assistant_reply_is_trimmed(self):
        cm.append_turn(self.conn, "copilot", "o@x.com", "assistant", "How can I help?")
        cm.append_turn(self.conn, "copilot", "o@x.com", "user", "roof quote")
        hist = cm.load_history(self.conn, "copilot", "o@x.com")
        self.check(hist[0]["role"] == "user", f"replay must start with user, got {hist[0]['role']}")

    def test_clear_only_touches_one_session(self):
        cm.append_turn(self.conn, "webchat", "visitor-a", "user", "hi")
        cm.append_turn(self.conn, "webchat", "visitor-b", "user", "hi")
        cm.clear_history(self.conn, "webchat", "visitor-a")
        self.check(cm.load_history(self.conn, "webchat", "visitor-a") == [], "not cleared")
        self.check(len(cm.load_history(self.conn, "webchat", "visitor-b")) == 1, "cleared too much")

    def test_retention_window_is_thirty_days_for_visitors(self):
        self.check(cm.retention_days("webchat") == 30, "visitor retention must be 30 days")
        self.check(cm.retention_days("copilot") > 365, "owner history should be kept")

    def test_empty_content_is_not_stored(self):
        cm.append_turn(self.conn, "copilot", "o@x.com", "user", "   ")
        self.check(self.conn.messages == [], "blank turn should not persist")

    def test_task_lifecycle_and_owner_isolation(self):
        tid = ct.add_task(self.conn, "owner@x.com", "Call Kelley back", "about the deposit")
        tasks = ct.list_tasks(self.conn, "owner@x.com")
        self.check(len(tasks) == 1 and tasks[0]["title"] == "Call Kelley back", "task not saved")

        # A different owner must not be able to close it.
        self.check(ct.update_task(self.conn, "someone@else.com", 1, "done") is False,
                   "task must be owner-scoped")

        self.check(ct.update_task(self.conn, "owner@x.com", 1, "done", "sent COI") is True,
                   "owner should be able to close it")
        self.check(ct.list_tasks(self.conn, "owner@x.com", "open") == [], "done task still open")
        done = ct.list_tasks(self.conn, "owner@x.com", "all")
        self.check(done and done[0]["result"] == "sent COI",
                   "completed task should stay searchable with its result")

    def test_task_status_is_validated(self):
        tid = ct.add_task(self.conn, "owner@x.com", "anything")
        with self.assertRaises(ValueError):
            ct.update_task(self.conn, "owner@x.com", tid, "banana")

    def test_task_retention_is_one_year(self):
        self.check(ct.RETENTION_DAYS == 365, "owner asked for 1 year")


class CalculatorTests(unittest.TestCase):
    def check(self, ok, detail=""):
        self.assertTrue(ok, detail)

    def test_arithmetic_is_exact(self):
        cases = {
            "1800*8.75": 15750,
            "(1800*8.75)*1.15": 18112.5,
            "round(32516.50/4, 2)": 8129.12,
            "sum([1,2,3])*100": 600,
            "2**10": 1024,
            "max(3, 9, 4)": 9,
            "19000+33000": 52000,
            "100000*0.85": 85000.0,
        }
        for expr, want in cases.items():
            got = calculate(expr)
            self.check(got.get("ok"), f"{expr} failed: {got}")
            self.check(abs(float(got["result"]) - float(want)) < 0.01,
                       f"{expr} -> {got['result']}, wanted {want}")

    def test_sandbox_blocks_escape_attempts(self):
        hostile = [
            "__import__('os').system('ls')",
            "open('/etc/passwd').read()",
            "[x for x in range(10)]",
            "().__class__",
            "lambda: 1",
            "eval('1+1')",
            "global_var",
            "2**99999999",
            "1/0",
        ]
        for expr in hostile:
            got = calculate(expr)
            self.check(got.get("ok") is False, f"SANDBOX BREACH: {expr!r} -> {got}")
            self.check("error" in got, f"{expr!r} should return an error, not a result")

    def test_empty_expression_is_reported(self):
        got = calculate("")
        self.check(
            got.get("ok") is False and "expression" in got["error"].lower(),
            f"empty expression should report an error, got {got!r}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)