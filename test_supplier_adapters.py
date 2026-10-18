"""Retail supplier adapters: the offline contract.

The property that matters is refusal. With no credentials these must report
themselves unconfigured, and a sync must fail loudly rather than writing rows
at zero -- a price book that silently reads $0 is worse than an empty one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

import supplier_adapters as sa


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch):
    for var in ("HOMEDEPOT_API_KEY", "HOMEDEPOT_AFFILIATE_ID",
                "LOWES_API_KEY", "LOWES_CONSUMER_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_nothing_is_configured_without_credentials():
    for entry in sa.status():
        assert entry["configured"] is False
        assert entry["missing"], f"{entry['key']} must name what it needs"


def test_home_depot_needs_both_values(monkeypatch):
    monkeypatch.setenv("HOMEDEPOT_API_KEY", "k")
    assert sa.HomeDepot.configured() is False
    assert "HOMEDEPOT_AFFILIATE_ID" in " ".join(sa.HomeDepot.missing())
    monkeypatch.setenv("HOMEDEPOT_AFFILIATE_ID", "a")
    assert sa.HomeDepot.configured() is True


def test_lowes_needs_both_values(monkeypatch):
    monkeypatch.setenv("LOWES_API_KEY", "k")
    assert sa.Lowes.configured() is False
    monkeypatch.setenv("LOWES_CONSUMER_KEY", "c")
    assert sa.Lowes.configured() is True


def test_unconfigured_adapter_raises_instead_of_returning_nothing(monkeypatch):
    with pytest.raises(sa.SupplierAPIError, match="not configured"):
        sa.HomeDepot.product("305842616")


def test_unconfigured_call_makes_no_network_request(monkeypatch):
    monkeypatch.setattr(sa, "_get", lambda *a, **k: pytest.fail("must not call out"))
    with pytest.raises(sa.SupplierAPIError):
        sa.HomeDepot.product("305842616")


def test_lowes_price_items_says_use_the_sheet_path(monkeypatch):
    monkeypatch.setenv("LOWES_API_KEY", "k")
    monkeypatch.setenv("LOWES_CONSUMER_KEY", "c")
    with pytest.raises(sa.SupplierAPIError, match="price-sheet"):
        sa.Lowes.price_items([{"itemNumber": "1"}])


def test_home_depot_has_no_bulk_catalog():
    with pytest.raises(sa.SupplierAPIError, match="no bulk catalog"):
        sa.HomeDepot.catalog_items()


def test_adapter_lookup_matches_by_name():
    assert sa.adapter_for("Home Depot") is sa.HomeDepot
    assert sa.adapter_for("home depot pro") is sa.HomeDepot
    assert sa.adapter_for("Lowe's") is sa.Lowes
    assert sa.adapter_for("Lowes") is sa.Lowes


def test_sheet_suppliers_have_no_api_adapter():
    """These go through the price sheet, so they must not claim an API client."""
    for name in ("Builders FirstSource", "Kempsville", "Richards", "ABC Supply"):
        assert sa.adapter_for(name) is None


def test_every_requested_line_comes_back_even_when_unreachable(monkeypatch):
    """Never fewer rows than requested, so nothing reads as free."""
    monkeypatch.setenv("HOMEDEPOT_API_KEY", "k")
    monkeypatch.setenv("HOMEDEPOT_AFFILIATE_ID", "a")
    monkeypatch.setattr(sa, "_get", lambda *a, **k: (_ for _ in ()).throw(
        sa.SupplierAPIError("HTTP 503")))
    items = [{"itemNumber": "1"}, {"itemNumber": "2"}, {"itemNumber": ""}]
    out = sa.HomeDepot.price_items(items)
    assert len(out) == len(items)
    assert all(line["unit_price"] is None for line in out)
    assert out[2]["reason"] == "no item id"
    assert "503" in out[0]["reason"]


def test_price_parses_home_depot_product_shape(monkeypatch):
    monkeypatch.setenv("HOMEDEPOT_API_KEY", "k")
    monkeypatch.setenv("HOMEDEPOT_AFFILIATE_ID", "a")
    monkeypatch.setattr(sa, "_get", lambda *a, **k: {"data": {"product": {
        "productName": "Shaw Floorset 12-in Luxury Vinyl Plank",
        "itemKey": "305842616", "unitLabel": "SQ FT",
        "currentPrice": {"value": 2.99, "currency": "USD"}}}})
    out = sa.HomeDepot.price_items([{"itemNumber": "305842616"}])
    assert out[0]["unit_price"] == 299
    assert out[0]["uom"] == "SQ FT"
    assert "Vinyl Plank" in out[0]["description"]


def test_price_handles_a_bare_numeric_price(monkeypatch):
    monkeypatch.setenv("HOMEDEPOT_API_KEY", "k")
    monkeypatch.setenv("HOMEDEPOT_AFFILIATE_ID", "a")
    monkeypatch.setattr(sa, "_get", lambda *a, **k: {"data": {"product": {
        "productName": "x", "currentPrice": 5.5}}})
    assert sa.HomeDepot.price_items([{"itemNumber": "1"}])[0]["unit_price"] == 550


def test_missing_price_is_null_not_zero(monkeypatch):
    monkeypatch.setenv("HOMEDEPOT_API_KEY", "k")
    monkeypatch.setenv("HOMEDEPOT_AFFILIATE_ID", "a")
    monkeypatch.setattr(sa, "_get", lambda *a, **k: {"data": {"product": {
        "productName": "x", "currentPrice": None}}})
    line = sa.HomeDepot.price_items([{"itemNumber": "1"}])[0]
    assert line["unit_price"] is None
    assert line["reason"] == "no current price on this item"


def test_empty_product_response_is_an_error(monkeypatch):
    monkeypatch.setenv("HOMEDEPOT_API_KEY", "k")
    monkeypatch.setenv("HOMEDEPOT_AFFILIATE_ID", "a")
    monkeypatch.setattr(sa, "_get", lambda *a, **k: {"data": {}})
    with pytest.raises(sa.SupplierAPIError, match="no product"):
        sa.HomeDepot.product("1")


@pytest.mark.parametrize("raw,expected", [
    ("2.99", 299), ("$5.45", 545), ("1,234.50", 123450), ("", None), (None, None),
    ("not a price", None), (0, 0),
])
def test_cents_conversion(raw, expected):
    assert sa._cents(raw) == expected


def test_headers_carry_both_credentials(monkeypatch):
    monkeypatch.setenv("HOMEDEPOT_API_KEY", "secret-key")
    monkeypatch.setenv("HOMEDEPOT_AFFILIATE_ID", "aff-1")
    headers = sa.HomeDepot._headers()
    assert headers["X-API-KEY"] == "secret-key"
    assert headers["X-Affiliate-Id"] == "aff-1"


def test_base_url_is_overridable(monkeypatch):
    monkeypatch.setenv("HOMEDEPOT_BASE_URL", "https://example.test/v9/")
    monkeypatch.setattr(sa, "_get", lambda *a, **k: pytest.fail("no creds"))
    with pytest.raises(sa.SupplierAPIError):
        sa.HomeDepot.product("1")


class _Cur:
    def __init__(self, rows): self.sql = ""; self.params = None; self._rows = rows
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.sql, self.params = sql, params
    def fetchall(self): return self._rows


class _Conn:
    def __init__(self, rows): self.cur = _Cur(rows)
    def cursor(self): return self.cur


def test_tracked_skus_keeps_the_store_filter_for_home_depot():
    """Both words are stop words, so a naive brand derivation drops the filter
    entirely and prices every tracked SKU regardless of store."""
    import supplier_prices as sp
    conn = _Conn([{"sku": "305842616", "name": "Cabinet", "unit": "each"}])
    rows = sp.tracked_skus(conn, "Home Depot")
    assert conn.cur.params[0] == "%home depot%"
    assert rows[0]["sku"] == "305842616"


def test_tracked_skus_falls_back_to_a_shorter_name():
    """A supplier row named more precisely than the store it prices."""
    import supplier_prices as sp
    seen = []

    class C(_Conn):
        def cursor(self):
            cur = _Cur([{"sku": "1", "name": "n", "unit": "each"}]
                       if len(seen) == 1 else [])
            orig = cur.execute
            def execute(sql, params=None):
                seen.append(params[0] if params else None)
                orig(sql, params)
            cur.execute = execute
            return cur

    sp.tracked_skus(C([]), "Home Depot - Norfolk VA")
    assert "%home depot%" in seen
    assert len(seen) >= 2, "should have tried a narrower name first"


def test_tracked_skus_returns_empty_rather_than_everything():
    """No match must be no rows, never an unscoped query."""
    import supplier_prices as sp
    assert sp.tracked_skus(_Conn([]), "Nonexistent Supply") == []
    assert sp.tracked_skus(_Conn([]), "") == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

def test_field_name_agreement(monkeypatch):
    """Regression: tracked_skus returns `sku` but adapters read `itemNumber`.

    Passing the rows straight through priced nothing and reported "0/3 priced",
    which reads as "the supplier has no prices" rather than "we built the call
    wrong". Every requested line must arrive carrying a usable identifier.
    """
    monkeypatch.setenv("HOMEDEPOT_API_KEY", "k")
    monkeypatch.setenv("HOMEDEPOT_AFFILIATE_ID", "a")
    seen = []

    def fake_get(url, headers, timeout=30):
        seen.append(url.rsplit("/", 1)[-1])
        return {"data": {"product": {"productName": "x", "unitLabel": "each",
                                     "currentPrice": {"value": 10.0}}}}

    monkeypatch.setattr(sa, "_get", fake_get)
    from supplier_prices import tracked_skus

    candidates = tracked_skus(
        _Conn([{"sku": "305842616", "name": "Cabinet", "unit": "each"}]),
        "Home Depot")
    mapped = [{"itemNumber": r["sku"], "description": r["description"],
               "uom": r["uom"]} for r in candidates]
    out = sa.HomeDepot.price_items(mapped)
    assert seen == ["305842616"]
    assert out[0]["unit_price"] == 1000
