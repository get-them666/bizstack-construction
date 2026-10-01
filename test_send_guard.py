"""The per-recipient cooldown that was missing, and the cap clamp.

A phone number received 39,222 outbound SMS rows over six days at roughly
0.25-second intervals, with TEXT_DAILY_CAP set to 50 the whole time.

The reason the cap did not stop it: the daily cap is a whole-list budget. It
bounds how much goes out across every recipient, so it never binds a single
number that gets reprocessed on every pass. There was a per-recipient guard in
this file the entire time -- _recipient_recently_sent -- and nothing ever
called it.

Offline: fake db, no network, no SignalWire.
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

FAILS = []


def check(name, ok, detail=""):
    detail = str(detail)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


class FakeDb:
    def __init__(self, hit=False, fail=False):
        self.hit = hit
        self.fail = fail
        self.queries = []

    def cursor(self):
        return _Cursor(self)


class _Cursor:
    def __init__(self, db):
        self.db = db
        self.last_params = None

    def execute(self, sql, params=None):
        if self.db.fail:
            raise RuntimeError("db down")
        self.db.queries.append((" ".join(sql.split()), params))
        self.last_params = params
        return self

    def fetchone(self):
        if "FROM comms_logs" in (self.db.queries[-1][0] if self.db.queries else ""):
            return {"exists": 1} if self.db.hit else None
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


import auto_reply  # noqa: E402

for _k in ("EMAIL_DAILY_CAP", "TEXT_DAILY_CAP"):
    os.environ.pop(_k, None)

print("[1] the guard blocks a recipient already contacted today")
db = FakeDb(hit=True)
ok, why = auto_reply._recent_send_guard(db, "+17034283639", "text", 1)
check("a repeat send is refused", ok is False, ok)
check("the reason names the recipient and window", "already text" in why and "17034283639" in why, why)

print("\n[2] a first contact is allowed")
db = FakeDb(hit=False)
ok, why = auto_reply._recent_send_guard(db, "+15551234567", "text", 1)
check("a new recipient may be sent to", ok is True, why)

print("\n[3] it fails closed when the lookup breaks")
# A db error must never become permission to send.
db = FakeDb(hit=False, fail=True)
ok, why = auto_reply._recent_send_guard(db, "+15551234567", "text", 1)
check("a failed lookup refuses the send", ok is False, ok)
ok, why = auto_reply._recent_send_guard(db, None, "text", 1)
check("a missing recipient refuses the send", ok is False, ok)
ok, why = auto_reply._recent_send_guard(None, "+15551234567", "text", 1)
check("a missing db refuses the send", ok is False, ok)

print("\n[4] the cooldown is checked per channel, not globally")
db = FakeDb(hit=True)
ok_email, _ = auto_reply._recent_send_guard(db, "a@b.com", "email", 1)
ok_text, _ = auto_reply._recent_send_guard(db, "a@b.com", "text", 1)
check("the guard is channel-aware", ok_email is False and ok_text is False,
      f"email={ok_email} text={ok_text}")

print("\n[5] a configured cap cannot be raised to unlimited")
# TEXT_DAILY_CAP / EMAIL_DAILY_CAP are read from the environment per call. A
# value of 0, -1 or an empty string must not become "no limit".
for value, want in (("0", 0), ("-1", 0), ("-999", 0), ("99999", 50), ("80", 50)):
    os.environ["EMAIL_DAILY_CAP"] = value
    got = auto_reply._hard_daily_ceiling("email")
    check(f"EMAIL_DAILY_CAP={value!r} -> {want}", got == want, got)
for value, want in (("0", 0), ("99999", 20), ("50", 20)):
    os.environ["TEXT_DAILY_CAP"] = value
    got = auto_reply._hard_daily_ceiling("text")
    check(f"TEXT_DAILY_CAP={value!r} -> {want}", got == want, got)

print("\n[6] an unset cap is the default, not unlimited and not disabled")
os.environ.pop("EMAIL_DAILY_CAP", None)
os.environ.pop("TEXT_DAILY_CAP", None)
check("unset email cap is 30, not 0", auto_reply._hard_daily_ceiling("email") == 30,
      auto_reply._hard_daily_ceiling("email"))
check("unset text cap is 30, not 0", auto_reply._hard_daily_ceiling("text") == 30,
      auto_reply._hard_daily_ceiling("text"))
os.environ["EMAIL_DAILY_CAP"] = ""
check("an empty string is treated as unset, not zero",
      auto_reply._hard_daily_ceiling("email") == 30, auto_reply._hard_daily_ceiling("email"))

print("\n[7] the guard is actually wired into both send paths")
import inspect  # noqa: E402

src = inspect.getsource(auto_reply)
# ensure_lead_reply is the function that decides email vs text. Both channels
# must consult the guard, and a comment is not a call.
fn = inspect.getsource(auto_reply.ensure_lead_reply)
check("the email path calls the cooldown guard", '_recent_send_guard(db, email, "email"' in fn)
check("the text path calls the cooldown guard", '_recent_send_guard(db, to, "text"' in fn)
check("the guard precedes the send in both", fn.count("_recent_send_guard") >= 2)
check("_channel_allowed uses the clamped cap", "_hard_daily_ceiling(channel)" in inspect.getsource(auto_reply._channel_allowed))

print("\n[8] the previously-dead helper is now reachable")
check("_recipient_recently_sent is called by the guard",
      "_recipient_recently_sent(db, recipient, channel, days)" in inspect.getsource(auto_reply._recent_send_guard))

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): {FAILS}")
    sys.exit(1)
print("All send-guard tests passed.")