"""ABC Supply product + pricing client.

Why this exists
---------------
An itemized quote is only as honest as the prices behind it. `materials_catalog`
held 17 hand-seeded rows, so any quote generated from it was a guess with a
decimal point. ABC Supply is the one supplier of the four in play (ABC Supply /
Lansing, Lowe's, Richards) that exposes a real, documented API for contract
pricing, so it is the one that can make the numbers true.

Getting access is a business step, not a code step: the ABC API is restricted to
customers with an existing credit account, and enrollment is reviewed by their
team. See :func:`setup_checklist`. Until those credentials exist this module is
inert -- every call returns ``None``/``[]`` rather than raising, so nothing that
imports it breaks.

Transport notes, from https://apidocs.abcsupply.com/authorization-methods:

  - OAuth 2.0 client credentials (Individuals & Businesses track), HTTP Basic.
    Client Credentials is the right grant here: Shaun is the account holder, not
    a platform reselling to third parties. The Aggregator track explicitly
    *cannot* read pricing, which is precisely what we need.
  - Access tokens live 30 minutes and client-credentials has no refresh token,
    so :func:`_token` caches and re-mints proactively rather than discovering
    expiry via a 401.
  - Sandbox and production are different Okta tenants with different auth URLs;
    both are configurable so the integration can be proven without touching
    live pricing.

Scope requested is deliberately read-only-minimal plus pricing: we quote, we do
not order. ``order.write`` is *not* requested -- a quoting tool has no business
being able to spend the company's money.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_PROD = "https://auth.partners.abcsupply.com/oauth2/ausvvp0xuwGKLenYy357/v1/token"
AUTH_SANDBOX = "https://sandbox.auth.partners.abcsupply.com/oauth2/aus1vp07knpuqf6Xz0h8/v1/token"

# Read-only scopes: catalog + contract pricing + account/location context.
# No order.write -- see module docstring.
SCOPES_READONLY = [
    "location.read",
    "product.read",
    "account.read",
    "pricing.read",
]

DEFAULT_BASE_PROD = "https://apis.abcsupply.com"
DEFAULT_BASE_SANDBOX = "https://sandbox.apis.abcsupply.com"

# Tokens are 30 min. Re-mint at 25 so a long-running quote build cannot be
# killed by a token expiring mid-loop.
_TOKEN_TTL = 25 * 60

_timeout = float(os.getenv("ABC_API_TIMEOUT", "20") or 20)
_cache: dict = {"token": None, "expires_at": 0.0, "base": None}


def configured() -> bool:
    """True when credentials are present. Checked before any network call."""
    return bool(os.getenv("ABC_CLIENT_ID") and os.getenv("ABC_CLIENT_SECRET"))


def _auth_url() -> str:
    if (os.getenv("ABC_SANDBOX", "") or "").lower() in ("1", "true", "yes", "on"):
        return os.getenv("ABC_AUTH_URL", AUTH_SANDBOX)
    return os.getenv("ABC_AUTH_URL", AUTH_PROD)


def base_url() -> str:
    if (os.getenv("ABC_SANDBOX", "") or "").lower() in ("1", "true", "yes", "on"):
        return os.getenv("ABC_BASE_URL", DEFAULT_BASE_SANDBOX)
    return os.getenv("ABC_BASE_URL", DEFAULT_BASE_PROD)


def setup_checklist() -> list[str]:
    """What the owner still has to do before this module does anything.

    Surfaced as text rather than hidden in docs so it can be rendered in the
    app and checked off, since it is a multi-week business process.
    """
    return [
        "Open a credit account with ABC Supply Customer Financial Services "
        "(credit application; contractors only -- they do not sell to homeowners).",
        "Create a myABCSupply account for the company.",
        "Complete the ABC API Enrollment Form at apidocs.abcsupply.com and accept "
        "the Terms and Conditions; their API team reviews and approves.",
        "On approval, set ABC_CLIENT_ID / ABC_CLIENT_SECRET, and ABC_BILL_TO to the "
        "Bill-To account number used for pricing.",
        "Set ABC_SANDBOX=1 first to validate against the sandbox tenant before "
        "pointing at live contract pricing.",
    ]


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def _token(force: bool = False) -> str:
    """Client-credentials token, cached with a safety margin."""
    if not configured():
        return ""
    base = base_url()
    # A sandbox/production switch invalidates the cached token: they are
    # different tenants, so the cached one will not authenticate on the other.
    if not force and _cache["token"] and _cache["base"] == base and time.time() < _cache["expires_at"]:
        return _cache["token"]

    cid = os.getenv("ABC_CLIENT_ID", "")
    secret = os.getenv("ABC_CLIENT_SECRET", "")
    raw = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": " ".join(SCOPES_READONLY),
    }).encode()
    req = urllib.request.Request(
        _auth_url(),
        data=body,
        headers={
            "Authorization": f"Basic {raw}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=_timeout) as resp:
        payload = json.loads(resp.read().decode() or "{}")
    token = payload.get("access_token") or ""
    _cache["token"] = token
    _cache["base"] = base
    _cache["expires_at"] = time.time() + _TOKEN_TTL
    return token


def _request(method: str, path: str, body=None, params=None, retry_auth: bool = True):
    """One API call. Returns parsed JSON, or ``None`` on any failure.

    Fails soft by design: a pricing lookup that raises would turn a vendor
    outage into a 500 on a customer-facing quote page. Callers treat ``None`` as
    "price unknown" and fall back to the curated catalog.
    """
    token = _token()
    if not token:
        return None
    url = f"{base_url()}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, '')})}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as resp:
            raw = resp.read().decode()
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and retry_auth:
            # Token minted early but rejected; force one re-mint and retry once.
            _token(force=True)
            return _request(method, path, body=body, params=params, retry_auth=False)
        print(f"[abc] {method} {path} -> HTTP {exc.code}", flush=True)
        return None
    except Exception as exc:
        print(f"[abc] {method} {path} failed: {exc}", flush=True)
        return None


# --------------------------------------------------------------------------
# product + pricing
# --------------------------------------------------------------------------

def search_items(query: str = "", *, category: str = "", page_size: int = 50,
                 skip: int = 0):
    """POST /search/items -- catalog search by attribute/keyword."""
    if not configured():
        return []
    body = {
        "searchText": query,
        "pageSize": int(page_size),
        "startIndex": int(skip),
    }
    if category:
        body["productCategory"] = category
    out = _request("POST", "/search/items", body=body) or {}
    items = out.get("items") or out.get("Items") or []
    return items if isinstance(items, list) else []


def get_item(item_number: str):
    """GET /item/{itemNumber}"""
    if not configured() or not item_number:
        return None
    return _request("GET", f"/item/{urllib.parse.quote(str(item_number))}")


def price_items(item_numbers, *, bill_to: str = ""):
    """POST /prices -- real-time *customer-specific* pricing.

    This is the whole point of the integration. Bill-To is not optional in
    practice: ABC returns list/contract pricing keyed to the account, and
    without it the numbers are not ours.
    """
    if not configured():
        return []
    bill_to = bill_to or os.getenv("ABC_BILL_TO", "")
    nums = [str(n) for n in (item_numbers or []) if str(n).strip()]
    if not nums:
        return []
    out = _request("POST", "/prices", body={
        "billToNumber": bill_to,
        "itemNumbers": nums,
    }) or {}
    prices = out.get("prices") or out.get("Prices") or out.get("items") or []
    return prices if isinstance(prices, list) else []


def _bill_scoped(kind: str):
    """recent / frequent / favorite items for the Bill-To account.

    ``frequent`` is the highest-value endpoint in the catalogue for quoting: it
    is what this company actually buys, ranked by purchase frequency, which
    makes a repeat job's price sheet almost free.
    """
    if not configured():
        return []
    bill_to = os.getenv("ABC_BILL_TO", "")
    if not bill_to:
        return []
    out = _request("GET", f"/api/product/v1/items/{urllib.parse.quote(bill_to)}/{kind}") or {}
    items = out.get("items") or out.get("Items") or []
    return items if isinstance(items, list) else []


def frequent_items():
    return _bill_scoped("frequents")


def recent_items():
    return _bill_scoped("recents")


def favorite_items():
    return _bill_scoped("favorites")


def availability(item_number: str, *, branch: str = "", zipcode: str = ""):
    """Where we can actually collect it. A price you cannot source is not a quote."""
    if not configured() or not item_number:
        return []
    out = _request("GET",
                   f"/availability/items/{urllib.parse.quote(str(item_number))}/branches",
                   params={"branchNumber": branch, "zipCode": zipcode}) or {}
    rows = out.get("branches") or out.get("Branches") or []
    return rows if isinstance(rows, list) else []


def hierarchy():
    """GET /hierarchy -- product tree, for the vendor/brand gallery."""
    if not configured():
        return []
    out = _request("GET", "/hierarchy") or {}
    rows = out.get("hierarchy") or out.get("items") or out.get("categories") or []
    return rows if isinstance(rows, list) else ([out] if isinstance(out, dict) and out else [])


# --------------------------------------------------------------------------
# mapping into materials_catalog
# --------------------------------------------------------------------------

def price_to_cents(entry) -> int | None:
    """Normalise a /prices row to integer cents.

    ABC returns decimal strings like "142.99". Returning ``None`` rather than 0
    matters: a zero price would silently become a $0 line on a customer quote,
    and 0 is a real (and wrong) number in a way that "unknown" is not.
    """
    if entry is None:
        return None
    if isinstance(entry, (int, float)):
        return int(round(float(entry) * 100))
    if not isinstance(entry, dict):
        return None
    raw = None
    for key in ("price", "Price", "unitPrice", "netPrice", "amount"):
        if entry.get(key) not in (None, ""):
            raw = entry[key]
            break
    if raw in (None, ""):
        return None
    try:
        return int(round(float(str(raw).replace("$", "").replace(",", "")) * 100))
    except (TypeError, ValueError):
        return None


def item_to_catalog_row(item: dict, price_cents: int | None = None) -> dict:
    """ABC item -> a `materials_catalog`-shaped row.

    Writes straight into the existing table's vocabulary: `store` carries the
    supplier, `brand` feeds the gallery, `sku` carries the ABC item number so a
    later refresh can re-price by key instead of fuzzy-matching on name.
    """
    item = item or {}
    brand = item.get("brandName") or item.get("brand") or ""
    desc = item.get("description") or item.get("itemDescription") or ""
    sku = item.get("itemNumber") or item.get("itemNbr") or ""
    unit = item.get("unitOfMeasure") or item.get("uom") or "each"
    category = item.get("categoryDescription") or item.get("productCategory") or ""
    return {
        "store": "ABC Supply",
        "brand": brand,
        "category": str(category)[:80],
        "name": str(desc)[:400],
        "model": item.get("manufacturerModel") or item.get("modelNumber") or "",
        "sku": str(sku),
        "style": item.get("style") or item.get("colorName") or "",
        "color": item.get("colorName") or item.get("color") or "",
        "unit": str(unit)[:40],
        "price_cents": int(price_cents) if price_cents is not None else 0,
        "notes": "",
    }


def refresh_catalog(db, *, limit: int = 500) -> dict:
    """Pull frequent + search results into `materials_catalog`.

    Upserts by SKU so re-running refreshes prices instead of duplicating rows.
    Returns a summary rather than raising: a supplier outage must not look like
    a broken app.
    """
    summary = {"items": 0, "priced": 0, "skipped": 0, "error": ""}
    if not configured():
        summary["error"] = "ABC_SUPPLY_NOT_CONFIGURED"
        return summary

    items = frequent_items()
    if not items:
        items = search_items(page_size=limit)
    items = items[:int(limit)]
    if not items:
        summary["error"] = "no items returned"
        return summary

    skus = [i.get("itemNumber") or i.get("itemNbr") for i in items if isinstance(i, dict)]
    skus = [str(s) for s in skus if s]
    price_by_sku = {}
    for row in price_items(skus):
        if isinstance(row, dict):
            key = str(row.get("itemNumber") or row.get("itemNbr") or "")
            cents = price_to_cents(row)
            if key and cents is not None:
                price_by_sku[key] = cents

    try:
        with db.cursor() as cur:
            for item in items:
                row = item_to_catalog_row(item, price_by_sku.get(row_sku(item)))
                if not row["sku"] or not row["name"]:
                    summary["skipped"] += 1
                    continue
                # Explicit upsert rather than ON CONFLICT (sku): materials_catalog
                # has no unique index on sku (only (category, brand, enabled)), and
                # adding one to a table holding live pricing risks failing on
                # pre-existing duplicate or blank SKUs -- turning a catalog refresh
                # into a schema migration.
                cur.execute(
                    "SELECT id FROM materials_catalog WHERE sku = %s LIMIT 1;",
                    (row["sku"],),
                )
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        "UPDATE materials_catalog SET price_cents = %s, brand = %s, "
                        "name = %s, store = %s, updated_at = NOW() WHERE id = %s;",
                        (row["price_cents"], row["brand"], row["name"], row["store"],
                         existing["id"]),
                    )
                else:
                    cur.execute(
                        "INSERT INTO materials_catalog "
                        "(store, brand, category, name, model, sku, style, color, unit, "
                        " price_cents, price_high_cents, notes, enabled) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,'',TRUE);",
                        (row["store"], row["brand"], row["category"], row["name"],
                         row["model"], row["sku"], row["style"], row["color"],
                         row["unit"], row["price_cents"]),
                    )
                summary["items"] += 1
                if row["price_cents"]:
                    summary["priced"] += 1
        db.commit()
    except Exception as exc:
        db.rollback()
        summary["error"] = str(exc)
    return summary


def row_sku(item: dict) -> str:
    item = item or {}
    return str(item.get("itemNumber") or item.get("itemNbr") or "")