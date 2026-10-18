"""Multi-vendor material pricing.

There is no single "get me a lumber price" API. Each supplier is reached a
different way, so this module keys everything off a supplier record that says
how that vendor is reached:

  api    -- a real API we authenticate against. ABC Supply is the one we
            have credentials-shaped access to (abc_supply.py).
  feed   -- a URL or emailed file that returns CSV/TSV of item -> price.
            This is how local yards and independent distributors are reached.
            They publish no API; what they publish is a price sheet.
  manual -- a last resort, kept so a vendor can be added before its feed is
            wired. Prices still carry an as-of date and go stale visibly.

The rule the UI depends on: EVERY price carries when it was observed, and a
price older than MAX_AGE_DAYS is marked stale rather than quietly used. A
vendor's emailed sheet is a snapshot, not a live feed, and a takeoff built on
a three-month-old sheet is wrong in a way nothing on screen would otherwise
reveal.

On comparing vendors: this module returns one price per supplier per item and
leaves the comparison to the caller. ABC Supply's terms forbid using its API
to build price comparison across competitors, so abc_supply prices are
labelled as ABC prices for ABC items and are not fed into a cross-vendor
comparison table.
"""
from __future__ import annotations

import csv
import datetime
import re
import io
import json
import os
import urllib.error
import urllib.request

import abc_supply

UA = "Buildstack-Materials/1.0"

# A price older than this is shown as stale. Feeds that refresh daily go
# stale in a week; a quarterly price book goes stale in a month.
MAX_AGE_DAYS = int(os.getenv("MATERIALS_STALE_DAYS", "30"))

SOURCE_TYPES = ["api", "feed", "manual"]

# Column names suppliers actually use for price sheets. Matched case- and
# punctuation-insensitively against a pasted or fetched file.
# Ordered most-specific first. "item" sits last in both lists because some
# sheets use it for the SKU and others for the description -- guessing wrong
# swaps the two fields, so the generic name only wins when nothing better
# matched.
_DESC_KEYS = ("description", "productdescription", "itemdescription", "productname",
              "itemname", "desc", "product", "name", "item")
_SKU_KEYS = ("itemnumber", "itemno", "item_number", "sku", "partnumber", "part",
             "upc", "code", "item")
_PRICE_KEYS = ("price", "unitprice", "unitcost", "cost", "sellprice", "yourprice", "netprice")
_UOM_KEYS = ("uom", "unit", "unitofmeasure", "unitofmesure", "uomdescription", "measure")


class SupplierError(RuntimeError):
    pass


def _key(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _num(v):
    if v in (None, ""):
        return None
    s = str(v).strip().replace("$", "").replace(",", "")
    if s.startswith("(") and s.endswith(")"):      # accounting negative
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def _pick(row: dict, keys: tuple) -> str:
    norm = {_key(k): v for k, v in row.items()}
    for k in keys:
        if norm.get(_key(k)) not in (None, ""):
            return str(norm[_key(k)]).strip()
    return ""


# --- suppliers ---------------------------------------------------------------
def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS material_suppliers (
                id SERIAL PRIMARY KEY,
                name VARCHAR(120) NOT NULL,
                kind VARCHAR(20) NOT NULL DEFAULT 'feed',
                feed_url TEXT,
                feed_auth_header VARCHAR(120),
                feed_token VARCHAR(255),
                default_uom VARCHAR(40) DEFAULT 'each',
                public_ok BOOLEAN NOT NULL DEFAULT FALSE,
                category VARCHAR(60) NOT NULL DEFAULT '',
                company VARCHAR(40) DEFAULT 'construction',
                enabled BOOLEAN DEFAULT TRUE,
                last_synced_at TIMESTAMP WITH TIME ZONE,
                last_sync_note TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("ALTER TABLE material_suppliers ADD COLUMN IF NOT EXISTS feed_url TEXT;")
        cur.execute("ALTER TABLE material_suppliers ADD COLUMN IF NOT EXISTS feed_auth_header VARCHAR(120);")
        cur.execute("ALTER TABLE material_suppliers ADD COLUMN IF NOT EXISTS feed_token VARCHAR(255);")
        cur.execute("ALTER TABLE material_suppliers ADD COLUMN IF NOT EXISTS default_uom VARCHAR(40) DEFAULT 'each';")
        cur.execute("ALTER TABLE material_suppliers ADD COLUMN IF NOT EXISTS public_ok BOOLEAN NOT NULL DEFAULT FALSE;")
        cur.execute("ALTER TABLE material_suppliers ADD COLUMN IF NOT EXISTS category VARCHAR(60) NOT NULL DEFAULT '';")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_material_suppliers_public ON material_suppliers(public_ok, category);")
        # Deliberately default FALSE. account-specific distributor pricing is
        # confidential, and the safe default means a new supplier is private
        # until somebody consciously decides otherwise. Flipping it to true is
        # a deliberate act, not an oversight.
        cur.execute("""
            COMMENT ON COLUMN material_suppliers.public_ok IS
            'May these prices appear on a public customer-facing page? Retail '
            'shelf prices: yes. Account-specific contract pricing: no.'
        """)
        cur.execute("ALTER TABLE material_suppliers ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMP WITH TIME ZONE;")
        cur.execute("ALTER TABLE material_suppliers ADD COLUMN IF NOT EXISTS last_sync_note TEXT;")
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_material_suppliers_name
                ON material_suppliers(company, lower(name))
        """)
        # One row per supplier per item. observed_at is what makes staleness
        # possible; without it a price has no age and can never be flagged.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS material_supplier_prices (
                id SERIAL PRIMARY KEY,
                supplier_id INTEGER NOT NULL REFERENCES material_suppliers(id) ON DELETE CASCADE,
                item_number VARCHAR(80) NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                uom VARCHAR(40) DEFAULT 'each',
                price_cents BIGINT NOT NULL DEFAULT 0,
                observed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                company VARCHAR(40) DEFAULT 'construction'
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_material_supplier_prices
                ON material_supplier_prices(supplier_id, item_number, uom)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_material_supplier_prices_obs
                ON material_supplier_prices(supplier_id, observed_at DESC)
        """)
    conn.commit()


def list_suppliers(conn, company: str = "construction", include_disabled: bool = False) -> list[dict]:
    sql = ["SELECT * FROM material_suppliers WHERE company = %s"]
    params: list = [company]
    if not include_disabled:
        sql.append("AND enabled")
    sql.append("ORDER BY name")
    with conn.cursor() as cur:
        cur.execute(" ".join(sql), params)
        return cur.fetchall()


def upsert_supplier(conn, name: str, kind: str = "feed", feed_url: str = "",
                    default_uom: str = "each", supplier_id: int | None = None,
                    feed_auth_header: str = "", feed_token: str = "",
                    public_ok: bool = False, category: str = "",
                    company: str = "construction") -> int:
    if kind not in SOURCE_TYPES:
        raise ValueError(f"kind must be one of {SOURCE_TYPES}")
    if not str(name).strip():
        raise ValueError("supplier name is required")
    if kind == "feed" and not str(feed_url).strip():
        raise ValueError("a feed supplier needs a URL")
    if kind == "api" and public_ok:
        # An authenticated API price is per-account by definition. Allowing a
        # supplier to be both api and public_ok is how confidential pricing
        # ends up on a public page.
        raise ValueError(
            "an api supplier cannot be public: api prices are account-specific. "
            "ABC Supply and any contract pricing are confidential by contract.")
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO material_suppliers
                (name, kind, feed_url, default_uom, company, feed_auth_header,
                 feed_token, public_ok, category)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (company, lower(name)) DO UPDATE SET
                kind = EXCLUDED.kind, feed_url = EXCLUDED.feed_url,
                default_uom = EXCLUDED.default_uom,
                feed_auth_header = EXCLUDED.feed_auth_header,
                feed_token = EXCLUDED.feed_token,
                public_ok = EXCLUDED.public_ok,
                category = EXCLUDED.category, enabled = TRUE
            RETURNING id
        """, (name.strip(), kind, feed_url.strip() or None, default_uom or "each",
              company, feed_auth_header.strip() or None, feed_token.strip() or None,
              bool(public_ok), (category or "").strip()))
        sid = cur.fetchone()["id"]
    conn.commit()
    return sid


def delete_supplier(conn, supplier_id: int, company: str = "construction") -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM material_suppliers WHERE id = %s AND company = %s RETURNING id",
                    (supplier_id, company))
        ok = cur.fetchone() is not None
    conn.commit()
    return ok


# --- parsing -----------------------------------------------------------------
def parse_price_sheet(text: str, default_uom: str = "each") -> list[dict]:
    """Read a vendor price sheet into {item_number, description, uom, price_cents}.

    Deliberately forgiving about column naming, because "Unit Price",
    "unitprice", "Your Price" and "NET PRICE" are all real headers and no two
    vendors agree. A sheet with no recognisable price column raises rather
    than importing every row at zero -- a silently zeroed price book is worse
    than no price book.
    """
    if not text or not text.strip():
        raise SupplierError("That file is empty.")
    lines = [l for l in text.replace("\r\n", "\n").split("\n") if l.strip()]
    if not lines:
        raise SupplierError("That file is empty.")

    delim = ","
    try:
        delim = csv.Sniffer().sniff(lines[0], delimiters=",;\t|").delimiter
    except csv.Error:
        delim = "," if "," in lines[0] else ("\t" if "\t" in lines[0] else ";")

    rows = list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delim))
    if not rows:
        raise SupplierError("No rows found. Is this a CSV or TSV export?")
    fields = list(rows[0].keys())
    if not any(_key(f) in _PRICE_KEYS for f in fields):
        raise SupplierError(
            "No price column found. Looked for one of: "
            + ", ".join(_PRICE_KEYS) + f". Columns present: {', '.join(fields[:12])}")

    out = []
    for r in rows:
        price = _num(_pick(r, _PRICE_KEYS))
        if price is None or price < 0:
            continue
        desc = _pick(r, _DESC_KEYS)
        sku = _pick(r, _SKU_KEYS)
        if not desc and not sku:
            continue
        # A sheet keyed only on "Item" resolves that one column as the
        # description. Keep it as the SKU too, otherwise the price has no
        # item number and can never be looked up again.
        if not sku and desc:
            sku = desc[:80]
        if not desc and sku:
            desc = sku[:2000]
        out.append({"item_number": sku[:80], "description": desc[:2000],
                    "uom": (_pick(r, _UOM_KEYS) or default_uom or "each")[:40],
                    "price_cents": int(round(price * 100))})
    if not out:
        raise SupplierError("Read the file but found no rows with a usable price.")
    return out


def save_prices(conn, supplier_id: int, rows: list[dict], company: str = "construction") -> int:
    with conn.cursor() as cur:
        for r in rows:
            cur.execute("""
                INSERT INTO material_supplier_prices
                    (supplier_id, item_number, description, uom, price_cents, company)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (supplier_id, item_number, uom) DO UPDATE SET
                    description = EXCLUDED.description,
                    price_cents = EXCLUDED.price_cents,
                    observed_at = now()
            """, (supplier_id, r["item_number"], r["description"], r["uom"],
                  r["price_cents"], company))
    conn.commit()
    return len(rows)


def _mark(conn, supplier_id: int, note: str, ok: bool = True) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE material_suppliers SET last_synced_at = now(), "
                    "last_sync_note = %s WHERE id = %s", (note[:500], supplier_id))
    conn.commit()


# --- sync --------------------------------------------------------------------
def _fetch(url: str, auth_header: str = "", token: str = "") -> str:
    if not str(url).startswith(("http://", "https://")):
        raise SupplierError("feed URL must be http or https")
    headers = {"User-Agent": UA}
    if auth_header and token:
        headers[auth_header] = token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=40) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise SupplierError(f"HTTP {e.code} from the feed") from e
    except urllib.error.URLError as e:
        raise SupplierError(f"could not reach the feed: {e.reason}") from e


def tracked_skus(conn, supplier_name: str, company: str = "construction",
                 limit: int = 200) -> list[dict]:
    """The store SKUs already recorded in `materials_catalog` for a supplier.

    A partner retail API prices an item the caller names; it does not hand over
    a catalogue. So the sync prices the SKUs we already track for that store,
    which is where the real Home Depot/Lowe's item numbers live.

    Matching walks from the most specific name to the least, because a supplier
    row is often named more precisely than the `store` column it prices. An
    earlier version stripped stop words to derive a brand, which deleted
    "Home Depot" entirely -- both words are stop words -- and silently widened
    the query to every tracked SKU.
    """
    raw = " ".join(str(supplier_name or "").split())
    if not raw:
        return []

    base = """SELECT sku, name, unit FROM materials_catalog
              WHERE sku <> '' AND enabled"""
    candidates = [raw]
    trimmed = re.split(r"\s+[-–—:]\s+|\s+\|\s+", raw)
    candidates += [t for t in trimmed if t and t != raw]
    words = [w for w in re.split(r"[\s_\-]+", raw.lower()) if w]
    if len(words) > 1:
        candidates.append(" ".join(words[:-1]))

    seen: set = set()
    for pattern in candidates:
        pattern = pattern.strip()
        if not pattern or pattern.lower() in seen:
            continue
        seen.add(pattern.lower())
        with conn.cursor() as cur:
            cur.execute(f"{base} AND LOWER(store) LIKE %s ORDER BY id LIMIT %s",
                        (f"%{pattern.lower()}%", max(1, min(int(limit), 1000))))
            rows = cur.fetchall()
        if rows:
            return [{"sku": r["sku"], "description": r["name"], "uom": r["unit"]}
                    for r in rows]
    return []


def sync_supplier(conn, supplier_id: int, company: str = "construction",
                  sheet_text: str = "") -> dict:
    """Refresh one supplier's prices.

    sheet_text short-circuits the fetch, which is how a price sheet that
    arrives by email gets loaded: the file is handed in rather than fetched.
    """
    sup = next((s for s in list_suppliers(conn, company, True) if s["id"] == supplier_id), None)
    if not sup:
        raise SupplierError("supplier not found")

    try:
        if sup["kind"] == "api":
            if sup["name"].lower().startswith("abc"):
                if not abc_supply.configured():
                    raise SupplierError("ABC Supply needs " + ", ".join(abc_supply.missing()))
                freq = abc_supply.frequent_items(50)
                rows = [{"item_number": f["itemNumber"], "description": f["description"],
                         "uom": f.get("uom") or "each", "price_cents": 0} for f in freq]
                # ABC prices are per-account and per-quantity, so a catalogue
                # scrape cannot produce them. Items land with price 0 and
                # observed_at unchanged until price_items() runs for a real qty.
                _mark(conn, supplier_id, f"{len(rows)} frequent items synced; "
                                        "prices are priced per order, not per catalogue")
                return {"supplier": sup["name"], "rows": len(rows), "priced": 0,
                        "note": "items listed; run a quote to get real prices"}
            # Other retailers dispatch to supplier_adapters. This has to sit
            # above the fallback, or every retail supplier dies on
            # "no API adapter" and the adapter module never runs.
            try:
                import supplier_adapters
                adapter = supplier_adapters.adapter_for(sup["name"])
            except Exception as e:
                _mark(conn, supplier_id, f"failed: {e}", ok=False)
                raise SupplierError(f"supplier adapter unavailable: {e}") from e
            if adapter is None:
                raise SupplierError(f"no API adapter for '{sup['name']}'")
        else:
            adapter = None

        if adapter is not None:
            try:
                if not adapter.configured():
                    raise SupplierError(
                        f"{adapter.name} is not configured; missing "
                        + ", ".join(adapter.missing()))
                # Neither retailer exposes a bulk catalogue on a partner API, so
                # price the SKUs already tracked in materials_catalog rather
                # than inventing an enumeration endpoint.
                candidates = tracked_skus(conn, sup["name"], company=company)
                if not candidates:
                    _mark(conn, supplier_id,
                          f"{adapter.name} configured but no tracked SKUs to price",
                          ok=False)
                    raise SupplierError(
                        f"{sup['name']}: no SKUs in materials_catalog for this store. "
                        f"Add the items first, then sync.")
                # Adapters read `itemNumber` (abc_supply's field name), while
                # tracked_skus returns `sku`. Map explicitly: leaving this to a
                # name coincidence wrote every row at 0 and reported "0/3
                # priced" as if the supplier had no prices at all.
                priced = adapter.price_items(
                    [{"itemNumber": row["sku"], "description": row["description"],
                      "uom": row["uom"]} for row in candidates],
                    purpose="catalogue")
                merged = []
                for row, p in zip(candidates, priced):
                    merged.append({
                        "item_number": row["sku"],
                        "description": p.get("description") or row["description"],
                        "uom": p.get("uom") or row.get("uom") or "each",
                        "price_cents": int(p.get("unit_price") or 0),
                    })
                n = save_prices(conn, supplier_id, merged, company)
                unpriced = sum(1 for m in merged if not m["price_cents"])
                _mark(conn, supplier_id, f"{n - unpriced}/{n} priced via {adapter.name}")
                return {"supplier": sup["name"], "rows": n,
                        "priced": n - unpriced, "unpriced": unpriced}
            except SupplierError:
                raise
            except Exception as e:
                _mark(conn, supplier_id, f"failed: {e}", ok=False)
                raise SupplierError(f"{sup['name']} sync failed: {e}") from e

        text = sheet_text or _fetch(sup["feed_url"], sup["feed_auth_header"] or "",
                                    sup["feed_token"] or "")
        rows = parse_price_sheet(text, sup["default_uom"] or "each")
        n = save_prices(conn, supplier_id, rows, company)
        _mark(conn, supplier_id, f"{n} prices")
        return {"supplier": sup["name"], "rows": n, "priced": n}
    except Exception as e:
        _mark(conn, supplier_id, f"failed: {e}", ok=False)
        raise


# --- lookup ------------------------------------------------------------------
def prices_for(conn, term: str, company: str = "construction", limit: int = 40) -> list[dict]:
    """Search every supplier's book at once, newest price per supplier/item.

    Rows come back per supplier so the caller can show where each price came
    from and how old it is. Stale rows are flagged, never hidden -- a
    six-month-old price that looks current is the failure mode here.
    """
    t = f"%{term.strip()}%"
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.supplier_id, s.name AS supplier, s.kind,
                   p.item_number, p.description, p.uom, p.price_cents,
                   p.observed_at,
                   (now() - p.observed_at) AS age
            FROM material_supplier_prices p
            JOIN material_suppliers s ON s.id = p.supplier_id
            WHERE p.company = %s AND s.enabled
              AND (p.description ILIKE %s OR p.item_number ILIKE %s)
            ORDER BY p.description, s.name
            LIMIT %s
        """, (company, t, t, limit * 4))
        rows = cur.fetchall()

    for r in rows:
        age_days = r["age"].days if r.get("age") is not None else 0
        r["age_days"] = age_days
        r["stale"] = age_days > MAX_AGE_DAYS
    return rows[:limit]


def price_item_numbers(conn, item_numbers: list[str], company: str = "construction") -> dict:
    """item -> {supplier: price_cents}, newest wins, for building a takeoff."""
    if not item_numbers:
        return {}
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (p.item_number, s.name)
                   p.item_number, s.name AS supplier, p.price_cents, p.observed_at
            FROM material_supplier_prices p
            JOIN material_suppliers s ON s.id = p.supplier_id
            WHERE p.company = %s AND s.enabled AND p.item_number = ANY(%s)
            ORDER BY p.item_number, s.name, p.observed_at DESC
        """, (company, list(item_numbers)))
        for r in cur.fetchall():
            out.setdefault(r["item_number"], {})[r["supplier"]] = {
                "price_cents": int(r["price_cents"]), "observed_at": r["observed_at"]}
    return out


def staleness(conn, company: str = "construction") -> list[dict]:
    """Per-supplier freshness, for the badge row on the materials page."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.id, s.name, s.kind, s.last_synced_at, s.last_sync_note,
                   count(p.id) AS items,
                   max(p.observed_at) AS newest_price
            FROM material_suppliers s
            LEFT JOIN material_supplier_prices p ON p.supplier_id = s.id
            WHERE s.company = %s AND s.enabled
            GROUP BY s.id ORDER BY s.name
        """, (company,))
        rows = cur.fetchall()
    for r in rows:
        newest = r.get("newest_price")
        age = (datetime.datetime.now(datetime.timezone.utc) - newest).days if newest else None
        r["age_days"] = age
        r["stale"] = age is None or age > MAX_AGE_DAYS
        r["items"] = r["items"] or 0
    return rows

# --- customer-facing surface -------------------------------------------------
# Everything below this line is reachable without a login, so it only ever
# touches suppliers flagged public_ok. The filter lives in the SQL rather than
# in the template on purpose: a forgotten {% if %} in a template leaks
# confidential contract pricing, whereas a WHERE clause cannot be skipped by
# forgetting to guard a column.

PUBLIC_CATEGORIES = ["all"]


def public_categories(conn, company: str = "construction") -> list[str]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT COALESCE(NULLIF(s.category,''), 'other') AS cat
            FROM material_supplier_prices p
            JOIN material_suppliers s ON s.id = p.supplier_id
            WHERE s.public_ok AND s.enabled AND s.company = %s
            ORDER BY 1
        """, (company,))
        return [r["cat"] for r in cur.fetchall()]


def public_prices(conn, term: str = "", category: str = "", limit: int = 60) -> list[dict]:
    """Items safe to show a customer. Public shelf prices only.

    Note the supplier join carries the public_ok predicate rather than
    filtering afterwards, so a private price is never even loaded into memory.
    """
    where = ["s.public_ok", "s.enabled", "p.price_cents > 0"]
    params: list = []
    if term.strip():
        where.append("(p.description ILIKE %s OR p.item_number ILIKE %s)")
        params += [f"%{term.strip()}%", f"%{term.strip()}%"]
    if category and category != "all":
        where.append("COALESCE(NULLIF(s.category,''),'other') = %s")
        params.append(category)
    params.append(max(1, min(int(limit), 200)))
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT p.item_number, p.description, p.uom, p.price_cents,
                   s.name AS supplier, s.category,
                   (now() - p.observed_at) AS age
            FROM material_supplier_prices p
            JOIN material_suppliers s ON s.id = p.supplier_id
            WHERE {' AND '.join(where)}
            ORDER BY p.description
            LIMIT %s
        """, params)
        rows = cur.fetchall()
    for r in rows:
        age = r["age"].days if r.get("age") is not None else 0
        r["age_days"] = age
        r["stale"] = age > MAX_AGE_DAYS
        # Never publish a stale price as if it were current.
        r["price_cents"] = None if r["stale"] else r["price_cents"]
    return rows


def public_price_count(conn, company: str = "construction") -> int:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) AS n
            FROM material_supplier_prices p
            JOIN material_suppliers s ON s.id = p.supplier_id
            WHERE s.public_ok AND s.enabled AND p.price_cents > 0 AND s.company = %s
        """, (company,))
        return cur.fetchone()["n"]
