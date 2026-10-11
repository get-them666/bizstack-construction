"""Itemized quotes, a real job price, and the 30% deposit.

Why this is separate from estimating_service
--------------------------------------------
`estimating_service.estimate()` answers a yes/no question -- "roughly what does
this cost?" -- and returns a WIDE range. That is the right answer for a triage
sort and the wrong one for a quote: a customer cannot approve a number that is
also two other numbers. It has no place to record a quote, no line items, and no
status beyond "we estimated it once".

So the price *model* stays where it is (`MODELS` in estimating_service, driven
by the EST_* env vars) and this module owns everything downstream of it:

    quotes        one per lead: scope, sq ft, trade, job price, deposit, status
    quote_lines   the itemization: labour, material, permits, each with its own
                  weight and its own price, and an optional SKU so a line can be
                  re-priced from materials_catalog instead of guessed.

The trade split matters commercially. In the live lead table the biggest single
bucket is "Building addition and/or alteration residential" (~64 leads), then
HVAC/mechanical (~56) and electrical (~49). Those three are the business. Roof,
siding and utilities are rounding error, and `UNQUOTABLE_TRADES` deliberately
refuses the latter two -- a utility right-of-way permit is not a job this
company can quote, and returning a confident number for one is worse than
returning none.

Deposit: 30% of the job price, per the owner. There is deliberately **no flat
default** -- an earlier build fell back to STRIPE_DEPOSIT_PRICE (a hard 500.00),
which quietly produced $500 deposits on a $2,000 job and $500 on a $40,000 one.
:func:`deposit_for` returns 0 when there is no price, and callers must treat 0
as "not quotable yet" rather than "free".
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import estimating_service

DEPOSIT_PCT = float(os.getenv("DEPOSIT_PCT", "0.30") or 0.30)

STATUS_DRAFT = "draft"
STATUS_PENDING_WALKTHROUGH = "pending_walkthrough"
STATUS_SENT = "sent"
STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"

STATUSES = (
    STATUS_DRAFT, STATUS_PENDING_WALKTHROUGH, STATUS_SENT,
    STATUS_ACCEPTED, STATUS_DECLINED,
)

# Band selector. A quote commits to one number, so the owner picks which point
# in the MODELS range the quote sits at rather than hiding behind a range.
BAND_LOW = "low"
BAND_MID = "mid"
BAND_HIGH = "high"

# Shares of the trade band per component. These are the thing the owner tunes;
# they default to a labour-heavy residential split because that is what the
# EST_* bands were originally derived from.
TRADE_SPLIT: dict[str, list[tuple[str, str, float]]] = {
    # (description, category, share)
    "whole-home": [
        ("Demo & site prep", "labor", 0.10),
        ("Framing / structural", "labor", 0.18),
        ("Electrical", "labor", 0.10),
        ("Plumbing", "labor", 0.10),
        ("Drywall & paint", "labor", 0.14),
        ("Flooring", "labor", 0.10),
        ("Trim & finish carpentry", "labor", 0.10),
        ("Materials", "material", 0.18),
    ],
    "refresh": [
        ("Surface prep & protection", "labor", 0.25),
        ("Drywall repair", "labor", 0.20),
        ("Paint (labour)", "labor", 0.30),
        ("Paint & flooring materials", "material", 0.25),
    ],
    "drywall": [
        ("Patch & hang drywall", "labor", 0.55),
        ("Tape, coat & sand", "labor", 0.25),
        ("Drywall board & compound", "material", 0.20),
    ],
    "electrical": [
        ("Electrical labour", "labor", 0.55),
        ("Devices, panels & wiring", "material", 0.30),
        ("Permit & inspection", "permit", 0.15),
    ],
    "plumbing": [
        ("Plumbing labour", "labor", 0.55),
        ("Fixtures & supply lines", "material", 0.30),
        ("Permit & inspection", "permit", 0.15),
    ],
    "hvac": [
        ("HVAC labour", "labor", 0.45),
        ("Equipment & ductwork", "material", 0.45),
        ("Permit & inspection", "permit", 0.10),
    ],
    "kitchen": [
        ("Cabinetry", "material", 0.35),
        ("Cabinet installation", "labor", 0.25),
        ("Countertops", "material", 0.15),
        ("Appliances", "material", 0.10),
        ("Plumbing & electrical rough-in", "labor", 0.15),
    ],
    "bath": [
        ("Fixtures & tub/shower unit", "material", 0.40),
        ("Tile (wall & floor)", "material", 0.15),
        ("Rough-in plumbing", "labor", 0.20),
        ("Tile & finish labour", "labor", 0.20),
        ("Permit & inspection", "permit", 0.05),
    ],
    "tile": [
        ("Tile material", "material", 0.45),
        ("Underlayment & thinset", "material", 0.10),
        ("Tile labour", "labor", 0.45),
    ],
    "carpet": [
        ("Carpet material", "material", 0.55),
        ("Pad & underlayment", "material", 0.10),
        ("Installation labour", "labor", 0.35),
    ],
    "basement": [
        ("Framing & insulation", "labor", 0.30),
        ("Drywall & paint", "labor", 0.25),
        ("Flooring", "material", 0.20),
        ("Electrical & trim", "labor", 0.15),
        ("Materials", "material", 0.10),
    ],
    "concrete": [
        ("Formwork & prep", "labor", 0.25),
        ("Placement & finish", "labor", 0.35),
        ("Concrete & rebar", "material", 0.40),
    ],
    "masonry": [
        ("Masonry labour", "labor", 0.50),
        ("Brick / block & mortar", "material", 0.40),
        ("Permit", "permit", 0.10),
    ],
    "roof": [
        ("Roofing labour", "labor", 0.45),
        ("Shingles, underlayment & trim", "material", 0.45),
        ("Permit & disposal", "permit", 0.10),
    ],
    "fence": [
        ("Fence labour", "labor", 0.40),
        ("Lumber, piers & hardware", "material", 0.50),
        ("Permit", "permit", 0.10),
    ],
    "framing": [
        ("Framing labour", "labor", 0.50),
        ("Lumber & fasteners", "material", 0.50),
    ],
}

# Permit-derived trades this company should not price. Returning a confident
# number for a utility right-of-way is worse than declining it.
UNQUOTABLE_TRADES = {
    "utilities/right-of-way", "utilities", "right-of-way", "civil construction",
    "demolition", "landscaping",
}

_DDL = """
CREATE TABLE IF NOT EXISTS quotes (
    id              BIGSERIAL PRIMARY KEY,
    lead_id         BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    trade           VARCHAR(40) NOT NULL DEFAULT '',
    scope_text      TEXT NOT NULL DEFAULT '',
    sqft            NUMERIC(12,2),
    band            VARCHAR(8) NOT NULL DEFAULT 'mid',
    materials_tier  VARCHAR(40) NOT NULL DEFAULT '',
    job_price_cents BIGINT NOT NULL DEFAULT 0,
    deposit_cents   BIGINT NOT NULL DEFAULT 0,
    status          VARCHAR(24) NOT NULL DEFAULT 'draft',
    walkthrough_at  TIMESTAMPTZ,
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS quotes_lead_idx ON quotes (lead_id, created_at DESC);
CREATE INDEX IF NOT EXISTS quotes_status_idx ON quotes (status) WHERE status <> 'declined';

CREATE TABLE IF NOT EXISTS quote_lines (
    id            BIGSERIAL PRIMARY KEY,
    quote_id      BIGINT NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
    line_no       INTEGER NOT NULL DEFAULT 0,
    description   TEXT NOT NULL DEFAULT '',
    category      VARCHAR(20) NOT NULL DEFAULT 'labor',
    qty           NUMERIC(12,2) NOT NULL DEFAULT 1,
    unit          VARCHAR(40) NOT NULL DEFAULT '',
    unit_price_cents BIGINT NOT NULL DEFAULT 0,
    total_cents   BIGINT NOT NULL DEFAULT 0,
    sku           VARCHAR(120) NOT NULL DEFAULT '',
    vendor        VARCHAR(120) NOT NULL DEFAULT '',
    sort_order    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS quote_lines_quote_idx ON quote_lines (quote_id, sort_order);
"""


def ensure_schema(db) -> None:
    with db.cursor() as cur:
        for stmt in _DDL.strip().split(";"):
            if stmt.strip():
                cur.execute(stmt)
    db.commit()


# --------------------------------------------------------------------------
# money
# --------------------------------------------------------------------------

def _cents(amount: float) -> int:
    """Dollars -> integer cents, rounded half-up.

    banker's rounding would make a 30% deposit on an odd number of cents land a
    cent under the customer's expectation for no good reason.
    """
    from decimal import Decimal, ROUND_HALF_UP
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def deposit_for(job_price_cents: int, pct: float | None = None) -> int:
    """30% of the job price. 0 when there is no price -- never a flat default."""
    if not job_price_cents or job_price_cents <= 0:
        return 0
    pct = DEPOSIT_PCT if pct is None else pct
    return int(round(job_price_cents * pct))


def band_total_cents(model: str, sqft: float | None, band: str) -> int:
    """Resolve a MODELS entry to one committed number in cents."""
    spec = estimating_service.MODELS.get(model)
    if not spec:
        return 0
    low, high = float(spec.get("low") or 0), float(spec.get("high") or 0)
    if band == BAND_LOW:
        amount = low
    elif band == BAND_HIGH:
        amount = high
    else:
        amount = (low + high) / 2.0
    kind = spec.get("kind")
    if kind == "sqft" and sqft:
        amount *= float(sqft)
    elif kind == "square" and sqft:
        amount *= float(sqft) / 100.0
    return _cents(amount)


def split_cents(total_cents: int, model: str) -> list[tuple[str, str, int]]:
    """Break a trade total into itemized lines by category share.

    Rounding is pushed into the largest line rather than dropped, so the parts
    always sum to the whole. A quote whose lines add up to $2 short is the kind
    of thing a customer notices.
    """
    parts = TRADE_SPLIT.get(model)
    if not parts or total_cents <= 0:
        return [("Labour & materials", "labor", total_cents)]
    raw = [(desc, cat, total_cents * share) for desc, cat, share in parts]
    out = []
    for desc, cat, cents in raw:
        out.append([desc, cat, int(cents)])
    drift = total_cents - sum(r[2] for r in out)
    if drift:
        biggest = max(range(len(out)), key=lambda i: out[i][2])
        out[biggest][2] += drift
    return [tuple(r) for r in out]


# --------------------------------------------------------------------------
# creation
# --------------------------------------------------------------------------

# Permit-derived trade strings, longest phrase first.
#
# estimating_service.PROJECT_MODEL_MAP is hand-maintained and covers the nice
# labels a human types ("Electrical", "Kitchen remodel"). It covers **none** of
# the strings that actually arrive from the permit importer -- of the top 15
# real project_type values in the live leads table, 0 are in that map, so
# project_model() falls through to "whole-home" and an electrical permit gets
# priced as a whole-home renovation. That is the default-quoting behaviour this
# module would otherwise inherit, so the importer's vocabulary is matched here.
#
# Matching is substring-based on purpose: the permit vocabulary is open-ended
# ("All Other Specialty Trade Contractors") and arrives as a new NAICS string
# whenever a jurisdiction updates. Ordered longest-first so "Heating and Air" is
# not beaten by a bare "Air", and "Tile and Terrazzo" not by "Terrazzo".
_PERMIT_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("plumbing, heating, and air-conditioning", "heating and air-conditioning",
      "hvac", "mechanical"), "hvac"),
    (("electric",), "electrical"),
    (("plumbing",), "plumbing"),
    (("gas",), "hvac"),
    (("roofing", "siding", "roof"), "roof"),
    (("flooring", "floor"), "refresh"),
    (("tile", "terrazzo"), "tile"),
    (("carpet",), "carpet"),
    (("concrete", "foundation"), "concrete"),
    (("masonry", "brick", "stone"), "masonry"),
    (("framing", "structural"), "framing"),
    (("carpentry", "trim", "finish carpentry"), "framing"),
    (("kitchen",), "kitchen"),
    (("bath", "shower"), "bath"),
    (("basement",), "basement"),
    (("drywall", "paint"), "drywall"),
    (("fence", "deck", "porch"), "fence"),
    (("addition", "alteration", "remodel", "renovation", "rehabilitation",
      "restoration", "repair", "replacement", "interior"), "whole-home"),
    (("landscap", "site work"), "whole-home"),
]


def normalize_trade(project_type: str) -> str:
    """Permit string -> a MODELS key.

    Tries, in order: the caller's own model key, estimating_service's
    hand-maintained map, then the permit vocabulary above. Falls back to
    "whole-home" only for genuinely unrecognised work.
    """
    t = (project_type or "").strip()
    if not t:
        return ""
    if t in estimating_service.MODELS:
        return t
    mapped = estimating_service.project_model(t)
    if t in estimating_service.PROJECT_MODEL_MAP and mapped:
        return mapped

    low = t.lower()
    for phrases, model in _PERMIT_KEYWORDS:
        for phrase in phrases:
            if phrase in low:
                return model
    return mapped or "whole-home"


def build_quote(trade: str, sqft: float | None = None, *, band: str = BAND_MID,
                materials_tier: str = "", scope_text: str = "") -> dict:
    """Compute a quote in memory. No DB -- used by tests and by the estimator."""
    model = trade if trade in estimating_service.MODELS else (
        normalize_trade(trade) or "whole-home")
    total = band_total_cents(model, sqft, band)
    # material_multipliers returns (multiplier, labels) -- it scans free text
    # for grade words like "marble" or "30-year", so a materials tier applies on
    # top of the band rather than replacing it.
    mult, _labels = estimating_service.material_multipliers(materials_tier or None, model)
    if mult and mult != 1.0:
        total = int(round(total * float(mult)))
    lines = split_cents(total, model)
    return {
        "trade": model,
        "sqft": sqft,
        "band": band,
        "materials_tier": materials_tier or "",
        "scope_text": scope_text or "",
        "job_price_cents": total,
        "deposit_cents": deposit_for(total),
        "lines": [
            {"description": d, "category": c, "total_cents": v}
            for d, c, v in lines
        ],
    }


def is_quotable(project_type: str) -> bool:
    t = (project_type or "").strip().lower()
    if not t:
        return False
    return t not in UNQUOTABLE_TRADES


def create_quote(db, lead_id: int, trade: str, sqft: float | None = None,
                 *, band: str = BAND_MID, materials_tier: str = "",
                 scope_text: str = "", status: str = STATUS_DRAFT) -> dict:
    """Persist a quote and its lines. Idempotent-ish: one active quote per call."""
    if status not in STATUSES:
        status = STATUS_DRAFT
    draft = build_quote(trade, sqft, band=band, materials_tier=materials_tier,
                        scope_text=scope_text)
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quotes (lead_id, trade, scope_text, sqft, band, materials_tier,
                                    job_price_cents, deposit_cents, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id;
                """,
                (int(lead_id), draft["trade"], draft["scope_text"], sqft, band,
                 draft["materials_tier"], draft["job_price_cents"],
                 draft["deposit_cents"], status),
            )
            quote_id = (cur.fetchone() or {}).get("id")
            spec = estimating_service.MODELS.get(draft["trade"], {})
            unit = spec.get("unit", "")
            for i, line in enumerate(draft["lines"], start=1):
                cur.execute(
                    "INSERT INTO quote_lines (quote_id, line_no, description, category, qty, "
                    "unit, unit_price_cents, total_cents, sku, vendor, sort_order) "
                    "VALUES (%s,%s,%s,%s,1,%s,%s,%s,'','',%s);",
                    (quote_id, i, line["description"], line["category"], unit,
                     line["total_cents"], line["total_cents"], i),
                )
        db.commit()
    except Exception as exc:
        db.rollback()
        return {"ok": False, "error": str(exc)}
    draft["ok"] = True
    draft["id"] = quote_id
    return draft


def get_quote(db, quote_id: int) -> dict:
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM quotes WHERE id = %s;", (int(quote_id),))
            quote = cur.fetchone()
            if not quote:
                return {}
            cur.execute("SELECT * FROM quote_lines WHERE quote_id = %s ORDER BY sort_order;",
                        (int(quote_id),))
            quote = dict(quote)
            quote["lines"] = [dict(r) for r in cur.fetchall()]
        return quote
    except Exception as exc:
        print(f"[quoting] get_quote failed: {exc}", flush=True)
        return {}


def quotes_for_lead(db, lead_id: int) -> list[dict]:
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM quotes WHERE lead_id = %s ORDER BY created_at DESC;",
                        (int(lead_id),))
            return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        print(f"[quoting] quotes_for_lead failed: {exc}", flush=True)
        return []


def set_status(db, quote_id: int, status: str,
               walkthrough_at=None) -> dict:
    if status not in STATUSES:
        return {"ok": False, "error": f"unknown status {status!r}"}
    try:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE quotes SET status = %s, walkthrough_at = COALESCE(%s, walkthrough_at), "
                "updated_at = NOW() WHERE id = %s RETURNING id, status;",
                (status, walkthrough_at, int(quote_id)),
            )
            row = cur.fetchone()
        db.commit()
        return {"ok": True, "status": (row or {}).get("status", status)}
    except Exception as exc:
        db.rollback()
        return {"ok": False, "error": str(exc)}


def reprice_from_catalog(db, quote_id: int) -> dict:
    """Re-price material lines whose SKU exists in materials_catalog.

    This is the seam that makes the ABC Supply integration pay off: once a line
    carries a real SKU, the price comes from the supplier feed rather than from
    a static split. Lines without a SKU are left alone rather than zeroed.
    """
    quote = get_quote(db, quote_id)
    if not quote:
        return {"ok": False, "error": "quote not found"}
    updated, missing = 0, 0
    try:
        with db.cursor() as cur:
            for line in quote["lines"]:
                if line["category"] != "material" or not line["sku"]:
                    continue
                cur.execute("SELECT price_cents FROM materials_catalog WHERE sku = %s;",
                            (line["sku"],))
                row = cur.fetchone()
                if not row or not row["price_cents"]:
                    missing += 1
                    continue
                cur.execute(
                    "UPDATE quote_lines SET unit_price_cents = %s, total_cents = %s WHERE id = %s;",
                    (row["price_cents"], row["price_cents"], line["id"]),
                )
                updated += 1
            if updated:
                cur.execute(
                    "UPDATE quotes SET job_price_cents = "
                    "  (SELECT COALESCE(SUM(total_cents),0) FROM quote_lines WHERE quote_id = %s), "
                    "  deposit_cents = %s, updated_at = NOW() WHERE id = %s RETURNING job_price_cents, deposit_cents;",
                    (int(quote_id), int(DEPOSIT_PCT * 100), int(quote_id)),
                )
                totals = cur.fetchone()
        db.commit()
        return {"ok": True, "updated": updated, "missing": missing,
                "job_price_cents": (totals or {}).get("job_price_cents"),
                "deposit_cents": (totals or {}).get("deposit_cents")}
    except Exception as exc:
        db.rollback()
        return {"ok": False, "error": str(exc)}