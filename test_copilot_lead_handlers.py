"""Regression tests for the Copilot's concrete lead-management handlers.

No network and no database. The SQL connection and provider boundaries are faked.
"""

import construction_main
import copilot_ops
import lead_enrich_loop


class _Cursor:
    def __init__(self, db):
        self.db = db
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.db.queries.append((normalized, params))
        self.rowcount = 1 if normalized.startswith("UPDATE") else 0
        if self.db.responses and (normalized.startswith("SELECT") or " RETURNING " in normalized):
            rows = self.db.responses.pop(0)
            self.rows = list(rows) if isinstance(rows, list) else [rows]
        else:
            self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Db:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.queries = []
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_lookup_leads_matches_a_stored_phone(monkeypatch):
    db = _Db(responses=[[{
        "id": 42, "name": "Jane", "phone": "+1 (757) 555-0142",
        "project_type": "Roof", "status": "new", "deposit_status": "none",
        "created_at": None,
    }]])
    handlers = construction_main.build_tool_handlers(db, object())
    result = handlers["lookup_leads"]("7575550142")
    assert [r["id"] for r in result["leads"]] == [42]
    assert result["leads"][0]["phone"] == "+1 (757) 555-0142"
    assert "SELECT id, name, phone" in db.queries[0][0]


def test_find_people_survives_an_optional_customers_table_miss():
    class _OptionalCustomerCursor(_Cursor):
        def execute(self, sql, params=()):
            if "FROM customers" in sql:
                raise RuntimeError('relation "customers" does not exist')
            return super().execute(sql, params)

    class _OptionalCustomerDb(_Db):
        def __init__(self):
            super().__init__(responses=[[], [{
                "id": 3, "name": "Jane", "phone": "", "email": "jane@example.com",
                "project_type": "Roof", "kind": "lead",
            }]])
            self.rolled_back = 0

        def cursor(self):
            return _OptionalCustomerCursor(self)

        def rollback(self):
            self.rolled_back += 1

    db = _OptionalCustomerDb()
    handlers = construction_main.build_tool_handlers(db, object())
    result = handlers["find_people"]("Jane")
    assert result["ok"] is True and result["people"][0]["id"] == 3
    assert db.rolled_back == 1


def test_update_lead_status_accepts_canonical_stages_and_recomputes(monkeypatch):
    db = _Db()
    seen = []
    monkeypatch.setattr(
        construction_main.lead_store, "recompute",
        lambda conn, lead_id: seen.append(lead_id) or {"status": "won", "next_action": "closed"},
    )
    handlers = construction_main.build_tool_handlers(db, object())
    result = handlers["update_lead_status"](42, "won")
    assert result == {"ok": True, "lead_id": 42, "status": "won", "next_action": "closed"}
    assert seen == [42]

    before = len(db.queries)
    assert handlers["update_lead_status"](42, "not-a-stage")["ok"] is False
    assert len(db.queries) == before


def test_save_contact_preserves_a_real_name_and_refreshes_the_queue(monkeypatch):
    db = _Db(responses=[
        {"id": 7, "name": "Real Customer", "email": "old@lead.local", "phone": ""},
        {"id": 7, "name": "Real Customer", "email": "new@example.com", "phone": "7575550100"},
    ])
    recomputed = []
    from bizstack_leads import store as lead_store
    monkeypatch.setattr(lead_store, "recompute", lambda conn, lead_id: recomputed.append(lead_id))
    handlers = copilot_ops.build_contact_tools(db, "owner@example.com")
    result = handlers["save_contact"](
        lead_id="7", name="Different Person", email="new@example.com", phone="7575550100",
    )
    assert result["saved_name"] == "Real Customer"
    assert recomputed == [7]
    update = next(q for q in db.queries if q[0].startswith("UPDATE leads SET name"))
    assert update[1][0] == update[1][1] == "", "the incoming name must not overwrite a real one"


def test_save_contact_replaces_only_a_placeholder_name(monkeypatch):
    db = _Db(responses=[
        {"id": 8, "name": "Norfolk", "email": "permit@lead.local", "phone": ""},
        {"id": 8, "name": "Jane Owner", "email": "jane@example.com", "phone": ""},
    ])
    from bizstack_leads import store as lead_store
    monkeypatch.setattr(lead_store, "recompute", lambda conn, lead_id: {})
    handlers = copilot_ops.build_contact_tools(db, "owner@example.com")
    result = handlers["save_contact"](lead_id="8", name="Jane Owner", email="jane@example.com")
    assert result["saved_name"] == "Jane Owner"


def test_batch_draft_uses_the_newly_resolved_name(monkeypatch):
    lead = {
        "id": 9, "name": "Norfolk", "email": "permit@lead.local", "phone": "",
        "address": "1 Main St, Norfolk, VA 23510", "draft_reply": "",
    }
    db = _Db()
    monkeypatch.setattr(lead_enrich_loop, "_mark_ran", lambda conn: None)
    monkeypatch.setattr(lead_enrich_loop, "select_batch", lambda conn, cap: [lead])
    monkeypatch.setattr(lead_enrich_loop.data_enrichment, "classify_reachable", lambda row: False)
    monkeypatch.setattr(
        lead_enrich_loop.data_enrichment, "lookup_address",
        lambda conn, address, cur: {
            "name": "Jane Owner", "email": "jane@example.com", "phone": "", "steps": [],
        },
    )
    import skiptrace_service
    monkeypatch.setattr(skiptrace_service, "backfill_lead_zips", lambda cur, company: {"resolved": 0})
    from bizstack_leads import store as lead_store
    monkeypatch.setattr(lead_store, "recompute", lambda conn, lead_id: {})
    import zoho_sync
    monkeypatch.setattr(zoho_sync, "sync_lead", lambda conn, lead_id: None)
    monkeypatch.setattr(lead_enrich_loop.auto_reply, "_review_mode", lambda: True)
    greeted = []

    def draft(name, address):
        greeted.append(name)
        return "Subject", "Body"

    monkeypatch.setattr(lead_enrich_loop, "first_touch_draft", draft)
    result = lead_enrich_loop.run_once(db=db, force=True, limit=1)
    assert result["drafts"] == 1
    assert greeted == ["Jane Owner"]


def test_bid_contact_research_uses_the_cached_contact_path(monkeypatch):
    db = _Db(responses=[{
        "location": "1 Main St, Norfolk, VA", "contact_name": "",
        "contact_email": "", "contact_phone": "",
    }])
    import skiptrace_service
    monkeypatch.setattr(skiptrace_service, "ensure_schema", lambda cur: None)
    monkeypatch.setattr(skiptrace_service, "cache_get", lambda cur, key: {"owner_of_record": "Jane Owner"})
    monkeypatch.setattr(
        skiptrace_service, "cache_put",
        lambda *args: (_ for _ in ()).throw(AssertionError("owner cache should already hit")),
    )
    monkeypatch.setattr(
        skiptrace_service, "trace_address",
        lambda *args: (_ for _ in ()).throw(AssertionError("owner cache should already hit")),
    )
    addresses = []

    def contact_lookup(address=""):
        addresses.append(address)
        return {
            "ok": True, "found": True, "cached": True,
            "emails": ["jane@example.com"],
            "phones": [{"e164": "+17575550100", "usable": True}],
        }

    monkeypatch.setattr(
        copilot_ops, "build_contact_tools",
        lambda db, owner_email: {"contact_lookup": contact_lookup},
    )
    monkeypatch.setattr(
        construction_main.skip_sherpa_service, "trace",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not bypass cached contact_lookup")),
    )
    handlers = construction_main._bid_document_tools(db, "owner@example.com")
    result = handlers["research_bid_contact"](15)
    assert result["ok"] is True
    assert result["email"] == "jane@example.com"
    assert result["phone"] == "+17575550100"
    assert addresses == ["1 Main St, Norfolk, VA"]
