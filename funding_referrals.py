"""Business/SBA loan referral tracking — the revenue side of `/finance`.

Why this module exists
----------------------
`/finance` captures financing applications and files them as ordinary leads with
``funding_needed = True``. Nothing ever routes a business-purpose application to
a lender, and nothing records the referral fee when one funds. This module is
the missing half: it turns a financing lead into a *referral record* with a
lifecycle, and records the commission the lender pays us when it funds.

The money rule, which is the whole reason this file is cautious
---------------------------------------------------------------
Referral fees on **business-purpose** loans — SBA 7(a), 504, commercial — are
permitted and routine: the lender pays them (never the borrower), the referral
source is disclosed on the loan application, and no mortgage-broker license is
required as long as we only *introduce*, never negotiate terms. That is the
business this module supports.

Residential-mortgage-purpose loans are the opposite. Taking an application or
offering/negotiating terms on a loan secured by someone's home makes you a
mortgage loan originator under the SAFE Act and needs a Virginia MLO license;
RESPA §8 separately bans pure referral fees on federally related mortgage loans.
`/finance` is a *homeowner* surface, so this boundary is not theoretical — most
submissions that mention a home are exactly the ones we may not be paid on.

So the guard is structural, not a prompt or a warning comment:
:data:`BUSINESS` is the only purpose that may advance past ``new``, and
:func:`set_status` raises :class:`ReferralGuardError` the instant anyone tries to
submit or commission a ``home`` row. A guard that fails closed on money is the
point; a comment would be talked out of.

What this module does NOT do
----------------------------
It does not submit anything to a lender and there is no bank API here — lenders
do not offer one. Submission is a person (or a partner portal) and is recorded
here as a status change. The partner and the commission terms are data, entered
per referral, so this file needs no change to add a new lender.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

# Purposes. Only BUSINESS may be submitted or commissioned.
BUSINESS = "business"
HOME = "home"
PURPOSES = (BUSINESS, HOME)

# Lifecycle. Forward-only except where noted; `declined` and `commission_paid`
# are terminal.
NEW = "new"
SUBMITTED = "submitted"
FUNDED = "funded"
DECLINED = "declined"
COMMISSION_PAID = "commission_paid"
STATUSES = (NEW, SUBMITTED, FUNDED, DECLINED, COMMISSION_PAID)

# The statuses that are only ever reachable for a business-purpose loan. A
# `home` row may sit at `new` or `declined` and nowhere else.
BUSINESS_ONLY_STATUS = (SUBMITTED, FUNDED, COMMISSION_PAID)

_ALLOWED_NEXT = {
    NEW: {SUBMITTED, DECLINED},
    SUBMITTED: {FUNDED, DECLINED},
    FUNDED: {COMMISSION_PAID},
    DECLINED: set(),
    COMMISSION_PAID: set(),
}


class ReferralGuardError(RuntimeError):
    """A transition that would put the business outside the law or the ledger.

    Raised, never swallowed. A call that reaches a commission on a
    mortgage-purpose loan must fail loudly, because the alternative is being
    paid for an unlicensed referral and not noticing.
    """


def check_transition(purpose: str, current: str, target: str) -> None:
    """Decide whether ``current -> target`` is allowed for ``purpose``.

    Pure, no database: every rule this module claims is enforced here, so the
    rules can be tested without a connection and `set_status` cannot drift from
    them. Raises :class:`ReferralGuardError` on any violation and returns None
    when the move is allowed.
    """
    target = (target or "").strip().lower()
    current = (current or "").strip().lower()
    if target not in STATUSES:
        raise ReferralGuardError(f"unknown status {target!r}")
    if _business_only(target) and purpose != BUSINESS:
        raise ReferralGuardError(
            f"refusing to move a {purpose!r} referral to {target!r}: only "
            f"business-purpose loans may be submitted or commissioned "
            f"(residential mortgage referrals require an MLO license)")
    if target != current and target not in _ALLOWED_NEXT.get(current, set()):
        raise ReferralGuardError(f"cannot move a referral from {current!r} to {target!r}")


def commission_cents_for(amount_cents, pct) -> int | None:
    """The commission in cents for an amount and a percentage. Pure.

    Returns None when either input is missing, so a referral that funds before
    the percentage is agreed simply books nothing rather than a wrong figure.
    """
    if amount_cents is None or pct is None:
        return None
    try:
        return int(round(int(amount_cents) * float(pct) / 100.0))
    except (TypeError, ValueError):
        return None


_DDL = """
CREATE TABLE IF NOT EXISTS funding_referrals (
    id                 BIGSERIAL PRIMARY KEY,
    lead_id            INTEGER,
    purpose            VARCHAR(12)  NOT NULL DEFAULT 'business',
    partner            VARCHAR(120) NOT NULL DEFAULT '',
    partner_ref        VARCHAR(120) NOT NULL DEFAULT '',
    amount_cents       BIGINT,
    status             VARCHAR(20)  NOT NULL DEFAULT 'new',
    commission_pct     NUMERIC(5,2),
    commission_cents   BIGINT,
    consent_at         TIMESTAMPTZ,
    consent_note       TEXT         NOT NULL DEFAULT '',
    note               TEXT         NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS funding_referrals_lead_idx
    ON funding_referrals (lead_id);
CREATE INDEX IF NOT EXISTS funding_referrals_status_idx
    ON funding_referrals (status, created_at DESC);

-- One referral per lead. A repeat /finance submission is the same relationship
-- with a lender, not a second one, so it updates rather than stacking.
CREATE UNIQUE INDEX IF NOT EXISTS funding_referrals_lead_uniq
    ON funding_referrals (lead_id) WHERE lead_id IS NOT NULL;
"""


def _now():
    return datetime.now(timezone.utc)


def _conn():
    """Open a short-lived connection. Mirrors construction_main.get_db().

    Imported lazily so this module stays importable in a test with no database
    configured, and so it never drags the 8k-line app in just to read a row.
    """
    import construction_main
    return construction_main.get_db().__next__()


def ensure_schema(db=None) -> None:
    """Idempotent. Safe to call on every boot and on first use."""
    own = db is None
    conn = db or _conn()
    try:
        with conn.cursor() as cur:
            for stmt in _DDL.strip().split(";"):
                if stmt.strip():
                    cur.execute(stmt)
        conn.commit()
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def normalize_purpose(value: str) -> str:
    """Map free text to one of :data:`PURPOSES`, defaulting to HOME.

    Defaulting *away* from the paid purpose is deliberate. An unrecognised or
    blank answer is treated as residential, which means it cannot be
    commissioned. Guessing "business" for an ambiguous answer would create
    exactly the unlicensed-referral risk this module exists to prevent; the
    owner can reclassify a genuine business loan explicitly.
    """
    v = (value or "").strip().lower()
    if v in ("business", "business_loan", "commercial", "sba", "sba_7a", "504",
             "investment", "investment_property", "rental_business"):
        return BUSINESS
    # Free-text answers ("Business Loan", "SBA 7(a)", "commercial property").
    # Every token here is unambiguous: none appears in a residential answer.
    for token in ("business", "commercial", "sba", "invest", "rental", "504"):
        if token in v:
            return BUSINESS
    return HOME


def _business_only(status: str) -> bool:
    return status in BUSINESS_ONLY_STATUS


def create(db, *, lead_id=None, purpose=BUSINESS, partner="", partner_ref="",
           amount_cents=None, consent_at=None, consent_note="", note="") -> dict:
    """Record a referral for a financing lead.

    Returns the row as a dict. ``purpose`` is normalised with
    :func:`normalize_purpose`, so a caller cannot accidentally file an
    unrecognised purpose that later reads as commissionable.
    """
    p = normalize_purpose(purpose)
    ensure_schema(db)
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO funding_referrals
                (lead_id, purpose, partner, partner_ref, amount_cents,
                 status, consent_at, consent_note, note)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (lead_id) WHERE lead_id IS NOT NULL DO UPDATE SET
                purpose = EXCLUDED.purpose,
                amount_cents = COALESCE(EXCLUDED.amount_cents, funding_referrals.amount_cents),
                partner = CASE WHEN EXCLUDED.partner <> '' THEN EXCLUDED.partner
                               ELSE funding_referrals.partner END,
                updated_at = NOW()
            RETURNING *
            """,
            (lead_id, p, (partner or "")[:120], (partner_ref or "")[:120],
             amount_cents, NEW, consent_at, (consent_note or ""),
             (note or "")),
        )
        row = cur.fetchone()
    db.commit()
    return dict(row)


def get(db, referral_id: int) -> dict | None:
    ensure_schema(db)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM funding_referrals WHERE id = %s", (referral_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def for_lead(db, lead_id: int) -> dict | None:
    ensure_schema(db)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM funding_referrals WHERE lead_id = %s", (lead_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def list_referrals(db, *, status="", limit: int = 200) -> list[dict]:
    ensure_schema(db)
    with db.cursor() as cur:
        if status:
            cur.execute(
                "SELECT * FROM funding_referrals WHERE status = %s "
                "ORDER BY created_at DESC LIMIT %s", (status, limit))
        else:
            cur.execute(
                "SELECT * FROM funding_referrals ORDER BY created_at DESC LIMIT %s",
                (limit,))
        return [dict(r) for r in (cur.fetchall() or [])]


def set_status(db, referral_id: int, status: str, *, note: str = "",
               commission_pct=None, commission_cents=None) -> dict:
    """Advance a referral's lifecycle. Raises :class:`ReferralGuardError`.

    The rules live in :func:`check_transition` (pure); this function only reads
    the row, applies them, and writes the result. That split is deliberate —
    the guard is testable without a database, and a route cannot reach the
    write without the check.
    """
    status = (status or "").strip().lower()

    row = get(db, referral_id)
    if not row:
        raise ReferralGuardError(f"referral {referral_id} not found")

    check_transition(row["purpose"], row["status"], status)

    pct = commission_pct if commission_pct is not None else row.get("commission_pct")
    cents = commission_cents if commission_cents is not None else row.get("commission_cents")
    if status == COMMISSION_PAID and cents is None:
        cents = commission_cents_for(row.get("amount_cents"), pct)

    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE funding_referrals
               SET status = %s,
                   commission_pct = COALESCE(%s, commission_pct),
                   commission_cents = COALESCE(%s, commission_cents),
                   note = CASE WHEN %s <> '' THEN %s ELSE note END,
                   updated_at = NOW()
             WHERE id = %s
            RETURNING *
            """,
            (status, pct, cents, (note or ""), (note or ""), referral_id),
        )
        updated = cur.fetchone()
    db.commit()
    return dict(updated)
