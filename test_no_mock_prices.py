"""No fabricated price may reach a customer surface.

These pin the rule rather than the implementation: the seeded brand catalogue
carries no price, an unpriced item stays unpriced, the internal estimate layer
moves only by the official BLS index, and the migration clears invented seeded
prices without touching anything the owner entered.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

import materials_service as ms
import cost_indices as ci


def test_brand_catalog_ships_no_prices():
    """The 17 seeded rows must not carry a number we invented."""
    assert ms.BRAND_CATALOG, "the real brand/SKU identities are still wanted"
    for row in ms.BRAND_CATALOG:
        assert not row.get("price_cents"), f"{row['name'][:40]} seeds a price"
        assert not row.get("price_high_cents"), f"{row['name'][:40]} seeds a high"


def test_brand_catalog_keeps_the_real_product_identity():
    """Dropping prices must not throw away the brand/style/SKU detail."""
    for row in ms.BRAND_CATALOG:
        assert row.get("brand") and row.get("name") and row.get("unit")
    assert sum(1 for r in ms.BRAND_CATALOG if r.get("sku")) > 0
    assert any(r.get("style") and r.get("color") for r in ms.BRAND_CATALOG)


def test_no_seeded_row_claims_a_ballpark():
    for row in ms.BRAND_CATALOG:
        assert "ballpark" not in (row.get("notes") or "").lower()


def test_escalation_uses_the_official_index(monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ms, "_escalation_cache", {"at": 0.0, "payload": None})
    monkeypatch.setattr(ci, "_http_post", lambda u, p: {
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"seriesID": "WPU1017", "data": [
            {"year": "2026", "period": "M09", "value": "388.580"},
            {"year": "2026", "period": "M01", "value": "315.000"}]}]}})
    esc = ms.bls_escalation()
    assert esc["escalated"] is True
    assert esc["bls_series"] == "WPU1017"
    assert esc["bls_period"] == "2026-09"
    assert esc["factor"] == pytest.approx(388.580 / 315.000, abs=1e-6)


def test_escalation_degrades_to_one_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ms, "_escalation_cache", {"at": 0.0, "payload": None})
    monkeypatch.setattr(ci, "_http_post", lambda u, p: (_ for _ in ()).throw(
        ci.BLSUnavailable("down")))
    esc = ms.bls_escalation()
    assert esc["factor"] == 1.0
    assert esc["escalated"] is False
    assert esc["reason"]


def test_escalation_leaves_zero_alone(monkeypatch):
    """No price must stay no price. Escalating 0 upward would invent a quote."""
    monkeypatch.setattr(ms, "bls_escalation", lambda: {
        "factor": 1.5, "escalated": True, "reason": None,
        "bls_series": "WPU1017", "bls_period": "2026-09", "bls_index": 388.58,
        "base_period": "2026-01"})
    assert ms._escalated_cents(0) == 0
    assert ms._escalated_cents(None) == 0


def test_escalation_moves_a_real_value(monkeypatch):
    monkeypatch.setattr(ms, "bls_escalation", lambda: {
        "factor": 1.25, "escalated": True, "reason": None,
        "bls_series": "WPU1017", "bls_period": "2026-09", "bls_index": 388.58,
        "base_period": "2026-01"})
    assert ms._escalated_cents(8000) == 10000


def test_estimate_materials_labels_the_bls_period(monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ms, "bls_escalation", lambda: {
        "factor": 1.2, "escalated": True, "reason": None,
        "bls_series": "WPU1017", "bls_period": "2026-09", "bls_index": 388.58,
        "base_period": "2026-01"})
    out = ms.BusinessMaterialsService().estimate_materials("roofing", 2000)
    assert out["source"] == "estimate+WPU1017@2026-09"
    assert out["pricing"]["bls_series"] == "WPU1017"
    assert out["pricing"]["absolute_source"] == "not a supplier price; an internal allowance"
    assert "Estimate" in out["note"]


def test_estimate_materials_warns_when_not_escalated(monkeypatch):
    monkeypatch.setattr(ms, "bls_escalation", lambda: {
        "factor": 1.0, "escalated": False, "reason": "BLS unreachable",
        "bls_series": None, "bls_period": None, "bls_index": None,
        "base_period": "2026-01"})
    out = ms.BusinessMaterialsService().estimate_materials("roofing", 2000)
    assert out["source"] == "estimate-stale"
    assert "NOT index-escalated" in out["note"]
    assert "BLS unreachable" in out["note"]


def test_estimate_materials_output_carries_no_ballpark_wording(monkeypatch, tmp_path):
    monkeypatch.setattr(ci, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ms, "_escalation_cache", {"at": 0.0, "payload": None})
    out = ms.BusinessMaterialsService().estimate_materials("kitchen", 200)
    blob = " ".join(str(v) for v in (out["source"], out["note"])).lower()
    assert "ballpark" not in blob


# --- migration ---------------------------------------------------------------

class _FakeCur:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params


class _FakeConn:
    def __init__(self, rowcount=1):
        self._cur = _FakeCur(rowcount)
        self.committed = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed += 1


def test_retire_clears_fabricated_seeded_prices():
    conn = _FakeConn(rowcount=4)
    assert ms.retire_fabricated_prices(conn) == 4
    assert "price_cents = 0" in conn._cur.sql


def test_retire_does_nothing_when_there_is_nothing_to_clear():
    conn = _FakeConn(rowcount=0)
    assert ms.retire_fabricated_prices(conn) == 0
    assert conn.committed == 0, "no change means no commit"


def test_retire_targets_only_the_fabricated_marker():
    """It must not become `WHERE price_cents > 0`, which would wipe owner rows."""
    conn = _FakeConn(rowcount=1)
    ms.retire_fabricated_prices(conn)
    sql = conn._cur.sql
    assert "notes ILIKE" in sql
    assert sql.count("notes ILIKE") == len(ms._FABRICATED_MARKERS)
    assert "price_cents <> 0" in sql
    assert conn._cur.params[1:] == ms._FABRICATED_MARKERS
    assert conn._cur.params[0] == ms.NO_PRICE_NOTE


def test_retire_handles_no_connection():
    assert ms.retire_fabricated_prices(None) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

# --- privilege: a sign-in must not grant the crew app -------------------------

def test_unknown_google_sign_in_is_not_given_worker():
    """Regression: the OAuth callback defaulted every unrecognised account to
    `worker`, so any customer clicking "sign in with Google" on the public
    sandbox was handed time sheets, payroll and internal jobs."""
    from auth_service import ROLES, CREW_ROLES

    assert "customer" in ROLES
    assert "customer" not in CREW_ROLES
    assert "worker" in CREW_ROLES, "crew must still be a role"


def test_customer_role_has_no_crew_features():
    import auth_service
    assert "customer" not in getattr(auth_service, "CREW_FEATURES", {})


def test_login_redirects_a_customer_to_their_account():
    import construction_main as m
    assert m._login_redirect("admin") == "/dashboard"
    assert m._login_redirect("customer") == "/account"


# --- who is an owner ---------------------------------------------------------

def test_admin_email_matches_any_listed_address(monkeypatch):
    """An owner signs in with more than one account over time. With a single
    ADMIN_EMAIL the second one was silently downgraded to an ordinary user and
    locked out of their own dashboard."""
    import construction_main as m
    monkeypatch.setenv("ADMIN_EMAIL", "soleary0006@gmail.com")
    monkeypatch.setenv("ADMIN_EMAILS", "hello@bizstackperks.com, ops@bizstackperks.com")
    assert m.is_admin_email("soleary0006@gmail.com")
    assert m.is_admin_email("hello@bizstackperks.com")
    assert m.is_admin_email("OPS@BizStackPerks.com")
    assert not m.is_admin_email("stranger@example.com")


def test_single_admin_env_still_works(monkeypatch):
    import construction_main as m
    monkeypatch.setenv("ADMIN_EMAIL", "soleary0006@gmail.com")
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    assert m.is_admin_email("soleary0006@gmail.com")
    assert not m.is_admin_email("hello@bizstackperks.com")


def test_admin_match_ignores_whitespace_and_case(monkeypatch):
    import construction_main as m
    monkeypatch.setenv("ADMIN_EMAIL", "a@b.com")
    monkeypatch.setenv("ADMIN_EMAILS", "  c@d.com , e@f.com  ")
    assert m.is_admin_email(" C@D.com ")
    assert m.is_admin_email("e@f.com")
    assert not m.is_admin_email("")


def test_blank_entries_are_ignored(monkeypatch):
    import construction_main as m
    monkeypatch.setenv("ADMIN_EMAIL", "a@b.com")
    monkeypatch.setenv("ADMIN_EMAILS", ",,  ,")
    assert m.admin_emails() == {"a@b.com"}
