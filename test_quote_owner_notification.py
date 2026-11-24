"""Every quote submission must alert the owner immediately.

Root cause this locks down: the website quote form posts to ``/submit-lead``,
which saved the lead and then ran a synchronous, best-effort owner alert with
no delivery record. A customer could get their ballpark while the owner heard
nothing. The instant quote, get-started and embed widget routes each had their
own duplicate call. They now all use one background helper that cannot block
or fail the customer's response.
"""

import asyncio
import inspect
from datetime import datetime, timezone

import pytest
from starlette.requests import Request

import construction_main as cm


@pytest.fixture
def owner_env(monkeypatch):
    monkeypatch.setenv("NOTIFY_EMAIL", "owner@example.com")
    monkeypatch.setenv("OWNER_MOBILE", "+17575550100")
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    return {"email": "owner@example.com", "mobile": "+17575550100"}


def _stub_email(monkeypatch, sent, *, delivered=True, boom=False):
    async def fake_send_email(cfg, to, subject, body):
        if boom:
            raise RuntimeError("smtp down")
        sent.append({"to": to, "subject": subject, "body": body})
        return delivered

    monkeypatch.setattr(cm.documents_service, "smtp_config_from_env", lambda: {"stub": True})
    monkeypatch.setattr(cm.documents_service, "smtp_configured", lambda cfg: True)
    monkeypatch.setattr(cm.documents_service, "send_email", fake_send_email)
    monkeypatch.setattr(cm.auto_reply, "run_coro", lambda coro: asyncio.run(coro))


def _stub_sms(monkeypatch, sent, *, ok=True, boom=False):
    class FakeSignalWire:
        def __init__(self):
            self.from_number = "+17578469275"

        def is_configured(self):
            return True

        def send_sms(self, to, body):
            if boom:
                raise RuntimeError("signalwire down")
            sent.append({"to": to, "body": body})
            return ok

    monkeypatch.setattr(cm, "SignalWireService", FakeSignalWire)


# --- the shared helper -------------------------------------------------------

def test_owner_quote_alert_sends_email_and_sms(monkeypatch, owner_env):
    emails, texts = [], []
    _stub_email(monkeypatch, emails)
    _stub_sms(monkeypatch, texts)

    result = cm._owner_quote_alert(
        lead_id=2348,
        name="Mathew Mccloud",
        phone="2568668581",
        email="mccloud_m@aol.com",
        service="Whole-Home Renovation",
        address="115 Ollin Dr, Newport News, VA 23602",
        source="quotes_page",
        quote_range="$78,750–$157,500",
        message="Interested in a whole-home renovation.",
        created_at=datetime(2026, 11, 2, 12, 26, 1, tzinfo=timezone.utc),
    )

    assert result["email_sent"] == 1
    assert result["sms"] is True
    assert len(emails) == 1 and len(texts) == 1
    assert emails[0]["to"] == owner_env["email"]
    assert "#2348" in emails[0]["subject"]
    assert "Whole-Home Renovation" in emails[0]["subject"]
    # Submitted time is shown in the owner's timezone, not UTC.
    assert "Nov 02, 2026 7:26 AM EST" in emails[0]["body"]
    assert "https://construction.bizstackperks.com/leads/2348" in emails[0]["body"]
    assert "$78,750–$157,500" in emails[0]["body"]
    assert "2568668581" in emails[0]["body"]
    # The SMS is short, but carries the ID, name, phone, service, and range.
    assert texts[0]["to"] == owner_env["mobile"]
    assert "New BuildStack quote #2348" in texts[0]["body"]
    assert "2568668581" in texts[0]["body"]
    assert "$78,750–$157,500" in texts[0]["body"]
    assert len(texts[0]["body"]) <= 300


def test_owner_quote_alert_swallows_transport_failures(monkeypatch, owner_env):
    emails, texts = [], []
    _stub_email(monkeypatch, emails, boom=True)
    _stub_sms(monkeypatch, texts, boom=True)

    result = cm._owner_quote_alert(lead_id=77, name="Test", phone="7575550101")

    assert result["email_sent"] == 0
    assert result["sms"] is False
    assert result["error"]


def test_owner_quote_alert_treats_unconfigured_email_as_non_fatal(monkeypatch, owner_env):
    monkeypatch.setattr(cm.documents_service, "smtp_config_from_env", lambda: {})
    monkeypatch.setattr(cm.documents_service, "smtp_configured", lambda cfg: False)
    texts = []
    _stub_sms(monkeypatch, texts)

    result = cm._owner_quote_alert(lead_id=78, name="Test")

    assert result["email_sent"] == 0
    assert result["error"] == "email transport not configured"
    assert result["sms"] is True


# --- the background wrapper --------------------------------------------------

def test_background_wrapper_starts_a_daemon_thread(monkeypatch):
    captured = {}

    class FakeThread:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(cm.threading, "Thread", FakeThread)

    cm._notify_owner_quote_bg(lead_id=91, name="Thread Test")

    assert captured["started"] is True
    assert captured["daemon"] is True
    assert captured["target"] is cm._owner_quote_alert
    assert captured["kwargs"]["lead_id"] == 91


def test_background_wrapper_never_raises_when_thread_cannot_start(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("no threads left")

    monkeypatch.setattr(cm.threading, "Thread", boom)

    cm._notify_owner_quote_bg(lead_id=92)  # must not raise


# --- the /submit-lead hook ---------------------------------------------------

def _request():
    return Request({
        "type": "http", "method": "POST", "path": "/submit-lead",
        "headers": [], "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80), "scheme": "http", "query_string": b"",
    })


def test_submit_lead_starts_the_owner_alert_with_lead_details(monkeypatch):
    captured = {}

    monkeypatch.setattr(cm, "_is_form_bot", lambda website, source: False)
    monkeypatch.setattr(cm, "_lead_submit_throttled", lambda ip: False)
    monkeypatch.setattr(cm, "_client_ip", lambda request: "127.0.0.1")
    monkeypatch.setattr(cm.lead_intake, "intake_lead",
                        lambda *a, **k: {"lead_id": 4242, "created": True})
    monkeypatch.setattr(cm.leads_db, "insert_lead", lambda **k: 1)
    monkeypatch.setattr(cm.auto_reply, "_auto_quote",
                        lambda *a, **k: ("ballpark", {"low_cents": 7875000, "high_cents": 15750000}))
    monkeypatch.setattr(cm.auto_reply, "auto_reply_to_lead", lambda *a, **k: None)
    monkeypatch.setattr(cm, "_follow_up_new_lead", lambda lead_id: None)
    monkeypatch.setattr(cm, "_notify_owner_quote_bg",
                        lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(cm.lead_pipeline, "on_lead_created", lambda db, lead_id: None)
    monkeypatch.setattr(cm.zoho_sync, "after_intake", lambda db, lead_id: None)
    monkeypatch.setattr(cm.tidy_service, "enrich_and_sync_tidy", lambda **k: None)

    class FakeTask:
        def add_done_callback(self, cb):
            pass

    def fake_create_task(coro):
        coro.close()
        return FakeTask()

    monkeypatch.setattr(cm.asyncio, "create_task", fake_create_task)

    response = asyncio.run(cm.submit_lead(
        request=_request(),
        name="Test Homeowner",
        phone="7575550101",
        email="homeowner@example.com",
        project_type="Kitchen Remodel",
        address="1 Main St, Williamsburg, VA",
        budget="",
        timeline="",
        description="Need the kitchen redone.",
        goal="",
        source="quotes_page",
        company_website="",
        coupon_code="",
        discount_amount="",
        db=object(),
    ))

    assert response.status_code == 200
    assert captured["lead_id"] == 4242
    assert captured["name"] == "Test Homeowner"
    assert captured["phone"] == "7575550101"
    assert captured["email"] == "homeowner@example.com"
    assert captured["service"] == "Kitchen Remodel"
    assert captured["source"] == "quotes_page"
    assert captured["quote_range"] == "$78,750–$157,500"


# --- every quote route uses the one helper -----------------------------------

@pytest.mark.parametrize("handler", [
    cm.submit_lead,
    cm.instant_quote_submit,
    cm.get_started_submit,
    cm.embed_quote_submit,
])
def test_every_quote_route_calls_the_shared_owner_alert(handler):
    source = inspect.getsource(handler)
    assert "_notify_owner_quote_bg(" in source
    # One helper, one alert: no duplicate synchronous owner email left behind.
    assert "notify_owner_email(" not in source
    assert "send_lead_alert(" not in source
