"""Settings page status helpers.

    venv/bin/python -m pytest test_settings_status.py -v

The flag parser is the important part: Railway stores an unset toggle as the
string "false", which is truthy, so bare os.getenv() truthiness reported
every background job as switched off (the bug test_flag_semantics.py was
written for). The OpenAI probe is exercised with urlopen patched.
"""
import io
import urllib.error

import pytest

import settings_status as ss


@pytest.mark.parametrize("raw,expected", [
    ("false", False), ("False", False), ("0", False), ("", False), ("off", False),
    ("true", True), ("1", True), ("on", True), ("YES", True),
])
def test_railway_false_string_is_off(monkeypatch, raw, expected):
    monkeypatch.setenv("DISABLE_LEAD_SCAN", raw)
    assert ss.flag_on("DISABLE_LEAD_SCAN") is expected
    job = next(j for j in ss.background_jobs() if j["switch"] == "DISABLE_LEAD_SCAN")
    assert job["off"] is expected


def test_review_mode_reads_as_review_mode(monkeypatch):
    monkeypatch.setenv("LEAD_AUTO_SEND", "false")
    assert "review mode" in ss.outbound_guardrails()[0]["value"]
    monkeypatch.setenv("LEAD_AUTO_SEND", "1")
    assert "ON" in ss.outbound_guardrails()[0]["value"]


def _patch_urlopen(monkeypatch, exc=None, status=200):
    class _Resp:
        def __init__(self): self.status = status
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake(req, timeout=0):
        if exc:
            raise exc
        return _Resp()
    monkeypatch.setattr(ss.urllib.request, "urlopen", fake)
    ss._OPENAI_CACHE["at"] = 0.0
    ss._OPENAI_CACHE["result"] = None


def test_rejected_key_is_named_with_the_fix(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-dead")
    _patch_urlopen(monkeypatch, urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"")))
    h = ss.openai_health(force=True)
    assert h["configured"] and not h["ok"]
    assert "401" in h["detail"] and "OPENAI_API_KEY" in h["detail"]


def test_good_key_is_cached(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-ok")
    _patch_urlopen(monkeypatch)
    assert ss.openai_health(force=True)["ok"] is True
    # Second call within the TTL must not hit the network even if it would fail.
    _patch_urlopen(monkeypatch, RuntimeError("network"))
    ss._OPENAI_CACHE["at"] = __import__("time").time()
    ss._OPENAI_CACHE["result"] = {"ok": True, "configured": True, "detail": "", "model": "m", "checked_at": ""}
    assert ss.openai_health()["ok"] is True


def test_missing_key_never_calls_out(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _patch_urlopen(monkeypatch, RuntimeError("must not be called"))
    h = ss.openai_health(force=True)
    assert not h["configured"] and "not set" in h["detail"]


def test_email_status_flags_the_legacy_transport(monkeypatch):
    monkeypatch.setenv("EMAIL_TRANSPORT", "legacy")
    assert ss.email_status(None)["legacy_warning"] is True
    monkeypatch.setenv("EMAIL_TRANSPORT", "gmail")
    assert ss.email_status({"ok": True, "detail": "x", "checked_at": ""})["ok"] is True
