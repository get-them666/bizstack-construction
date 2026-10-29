"""Zoho sync rules: who reaches the CRM, what the Campaigns call looks like."""
import json

import zoho_sync as zs
import zoho_campaigns as zc
import zoho_oauth as zo


def test_placeholder_permit_leads_are_not_crm_contacts():
    assert not zs.reachable({"email": "con-permit-abc@lead.local", "phone": ""})
    assert zs.reachable({"email": "jane@example.com", "phone": ""})
    assert zs.reachable({"email": "con-permit-abc@lead.local", "phone": "(757) 555-0100"})


def test_payload_never_carries_a_placeholder_email():
    p = zs._payload({"name": "Jane Smith", "email": "x@lead.local", "phone": "7575550100",
                     "status": "quoted", "source": "permit_finder", "project_type": "Roof", "notes": ""})
    assert "Email" not in p
    assert p["Lead_Status"] == "Pre-Qualified" and p["First_Name"] == "Jane" and p["Last_Name"] == "Smith"
    assert p["Description"].startswith("Project: Roof")


def test_legacy_entry_point_skips_placeholders():
    assert zs.send_to_zoho_crm({"Email": "con-permit-1@lead.local", "Phone": ""}) is None


def test_campaigns_call_is_form_encoded_with_json_contactinfo(monkeypatch):
    captured = {}
    class R:
        status_code = 200
        text = "{}"
        def json(self): return {"status": "success"}
    def fake_request(method, url, **kw):
        captured.update(method=method, url=url, **kw); return R()
    monkeypatch.setattr(zc.zoho_client, "is_configured", lambda: True)
    monkeypatch.setattr(zc.zoho_client, "api_request", fake_request)
    monkeypatch.setattr(zc, "list_key", lambda: "LISTKEY")
    assert zc.add_subscriber({"name": "Jane Smith", "email": "jane@example.com"})
    assert captured["url"].endswith("/json/listsubscribe")
    assert captured["params"]["listkey"] == "LISTKEY" and captured["params"]["resfmt"] == "JSON"
    info = json.loads(captured["params"]["contactinfo"])
    assert info == {"Contact Email": "jane@example.com", "First Name": "Jane", "Last Name": "Smith"}
    assert "json" not in captured, "the Campaigns API takes parameters, not a JSON body"


def test_oauth_scope_status_reads_capabilities(monkeypatch):
    monkeypatch.setattr(zo, "_db", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    monkeypatch.setenv("ZOHO_REFRESH_TOKEN", "x")
    monkeypatch.setenv("ZOHO_TOKEN_SCOPES", "ZohoCRM.modules.ALL ZohoCampaigns.contact.ALL")
    st = zo.status()
    assert st["connected"] and st["source"] == "environment"
    assert st["crm"] and st["campaigns"] and not st["mail"] and not st["calendar"]
    assert "ZohoMail.messages.CREATE" in zo.SCOPES and "ZohoCalendar.event.ALL" in zo.SCOPES
    assert zo.redirect_uri().endswith("/oauth/zoho/callback")
