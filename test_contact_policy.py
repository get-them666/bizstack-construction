"""Tests for the outreach contact policy.

Run: venv/bin/python test_contact_policy.py

The rule under test: a lead is contacted once. A follow-up may follow a reply,
or once 5 days have passed. After that they are never contacted again unless
they respond. Broom guards this in test_dedup.py; this is the same coverage for
the construction app, which has no other suite covering contact_policy_allows.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")

# auto_reply pulls in documents_service and stripe_service at module load, which
# in turn import fpdf and stripe. This test exercises the contact policy only:
# it never builds a PDF or charges a card, and the local venv has no pip. Stand
# in for the missing third-party packages rather than mutating the venv, so the
# test runs anywhere.
def _stub(name, **attrs):
    import types

    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


for _name in ("fpdf", "stripe"):
    try:
        __import__(_name)
    except ModuleNotFoundError:
        _stub(_name, FPDF=type("FPDF", (), {}), StripeClient=type("StripeClient", (), {}))

import auto_reply

PASSED = 0
FAILED = 0
FAILURES = []


def check(label, condition, detail: object = ""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        FAILURES.append(f"{label} {detail}".strip())
        print(f"  FAIL {label} {detail}".rstrip())


class PolicyCur:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.db.sql = " ".join(sql.split())
        return self

    def fetchone(self):
        return self.db.answer(self.db.sql)

    def fetchall(self):
        return []


class PolicyDb:
    """Answers the touch-count and lead-reply lookups without a real database."""

    def __init__(self, touches=0, last_at=None, replied=False, boom=False):
        self.touches, self.last_at, self.replied, self.boom = touches, last_at, replied, boom
        self.sql = ""

    def cursor(self):
        return PolicyCur(self)

    def commit(self):
        pass

    def close(self):
        pass

    def answer(self, sql):
        if self.boom:
            raise RuntimeError("db down")
        if "FROM comms_logs" in sql:
            return {"n": self.touches, "last_at": self.last_at}
        if "FROM leads" in sql:
            return {"last_reply_at": "2026-09-30" if self.replied else None}
        return None


def test_contact_policy():
    print("\n[1] contact once, follow up on reply or after 5d, then never again")
    os.environ["LEAD_MAX_TOUCHES"] = "2"
    os.environ["LEAD_RETOUCH_DAYS"] = "5"
    os.environ["LEAD_MAX_TOUCHES_REPLIED"] = "3"
    now = datetime.now(timezone.utc)

    ok, why = auto_reply.contact_policy_allows(PolicyDb(touches=0), "a@x.com", "email")
    check("first contact always allowed", ok is True, why)

    ok, why = auto_reply.contact_policy_allows(PolicyDb(touches=1, last_at=now), "a@x.com", "email", 42)
    check("follow-up blocked before 5 days", ok is False, why)
    check("  and explains the wait", "waits" in why, why)

    ok, why = auto_reply.contact_policy_allows(
        PolicyDb(touches=1, last_at=now - timedelta(days=6)), "a@x.com", "email", 42)
    check("follow-up allowed after 5 days with no reply", ok is True, why)

    original = auto_reply._lead_replied
    auto_reply._lead_replied = lambda db, lid: True
    try:
        ok, why = auto_reply.contact_policy_allows(
            PolicyDb(touches=1, last_at=now), "a@x.com", "email", 42)
        check("a reply unlocks the follow-up immediately", ok is True, why)

        ok, why = auto_reply.contact_policy_allows(
            PolicyDb(touches=2, last_at=now - timedelta(days=3650)), "a@x.com", "email", 42)
        check("a response reopens contact after the 5d follow-up", ok is True, why)

        ok, why = auto_reply.contact_policy_allows(
            PolicyDb(touches=3, last_at=now - timedelta(days=3650)), "a@x.com", "email", 42)
        check("reply ceiling stops it becoming a cadence", ok is False, why)
    finally:
        auto_reply._lead_replied = original

    for n in (2, 3, 68000):
        ok, why = auto_reply.contact_policy_allows(
            PolicyDb(touches=n, last_at=now - timedelta(days=3650)), "a@x.com", "email", 42)
        check(f"{n} touches and no reply is never contacted again", ok is False, why)
        check(f"  {n}x names the reason", "no reply" in why, why)

    ok, why = auto_reply.contact_policy_allows(PolicyDb(boom=True), "a@x.com", "email")
    check("fails CLOSED when the count cannot be read", ok is False, why)

    ok, why = auto_reply.contact_policy_allows(PolicyDb(touches=0), "", "email")
    check("no recipient is not sendable", ok is False, why)

    ok, why = auto_reply.contact_policy_allows(PolicyDb(touches=0), "+15551234567", "text", 42)
    check("policy is per-channel: text starts fresh", ok is True, why)

    os.environ["LEAD_MAX_TOUCHES"] = "0"
    ok, why = auto_reply.contact_policy_allows(PolicyDb(touches=0), "a@x.com", "email")
    check("LEAD_MAX_TOUCHES=0 disables all automated contact", ok is False, why)
    os.environ["LEAD_MAX_TOUCHES"] = "2"
    os.environ.pop("LEAD_RETOUCH_DAYS", None)
    os.environ.pop("LEAD_MAX_TOUCHES_REPLIED", None)


def main():
    for fn in (test_contact_policy,):
        fn()
    print("\n" + "=" * 46)
    print(f"{PASSED} passed, {FAILED} failed")
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print("  -", f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
