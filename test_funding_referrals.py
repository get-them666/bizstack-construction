"""Business/SBA loan referral tracking — exercised against a real database.

Two things are being pinned, and both are money-and-law rather than style:

1. The business-purpose guard. Referral fees on business/SBA loans are
   permitted (lender-paid, disclosed); referral fees on residential-mortgage-
   purpose loans are not, without an MLO license. So a `home` referral must be
   unable to reach submitted / funded / commission_paid, and an unrecognised
   purpose must default to the *unpaid* one rather than the paid one.

2. Ledger idempotency and the same guard on the books. A commission posts to
   4030 Referral Commission Revenue exactly once no matter how many times the
   button is pressed, and a non-business referral cannot reach the ledger even
   by calling the bridge directly.

Runs against a throwaway database, like test_finance_intake.py. Set
TEST_DATABASE_URL to a disposable database:

    createdb bz_test
    TEST_DATABASE_URL=postgresql://$(whoami)@localhost:5432/bz_test \
        venv/bin/python test_funding_referrals.py

Script-style on purpose: booting the app runs its lifespan, which builds the
full schema, so importing it at pytest time would leave other tests unable to
TRUNCATE. The pure rules below still run under pytest; the database cases run
under __main__.
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import funding_referrals as fr  # noqa: E402
import ledger  # noqa: E402
import ledger_bridge  # noqa: E402


# --- pure rules: no database, always run ------------------------------------

def test_unknown_purpose_defaults_to_home_not_business():
    # Defaulting toward the *paid* purpose would create the unlicensed-referral
    # risk silently. Ambiguity must resolve to the unpaid one.
    for v in ("", None, "home", "my house", "kitchen", "residential", "??"):
        assert fr.normalize_purpose(v) == fr.HOME, repr(v)
    for v in ("business", "Business Loan", "SBA", "commercial", "investment_property"):
        assert fr.normalize_purpose(v) == fr.BUSINESS, v


def test_check_transition_blocks_home_from_paid_statuses():
    for target in (fr.SUBMITTED, fr.FUNDED, fr.COMMISSION_PAID):
        try:
            fr.check_transition(fr.HOME, fr.NEW, target)
        except fr.ReferralGuardError:
            continue
        raise AssertionError(f"home referral was allowed to reach {target!r}")


def test_check_transition_allows_full_business_lifecycle():
    fr.check_transition(fr.BUSINESS, fr.NEW, fr.SUBMITTED)
    fr.check_transition(fr.BUSINESS, fr.SUBMITTED, fr.FUNDED)
    fr.check_transition(fr.BUSINESS, fr.FUNDED, fr.COMMISSION_PAID)


def test_check_transition_is_forward_only_and_rejects_unknowns():
    for current, target in ((fr.NEW, fr.FUNDED), (fr.SUBMITTED, fr.NEW),
                            (fr.FUNDED, fr.SUBMITTED), (fr.NEW, "approved")):
        try:
            fr.check_transition(fr.BUSINESS, current, target)
        except fr.ReferralGuardError:
            continue
        raise AssertionError(f"{current!r}->{target!r} should have been rejected")


def test_commission_cents_for():
    assert fr.commission_cents_for(50000000, 1.0) == 500000   # 1% of $500,000
    assert fr.commission_cents_for(50000000, 1.5) == 750000
    assert fr.commission_cents_for(None, 1.0) is None
    assert fr.commission_cents_for(50000000, None) is None


def test_bridge_refuses_non_business_referral():
    # The ledger cannot be used as a bypass around the referral guard.
    try:
        ledger_bridge.post_referral_commission(
            None, {"id": 1, "purpose": "home", "commission_cents": 100000})
    except ledger.LedgerError:
        return
    raise AssertionError("bridge booked a commission on a home referral")


def test_referral_commission_account_is_in_the_chart():
    codes = {c for c, _n, _t in ledger.CHART}
    assert "4030" in codes


# --- database cases: only under __main__ ------------------------------------

def _rows(dsn, sql, args=None):
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, row_factory=dict_row) as c, c.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def main():
    import psycopg
    from psycopg.rows import dict_row

    DSN = os.environ.get("DATABASE_URL") or ""
    if not DSN or ("localhost" not in DSN and "127.0.0.1" not in DSN):
        print("SKIP: set TEST_DATABASE_URL to a throwaway database")
        return

    import construction_main as m

    FAILS = []

    def check(name, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if not ok and detail else ""))
        if not ok:
            FAILS.append(name)

    # Boot the app so its lifespan builds the schema, as production does.
    from fastapi.testclient import TestClient
    with TestClient(m.app):
        with psycopg.connect(DSN, row_factory=dict_row) as db:
            fr.ensure_schema(db)
            ledger.ensure_schema(db)

            # --- create a business referral, walk the lifecycle -------------
            with db.cursor() as cur:
                cur.execute("DELETE FROM funding_referrals WHERE lead_id IN (-1, -2)")
            db.commit()

            biz = fr.create(db, lead_id=-1, purpose="SBA", amount_cents=50000000)
            check("business referral created and normalized", biz["purpose"] == fr.BUSINESS)
            check("starts in new", biz["status"] == fr.NEW)

            fr.set_status(db, biz["id"], fr.SUBMITTED)
            fr.set_status(db, biz["id"], fr.FUNDED)
            paid = fr.set_status(db, biz["id"], fr.COMMISSION_PAID, commission_pct=1.0)
            check("business referral can be commissioned", paid["status"] == fr.COMMISSION_PAID)
            check("1% of $500,000 is $5,000", paid["commission_cents"] == 500000,
                  f"got {paid['commission_cents']}")

            # --- commission posts to the books, exactly once ----------------
            out1 = ledger_bridge.post_referral_commission(db, paid)
            out2 = ledger_bridge.post_referral_commission(db, paid)
            check("first post lands", out1.get("posted") is True, str(out1))
            check("second post is idempotent", out2.get("posted") is False, str(out2))

            bal = _rows(DSN,
                "SELECT COALESCE(SUM(credit_cents),0) AS cr FROM ledger_lines "
                "WHERE account_code = '4030'")
            check("4030 carries the $5,000 once", bal[0]["cr"] == 500000, f"got {bal[0]['cr']}")

            # --- a home referral can never be paid --------------------------
            home = fr.create(db, lead_id=-2, purpose="home", amount_cents=30000000)
            blocked = False
            try:
                fr.set_status(db, home["id"], fr.COMMISSION_PAID)
            except fr.ReferralGuardError:
                blocked = True
            check("home referral cannot be commissioned", blocked)
            still = fr.get(db, home["id"])
            check("home referral left at new", still["status"] == fr.NEW, str(still["status"]))

            # --- cleanup ----------------------------------------------------
            with db.cursor() as cur:
                cur.execute("DELETE FROM funding_referrals WHERE lead_id IN (-1, -2)")
            db.commit()

    print()
    if FAILS:
        print(f"FAILED: {', '.join(FAILS)}")
        sys.exit(1)
    print("ALL funding referral checks PASSED")


if __name__ == "__main__":
    main()
