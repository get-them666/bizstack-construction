"""Retail supplier adapters: Home Depot and Lowe's.

Same interface as ``abc_supply`` so ``supplier_prices.sync_supplier`` can
dispatch to any of them without special-casing:

    configured() -> bool
    missing()    -> list[str]
    catalog_items(limit) -> list[dict]
    price_items(items, purpose) -> list[dict]

Both follow the rule ``abc_supply`` already sets: every requested line comes
back, with ``unit_price`` None and a ``reason`` when the supplier could not
price it, so an unpriced item is never mistaken for a free one.

Status of each
--------------
Home Depot
    The affiliate/partner Product API at ``developer.homedepot.com``. Joining
    needs an affiliate ID and an API key, so ``configured()`` is False until
    ``HOMEDEPOT_API_KEY`` and ``HOMEDEPOT_AFFILIATE_ID`` are set. Path
    templates live in ``_paths`` and are overridable with
    ``HOMEDEPOT_BASE_URL`` because the retailer has moved its host before.

Lowe's
    Partner/enterprise gated, not self-serve. ``HOMEDEPOT``-shaped env vars
    only, and it reports itself unconfigured until then.

Builders FirstSource / Kempsville
    No public API. They use the price-sheet path in ``supplier_prices``, which
    already works: ``sync_supplier(sheet_text=...)`` parses a CSV/TSV export
    and publishes it. ``register_sheet_supplier`` below is the one-liner.

Neither retailer is contacted unless credentials exist, and nothing here
invents a price when a call fails -- it raises.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

USER_AGENT = "Buildstack-Materials/1.0"
DEFAULT_TIMEOUT = int(os.getenv("SUPPLIER_API_TIMEOUT", "30"))


class SupplierAPIError(RuntimeError):
    """A supplier could not be read. Never answered with a substitute price."""


def _get(url: str, headers: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise SupplierAPIError(f"HTTP {e.code} for {url} {body}") from e
    except SupplierAPIError:
        raise
    except Exception as e:
        raise SupplierAPIError(f"unreachable: {e}") from e


def _cents(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(round(float(str(value).replace("$", "").replace(",", "")) * 100))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Home Depot
# ---------------------------------------------------------------------------

class HomeDepot:
    name = "Home Depot"
    base_url = os.getenv("HOMEDEPOT_BASE_URL", "https://api.homedepot.com").rstrip("/")

    @staticmethod
    def configured() -> bool:
        return bool(os.getenv("HOMEDEPOT_API_KEY")) and bool(
            os.getenv("HOMEDEPOT_AFFILIATE_ID"))

    @staticmethod
    def missing() -> list[str]:
        out = []
        if not os.getenv("HOMEDEPOT_API_KEY"):
            out.append("HOMEDEPOT_API_KEY (from developer.homedepot.com)")
        if not os.getenv("HOMEDEPOT_AFFILIATE_ID"):
            out.append("HOMEDEPOT_AFFILIATE_ID")
        return out

    @staticmethod
    def _headers() -> dict:
        return {
            "X-API-KEY": os.getenv("HOMEDEPOT_API_KEY", ""),
            "X-Affiliate-Id": os.getenv("HOMEDEPOT_AFFILIATE_ID", ""),
        }

    @classmethod
    def product(cls, item_id: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
        """One product by Home Depot item id (the 9-digit number in a URL)."""
        if not cls.configured():
            raise SupplierAPIError(
                f"{cls.name} is not configured; missing " + ", ".join(cls.missing()))
        doc = _get(f"{cls.base_url}/v1/products/{item_id}", cls._headers(), timeout)
        product = (doc.get("data") or {}).get("product") if isinstance(doc, dict) else None
        if not product:
            raise SupplierAPIError(f"{cls.name}: no product for item {item_id}")
        return product

    @classmethod
    def price_items(cls, items: list[dict], purpose: str = "estimating",
                    timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
        out = []
        for it in items:
            item_id = str(it.get("itemNumber") or it.get("item_number") or "").strip()
            line = {"itemNumber": item_id, "description": it.get("description", ""),
                    "uom": it.get("uom") or "each", "quantity": it.get("quantity", 1),
                    "unit_price": None, "reason": None}
            if not item_id:
                line["reason"] = "no item id"
                out.append(line)
                continue
            try:
                product = cls.product(item_id, timeout)
            except SupplierAPIError as e:
                line["reason"] = str(e)[:200]
                out.append(line)
                continue
            line["description"] = product.get("productName") or line["description"]
            line["uom"] = product.get("unitLabel") or line["uom"]
            line["unit_price"] = _cents(
                (product.get("currentPrice") or {}).get("value")
                if isinstance(product.get("currentPrice"), dict)
                else product.get("currentPrice"))
            if line["unit_price"] is None:
                line["reason"] = "no current price on this item"
            out.append(line)
        return out

    @classmethod
    def catalog_items(cls, limit: int = 100, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
        """Needs a keyword; there is no bulk catalog export on the partner API."""
        raise SupplierAPIError(
            f"{cls.name} has no bulk catalog on the partner API. Price the SKUs you "
            f"already track with price_items(), or paste a price sheet.")


# ---------------------------------------------------------------------------
# Lowe's
# ---------------------------------------------------------------------------

class Lowes:
    name = "Lowe's"
    base_url = os.getenv("LOWES_BASE_URL", "https://apis.lowes.com").rstrip("/")

    @staticmethod
    def configured() -> bool:
        return bool(os.getenv("LOWES_API_KEY")) and bool(os.getenv("LOWES_CONSUMER_KEY"))

    @staticmethod
    def missing() -> list[str]:
        out = []
        if not os.getenv("LOWES_API_KEY"):
            out.append("LOWES_API_KEY (partner access; not self-serve)")
        if not os.getenv("LOWES_CONSUMER_KEY"):
            out.append("LOWES_CONSUMER_KEY")
        return out

    @classmethod
    def price_items(cls, items: list[dict], purpose: str = "estimating",
                    timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
        raise SupplierAPIError(
            f"{cls.name} does not publish a self-serve product/pricing API. "
            f"Use the price-sheet path: sync_supplier(sheet_text=...)")


ADAPTERS: dict = {
    "home_depot": HomeDepot,
    "lowes": Lowes,
}


def adapter_for(supplier_name: str):
    """Pick an adapter by substring, so a named supplier row finds its client."""
    name = (supplier_name or "").lower()
    if name.startswith("home depot") or "home depot" in name:
        return HomeDepot
    if name.startswith("lowes") or "lowe" in name:
        return Lowes
    return None


def status() -> list[dict]:
    """What can be pulled right now, and what is waiting on credentials."""
    out = []
    for key, cls in ADAPTERS.items():
        missing = cls.missing()
        out.append({"key": key, "name": cls.name, "configured": cls.configured(),
                    "missing": missing})
    return out


def register_sheet_supplier(conn, name: str, sheet_text: str, category: str = "",
                            default_uom: str = "each", public_ok: bool = True,
                            company: str = "construction") -> dict:
    """One call to publish a vendor CSV/TSV export as a real price source.

    This is the working path for Builders FirstSource, Kempsville, Richards and
    any yard that sends a price sheet instead of a feed.
    """
    import supplier_prices as sp
    supplier_id = sp.upsert_supplier(
        conn, name, kind="manual", default_uom=default_uom,
        public_ok=public_ok, category=category, company=company)
    return sp.sync_supplier(conn, supplier_id, company=company, sheet_text=sheet_text)