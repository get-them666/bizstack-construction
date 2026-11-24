"""Free permit feeds must be tried before Shovels, or credits run out early.

Why this exists
---------------
`fetch_permits` used to ask Shovels first for every city, including Virginia
Beach and Norfolk, which publish their permits as free public ArcGIS feeds.
Every Shovels row costs a credit from a hard monthly allowance (500 on the base
plan). Re-fetching VB and Norfolk permits that the cities already publish is
what drove the account to `402 Monthly credit limit exceeded`, which then cut
off the nine cities that have no public feed at all.

The order must be: free municipal feed first, Shovels second. Shovels is still
the fallback when a city's endpoint is empty or down, so a free-feed outage does
not silently produce zero permits.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))

import permit_service as ps  # noqa: E402


CALLS = {"shovels": 0, "open": 0}


def _patch(open_rows, shovels_rows, configured=True):
    CALLS["shovels"] = 0
    CALLS["open"] = 0

    def fake_open(city, days=45, limit=200):
        CALLS["open"] += 1
        return list(open_rows)

    def fake_shovels(geo_ids=None, days=21, per_geo=4):
        CALLS["shovels"] += 1
        return list(shovels_rows)

    ps.fetch_open_data = fake_open
    ps.search_permits = fake_shovels
    ps.is_configured = lambda: configured


def test_free_feed_wins_for_virginia_beach():
    _patch(open_rows=[{"property_address": "1 OCEAN AVE, Virginia Beach, VA"}],
           shovels_rows=[{"property_address": "SHOULD NOT BE USED"}])
    rows = ps.fetch_permits("Virginia Beach", days=30, limit=5)
    assert len(rows) == 1
    assert rows[0]["property_address"].startswith("1 OCEAN AVE")
    assert CALLS["shovels"] == 0, "Shovels was billed for a city with a free feed"


def test_free_feed_wins_for_norfolk():
    _patch(open_rows=[{"property_address": "1 MAIN ST, Norfolk, VA"}],
           shovels_rows=[{"property_address": "SHOULD NOT BE USED"}])
    rows = ps.fetch_permits("Norfolk", days=30, limit=5)
    assert len(rows) == 1
    assert CALLS["shovels"] == 0, "Shovels was billed for a city with a free feed"


def test_shovels_covers_a_city_with_no_feed():
    _patch(open_rows=[], shovels_rows=[{"property_address": "1 DOCKSIDE DR, Chesapeake, VA"}])
    rows = ps.fetch_permits("Chesapeake", days=30, limit=5)
    assert len(rows) == 1
    assert CALLS["shovels"] == 1, "a feedless city must fall through to Shovels"


def test_empty_free_feed_falls_back_to_shovels():
    # A dead or empty municipal endpoint must not silently produce zero permits.
    _patch(open_rows=[], shovels_rows=[{"property_address": "2 BACKUP RD, Virginia Beach, VA"}])
    rows = ps.fetch_permits("Virginia Beach", days=30, limit=5)
    assert len(rows) == 1
    assert CALLS["open"] == 1 and CALLS["shovels"] == 1


def test_unconfigured_shovels_returns_empty_not_fake():
    _patch(open_rows=[], shovels_rows=[{"property_address": "NOPE"}], configured=False)
    assert ps.fetch_permits("Hampton", days=30, limit=5) == []
    assert CALLS["shovels"] == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS  {name}")
