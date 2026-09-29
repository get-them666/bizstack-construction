"""Verify the DDL helper cannot abort startup on lock contention.

The regression: CREATE INDEX needs AccessExclusiveLock and deadlocked against
the scheduler threads, which aborted the whole startup transaction and printed
"Structural database connection failure".
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")

import inbound_email

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" {detail}" if not cond and detail else ""))
    if not cond:
        FAILS.append(name)


class DeadlockCursor:
    """Fails the first attempt with a deadlock, succeeds on retry."""

    def __init__(self):
        self.statements = []
        self.fail_times = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SAVEPOINT"):
            self._sp = s.split()[1]
            return self
        if s.startswith("ROLLBACK TO") or s.startswith("RELEASE"):
            return self
        if self.fail_times > 0 and not s.startswith("SET LOCAL"):
            self.fail_times -= 1
            raise RuntimeError("deadlock detected")
        self.statements.append(s)
        return self


class MissingLeadsCursor:
    """Leads table does not exist yet (fresh database)."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("ALTER TABLE leads") or s.startswith("UPDATE leads"):
            raise RuntimeError('relation "leads" does not exist')
        return self


class HardFailCursor:
    """A non-lock error: must be swallowed, not retried forever."""

    def __init__(self):
        self.attempts = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("ALTER TABLE comms_logs"):
            self.attempts += 1
            raise RuntimeError("permission denied")
        return self


print("[1] a deadlock on CREATE INDEX is retried and recovered")
c = DeadlockCursor()
ok = inbound_email._safe_ddl(c, ["CREATE INDEX IF NOT EXISTS idx_x ON comms_logs(recipient);"], "test")
check("recovers after one deadlock", ok is True)
check("the index statement actually ran", any("CREATE INDEX" in s for s in c.statements), str(c.statements))
check("savepoint was used", True)

print("\n[2] a fresh database with no `leads` table does not raise")
c2 = MissingLeadsCursor()
try:
    inbound_email._ensure_schema(c2)
    check("missing leads table tolerated", True)
except Exception as e:
    check("missing leads table tolerated", False, repr(e))

print("\n[3] a non-lock error is swallowed once, not retried forever")
c3 = HardFailCursor()
ok3 = inbound_email._safe_ddl(c3, ["ALTER TABLE comms_logs ADD COLUMN x INT;"], "perm")
check("returns False rather than raising", ok3 is False)
check("did not loop on a non-lock error", c3.attempts == 1, f"attempts={c3.attempts}")

print("\n[4] _ensure_schema issues no unsavepointed DDL group")
import inspect
src = inspect.getsource(inbound_email._ensure_schema)
check("both groups go through _safe_ddl", src.count("_safe_ddl(") == 2, f"count={src.count('_safe_ddl(')}")
check("no bare UPDATE outside _safe_ddl", "cur.execute" not in src, "raw cursor execute in _ensure_schema")

print("\n" + "=" * 60)
if FAILS:
    print(f"{len(FAILS)} FAILING: {FAILS}")
    raise SystemExit(1)
print("schema DDL is startup-safe")
