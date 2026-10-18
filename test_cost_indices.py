"""BLS cost indices: offline, and explicit about what an index cannot check.

The important assertions here are the refusals. An index series is used to
escalate; a percent-change series is rejected; a missing observation period
returns the input unchanged with ``escalated`` False rather than a plausible
wrong number.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

import cost_indices as ci

MATERIALS = {
    "BLS_SERIES": ci.SERIES,
    "status": "REQUEST_SUCCEEDED",
    "Results": {"series": [{
        "seriesID": "WPU1017",
        "data": [
            {"year": "2026", "period": "M09", "value": "388.580"},
            {"year": "2026", "period": "M08", "value": "386.100"},
            {"year": "2026", "period": "M07", "value": "380.000"},
            {"year": "2026", "period": "M13", "value": "385.000"},
            {"year": "2025", "period": "M12", "value": "370.000"},
            {"year": "2025", "period": "M06", "value": "300.000"},
        ],
    }]},
}


@pytest.fixture(autouse=True)
def _no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ci, "BLS_API_KEY", "")


def _stub(monkeypatch, payload=MATERIALS, code=None):
    def fake(url, payload_body):
        if code is not None:
            raise ci.BLSUnavailable(f"BLS HTTP {code}")
        return payload
    monkeypatch.setattr(ci, "_http_post", fake)


def test_keyless_request_uses_v1(monkeypatch):
    seen = {}

    def fake(url, payload_body):
        seen["url"] = url
        return MATERIALS
    monkeypatch.setattr(ci, "_http_post", fake)
    ci.fetch("construction_materials")
    assert seen["url"] == ci.API_V1, "no key must use v1; v2 refuses keyless calls"


def test_v2_is_used_when_a_key_exists(monkeypatch):
    monkeypatch.setattr(ci, "BLS_API_KEY", "abc123")
    seen = {}
    monkeypatch.setattr(ci, "_http_post", lambda url, p: (seen.update(url=url), MATERIALS)[1])
    ci.fetch("construction_materials")
    assert seen["url"] == ci.API_V2


def test_monthly_periods_normalise_and_m13_is_dropped(monkeypatch):
    _stub(monkeypatch)
    data = ci.fetch("construction_materials")
    periods = [o["period"] for o in data["observations"]]
    assert periods == ["2026-09", "2026-08", "2026-07", "2025-12", "2025-06"]
    assert "2026-13" not in periods, "M13 is an annual average, not a month"


def test_observations_are_newest_first(monkeypatch):
    _stub(monkeypatch)
    data = ci.fetch("construction_materials")
    assert data["latest"]["period"] == "2026-09"
    assert data["latest"]["value"] == 388.580


def test_quarterly_periods_normalise(monkeypatch):
    _stub(monkeypatch, {
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"seriesID": "CIU2020000000000I", "data": [
            {"year": "2026", "period": "Q2", "value": "179.304"},
            {"year": "2026", "period": "Q1", "value": "176.000"}]}]}})
    data = ci.fetch("labor_comp_private")
    assert [o["period"] for o in data["observations"]] == ["2026-Q2", "2026-Q1"]


def test_unknown_series_raises_instead_of_defaulting(monkeypatch):
    monkeypatch.setattr(ci, "_http_post", lambda u, p: (_ for _ in ()).throw(
        AssertionError("must not call BLS for an unknown series")))
    with pytest.raises(ci.BLSUnavailable, match="unknown BLS series"):
        ci.fetch("not_a_real_series")


def test_rejected_status_raises_with_the_message(monkeypatch):
    _stub(monkeypatch, {"status": "REQUEST_FAILED", "message": ["Series does not exist"]})
    with pytest.raises(ci.BLSUnavailable, match="does not exist"):
        ci.fetch("construction_materials")


def test_empty_result_raises(monkeypatch):
    _stub(monkeypatch, {"status": "REQUEST_SUCCEEDED",
                        "Results": {"series": [{"seriesID": "WPU1017", "data": []}]}})
    with pytest.raises(ci.BLSUnavailable, match="no observations"):
        ci.fetch("construction_materials")


def test_http_error_is_wrapped(monkeypatch):
    _stub(monkeypatch, code=503)
    with pytest.raises(ci.BLSUnavailable, match="HTTP 503"):
        ci.fetch("construction_materials")


def test_ratio_across_periods(monkeypatch):
    _stub(monkeypatch)
    # 388.580 / 380.000
    assert ci.ratio("construction_materials", "2026-07", "2026-09") == pytest.approx(
        388.580 / 380.000)


def test_ratio_is_none_for_an_unavailable_period(monkeypatch):
    _stub(monkeypatch)
    assert ci.ratio("construction_materials", "2019-01", "2026-09") is None


def test_escalate_moves_a_price_by_the_official_index(monkeypatch):
    _stub(monkeypatch)
    out = ci.escalate(10000, "construction_materials", "2026-07", "2026-09")
    assert out["escalated"] is True
    assert out["cents"] == round(10000 * (388.580 / 380.000))
    assert out["bls_series"] == "WPU1017"
    assert out["bls_period"] == "2026-09"


def test_escalate_without_a_observed_period_does_not_guess(monkeypatch):
    """Nothing to escalate from, so the price must not move."""
    _stub(monkeypatch)
    out = ci.escalate(10000, "construction_materials")
    assert out["escalated"] is False
    assert out["cents"] == 10000
    assert "no observation period" in out["reason"]


def test_escalate_with_an_unknown_period_keeps_the_price_and_explains(monkeypatch):
    _stub(monkeypatch)
    out = ci.escalate(10000, "construction_materials", "2019-01")
    assert out["escalated"] is False
    assert out["cents"] == 10000
    assert "2019-01" in out["reason"]


def test_escalate_refuses_a_percent_change_series(monkeypatch):
    """3.4 is a rate, not a level. Dividing it by 176 would be meaningless."""
    _stub(monkeypatch, {
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"seriesID": "CIU1010000000000A", "data": [
            {"year": "2026", "period": "Q2", "value": "3.4"}]}]}})
    with pytest.raises(ci.BLSUnavailable, match="pct_change"):
        ci.escalate(10000, "labor_wages_private", "2026-Q1", "2026-Q2")


def test_verify_anchor_reports_movement_but_admits_it_cannot_check_level(monkeypatch):
    _stub(monkeypatch)
    out = ci.verify_anchor(10000, "2026-07", "construction_materials")
    assert out["checks"]["price_moves_with_index"] is True
    assert out["checks"]["absolute_price_level"] == "not_checkable_by_an_index"
    assert out["deviation_pct"] == pytest.approx((388.580 / 380.000 - 1) * 100, abs=0.01)


def test_verify_anchor_flags_large_drift(monkeypatch):
    _stub(monkeypatch)
    out = ci.verify_anchor(10000, "2025-06", "construction_materials")
    assert out["deviation_pct"] == pytest.approx((388.580 / 300.000 - 1) * 100, abs=0.01)
    assert out["warn"] is True


def test_small_drift_is_not_flagged(monkeypatch):
    """+5% over nine months is ordinary, not a signal worth interrupting for."""
    _stub(monkeypatch)
    out = ci.verify_anchor(10000, "2025-12", "construction_materials")
    assert out["deviation_pct"] == pytest.approx((388.580 / 370.000 - 1) * 100, abs=0.01)
    assert out["warn"] is False


def test_verify_anchor_does_not_warn_on_small_drift(monkeypatch):
    _stub(monkeypatch)
    assert ci.verify_anchor(10000, "2026-08", "construction_materials")["warn"] is False


def test_verify_anchor_without_a_period_is_not_a_pass(monkeypatch):
    _stub(monkeypatch)
    out = ci.verify_anchor(10000, "")
    assert out["checks"]["price_moves_with_index"] is False
    assert out["warn"] is False
    assert out["escalated"] is False


def test_fresh_cache_never_calls_bls(monkeypatch):
    calls = []

    def fake(url, payload_body):
        calls.append(url)
        return MATERIALS
    monkeypatch.setattr(ci, "_http_post", fake)
    ci.fetch("construction_materials")
    ci.fetch("construction_materials")
    assert len(calls) == 1, "second call should be served from cache"
    assert ci.fetch("construction_materials")["stale"] is False


def test_stale_cache_is_served_when_bls_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ci, "_http_post", lambda u, p: MATERIALS)
    ci.fetch("construction_materials")
    monkeypatch.setattr(ci, "CACHE_TTL", -1)
    monkeypatch.setattr(ci, "_http_post", lambda u, p: (_ for _ in ()).throw(
        ci.BLSUnavailable("down")))
    out = ci.fetch("construction_materials")
    assert out["stale"] is True
    assert out["latest"]["value"] == 388.580


def test_cache_ttl_is_resolved_at_call_time(tmp_path, monkeypatch):
    """A default argument would bind at import and make this inert."""
    monkeypatch.setattr(ci, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ci, "_http_post", lambda u, p: MATERIALS)
    ci.fetch("construction_materials")
    monkeypatch.setattr(ci, "CACHE_TTL", -1)
    calls = []

    def counting(url, payload_body):
        calls.append(url)
        return MATERIALS
    monkeypatch.setattr(ci, "_http_post", counting)
    ci.fetch("construction_materials")
    assert len(calls) == 1, "an expired cache must re-fetch"


def test_provenance_carries_series_period_and_index(monkeypatch):
    _stub(monkeypatch)
    prov = ci.provenance()
    assert prov["construction_materials"]["series_id"] == "WPU1017"
    assert prov["construction_materials"]["period"] == "2026-09"
    assert prov["construction_materials"]["index"] == 388.580


def test_provenance_reports_errors_per_series_without_raising(monkeypatch):
    _stub(monkeypatch, {"status": "REQUEST_FAILED", "message": ["nope"]})
    prov = ci.provenance()
    assert "error" in prov["construction_materials"]


def test_every_registered_series_has_a_real_id_shape():
    for key, s in ci.SERIES.items():
        assert s.kind in ("index", "pct_change"), key
        assert s.freq in ("M", "Q"), key
        assert s.series_id[:1] in "WPUC", f"{key} looks unlike a BLS id"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))