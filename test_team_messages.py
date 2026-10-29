"""Owner <-> crew messages: visibility, the crew PWA contract, and the socket.

    venv/bin/python -m pytest test_team_messages.py -v

Crew have been able to DM "the office" from the PWA since the fork, but no
office-side page ever read the table, and the WebSocket accepted anyone and
broadcast every crew DM to every connection. These tests pin the module's
visibility rule with no database, and -- when a scratch Postgres is reachable
at TEAM_MESSAGES_TEST_DB -- the full HTTP + WebSocket round trip.
"""
import os

import pytest

import team_messages as tm


ADMIN = {"role": "admin", "id": 1, "name": "Shaun"}
ADMIN2 = {"role": "admin", "id": 2, "name": "Office 2"}
BOB = {"role": "crew", "id": 10, "name": "Bob"}
CARL = {"role": "crew", "id": 11, "name": "Carl"}


def _dm(author, to, body="hi", mid=1):
    return {"id": mid, "kind": "dm", "author_role": author["role"], "author_id": author["id"],
            "author_name": author["name"], "recipient_role": to["role"],
            "recipient_id": None if to["role"] == "admin" else to["id"],
            "recipient_name": to["name"], "body": body, "created_at": None}


def test_crew_dm_to_office_is_read_by_admins_and_its_author_only():
    m = _dm(BOB, ADMIN)
    assert tm.visible(m, ADMIN) and tm.visible(m, ADMIN2) and tm.visible(m, BOB)
    assert not tm.visible(m, CARL)


def test_admin_dm_to_one_crew_member_does_not_leak_to_another():
    m = _dm(ADMIN, BOB)
    assert tm.visible(m, BOB) and tm.visible(m, ADMIN)
    assert not tm.visible(m, CARL)
    # The old crew check compared recipient_id alone, so an admin->admin DM
    # with a matching numeric id leaked to the crew member with that id.
    office_note = _dm(ADMIN, ADMIN)
    office_note["recipient_id"] = BOB["id"]
    assert not tm.visible(office_note, BOB)


def test_announcements_reach_everyone():
    m = {"id": 3, "kind": "announcement", "author_role": "admin", "author_id": 1,
         "author_name": "Shaun", "body": "Rain day", "created_at": None}
    assert tm.visible(m, BOB) and tm.visible(m, CARL) and tm.visible(m, ADMIN)


def test_legacy_worker_rows_count_as_crew():
    m = _dm(BOB, ADMIN)
    m["author_role"] = "worker"
    assert tm.visible(m, BOB) and tm.visible(m, ADMIN)
    assert tm.serialize(m)["author"] == "crew"


def test_serializer_keeps_the_crew_pwa_contract():
    out = tm.serialize(_dm(ADMIN, BOB, mid=7))
    for key in ("id", "kind", "author", "author_id", "author_name", "body", "at"):
        assert key in out
    assert out["author"] == "office"


def test_nobody_is_an_actor_without_a_session():
    assert tm.normalize_actor(None, None) is None
    assert tm.normalize_actor({"role": "customer", "id": 5}, None) is None
    assert tm.normalize_actor({"role": "admin", "id": 1, "email": "o@x.com"}, None) == \
        {"role": "admin", "id": 1, "name": "o@x.com"}
    assert tm.normalize_actor(None, {"role": "worker", "id": 10, "name": "Bob"}) == \
        {"role": "crew", "id": 10, "name": "Bob"}


# --- the wire, against a scratch database --------------------------------------

DB = os.getenv("TEAM_MESSAGES_TEST_DB", "")


@pytest.mark.skipif(not DB, reason="set TEAM_MESSAGES_TEST_DB=postgresql://... to run")
def test_round_trip_over_http_and_websocket(monkeypatch):
    import psycopg
    from psycopg.rows import dict_row
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", DB)
    for flag in ("DISABLE_PERMIT_IMPORT", "DISABLE_LEAD_SCAN", "DISABLE_LOAN_OUTREACH", "DISABLE_LEAD_ENRICH"):
        monkeypatch.setenv(flag, "1")
    import construction_main as main

    conn = psycopg.connect(DB, row_factory=dict_row)
    tm.ensure_schema(conn, force=True)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM messages;")
        cur.execute("""CREATE TABLE IF NOT EXISTS crew (id SERIAL PRIMARY KEY, name TEXT, phone TEXT,
                       email TEXT, role TEXT, is_active BOOLEAN DEFAULT TRUE);""")
        cur.execute("""CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, email TEXT, name TEXT, role TEXT);""")
        cur.execute("DELETE FROM crew; DELETE FROM users;")
        cur.execute("INSERT INTO users (id, email, name, role) VALUES (1, 'owner@example.com', 'Shaun', 'admin');")
        cur.execute("INSERT INTO crew (id, name, is_active) VALUES (10, 'Bob', TRUE), (11, 'Carl', TRUE);")
    conn.commit()

    main.app.dependency_overrides[main.get_db] = lambda: conn
    monkeypatch.setattr(main, "_require_crew_feature", lambda db, feat: True)
    sessions = {"admin": ADMIN, "bob": BOB, "carl": CARL}

    def fake_actor(req):
        who = req.cookies.get("who")
        return {"role": "admin", "id": 1, "email": "owner@example.com", "name": "Shaun"} if who == "admin" else None

    def fake_worker(req):
        who = req.cookies.get("who")
        if who in ("bob", "carl"):
            a = sessions[who]
            return {"role": "worker", "id": a["id"], "name": a["name"]}
        return None

    monkeypatch.setattr(main, "current_actor", fake_actor)
    monkeypatch.setattr(main, "current_worker", fake_worker)
    monkeypatch.setattr(main, "require_admin", lambda req: None if req.cookies.get("who") == "admin" else (_ for _ in ()).throw(main.HTTPException(403)))
    monkeypatch.setattr(main, "require_worker", lambda req: fake_worker(req) or (_ for _ in ()).throw(main.HTTPException(401)))

    client = TestClient(main.app)
    try:
        # Anonymous socket is refused.
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/messages"):
                pass

        # Bob (crew) connects, then the owner DMs Bob -- Bob gets it live, Carl does not.
        with client.websocket_connect("/ws/messages", cookies={"who": "bob"}) as bob_ws, \
             client.websocket_connect("/ws/messages", cookies={"who": "carl"}) as carl_ws:
            r = client.post("/api/messages", data={"body": "Bob, 7am at Dana Dr", "kind": "dm",
                                                   "to_role": "crew", "to_id": "10"}, cookies={"who": "admin"})
            assert r.status_code == 200, r.text
            got = bob_ws.receive_json()
            assert got["body"] == "Bob, 7am at Dana Dr" and got["author"] == "office" and got["recipient_name"] == "Bob"
            # Carl's socket must stay silent: an announcement is the next thing he hears.
            client.post("/api/messages", data={"body": "Rain day tomorrow", "kind": "announcement"}, cookies={"who": "admin"})
            assert carl_ws.receive_json()["body"] == "Rain day tomorrow"
            assert bob_ws.receive_json()["body"] == "Rain day tomorrow"

        # Bob replies from the PWA endpoint; the owner reads it on /api/messages.
        r = client.post("/api/crew/app/messages", data={"body": "On my way"}, cookies={"who": "bob"})
        assert r.status_code == 200, r.text
        office = client.get("/api/messages?after=0", cookies={"who": "admin"}).json()["messages"]
        assert [m["body"] for m in office] == ["Bob, 7am at Dana Dr", "Rain day tomorrow", "On my way"]
        # Carl sees only the announcement.
        carl = client.get("/api/crew/app/messages?after=0", cookies={"who": "carl"}).json()["messages"]
        assert [m["body"] for m in carl] == ["Rain day tomorrow"]
        # Roster is owner + active crew, nothing else.
        roster = client.get("/api/messages/roster", cookies={"who": "admin"}).json()["roster"]
        assert {(u["role"], u["name"]) for u in roster} == {("admin", "Shaun"), ("crew", "Bob"), ("crew", "Carl")}
        # The page itself renders for the owner.
        monkeypatch.setattr(main, "require_auth", lambda req: (True, "owner@example.com"))
        page = client.get("/messages", cookies={"who": "admin"})
        assert page.status_code == 200 and "Messages" in page.text
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)
        conn.close()
