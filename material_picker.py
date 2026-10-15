"""Customer-facing material picker.

The sandbox: a visitor browses real stock, picks what they like, and gets a
price list back. Public, no login.

WHAT MAY APPEAR HERE
--------------------
Only suppliers flagged public_ok -- retail shelf prices the public can
already see elsewhere. Account-specific pricing is never reachable from this
module, because the queries that feed it filter on public_ok and there is no
parameter that turns it off.

PRICE DERIVATION
----------------
A customer's figure is cost x markup, rounded up to a step. The rounding is
not cosmetic. A flat markup is exactly invertible: a displayed price divided
by a guessed rate returns the cost to the cent, and one guessed rate then
reverses every other item on the page. Rounding up to $5 and varying the
markup per order means a displayed price is consistent with a *range* of
costs rather than a single one, so the division does not land.

So `customer_price` never returns the raw cost, at any markup setting, and
refuses a markup of zero because that would publish it.
"""
from __future__ import annotations

import os
import random
from datetime import date

import supplier_prices as sp

# Rounding step for customer-facing figures. Coarse steps obscure cost better
# but look less precise; $5 reads as a real retail price rather than a dodge.
PRICE_STEP_CENTS = int(os.getenv("SANDBOX_PRICE_STEP", "500"))

MARKUP_MIN = 0.20
MARKUP_MAX = 0.45
MARKUP_DEFAULT = 0.30

# Per-item jitter around the order markup, so no two lines share a rate and a
# single division cannot be repeated across the page.
_JITTER = 0.06


def markup_pct(order_markup: float | None = None) -> float:
    base = MARKUP_DEFAULT if order_markup is None else float(order_markup)
    return max(MARKUP_MIN, min(MARKUP_MAX, base))


def _line_markup(order_markup: float | None, salt: str) -> float:
    """Markup for one line: the order rate plus a little stable jitter.

    Deterministic on the item so re-rendering a list does not change prices
    under the customer, but different per item so the rate cannot be solved
    from two lines.
    """
    base = markup_pct(order_markup)
    h = sum(ord(c) * (i + 7) for i, c in enumerate(salt or "x"))
    return base + ((h % 1000) / 1000.0 - 0.5) * 2 * _JITTER


def customer_price(cost_cents: int, salt: str = "",
                   order_markup: float | None = None) -> int | None:
    """Rounded-up, per-line marked-up price. None when cost is unknown.

    Refuses to return the cost itself: a markup of 0 would be a straight
    publication of a private price, so it is treated as a bug rather than a
    configuration.
    """
    if not cost_cents or cost_cents <= 0:
        return None
    m = _line_markup(order_markup, salt)
    if m <= 0:
        m = markup_pct(order_markup)
    raw = cost_cents * (1 + m)
    step = max(100, PRICE_STEP_CENTS)
    return int(-(-raw // step) * step)


def browse(search: str = "", category: str = "") -> list[dict]:
    """Public catalogue. Never returns a cost.

    Takes no connection because it must not be reachable with one that has
    access to private suppliers -- it goes through public_prices(), which
    only ever sees public_ok rows.
    """
    from contextlib import closing
    import psycopg
    from psycopg.rows import dict_row

    url = os.getenv("DATABASE_URL", "")
    if not url:
        return []
    with closing(psycopg.connect(url, row_factory=dict_row)) as conn:
        sp.ensure_schema(conn)
        rows = sp.public_prices(conn, term=search, category=category, limit=120)
    out = []
    for r in rows:
        shown = customer_price(r["price_cents"], salt=r["item_number"] or r["description"])
        out.append({
            "item_number": r["item_number"],
            "description": r["description"],
            "uom": r["uom"],
            "supplier": r["supplier"],
            "price": shown,
            "stale": r["stale"],
        })
    return out


def categories() -> list[str]:
    from contextlib import closing
    import psycopg
    from psycopg.rows import dict_row

    url = os.getenv("DATABASE_URL", "")
    if not url:
        return []
    with closing(psycopg.connect(url, row_factory=dict_row)) as conn:
        sp.ensure_schema(conn)
        return sp.public_categories(conn)


def price_list(items: list[dict], order_markup: float | None = None) -> dict:
    """Price a customer's chosen list.

    items: [{item_number, description, uom, qty}]
    Prices come from the public catalogue only.
    """
    from contextlib import closing
    import psycopg
    from psycopg.rows import dict_row

    url = os.getenv("DATABASE_URL", "")
    if not url:
        return {"lines": [], "subtotal": 0, "count": 0}

    nums = [str(i.get("item_number", "")).strip() for i in items
            if str(i.get("item_number", "")).strip()]
    with closing(psycopg.connect(url, row_factory=dict_row)) as conn:
        sp.ensure_schema(conn)
        public = {r["item_number"]: r for r in sp.public_prices(conn, limit=1000)} \
            if not nums else None
        if nums:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.item_number, p.description, p.uom, p.price_cents, s.name
                    FROM material_supplier_prices p
                    JOIN material_suppliers s ON s.id = p.supplier_id
                    WHERE s.public_ok AND s.enabled AND p.price_cents > 0
                      AND p.item_number = ANY(%s)
                """, (nums,))
                public = {r["item_number"]: r for r in cur.fetchall()}

    lines, subtotal = [], 0
    for it in items:
        sku = str(it.get("item_number", "")).strip()
        try:
            qty = max(1, int(float(it.get("qty") or 1)))
        except (TypeError, ValueError):
            qty = 1
        p = public.get(sku)
        if not p:
            lines.append({"item_number": sku, "description": it.get("description", ""),
                          "uom": it.get("uom", "each"), "qty": qty,
                          "unit_price": None, "extended": None,
                          "unavailable": "Not currently priced — we'll confirm."})
            continue
        unit = customer_price(p["price_cents"], salt=sku, order_markup=order_markup)
        ext = unit * qty if unit else None
        if ext:
            subtotal += ext
        lines.append({"item_number": sku, "description": p["description"],
                      "uom": p["uom"], "qty": qty, "unit_price": unit,
                      "extended": ext, "supplier": p["name"], "unavailable": None})
    return {"lines": lines, "subtotal": subtotal, "count": len(lines),
            "generated": date.today().isoformat()}


def submission_creates_lead() -> bool:
    """Whether sending a list should raise a lead.

    Defaults true, because a list someone built is the strongest buying
    signal the site gets. Flip with SANDBOX_CREATES_LEAD=false to browse
    without anyone being contacted.
    """
    return os.getenv("SANDBOX_CREATES_LEAD", "true").strip().lower() in ("1", "true", "yes")