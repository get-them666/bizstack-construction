"""The Zoho redirect URI must be exactly what is registered on the client.

Zoho compares the value in the authorize request with the one saved on the
OAuth client and rejects the sign-in before it ever reaches this app, with
"Invalid Redirect Uri". A scheme or trailing slash leaked into the derived
value used to make that mismatch impossible to spot, so the derivation is
pinned here.

No network: environment only.
"""

import zoho_oauth

ENV_KEYS = ("ZOHO_REDIRECT_URI", "APP_BASE_URL", "COMPANY_DOMAIN")


def _clean(monkeypatch, **env):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_an_explicit_redirect_uri_is_used_verbatim(monkeypatch):
    _clean(monkeypatch, ZOHO_REDIRECT_URI="https://old.example.com/zoho/cb",
           APP_BASE_URL="https://construction.bizstackperks.com")
    assert zoho_oauth.redirect_uri() == "https://old.example.com/zoho/cb"


def test_the_deployed_base_url_wins_and_a_trailing_slash_is_removed(monkeypatch):
    _clean(monkeypatch, APP_BASE_URL="https://construction.bizstackperks.com/")
    assert zoho_oauth.redirect_uri() == "https://construction.bizstackperks.com/oauth/zoho/callback"


def test_a_domain_with_a_scheme_does_not_produce_a_double_scheme(monkeypatch):
    _clean(monkeypatch, COMPANY_DOMAIN="https://construction.bizstackperks.com/")
    assert zoho_oauth.redirect_uri() == "https://construction.bizstackperks.com/oauth/zoho/callback"


def test_the_default_domain_is_the_live_site(monkeypatch):
    _clean(monkeypatch)
    assert zoho_oauth.redirect_uri() == "https://construction.bizstackperks.com/oauth/zoho/callback"


def test_settings_shows_the_uri_and_explains_the_self_client_case():
    from pathlib import Path
    html = Path("templates/construction/settings.html").read_text(encoding="utf-8")
    assert "zoho-redirect-uri" in html
    assert "Invalid Redirect Uri" in html
    assert "Self Client" in html
    assert "Authorized Redirect URIs" in html
