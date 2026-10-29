"""BuildingConnected -> bid board mapping, with no network.

    venv/bin/python -m pytest test_bid_sources_bc.py -v
"""
import datetime as dt

import bid_sources as bs


OPP = {
    "id": "64f1c2d3e4a5b6c7d8e9f012",
    "name": "Library Renovation — Drywall & Paint",
    "state": "UNDECIDED",
    "client": {"company": {"name": "Premier Architecture"},
               "lead": {"firstName": "Dana", "lastName": "Reyes", "email": "dana@premierarch.com", "phone": "757-555-0100"}},
    "dueAt": "2026-11-12T19:00:00.000Z",
    "jobWalkAt": "2026-11-03T14:00:00.000Z",
    "rfisDueAt": "2026-11-07T21:00:00.000Z",
    "location": {"streetNumber": "301", "streetName": "E City Hall Ave", "city": "Norfolk", "state": "VA", "zip": "23510"},
    "tradeName": "Drywall",
    "projectSize": 12000,
    "isNdaRequired": True,
    "value": 150000,
}


def test_invitation_maps_to_a_board_row():
    row = bs.normalize_bc_opportunity(OPP)
    assert row["name"].startswith("Library Renovation")
    assert row["owner"] == "Premier Architecture"
    assert (row["contact_name"], row["contact_email"]) == ("Dana Reyes", "dana@premierarch.com")
    assert row["reference"] == "bc:64f1c2d3e4a5b6c7d8e9f012"
    assert row["trade"] == "Drywall"
    assert row["location"] == "301 E City Hall Ave, Norfolk, VA 23510"
    assert row["due_date"] == dt.date(2026, 11, 12) or str(row["due_date"]).startswith("2026-11-12")
    assert row["external_url"] == "https://app.buildingconnected.com/opportunities/64f1c2d3e4a5b6c7d8e9f012/info"
    assert "Job walk: 2026-11-03" in row["notes"] and "RFIs due: 2026-11-07" in row["notes"] and "NDA required" in row["notes"]
    assert row["status"] == "new" and row["role"] == "sub"


def test_declined_and_lost_invitations_stay_off_the_board():
    for state in ("DECLINED", "NOT_BIDDING", "LOST", "ARCHIVED"):
        assert bs.normalize_bc_opportunity({**OPP, "state": state}) is None


def test_submitted_and_won_states_carry_over():
    assert bs.normalize_bc_opportunity({**OPP, "state": "SUBMITTED"})["status"] == "submitted"
    assert bs.normalize_bc_opportunity({**OPP, "state": "WON"})["status"] == "won"


def test_pagination_follows_next_url_and_stops(monkeypatch):
    pages = {
        "/construction/buildingconnected/v2/opportunities?limit=100": {
            "pagination": {"nextUrl": "https://developer.api.autodesk.com/construction/buildingconnected/v2/opportunities?limit=100&cursorState=abc"},
            "results": [OPP]},
        "/construction/buildingconnected/v2/opportunities?limit=100&cursorState=abc": {
            "pagination": {}, "results": [{**OPP, "id": "aaaaaaaaaaaaaaaaaaaaaaaa", "name": "Second"}]},
    }
    calls = []
    monkeypatch.setattr(bs, "_bc_get", lambda path, user, timeout: (calls.append(path), pages[path])[1])
    import autodesk_oauth as ao
    monkeypatch.setattr(ao, "configured", lambda: True)
    rows = bs.fetch_buildingconnected(user_email="owner@example.com")
    assert [r["name"][:6] for r in rows] == ["Librar", "Second"]
    assert len(calls) == 2


def test_forbidden_explains_the_subscription(monkeypatch):
    import autodesk_oauth as ao
    monkeypatch.setattr(ao, "configured", lambda: True)
    def boom(path, user, timeout): raise RuntimeError("APS GET failed: HTTP 403 forbidden")
    monkeypatch.setattr(bs, "_bc_get", boom)
    try:
        bs.fetch_buildingconnected(user_email="owner@example.com")
    except bs.SourceError as e:
        assert "Bid Board Pro" in str(e)
    else:
        raise AssertionError("expected SourceError")


def test_source_registry_lists_buildingconnected_as_real():
    s = bs.SOURCES["buildingconnected"]
    assert s["fn"] is bs.fetch_buildingconnected and not s.get("unavailable")
