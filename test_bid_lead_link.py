"""Manual bid -> lead link: search and link endpoints.

The route is exercised through TestClient with a fake DB, so no Postgres is
needed. It covers the three states the UI depends on: search results, a link,
and an unlink.
"""

import pytest
from fastapi.testclient import TestClient

import construction_main as cm


class _Cursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _DB:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(cm, "require_admin", lambda request: None)
    return TestClient(cm.app)


def test_lead_search_returns_matching_leads(client, monkeypatch):
    rows = [{"id": 7, "name": "Ann Example", "email": "ann@example.com",
             "phone": "7575550100", "address": "1 Main St",
             "status": "contacted", "project_type": "Kitchen"}]
    db = _DB(_Cursor(rows))
    cm.app.dependency_overrides[cm.get_db] = lambda: db
    try:
        response = client.get("/api/leads/search?q=ann")
    finally:
        cm.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["leads"][0]["id"] == 7
    assert response.json()["leads"][0]["name"] == "Ann Example"


def test_lead_search_needs_two_characters(client):
    db = _DB(_Cursor([]))
    cm.app.dependency_overrides[cm.get_db] = lambda: db
    try:
        response = client.get("/api/leads/search?q=a")
    finally:
        cm.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["leads"] == []
    assert db._cursor.executed == []


def test_link_lead_updates_the_bid_and_project(client, monkeypatch):
    lead = {"id": 7, "name": "Ann Example", "email": "ann@example.com",
            "phone": "7575550100", "address": "1 Main St", "status": "contacted"}
    cursor = _Cursor([lead])
    db = _DB(cursor)
    monkeypatch.setattr(cm.bid_opportunities, "ensure_schema", lambda conn: None)
    monkeypatch.setattr(cm.bid_opportunities, "get_bid",
                        lambda conn, bid_id, company: {"id": 42, "status": "quoted",
                                                       "contact_email": ""})
    monkeypatch.setattr(cm.bid_costing, "ensure_accounting_schema", lambda conn: None)
    cm.app.dependency_overrides[cm.get_db] = lambda: db
    try:
        response = client.post("/api/bids/42/link-lead", data={"lead_id": "7"})
    finally:
        cm.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["linked"] == 7
    sqls = [sql for sql, _ in cursor.executed]
    assert any("UPDATE bid_opportunities SET" in sql for sql in sqls)
    assert any("UPDATE projects SET lead_id" in sql for sql in sqls)
    assert db.committed is True


def test_unlink_clears_the_bid_link(client, monkeypatch):
    cursor = _Cursor([])
    db = _DB(cursor)
    monkeypatch.setattr(cm.bid_opportunities, "ensure_schema", lambda conn: None)
    monkeypatch.setattr(cm.bid_opportunities, "get_bid",
                        lambda conn, bid_id, company: {"id": 42, "status": "quoted"})
    cm.app.dependency_overrides[cm.get_db] = lambda: db
    try:
        response = client.post("/api/bids/42/link-lead", data={"lead_id": ""})
    finally:
        cm.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["linked"] is None
    assert any("lead_id = NULL" in sql for sql, _ in cursor.executed)


def test_link_unknown_lead_is_404(client, monkeypatch):
    cursor = _Cursor([])
    db = _DB(cursor)
    monkeypatch.setattr(cm.bid_opportunities, "ensure_schema", lambda conn: None)
    monkeypatch.setattr(cm.bid_opportunities, "get_bid",
                        lambda conn, bid_id, company: {"id": 42, "status": "quoted"})
    monkeypatch.setattr(cm.bid_costing, "ensure_accounting_schema", lambda conn: None)
    cm.app.dependency_overrides[cm.get_db] = lambda: db
    try:
        response = client.post("/api/bids/42/link-lead", data={"lead_id": "999"})
    finally:
        cm.app.dependency_overrides.clear()
    assert response.status_code == 404
