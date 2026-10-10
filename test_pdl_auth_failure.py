"""A rejected PDL key must never look like "this person has no phone".

    python3 -m pytest test_pdl_auth_failure.py -v

Measured on 2026-10-10 against the live API with the production key: ten real
Norfolk permit addresses returned zero records under every query shape tried
(exact address string, with and without the residential filter, and
street+city components). Raw HTTP showed why -- a 401 on all of them.

PDL returns a byte-identical body for the real key, a deliberately invalid key
and an empty key:

    HTTP 401 {"error": {"type": ["authentication_error"],
                        "message": "Your request is missing an api key."}}

so a dead credential cannot be told apart from a missing header by reading the
response. It has to be surfaced explicitly, which is what these tests pin --
alongside the caching rule that makes fixing the key actually work.

No network: the PDL client is stubbed.
"""
import pytest

import enrichment
import pdl_contact_service as pdl


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


AUTH_BODY = {"status": 401,
             "error": {"type": ["authentication_error"],
                       "message": "Your request is missing an api key."}}


def _stub(monkeypatch, resp):
    import sys, types
    mod = types.ModuleType("peopledatalabs")

    class PDLPY:
        def __init__(self, api_key=""):
            self.person = types.SimpleNamespace(search=lambda **kw: resp)

    mod.PDLPY = PDLPY
    monkeypatch.setitem(sys.modules, "peopledatalabs", mod)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("PDL_API_KEY", "x" * 64)
    enrichment.SCHEMA_READY = False


# ── the provider layer ──────────────────────────────────

def test_auth_failure_is_not_a_miss(configured, monkeypatch):
    _stub(monkeypatch, _Resp(401, AUTH_BODY))
    out = enrichment._enrich_address("4701 Winthrop Street, Norfolk, VA 23513")
    assert out.get("_auth_failed") is True
    assert "rejected the API key" in out["message"]


def test_a_real_miss_is_still_a_miss(configured, monkeypatch):
    """The distinction this file exists to protect: 0 results != 401."""
    _stub(monkeypatch, _Resp(200, {"total": 0, "data": []}))
    out = enrichment._enrich_address("4701 Winthrop Street, Norfolk, VA 23513")
    assert out == {}
    assert "_auth_failed" not in out


def test_a_real_hit_is_unaffected(configured, monkeypatch):
    # PDL returns the email under "address" and the phone under "number";
    # _pick_email/_pick_phone read exactly those keys.
    _stub(monkeypatch, _Resp(200, {"total": 1, "data": [{
        "first_name": "Ada", "last_name": "Byron",
        "emails": [{"address": "ada@example.com"}],
        "phone_numbers": [{"number": "+17555550100"}]}]}))
    out = enrichment._enrich_address("x")
    assert out.get("email") == "ada@example.com"
    assert "_auth_failed" not in out


# ── the service layer ───────────────────────────────────

def test_fetch_contact_reports_the_credential_fault(configured, monkeypatch):
    """The regression: this used to say 'No contact found for this address',
    on every lead, which reads like thin data rather than a dead key."""
    _stub(monkeypatch, _Resp(401, AUTH_BODY))
    out = pdl.fetch_contact("4701 Winthrop Street, Norfolk, VA 23513")
    assert out["found"] is False
    assert out.get("auth_failed") is True
    assert "No contact found" not in out.get("message", "")
    assert "EVERY address" in out["message"]


def test_genuine_miss_still_says_no_contact_found(configured, monkeypatch):
    _stub(monkeypatch, _Resp(200, {"total": 0, "data": []}))
    out = pdl.fetch_contact("4701 Winthrop Street, Norfolk, VA 23513")
    assert out["found"] is False
    assert "No contact found" in out["message"]
    assert not out.get("auth_failed")


# ── caching: the rule that makes fixing the key work ────

class _Cur:
    def __init__(self):
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self.rowcount = 1

    def fetchone(self):
        return None


def test_auth_failure_is_never_cached(configured, monkeypatch):
    """If a credential fault were cached, repairing the key would change
    nothing and every address would stay negative until someone hand-cleared
    the table."""
    _stub(monkeypatch, _Resp(401, AUTH_BODY))
    cur = _Cur()
    pdl.lookup("4701 Winthrop Street", "Norfolk", "VA", "23513", cur=cur)
    wrote_cache = any("INSERT INTO pdl_contact_cache" in s for s in cur.executed)
    assert not wrote_cache, "a rejected key must not poison the negative cache"


def test_genuine_miss_is_still_cached(configured, monkeypatch):
    """The whole reason the cache exists: do not re-bill a dead address."""
    _stub(monkeypatch, _Resp(200, {"total": 0, "data": []}))
    cur = _Cur()
    pdl.lookup("4701 Winthrop Street", "Norfolk", "VA", "23513", cur=cur)
    assert any("INSERT INTO pdl_contact_cache" in s for s in cur.executed)


def test_purge_helper_targets_only_negative_rows(configured, monkeypatch):
    """Clearing the cache after a repair must not throw away real hits."""
    import construction_main
    purges = getattr(construction_main, "purge_pdl_negative_cache", None)
    assert callable(purges), "no way to clear misses created under a dead key"
    sqls = []
    cur = _Cur()
    cur.fetchone = lambda: type("R", (), {"found": False})()
    purges(cur)
    sqls = cur.executed
    assert any("found = FALSE" in s or "found=FALSE" in s for s in sqls)
    assert not any("DELETE FROM pdl_contact_cache" in s and "TRUE" in s for s in sqls)