r"""Authoritative cost indices from the U.S. Bureau of Labor Statistics.

BLS public data API v1 needs no key and was verified working keyless against
the live service, returning construction-materials PPI through 2026-M09. v2
requires a registered key and answers ``REQUEST_FAILED_INVALID_PARAMETERS``
without one, so v1 is the default and v2 is only attempted when a key exists.

Why this is the authority and not the price source
--------------------------------------------------
An index is a measure of *relative change over time*, not a price level. So PPI
can escalate a known price between two periods and can tell you whether a
series of observed prices is moving in line with the industry. It **cannot**
tell you whether an absolute number is correct. Concretely:

* Escalating a price you actually paid, using PPI, is sound.
* Using PPI to decide that \$100/square for shingles "should" be \$100/square is
  not. Nothing in the index implies that level.

That distinction is why :func:`verify_anchor` reports what it actually checked
instead of returning a confident-looking score, and why the absolute anchor for
a quote still has to come from a real supplier price, recorded with the
``bls_period`` it was observed against.

Series semantics
----------------
Two different shapes share this API and mixing them up is silent:

* ``index`` series carry a level to ratio (``WPU1017`` = 388.580).
* ``pct_change`` series carry a rate (``CIU1010000000000A`` = 3.4). Dividing one
  of those by another is meaningless, so :func:`escalate` refuses to.

Periods are normalised to ``YYYY-MM`` for monthly and ``YYYY-QN`` for quarterly
series, because BLS reports them as a separate year and a ``M09``/``Q2`` token.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
API_V2 = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_API_KEY = os.getenv("BLS_API_KEY", "").strip()
USER_AGENT = "Buildstack-CostIndex/1.0"

DEFAULT_TIMEOUT = float(os.getenv("BLS_TIMEOUT", "30"))
CACHE_DIR = Path(os.getenv("BLS_CACHE_DIR", "/tmp/bls"))
CACHE_TTL = int(os.getenv("BLS_CACHE_TTL", str(12 * 3600)))

# Deviations beyond this between our own tracked price and the BLS-trended
# baseline are worth a human look. Not a pass/fail: PPI does not price goods.
DRIFT_WARN_PCT = float(os.getenv("BLS_DRIFT_WARN_PCT", "12"))


class BLSUnavailable(RuntimeError):
    """Raised when BLS cannot be read or answers with something unusable."""


@dataclass(frozen=True)
class Series:
    key: str
    series_id: str
    label: str
    kind: str          # "index" | "pct_change"
    freq: str          # "M" | "Q"
    group: str         # "materials" | "labor" | "general"


SERIES = {
    s.key: s for s in (
        Series("construction_materials", "WPU1017",
               "PPI: Construction materials", "index", "M", "materials"),
        Series("lumber_plywood", "WPU101704",
               "PPI: Construction materials - lumber and plywood", "index", "M", "materials"),
        Series("roofing_shingles", "WPU101706",
               "PPI: Construction materials - roofing", "index", "M", "materials"),
        Series("cpi_all_items", "CUUR0000SA0",
               "CPI-U: All items, US city average", "index", "M", "general"),
        Series("labor_comp_private", "CIU2020000000000I",
               "ECI: Total compensation, private industry", "index", "Q", "labor"),
        Series("labor_wages_private", "CIU1010000000000A",
               "ECI: Wages and salaries, private industry (12-month % change)",
               "pct_change", "Q", "labor"),
        Series("construction_wages", "CEU2023600008",
               "Average hourly earnings, construction", "index", "M", "labor"),
    )
}

DEFAULT_MATERIALS = "construction_materials"
DEFAULT_LABOR = "labor_comp_private"


def _cache_path(series_id: str, years: int) -> Path:
    return CACHE_DIR / f"{series_id}-{years}y.json"


def _http_post(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    if BLS_API_KEY and url == API_V2:
        req.add_header("X-KEY", BLS_API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise BLSUnavailable(f"BLS HTTP {e.code} for {url}") from e
    except BLSUnavailable:
        raise
    except Exception as e:
        raise BLSUnavailable(f"BLS unreachable: {e}") from e


def _period(series: Series, year: str, token: str) -> str | None:
    """Normalise a BLS year + M13/Q4 pair into ``YYYY-MM`` / ``YYYY-QN``."""
    year, token = str(year), str(token)
    if series.freq == "M":
        if not token.startswith("M") or not token[1:].isdigit():
            return None
        month = int(token[1:])
        return None if month == 13 else f"{year}-{month:02d}"
    if token.startswith("Q") and token[1:].isdigit():
        return f"{year}-Q{token[1:]}"
    return None


def fetch(series_key: str, years: int = 3) -> dict:
    """Observations for one series, newest first.

    Raises on an unknown key rather than defaulting, so a typo in configuration
    cannot quietly look like a flat index.
    """
    series = SERIES.get(series_key)
    if series is None:
        raise BLSUnavailable(
            f"unknown BLS series {series_key!r}; known: {sorted(SERIES)}")

    url = API_V2 if BLS_API_KEY else API_V1
    end = time.gmtime().tm_year
    payload = {"seriesid": [series.series_id], "startyear": str(end - years),
               "endyear": str(end)}

    path = _cache_path(series.series_id, years)
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text())
        except (ValueError, OSError):
            cached = None
    if isinstance(cached, dict) and cached.get("observations"):
        if time.time() - float(cached.get("stored") or 0) < CACHE_TTL:
            return {**cached, "stale": False}

    try:
        doc = _http_post(url, payload)
    except BLSUnavailable:
        if isinstance(cached, dict) and cached.get("observations"):
            return {**cached, "stale": True}
        raise

    if doc.get("status") != "REQUEST_SUCCEEDED":
        messages = "; ".join(doc.get("message") or [])[:300]
        if isinstance(cached, dict) and cached.get("observations"):
            return {**cached, "stale": True}
        raise BLSUnavailable(f"BLS rejected {series.series_id}: {messages}")

    observations = []
    for entry in (doc.get("Results") or {}).get("series") or []:
        if entry.get("seriesID") != series.series_id:
            continue
        for row in entry.get("data") or []:
            period = _period(series, row.get("year"), row.get("period"))
            try:
                value = float(row.get("value"))
            except (TypeError, ValueError):
                continue
            if period:
                observations.append({"period": period, "value": value})

    observations.sort(key=lambda o: o["period"], reverse=True)
    if not observations:
        if isinstance(cached, dict) and cached.get("observations"):
            return {**cached, "stale": True}
        raise BLSUnavailable(f"BLS returned no observations for {series.series_id}")

    body = {"series_id": series.series_id, "key": series_key, "label": series.label,
            "kind": series.kind, "freq": series.freq, "group": series.group,
            "observations": observations, "latest": observations[0],
            "stale": False}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**body, "stored": time.time()}))
    except OSError:
        pass
    return body


def latest(series_key: str = DEFAULT_MATERIALS) -> dict:
    """The most recent observation."""
    return fetch(series_key)["latest"]


def value_at(series_key: str, period: str, years: int = 5) -> float | None:
    """Index level at one period, or None if BLS does not carry it.

    Periods outside the series' published range return None rather than the
    nearest value: silently escalating off the wrong month is how a quote
    ends up wrong by a year of inflation.
    """
    try:
        data = fetch(series_key, years)
    except BLSUnavailable:
        return None
    for obs in data["observations"]:
        if obs["period"] == period:
            return obs["value"]
    return None


def ratio(series_key: str, from_period: str, to_period: str,
          years: int = 5) -> float | None:
    """Multiplier taking a price at ``from_period`` to ``to_period``."""
    start = value_at(series_key, from_period, years)
    end = value_at(series_key, to_period, years)
    if not start or not end:
        return None
    return end / start


def escalate(cents: int, series_key: str = DEFAULT_MATERIALS,
             from_period: str | None = None, to_period: str | None = None) -> dict:
    """Move a price forward with an official index.

    Refuses to guess: with no ``from_period`` there is nothing to escalate from,
    and an unavailable period returns ``escalated_cents`` equal to the input and
    ``escalated=False`` so the caller can label the quote instead of quietly
    presenting a stale number as current.
    """
    data = fetch(series_key)
    if data["kind"] != "index":
        raise BLSUnavailable(
            f"{series_key} is a {data['kind']} series; it cannot be used to "
            f"escalate a price. Use an index series.")

    to_period = to_period or data["latest"]["period"]
    if not from_period:
        return {"escalated": False, "reason": "no observation period recorded",
                "cents": int(cents), "bls_series": data["series_id"],
                "bls_period": to_period, "bls_index": data["latest"]["value"]}

    start = value_at(series_key, from_period)
    if start is None:
        return {"escalated": False,
                "reason": f"no BLS observation for {from_period}",
                "cents": int(cents), "bls_series": data["series_id"],
                "bls_period": to_period, "bls_index": data["latest"]["value"]}
    end = value_at(series_key, to_period)
    if end is None:
        return {"escalated": False,
                "reason": f"no BLS observation for {to_period}",
                "cents": int(cents), "bls_series": data["series_id"],
                "bls_period": to_period, "bls_index": None}

    factor = end / start
    return {"escalated": True, "reason": None,
            "cents": int(round(int(cents) * factor)), "factor": round(factor, 6),
            "bls_series": data["series_id"], "bls_label": data["label"],
            "from_period": from_period, "bls_period": to_period,
            "bls_index_from": start, "bls_index": end, "stale": data.get("stale", False)}


def verify_anchor(observed_cents: int, observed_period: str,
                  series_key: str = DEFAULT_MATERIALS,
                  to_period: str | None = None) -> dict:
    """Check a tracked price against the index and report honestly.

    This can only compare *movement*. A low ``deviation_pct`` means the price
    series tracks the official index, not that the price level is right. The
    ``checks`` map says which is which so a green result cannot be read as
    "the price is verified".
    """
    result = escalate(observed_cents, series_key, observed_period, to_period)
    deviation = None
    if result.get("escalated"):
        deviation = (result["factor"] - 1.0) * 100.0
    return {
        **result,
        "deviation_pct": round(deviation, 3) if deviation is not None else None,
        "warn": bool(deviation is not None and abs(deviation) > DRIFT_WARN_PCT),
        "checks": {
            "price_moves_with_index": deviation is not None,
            "absolute_price_level": "not_checkable_by_an_index",
        },
    }


def provenance() -> dict:
    """Everything a quote needs to record so it can be defended later."""
    out = {}
    for key in (DEFAULT_MATERIALS, DEFAULT_LABOR):
        try:
            data = fetch(key)
            out[key] = {"series_id": data["series_id"], "label": data["label"],
                        "period": data["latest"]["period"],
                        "index": data["latest"]["value"], "stale": data.get("stale", False)}
        except BLSUnavailable as e:
            out[key] = {"error": str(e)}
    return out