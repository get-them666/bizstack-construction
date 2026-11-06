"""Tests for pulling solicitation attachments onto a bid card.

Run: venv/bin/python test_bid_attachments.py   (pytest also collects it)

The dangerous outcomes this pins down:
  - an executable or HTML file from an inbound email being stored and served,
  - a second press of "Pull attachments" duplicating every document,
  - a failed download taking the whole extraction down with it.
No network, no Zoho, no R2.
"""

from __future__ import annotations

import os


def test_classify_prefers_drawings_over_incidental_words():
    import bid_attachments as ba
    cases = [
        ("A-101 Floor Plans.pdf", "plans"),
        ("Structural Drawings.pdf", "plans"),
        ("foundation.dwg", "plans"),
        ("Scope of Work.pdf", "sow"),
        ("Specifications.pdf", "sow"),
        ("Bid Form.pdf", "quote"),
        ("Itemized Pricing.xlsx", "itemized"),
        ("Contract Agreement.docx", "contract"),
        ("addendum-2.pdf", "other"),
    ]
    for name, want in cases:
        got = ba.classify(name)
        assert got == want, f"{name}: {got} != {want}"


def test_a_plan_sheet_mentioning_scope_is_still_a_drawing():
    import bid_attachments as ba
    assert ba.classify("S-201 Scope and Notes Plan.pdf") == "plans"


def test_extract_reports_a_bid_with_no_matching_email(monkeypatch):
    import bid_attachments as ba
    import bid_opportunities
    import zoho_mail

    monkeypatch.setattr(bid_opportunities, "ensure_schema", lambda db: None)
    monkeypatch.setattr(bid_opportunities, "source_emails", lambda *a: [])
    monkeypatch.setattr(bid_opportunities, "get_bid", lambda *a: {
        "id": 5, "name": "Riverside", "contact_email": "", "reference": ""})
    monkeypatch.setattr(zoho_mail, "available", lambda: True)
    monkeypatch.setattr(zoho_mail, "account", lambda: ("ACC1", "me@example.com"))
    monkeypatch.setattr(zoho_mail, "list_recent", lambda **k: [])

    result = ba.extract(None, 5)
    assert result["ok"] is False
    assert "could be matched" in result["reason"].lower()


def test_extract_finds_and_links_an_older_bid_by_sender(monkeypatch):
    """Bids that predate the link must still be able to pull their drawings."""
    import bid_attachments as ba
    import bid_opportunities
    import zoho_mail

    linked = []

    def _sources(db, bid_id, company):
        return [{"account_id": "ACC1", "folder_id": "F1", "message_id": "M9",
                 "sender": "gc@example.com", "subject": "Invitation to bid"}] if linked else []

    monkeypatch.setattr(bid_opportunities, "ensure_schema", lambda db: None)
    monkeypatch.setattr(bid_opportunities, "source_emails", _sources)
    monkeypatch.setattr(bid_opportunities, "get_bid", lambda *a: {
        "id": 5, "name": "Riverside", "contact_email": "gc@example.com", "reference": ""})
    monkeypatch.setattr(bid_opportunities, "remember_source_email",
                        lambda db, bid_id, company, **kw: linked.append(kw) or True)
    monkeypatch.setattr(bid_opportunities, "list_documents", lambda *a: [])
    monkeypatch.setattr(bid_opportunities, "add_document",
                        lambda db, bid_id, company, **kw: 7)
    monkeypatch.setattr(zoho_mail, "available", lambda: True)
    monkeypatch.setattr(zoho_mail, "account", lambda: ("ACC1", "me@example.com"))
    monkeypatch.setattr(zoho_mail, "list_recent", lambda **k: [{
        "id": "M9", "folder_id": "F1", "account_id": "ACC1",
        "from": "GC <gc@example.com>", "subject": "Invitation to bid — Riverside",
        "received_ms": 1_000, "summary": "",
    }])
    monkeypatch.setattr(zoho_mail, "attachments", lambda a, f, m: [
        {"id": "1", "name": "plans.pdf", "size": 10, "inline": False}])
    monkeypatch.setattr(zoho_mail, "attachment_bytes", lambda a, f, m, i: b"plan")

    import photo_store
    monkeypatch.setattr(photo_store, "save", lambda raw, name, key_prefix="": {
        "key": f"{key_prefix}/{photo_store.safe_key(name)}",
        "url": f"/media/{key_prefix}/{photo_store.safe_key(name)}"})

    result = ba.extract(None, 5)
    assert linked and linked[0]["message_id"] == "M9"
    assert result["ok"] is True
    assert result["saved"] == 1


def _wire(monkeypatch, *, attachments, bodies, docs=None, unavailable=False):
    """Patch the collaborators extract() uses, returning the recorded calls."""
    import bid_attachments as ba
    import bid_opportunities
    import photo_store
    import zoho_mail

    calls = {"added": [], "downloaded": []}

    monkeypatch.setattr(bid_opportunities, "ensure_schema", lambda db: None)
    monkeypatch.setattr(bid_opportunities, "source_emails", lambda db, bid_id, company: [{
        "account_id": "ACC1", "folder_id": "F1", "message_id": "M1",
        "sender": "gc@example.com", "subject": "Invitation to bid — Riverside",
    }])
    monkeypatch.setattr(bid_opportunities, "list_documents",
                        lambda db, bid_id, company: list(docs or []))
    monkeypatch.setattr(bid_opportunities, "source_email_body", lambda *a: "")
    monkeypatch.setattr(bid_opportunities, "add_document",
                        lambda db, bid_id, company, **kw: (calls["added"].append(kw) or 99))
    monkeypatch.setattr(photo_store, "save", lambda raw, name, key_prefix="": {
        "key": f"{key_prefix}/{photo_store.safe_key(name)}",
        "url": f"/media/{key_prefix}/{photo_store.safe_key(name)}",
    })
    monkeypatch.setattr(zoho_mail, "available", lambda: not unavailable)
    monkeypatch.setattr(zoho_mail, "attachments", lambda a, f, m: list(attachments))

    def _download(a, f, m, aid):
        calls["downloaded"].append(aid)
        if aid in bodies and isinstance(bodies[aid], Exception):
            raise bodies[aid]
        return bodies.get(aid, b"bytes")

    monkeypatch.setattr(zoho_mail, "attachment_bytes", _download)
    return calls


def test_extract_stores_paperwork_with_the_right_document_type(monkeypatch):
    import bid_attachments as ba
    calls = _wire(monkeypatch, attachments=[
        {"id": "1", "name": "A-101 Floor Plans.pdf", "size": 100, "inline": False},
        {"id": "2", "name": "Scope of Work.pdf", "size": 100, "inline": False},
    ], bodies={"1": b"plan", "2": b"scope"})

    result = ba.extract(None, 5)
    assert result["ok"] is True, result
    assert result["saved"] == 2, result
    types = {a["filename"]: a["document_type"] for a in calls["added"]}
    assert types == {"A-101 Floor Plans.pdf": "plans", "Scope of Work.pdf": "sow"}
    assert all(a["uploaded_by"] == "solicitation email" for a in calls["added"])


def test_extract_refuses_executables_and_html(monkeypatch):
    import bid_attachments as ba
    calls = _wire(monkeypatch, attachments=[
        {"id": "1", "name": "setup.exe", "size": 10, "inline": False},
        {"id": "2", "name": "invoice.html", "size": 10, "inline": False},
        {"id": "3", "name": "plans.pdf", "size": 10, "inline": False},
    ], bodies={"1": b"x", "2": b"x", "3": b"x"})

    result = ba.extract(None, 5)
    assert result["saved"] == 1
    assert result["skipped"] == 2
    assert [a["filename"] for a in calls["added"]] == ["plans.pdf"]
    assert calls["downloaded"] == ["3"]  # never even fetched the bad ones


def test_extract_skips_inline_images_and_duplicates(monkeypatch):
    import bid_attachments as ba
    existing = [{"key": "bids/5/plans-3.pdf"}]
    calls = _wire(monkeypatch, attachments=[
        {"id": "9", "name": "logo.png", "size": 10, "inline": True},
        {"id": "3", "name": "plans.pdf", "size": 10, "inline": False},
    ], bodies={"9": b"x", "3": b"x"}, docs=existing)

    result = ba.extract(None, 5)
    assert result["saved"] == 0
    assert result["skipped"] == 1
    assert calls["added"] == []


def test_one_failed_download_does_not_lose_the_others(monkeypatch):
    import bid_attachments as ba
    calls = _wire(monkeypatch, attachments=[
        {"id": "1", "name": "broken.pdf", "size": 10, "inline": False},
        {"id": "2", "name": "good.pdf", "size": 10, "inline": False},
    ], bodies={"1": RuntimeError("Zoho said no"), "2": b"ok"})

    result = ba.extract(None, 5)
    assert result["ok"] is True
    assert result["saved"] == 1
    assert result["errors"] and "broken.pdf" in result["errors"][0]
    assert [a["filename"] for a in calls["added"]] == ["good.pdf"]


def test_extract_explains_an_unconnected_mailbox(monkeypatch):
    import bid_attachments as ba
    _wire(monkeypatch, attachments=[], bodies={}, unavailable=True)
    result = ba.extract(None, 5)
    assert result["ok"] is False
    assert "not connected" in result["reason"].lower()


# ── links in the email body (source-agnostic) ──────────────────────────────

def test_document_links_separates_direct_files_from_login_walls():
    import bid_attachments as ba
    body = ("Plans: https://cdn.example.com/files/A-101.pdf and "
            "https://app.buildingconnected.com/opportunities/abc/info plus "
            "https://files.example.com/download/xyz?token=1.")
    links = ba.document_links(body)
    by_host = {l["host"]: l for l in links}
    assert by_host["cdn.example.com"]["login_wall"] is False
    assert by_host["app.buildingconnected.com"]["login_wall"] is True
    assert by_host["files.example.com"]["login_wall"] is False
    assert len(links) == 3


def test_document_links_ignores_unrelated_and_unsubscribe_links():
    import bid_attachments as ba
    body = ("https://example.com/about  https://example.com/login "
            "https://app.buildingconnected.com/unsubscribe?x=1")
    assert ba.document_links(body) == []


def test_private_and_local_urls_are_refused():
    import bid_attachments as ba
    assert ba._public_http("http://127.0.0.1/a.pdf")[0] is False
    assert ba._public_http("http://localhost/a.pdf")[0] is False
    assert ba._public_http("file:///etc/passwd")[0] is False


def test_extract_follows_a_direct_document_link(monkeypatch):
    import bid_attachments as ba
    import bid_opportunities

    calls = _wire(monkeypatch, attachments=[], bodies={})
    monkeypatch.setattr(bid_opportunities, "source_email_body", lambda *a: (
        "Please find drawings at https://cdn.example.com/A-101.pdf and the plan "
        "room at https://app.buildingconnected.com/opportunities/abc/info"))
    monkeypatch.setattr(ba, "_public_http", lambda url: (True, ""))
    monkeypatch.setattr(ba, "_download", lambda url, max_bytes: (
        b"plan", "A-101.pdf", "application/pdf"))

    result = ba.extract(None, 5)
    assert result["ok"] is True
    assert result["saved"] == 1, result
    assert result["checked"] == 1  # the walled board link is not fetched
    added = calls["added"]
    assert added and added[0]["uploaded_by"] == "solicitation link"
    assert added[0]["document_type"] == "plans"
    # The owner is told where the gated drawings actually live.
    assert any("buildingconnected.com" in n for n in result["notes"])


def test_extract_does_not_fetch_a_login_page(monkeypatch):
    import bid_attachments as ba
    import bid_opportunities

    _wire(monkeypatch, attachments=[], bodies={})
    monkeypatch.setattr(bid_opportunities, "source_email_body", lambda *a: (
        "Bid here: https://app.planhub.com/bids/123"))
    result = ba.extract(None, 5)
    assert result["checked"] == 0
    assert result["saved"] == 0
    assert "sign-in" in result["reason"].lower()


if __name__ == "__main__":
    import sys
    import types

    FAILS = []

    class _Patch:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)

    def run(name, fn):
        if "monkeypatch" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
            patch = _Patch()
            try:
                fn(patch)
                print(f"  PASS  {name}")
            except AssertionError as exc:
                print(f"  FAIL  {name}\n        {exc}")
                FAILS.append(name)
            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR {name}\n        {type(exc).__name__}: {exc}")
                FAILS.append(name)
            finally:
                patch.undo()
        else:
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL  {name}\n        {exc}")
                FAILS.append(name)

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and isinstance(f, types.FunctionType)]
    print(f"running {len(tests)} bid attachment tests (no network, no db)")
    for name, fn in tests:
        run(name, fn)
    print(f"\n{len(tests) - len(FAILS)}/{len(tests)} pass")
    sys.exit(1 if FAILS else 0)
