"""Lowe's product catalog client.

Status: credentials not yet available. This is a ready-to-wire adapter, not a
working integration, and it says so rather than pretending otherwise.

Do not confuse this with a Lowe's shopping account
----------------------------------------------------
A retail or Pro Scan account gives you trade pricing on the website and in the
Lowe's app. It grants **no** API access. The catalog is a separate program:
developer.lowes.com, where a business registers its organization, accepts a
partner agreement, and is issued `X-Partner-Id` credentials. Opening a shopping
account does not advance that at all, so if the goal is the API, the enrollment
to pursue is the Developer Hub one.

What the API actually offers
----------------------------
Two delivery paths, per their Product Catalog docs:

  - **File feeds over SFTP** (daily CSV): National or Local product content, plus
    National or Local price & inventory. This is the mature path and how most
    partners start. Configurable refresh cadence.
  - **REST API** (`developer.lowes.com`): per-product detail, optionally
    localized by `storeId` / `zipCode`, plus contract pricing where the caller is
    an eligible Pro customer.

Pricing is localized: **storeId is required for a local price**. A national price
with no store context is the wrong number for a Hampton Roads customer.

Role in this system
-------------------
Lowe's is the *customer reference price*, not our cost. ABC Supply contract
pricing (see abc_supply.py) is what a quote is built from; Lowe's is the
"what this runs at retail today" line and the source for owner-supplied / DIY
materials. A quote total must never be derived from a Lowe's number.

Auth is deliberately configurable and marked unverified: their portal issues
`X-Partner-Id`, and whether the session additionally needs a bearer token or an
API key is something to confirm against the partner's own credentials rather
than guess at. Until someone has portal access, :func:`configured()` is False
and every call returns empty.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

# From the endpoints page / OpenAPI references. Stage is the sandbox tenant.
DEFAULT_BASE = "https://apis.lowes.com"
DEFAULT_BASE_STAGE = "https://apis-b2b-stage.lowes.com"

# Fields we actually consume from the catalog feed/API.
FIELD_MAP = {
    "item_number": ("itemNumber", "Item Number", "item_number"),
    "model": ("modelNumber", "Model", "model"),
    "upc": ("upc", "UPC"),
    "brand": ("brandName", "Brand", "brand"),
    "title": ("productName", "Title", "name"),
    "category": ("productCategory", "Category", "category_path"),
    "price": ("price", "Price", "map_price", "unitPrice"),
    "image": ("primaryImageUrl", "primary_image_url"),
}


def configured() -> bool:
    return bool(os.getenv("LOWES_PARTNER_ID"))


def base_url() -> str:
    default = DEFAULT_BASE_STAGE if _stage() else DEFAULT_BASE
    return os.getenv("LOWES_BASE_URL", default)


def _stage() -> bool:
    return (os.getenv("LOWES_STAGE", "") or "").lower() in ("1", "true", "yes", "on")


def setup_checklist() -> list[str]:
    """What has to happen before this does anything."""
    return [
        "Register the company at developer.lowes.com and accept the partner "
        "agreement (this is the step a shopping account does NOT cover).",
        "Create the app in the portal and obtain the X-Partner-Id credential.",
        "Confirm with the Lowe's contact whether product detail additionally "
        "requires a bearer token or API key, and set LOWES_BEARER_TOKEN if so.",
        "Confirm SFTP feed access if you want the daily catalog CSV rather than "
        "REST calls (set LOWES_FEED_SFTP_HOST / LOWES_FEED_USER).",
        "Set LOWES_STORE_ID for the store whose pricing customers will actually "
        "see -- a national price without store context is the wrong number.",
    ]


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def _headers() -> dict:
    h = {
        "X-Partner-Id": os.getenv("LOWES_PARTNER_ID", ""),
        "Accept": "application/json",
    }
    bearer = os.getenv("LOWES_BEARER_TOKEN", "")
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    key = os.getenv("LOWES_API_KEY", "")
    if key:
        h["Ocp-Apim-Subscription-Key"] = key
    return h


def _get(path: str, params: dict | None = None):
    if not configured():
        return None
    url = f"{base_url()}{path}"
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    if clean:
        url = f"{url}?{urllib.parse.urlencode(clean)}"
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=float(os.getenv("LOWES_API_TIMEOUT", "20") or 20)) as resp:
            raw = resp.read().decode()
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        print(f"[lowes] GET {path} -> HTTP {exc.code}", flush=True)
        return None
    except Exception as exc:
        print(f"[lowes] GET {path} failed: {exc}", flush=True)
        return None


def _field(entry: dict, logical: str):
    """Feed headers and API payloads do not agree on key names; try both."""
    if not isinstance(entry, dict):
        return None
    for key in FIELD_MAP.get(logical, ()):
        if entry.get(key) not in (None, ""):
            return entry[key]
    return None


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------

def get_product(product_id: str, *, store_id: str = "", zipcode: str = ""):
    """GET /api/v1/products/{id}.

    ``store_id`` is what turns a national price into the price that customer
    would actually pay at their nearest Lowe's. Omitting it silently returns a
    national price, which is the number most likely to be wrong.
    """
    if not configured() or not product_id:
        return None
    return _get(f"/api/v1/products/{urllib.parse.quote(str(product_id))}", {
        "storeId": store_id or os.getenv("LOWES_STORE_ID", ""),
        "zipCode": zipcode,
    })


def search_products(keyword: str = "", *, category: str = "", store_id: str = "",
                    zipcode: str = "", limit: int = 50) -> list[dict]:
    """Keyword/category lookup.

    Exact search-path shape is not documented in the public portal summary and
    varies by partner capability; when the partner's own docs are to hand this
    should be pointed at the real path rather than guessed.
    """
    if not configured():
        return []
    out = _get("/api/v1/products", {
        "keyword": keyword, "category": category,
        "storeId": store_id or os.getenv("LOWES_STORE_ID", ""),
        "zipCode": zipcode, "limit": int(limit),
    }) or {}
    items = out.get("products") or out.get("items") or out.get("value") or []
    return items if isinstance(items, list) else []


def price_to_cents(entry) -> int | None:
    """Normalise a Lowe's price to integer cents, or None when unknown.

    None rather than 0 for the same reason as abc_supply: a 0 becomes a real $0
    line on a customer's quote, and "unknown" is not the same thing as "free".
    """
    raw = _field(entry, "price") if isinstance(entry, dict) else entry
    if raw in (None, "", {}):
        return None
    if isinstance(raw, (int, float)):
        return int(round(float(raw) * 100))
    try:
        return int(round(float(str(raw).replace("$", "").replace(",", "")) * 100))
    except (TypeError, ValueError):
        return None


def product_to_catalog_row(product: dict, price_cents: int | None = None) -> dict:
    """Lowe's product -> a `materials_catalog`-shaped row (same as abc_supply)."""
    product = product or {}
    sku = str(_field(product, "item_number") or _field(product, "upc") or "")
    return {
        "store": "Lowe's",
        "brand": str(_field(product, "brand") or ""),
        "category": str(_field(product, "category") or "")[:80],
        "name": str(_field(product, "title") or "")[:400],
        "model": str(_field(product, "model") or ""),
        "sku": sku,
        "style": "",
        "color": "",
        "unit": "each",
        # Retail reference price, deliberately NOT a quote input. abc_supply.py
        # holds our cost; this is the "what it costs the customer at Lowe's".
        "price_cents": int(price_cents) if price_cents is not None else 0,
        "notes": "retail reference price",
    }


def refresh_catalog(db, *, limit: int = 500) -> dict:
    """Upsert Lowe's reference prices into materials_catalog.

    Marked in ``notes`` as a retail reference so a later quote can tell a
    reference price from a cost. Explicit upsert rather than ON CONFLICT(sku)
    for the same reason as abc_supply: no unique index on that column.
    """
    summary = {"items": 0, "priced": 0, "skipped": 0, "error": ""}
    if not configured():
        summary["error"] = "LOWES_NOT_CONFIGURED"
        return summary
    products = search_products(limit=limit)[:int(limit)]
    if not products:
        summary["error"] = "no products returned"
        return summary
    try:
        with db.cursor() as cur:
            for product in products:
                row = product_to_catalog_row(product, price_to_cents(product))
                if not row["sku"] or not row["name"]:
                    summary["skipped"] += 1
                    continue
                cur.execute("SELECT id FROM materials_catalog WHERE sku = %s LIMIT 1;",
                            (row["sku"],))
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        "UPDATE materials_catalog SET price_cents = %s, brand = %s, "
                        "name = %s, store = %s, notes = %s, updated_at = NOW() WHERE id = %s;",
                        (row["price_cents"], row["brand"], row["name"], row["store"],
                         row["notes"], existing["id"]),
                    )
                else:
                    cur.execute(
                        "INSERT INTO materials_catalog (store, brand, category, name, model, "
                        "sku, style, color, unit, price_cents, price_high_cents, notes, enabled) "
                        "VALUES (%s,%s,%s,%s,%s,%s,'','',%s,%s,NULL,%s,TRUE);",
                        (row["store"], row["brand"], row["category"], row["name"],
                         row["model"], row["sku"], row["unit"], row["price_cents"], row["notes"]),
                    )
                summary["items"] += 1
                if row["price_cents"]:
                    summary["priced"] += 1
        db.commit()
    except Exception as exc:
        db.rollback()
        summary["error"] = str(exc)
    return summary