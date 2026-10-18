"""EstimationPro cost data: real regional construction costs, free and keyless.

Public API, no credentials, verified live 2026-10-18::

    GET https://estimationpro.ai/api/v1/index
    GET https://estimationpro.ai/api/v1/costs?trade=roofing&zip=23464

Methodology, quoted from the API's own ``meta.methodology``:

    EPCI = 100 * regional multiplier.  Project costs = national typical cost
    * regional multiplier.  Multipliers derived from BLS OEWS wage data.

Shape notes, because getting these wrong is the whole failure mode here:

* Every response nests its payload under a top-level ``data`` key. Reading
  ``multiplier`` or ``projectTypes`` off the top level silently yields nothing,
  which is how an earlier script ended up publishing a hardcoded multiplier of
  1.0 and a fabricated ``base_labor_hourly`` of $45 while appearing to work.
* There is no ``base_labor_hourly`` field. Labour is regionalised separately
  via each region's ``laborMultiplier``.
* The ZIP is resolved by the provider: ``/costs`` returns the ``location`` it
  matched and the multiplier it applied. We deliberately do **not** keep a
  local ZIP-to-state table. A hand-written prefix table was tried and it was
  wrong (07030 is NJ not CT, 60614 is IL not PR), and a wrong region silently
  misprices every quote in that ZIP.

Pricing provenance
------------------
These figures are a public regional benchmark, not a negotiated supplier cost.
They are sound for pricing a customer quote and are stamped with
``lastVerified`` so a quote can be defended later. They must never be booked
as cost of goods: real job cost comes from a supplier invoice. See
``ledger_sync.sync_cost_line``, which takes its cost from ``bid_cost_lines``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = os.getenv("ESTIMATIONPRO_BASE_URL", "https://estimationpro.ai/api/v1").rstrip("/")
USER_AGENT = "Buildstack-Materials/1.0"

DEFAULT_TIMEOUT = float(os.getenv("ESTIMATIONPRO_TIMEOUT", "20"))
CACHE_DIR = Path(os.getenv("ESTIMATIONPRO_CACHE_DIR", "/tmp/estimationpro"))
CACHE_TTL = int(os.getenv("ESTIMATIONPRO_CACHE_TTL", str(6 * 3600)))

TRADES = ("bathroom-remodel", "kitchen-remodel", "deck", "paint", "roofing")

PROJECT_TYPES = {
    "bathroom_remodel": "bathroom-remodel",
    "kitchen_remodel": "kitchen-remodel",
    "deck_build": "deck",
    "interior_paint": "paint",
    "roof_replacement": "roofing",
}


class EstimationProUnavailable(RuntimeError):
    """Raised when the provider cannot be read or the payload is not understood.

    Callers must surface this rather than substituting a number. A price the
    model invented is worse than no price, because it reaches an invoice.
    """


def _http_get(url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Single network entry point. Tests patch this to run offline."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise EstimationProUnavailable(f"EstimationPro HTTP {e.code} for {url}") from e
    except EstimationProUnavailable:
        raise
    except Exception as e:
        raise EstimationProUnavailable(f"EstimationPro unreachable: {e}") from e


def _cache_path(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    return CACHE_DIR / f"{safe}.json"


def _cached_get(url: str, cache_key: str, ttl: int | None = None) -> tuple[str, bool]:
    """Return ``(body, stale)``.

    When the provider is down a cached body is served rather than losing the
    screen, but it is flagged ``stale`` so a week-old price can be labelled as
    such instead of looking current.

    ``ttl`` resolves inside the call rather than as a default argument: a
    default would bind at import time, making ``ESTIMATIONPRO_CACHE_TTL`` and
    any runtime override silently inert.
    """
    ttl = CACHE_TTL if ttl is None else ttl
    path = _cache_path(cache_key)
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text())
        except (ValueError, OSError):
            cached = None
    if isinstance(cached, dict) and cached.get("body"):
        if time.time() - float(cached.get("stored") or 0) < ttl:
            return cached["body"], False
    try:
        body = _http_get(url)
    except EstimationProUnavailable:
        if isinstance(cached, dict) and cached.get("body"):
            return cached["body"], True
        raise
    except Exception as e:
        if isinstance(cached, dict) and cached.get("body"):
            return cached["body"], True
        raise EstimationProUnavailable(f"EstimationPro unreachable: {e}") from e
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"body": body, "stored": time.time()}))
    except OSError:
        pass
    return body, False


def _payload(raw: str, what: str) -> dict:
    """Unwrap the ``data`` envelope, refusing anything we do not recognise."""
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise EstimationProUnavailable(f"EstimationPro {what}: response was not JSON") from e
    if not isinstance(doc, dict):
        raise EstimationProUnavailable(f"EstimationPro {what}: expected an object")
    data = doc.get("data")
    if not isinstance(data, dict):
        raise EstimationProUnavailable(
            f"EstimationPro {what}: no 'data' envelope (top-level keys: "
            f"{sorted(doc)[:6]}). Refusing to guess at the payload shape."
        )
    return data


def fetch_index(timeout: float = DEFAULT_TIMEOUT) -> dict:
    """All regions plus the project-type map."""
    raw, stale = _cached_get(f"{BASE_URL}/index", "index")
    data = _payload(raw, "index")
    meta = data.get("meta") or {}
    index = data.get("index")
    if not isinstance(index, dict) or not index:
        raise EstimationProUnavailable("EstimationPro index: no regions")
    if not isinstance(meta.get("projectTypes"), dict):
        raise EstimationProUnavailable("EstimationPro index: no projectTypes")
    return {
        "version": meta.get("version"),
        "generated": meta.get("generated"),
        "methodology": meta.get("methodology", ""),
        "project_types": meta["projectTypes"],
        "regions": index,
        "trades": sorted({str(pt.get("trade")) for pt in meta["projectTypes"].values()
                          if pt.get("trade")}),
        "stale": stale,
    }


def fetch_costs(trade: str, zip_code: str | int,
                timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Item-level costs for one trade, regionalised to a ZIP by the provider."""
    trade = str(trade).strip().lower()
    if trade not in TRADES:
        raise EstimationProUnavailable(
            f"unknown trade {trade!r}; the provider offers {list(TRADES)}")
    zip_code = str(zip_code).strip()
    if len(zip_code) != 5 or not zip_code.isdigit():
        raise EstimationProUnavailable(f"ZIP must be 5 digits, got {zip_code!r}")

    url = f"{BASE_URL}/costs?{urllib.parse.urlencode({'trade': trade, 'zip': zip_code})}"
    raw, stale = _cached_get(url, f"costs-{trade}-{zip_code}")
    data = _payload(raw, f"costs {trade}/{zip_code}")

    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise EstimationProUnavailable(
            f"EstimationPro costs {trade}/{zip_code}: no items returned")

    items = []
    for it in raw_items:
        if not isinstance(it, dict) or it.get("typical") is None:
            continue
        items.append({
            "item_id": str(it.get("id") or "").strip(),
            "description": str(it.get("description") or "").strip(),
            "unit": str(it.get("unit") or "each").strip(),
            "typical": float(it["typical"]),
            "low": float(it["low"]) if it.get("low") is not None else None,
            "high": float(it["high"]) if it.get("high") is not None else None,
            "volatility": it.get("volatility"),
            "last_verified": it.get("lastVerified"),
            "typical_cents": int(round(float(it["typical"]) * 100)),
            "low_cents": int(round(float(it["low"]) * 100)) if it.get("low") is not None else None,
            "high_cents": int(round(float(it["high"]) * 100)) if it.get("high") is not None else None,
        })
    if not items:
        raise EstimationProUnavailable(
            f"EstimationPro costs {trade}/{zip_code}: items carried no 'typical' price")

    return {
        "trade": data.get("trade") or trade,
        "location": data.get("location"),
        "multiplier": data.get("multiplier"),
        "item_count": data.get("itemCount", len(items)),
        "items": items,
        "source": "estimationpro",
        "stale": stale,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def project_estimate(trade: str, zip_code: str | int, sqft: float | None = None) -> dict:
    """Regionalised low/typical/high for a whole project.

    ``roofing`` and ``bathroom-remodel`` are priced per project; the rest are
    per square foot, so ``sqft`` is required for those or this raises rather
    than guessing a size.
    """
    idx = fetch_index()
    key = next((k for k, v in idx["project_types"].items()
                if v.get("trade") == trade), None)
    if key is None:
        raise EstimationProUnavailable(f"no project type for trade {trade!r}")

    costs = fetch_costs(trade, zip_code)
    multiplier = costs.get("multiplier")
    if multiplier is None:
        raise EstimationProUnavailable(
            f"EstimationPro costs {trade}/{zip_code}: no regional multiplier")

    unit = str(idx["project_types"][key].get("unit") or "project").lower()
    if unit != "project" and not sqft:
        raise EstimationProUnavailable(f"trade {trade!r} is priced per {unit}; supply sqft")

    row = _region_for(idx["regions"], costs.get("location")).get("projects") or {}
    figures = row.get(key)
    if not isinstance(figures, dict):
        raise EstimationProUnavailable(
            f"region for {costs.get('location')!r} has no {key} figures")

    band = {}
    for label in ("low", "typical", "high"):
        value = figures.get(label)
        if value is None:
            continue
        value = float(value) * float(multiplier)
        band[label] = value if unit == "project" else value * float(sqft)

    return {
        "trade": trade,
        "project_type": key,
        "description": idx["project_types"][key].get("description"),
        "unit": unit,
        "sqft": float(sqft) if sqft else None,
        "location": costs.get("location"),
        "regional_multiplier": float(multiplier),
        "epci": round(float(multiplier) * 100),
        "low_cents": int(round(band["low"] * 100)) if "low" in band else None,
        "typical_cents": int(round(band["typical"] * 100)) if "typical" in band else None,
        "high_cents": int(round(band["high"] * 100)) if "high" in band else None,
        "labor_multiplier": _region_for(idx["regions"], costs.get("location")).get("laborMultiplier"),
        "methodology": idx["methodology"],
        "source": "estimationpro",
        "stale": costs.get("stale"),
        "generated": idx.get("generated"),
    }


def _region_for(regions: dict, location) -> dict:
    """Match the provider's resolved ``location`` back to an index region.

    The provider has already decided which region a ZIP falls in, so we defer
    to it rather than re-deriving state from the ZIP. A state wins over a metro
    only when the location names no metro, which keeps "VA - Hampton Roads"
    from silently resolving to Virginia when a metro figure was matched.
    """
    if not location:
        raise EstimationProUnavailable(
            "EstimationPro costs: no 'location' returned, cannot pick a region")
    text = str(location)
    head = text.split(" - ")[0].strip().lower()
    haystack = text.lower()

    metros = [r for r in regions.values() if r.get("type") == "metro"]
    for r in metros:
        if str(r.get("name", "")).lower() in haystack:
            return r
    for r in regions.values():
        if r.get("type") == "state" and str(r.get("name", "")).lower() == head:
            return r
    for r in metros:
        if str(r.get("name", "")).split("-")[0].strip().lower() == head:
            return r
    raise EstimationProUnavailable(
        f"location {location!r} matches none of the {len(regions)} index regions")


def supplier_rows(costs: dict, trade: str) -> list[dict]:
    """Map fetched costs onto ``material_supplier_prices`` rows.

    Lets the existing importer, ``material_picker.price_list`` and the public
    sandbox catalogue work unchanged: item numbers are EstimationPro's own
    stable ids, and the typical cost is the benchmark that ``customer_price()``
    then marks up so the raw cost is never published.
    """
    rows = []
    for it in costs.get("items", []):
        if not it.get("item_id"):
            continue
        rows.append({
            "item_number": it["item_id"],
            "description": it["description"],
            "uom": it["unit"],
            "price_cents": it["typical_cents"],
            "trade": trade,
            "last_verified": it.get("last_verified"),
        })
    return rows


def supplier_name(trade: str, zip_code: str | int) -> str:
    """Stable name so a re-sync updates one supplier row instead of cloning."""
    return f"EstimationPro benchmark - {trade} {zip_code}"


def sync_into_catalogue(conn, trade: str, zip_code: str | int,
                        company: str = "construction") -> dict:
    """Publish a trade's real costs into the public sandbox catalogue.

    Registered as ``kind="feed"`` rather than ``kind="api"`` deliberately:
    ``upsert_supplier`` rejects an api supplier that is also ``public_ok``,
    because api pricing is per-account and confidential. That guard is right
    for ABC Supply and contract pricing. EstimationPro is the opposite case --
    a public, keyless, regional benchmark -- so a feed with a real URL and
    ``public_ok`` is both allowed and the accurate description of it.
    """
    import supplier_prices as sp

    costs = fetch_costs(trade, zip_code)
    rows = supplier_rows(costs, trade)
    if not rows:
        raise EstimationProUnavailable(
            f"EstimationPro {trade}/{zip_code}: no rows to publish")

    url = f"{BASE_URL}/costs?trade={trade}&zip={zip_code}"
    supplier_id = sp.upsert_supplier(
        conn, supplier_name(trade, zip_code), kind="feed", feed_url=url,
        default_uom="each", public_ok=True, category=trade, company=company)
    saved = sp.save_prices(conn, supplier_id, rows, company)

    verified = sorted({r["last_verified"] for r in rows if r.get("last_verified")})
    note = f"{saved} prices; location {costs.get('location')}; multiplier {costs.get('multiplier')}"
    if verified:
        note += f"; verified {verified[-1]}"
    if costs.get("stale"):
        note += "; SERVED FROM CACHE WHILE PROVIDER WAS DOWN"
    sp._mark(conn, supplier_id, note, ok=True)

    return {
        "supplier_id": supplier_id,
        "supplier": supplier_name(trade, zip_code),
        "trade": trade,
        "zip": str(zip_code),
        "location": costs.get("location"),
        "multiplier": costs.get("multiplier"),
        "last_verified": verified[-1] if verified else None,
        "rows": saved,
        "stale": costs.get("stale"),
    }