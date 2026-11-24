"""Regressions for the P1 fixes (2026-11-23 audit, second round).

Covers Kit's honest enrollment result, the opt-out parser's subject-line
handling, the lead/bid linkage by contact email, and the locked-down owner
health endpoint. No network and no database.
"""

import os

import pytest
from fastapi.testclient import TestClient

import bid_costing
import construction_main as cm
import followups as fu
import kit_service
from bizstack_leads import lifecycle


# --- Kit: paused / failed enrollment must not claim success -----------------

def test_newsletter_reports_a_paused_sequence(monkeypatch):
    monkeypatch.setattr(kit_service, "configured", lambda: True)
    monkeypatch.setattr(kit_service, "subscribe", lambda *a, **k: 123)
    monkeypatch.setattr(kit_service, "subscriber_state", lambda sid: "active")
    monkeypatch.setattr(kit_service, "add_tag", lambda *a, **k: True)
    monkeypatch.setattr(kit_service, "add_to_sequence", lambda *a, **k: True)
    monkeypatch.setattr(kit_service, "sequence_status",
                        lambda sid: {"ok": True, "hold": True})

    result = kit_service.subscribe_newsletter("reader@example.com")

    assert result["ok"] is True
    assert result["paused"] is True
    assert result["sequence_started"] is False
    assert "resumes" in result["note"]


def test_newsletter_reports_a_failed_sequence_start(monkeypatch):
    monkeypatch.setattr(kit_service, "configured", lambda: True)
    monkeypatch.setattr(kit_service, "subscribe", lambda *a, **k: 123)
    monkeypatch.setattr(kit_service, "subscriber_state", lambda sid: "active")
    monkeypatch.setattr(kit_service, "add_tag", lambda *a, **k: True)
    monkeypatch.setattr(kit_service, "add_to_sequence", lambda *a, **k: False)
    monkeypatch.setattr(kit_service, "sequence_status",
                        lambda sid: {"ok": True, "hold": False})

    result = kit_service.subscribe_newsletter("reader@example.com")

    assert result["ok"] is False
    assert result["sequence_started"] is False
    assert "couldn't start" in result["reason"]


def test_newsletter_reports_a_bounced_address(monkeypatch):
    monkeypatch.setattr(kit_service, "configured", lambda: True)
    monkeypatch.setattr(kit_service, "subscribe", lambda *a, **k: 123)
    monkeypatch.setattr(kit_service, "subscriber_state", lambda sid: "bounced")

    result = kit_service.subscribe_newsletter("bounced@example.com")

    assert result["ok"] is False
    assert result["state"] == "bounced"
    assert "bounce" in result["reason"]


def test_lead_sync_reports_a_bounced_address(monkeypatch):
    monkeypatch.setattr(kit_service, "configured", lambda: True)
    monkeypatch.setattr(kit_service, "subscribe", lambda *a, **k: 123)
    monkeypatch.setattr(kit_service, "subscriber_state", lambda sid: "bounced")

    result = kit_service.sync_lead("Pat Doe", "bounced@example.com", "7575550100")

    assert result["ok"] is False
    assert result["state"] == "bounced"


def test_lead_sync_reports_a_failed_sequence_start(monkeypatch):
    monkeypatch.setattr(kit_service, "configured", lambda: True)
    monkeypatch.setattr(kit_service, "subscribe", lambda *a, **k: 123)
    monkeypatch.setattr(kit_service, "subscriber_state", lambda sid: "active")
    monkeypatch.setattr(kit_service, "add_tag", lambda *a, **k: True)
    monkeypatch.setattr(kit_service, "add_to_sequence", lambda *a, **k: False)

    result = kit_service.sync_lead("Pat Doe", "pat@example.com", "7575550100")

    assert result["ok"] is False
    assert result["sequence_started"] is False


# --- Opt-out parser ----------------------------------------------------------

def test_opt_out_after_a_subject_line_is_recognised():
    assert fu.is_opt_out(
        "Re: Free estimate for 4905 Athens Blvd, BuildStack Construction\n\nUnsubscribe")


def test_polite_remove_from_your_list_is_recognised():
    assert fu.is_opt_out("Re: Roof\n\nPlease remove me from your list")


def test_stop_by_is_still_ordinary_prose():
    assert not fu.is_opt_out("Can you stop by Tuesday?")


def test_cancel_my_appointment_is_not_a_bare_cancel():
    assert not fu.is_opt_out("I'd like to cancel my appointment")


# --- Lifecycle vocabulary ----------------------------------------------------

def test_scheduled_legacy_rows_are_quoted_not_new():
    # 39 production rows sat in this status and were read as 'new'.
    assert lifecycle.to_stage("scheduled") == lifecycle.QUOTED


# --- Bid -> lead linkage -----------------------------------------------------

class _Cursor:
    def __init__(self, lead_row):
        self.lead_row = lead_row
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.lead_row


def test_bid_project_links_the_matching_lead_and_marks_it_won():
    cur = _Cursor({"id": 77})
    bid_costing._link_bid_lead_to_project(
        cur, {"contact_email": "Client@Example.com"}, "construction", 42)

    sqls = [" ".join(sql.split()) for sql, _ in cur.executed]
    assert any("LOWER(email) = %s" in sql for sql in sqls)
    assert any("UPDATE projects SET lead_id" in sql for sql in sqls)
    assert any("UPDATE leads SET status = 'won'" in sql for sql in sqls)


def test_bid_explicit_lead_link_wins_over_email():
    cur = _Cursor({"id": 88})
    bid_costing._link_bid_lead_to_project(
        cur, {"lead_id": 88, "contact_email": "other@example.com"},
        "construction", 42)
    sqls = [" ".join(sql.split()) for sql, _ in cur.executed]
    assert any("WHERE id = %s AND company = %s" in sql for sql in sqls)
    assert any("UPDATE projects SET lead_id" in sql for sql in sqls)


def test_bid_without_a_contact_email_links_nothing():
    cur = _Cursor({"id": 77})
    bid_costing._link_bid_lead_to_project(cur, {}, "construction", 42)
    assert cur.executed == []


def test_bid_with_no_matching_lead_links_nothing():
    cur = _Cursor(None)
    bid_costing._link_bid_lead_to_project(
        cur, {"contact_email": "nobody@example.com"}, "construction", 42)
    sqls = [" ".join(sql.split()) for sql, _ in cur.executed]
    assert any("SELECT id FROM leads" in sql for sql in sqls)
    assert not any("UPDATE projects" in sql for sql in sqls)


# --- Lead pipeline status writes --------------------------------------------

def test_lead_pipeline_status_update_does_not_touch_a_missing_column():
    import lead_pipeline

    executed = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=()):
            executed.append((" ".join(sql.split()), params))

    class _DB:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    assert lead_pipeline._update_lead_status(_DB(), 5, "won") is True
    assert executed
    assert "updated_at" not in executed[0][0]


# --- Transaction hygiene -----------------------------------------------------
class _BrokenDB:
    def __init__(self):
        self.rolled_back = False

    def cursor(self):
        raise RuntimeError("deadlock")

    def rollback(self):
        self.rolled_back = True


def test_draft_column_failure_rolls_back_the_connection():
    import auto_reply
    db = _BrokenDB()
    auto_reply._ensure_draft_column(db)
    assert db.rolled_back is True


def test_draft_stage_failure_rolls_back_and_returns_false():
    import auto_reply
    db = _BrokenDB()
    assert auto_reply._stage_draft(
        db, 1, "construction", "email", "x@example.com", "body") is False
    assert db.rolled_back is True


# --- Public owner endpoint and API docs -------------------------------------

def test_branded_404_page():
    response = TestClient(cm.app).get("/this-page-does-not-exist")
    assert response.status_code == 404
    assert "Page not found" in response.text
    assert "Get a free estimate" in response.text


def test_api_404_stays_json():
    response = TestClient(cm.app).get("/api/this-does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_sitemap_lists_the_linked_public_pages():
    response = TestClient(cm.app).get("/sitemap.xml")
    assert response.status_code == 200
    assert "/rental-make-ready" in response.text
    assert "/refund-policy" in response.text


def test_bot_health_requires_an_admin_session():
    cm.app.dependency_overrides[cm.get_db] = lambda: object()
    try:
        response = TestClient(cm.app).get("/api/bot/health")
    finally:
        cm.app.dependency_overrides.clear()
    assert response.status_code == 401


def test_api_docs_are_disabled_unless_explicitly_enabled():
    assert cm.app.docs_url is None
    assert cm.app.openapi_url is None
