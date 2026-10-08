"""record_manual_touch: an owner-initiated contact must count like a bot touch.

The bug this pins down is a policy hole, not a formatting one. The bot only ever
wrote its own outbound rows, so a lead the owner reached in person -- a printed
letter, a door knock -- still read as zero touches to contact_policy_allows. The
email sweep would then contact that person, who had already been mailed, against
a policy that allows one touch. Two different channels, one lead, and the count
that should have stopped the second one was blind to the first.

Recorded offline against a fake db/cursor. No network, no PostgreSQL.
"""

if __name__ == "__main__":
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


    class FakeCursor:
        def __init__(self, log, fail=False):
            self.log = log
            self.fail = fail
            self._rows = [{"notes": ""}]

        def execute(self, sql, params=None):
            if self.fail:
                raise RuntimeError("db down")
            self.log.append((" ".join(sql.split()), params))
            return self

        def fetchone(self):
            return self._rows[0]

        # auto_reply uses `with db.cursor() as cur:`, so the fake has to be a
        # context manager or every assertion fails on the protocol rather than on
        # the behaviour under test.
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False


    class FakeDb:
        def __init__(self, fail=False):
            self.log = []
            self.fail = fail
            self.commits = 0

        def cursor(self):
            return FakeCursor(self.log, self.fail)

        def commit(self):
            self.commits += 1

        def statements(self):
            return [s for s, _ in self.log]

        def inserts(self):
            return [(s, p) for s, p in self.log if s.upper().startswith("INSERT")]


    import auto_reply  # noqa: E402

    print("[1] an owner touch is written as a real comms_logs row")
    db = FakeDb()
    res = auto_reply.record_manual_touch(db, 42, "letter", "left in the door")
    check("returns ok", res.get("ok") is True, res)
    check("the insert targets comms_logs",
          any("comms_logs" in s for s in db.statements()), db.statements()[:1])
    ins = db.inserts()
    check("the row is attributed to the lead via lead_id",
          any(p and p[-1] == 42 for _, p in ins), ins)
    check("the row is outbound", any("'outbound'" in s for s, _ in ins))
    check("the channel is stored as given",
          any(p and p[0] == "letter" for _, p in ins), ins)
    check("the sender is the owner, not the bot",
          any("'owner'" in s for s, _ in ins), ins)
    check("it commits", db.commits == 1, db.commits)
    check("the note carries the timestamp marker",
          any(p and "owner letter" in str(p[-2]) for _, p in ins), ins)

    print("\n[2] a lead the owner reached is no longer 'new'")
    db = FakeDb()
    auto_reply.record_manual_touch(db, 7, "door-knock")
    check("status is moved off 'new'",
          any("status = 'contacted'" in s and "status = 'new'" in s for s in db.statements()),
          [s for s in db.statements() if "status" in s])
    check("the update is scoped to leads that are still new",
          any("WHERE id = %s AND status = 'new'" in s for s in db.statements()))

    print("\n[3] every physical channel is accepted, junk is not")
    for chan in ("letter", "door-knock", "phone", "sms", "in-person", "other"):
        db = FakeDb()
        r = auto_reply.record_manual_touch(db, 1, chan)
        check(f"{chan!r} accepted", r.get("ok") is True, r)
    for bad in ("email", "", "LETTER DROP TABLE", "carrier pigeon"):
        db = FakeDb()
        r = auto_reply.record_manual_touch(db, 1, bad)
        check(f"{bad!r} rejected", r.get("ok") is False, r)
        check(f"{bad!r} wrote nothing", not db.log, db.log)

    print("\n[4] a bad channel cannot fake a touch, and a db failure is not fatal")
    db = FakeDb()
    r = auto_reply.record_manual_touch(db, 0, "letter")
    check("a missing lead_id is rejected", r.get("ok") is False, r)
    check("a missing lead_id wrote nothing", not db.log)
    r = auto_reply.record_manual_touch(None, 5, "letter")
    check("a missing db is rejected", r.get("ok") is False, r)
    db = FakeDb(fail=True)
    r = auto_reply.record_manual_touch(db, 9, "letter")
    check("a db failure is reported, not raised", r.get("ok") is False, r)

    print("\n[5] case is normalised so a stray capital does not lose the touch")
    db = FakeDb()
    r = auto_reply.record_manual_touch(db, 3, "Letter")
    check("'Letter' is accepted and stored lower", r.get("ok") is True and r.get("channel") == "letter", r)
    check("the stored channel is lowercase",
          any(p and p[0] == "letter" for _, p in db.inserts()), db.inserts())

    print("\n[6] the touch is visible to the contact policy")
    # This is the reason the function exists. contact_policy_allows prefers the
    # per-lead count, which is exactly the column this write populates, so a mailed
    # lead consumes the single touch an email would have.
    db = FakeDb()
    auto_reply.record_manual_touch(db, 500, "letter")
    ins = db.inserts()
    check("lead_id is the parameter the policy's lead-wide query filters on",
          ins and ins[0][1][-1] == 500, ins)
    check("recipient is lead-scoped, not an address the policy cannot match",
          ins and str(ins[0][1][1]).startswith("lead:"), ins)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        sys.exit(1)
    print("All manual-touch tests passed.")
