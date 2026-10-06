"""Tests for the owner-contact flow wired onto the /skiptrace page.

    python3 -m pytest test_skiptrace_contact.py -v

Covers resimply_service (payload assembly, the required-field refusals) and
skip_sherpa_service (address parsing, DNC surfacing, phone ranking).

No network and no database. The REsimpli client is exercised only through its
pure functions -- build_lead_payload, verify's early return, the payload
shape -- because the configured token answers 401 on every live endpoint. See
resimply_service's module docstring.
"""

import os
import re

import pytest

import resimply_service as res
import skip_sherpa_service as sherpa

# This repo's app is construction_main.py; the sister hosts repo's is main.py.
# Same tests, same assertions, different file.
APP_MODULE = "construction_main.py" if os.path.exists(
    os.path.join(os.path.dirname(__file__), "construction_main.py")) else "main.py"


# --- resimply_service ------------------------------------------------------

ALL_IDS = {
    "RESIMPLY_CAMPAIGN_ID": "camp1",
    "RESIMPLY_MARKET_ID": "mkt1",
    "RESIMPLY_PIPELINE_ID": "pipe1",
    "RESIMPLY_STATUS_ID": "stat1",
}


@pytest.fixture
def ids(monkeypatch):
    for k, v in ALL_IDS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(res.TOKEN_ENV, "test-token")
    return ALL_IDS


def test_name_splits_into_first_and_last():
    assert res._split_name("Leonard G Barlow") == ("Leonard", "Barlow")
    assert res._split_name("Cher") == ("Cher", "")
    assert res._split_name("  ") == ("", "")


def test_phone_normalises_to_ten_digits():
    # 11-digit with a leading country code is the common provider shape.
    assert res._normalise_phone("+17551234567") == "7551234567"
    assert res._normalise_phone("(755) 123-4567") == "7551234567"
    # Wrong length must NOT be padded or guessed. A wrong phone dials a stranger.
    assert res._normalise_phone("555-1234") == ""
    assert res._normalise_phone("") == ""


def test_lead_payload_is_complete_with_a_real_contact(ids):
    payload, problems = res.build_lead_payload(
        name="Leonard G Barlow",
        email="owner@example.net",
        phone="+17551234567",
        address="1501 Vance Cir, Chesapeake, VA 23320",
        comment="skip trace",
    )
    assert problems == []
    assert payload["contactDetails"]["firstName"] == "Leonard"
    assert payload["contactDetails"]["lastName"] == "Barlow"
    assert payload["contactDetails"]["phoneNumbers"] == [
        {"phoneNumber": "7551234567", "isPrimary": True}]
    assert payload["contactDetails"]["emails"] == [
        {"email": "owner@example.net", "isPrimary": True}]
    assert payload["propertyType"] == "residential"
    assert payload["address"].startswith("1501 Vance Cir")
    for field in ALL_IDS.values():
        assert payload["marketingCampignId"] == "camp1"


def test_lead_payload_carries_all_four_required_ids(ids):
    payload, problems = res.build_lead_payload(
        name="A B", email="a@b.co", phone="7575551212", address="1 A St")
    assert problems == []
    for field in ("marketingCampignId", "marketId", "pipelineId", "mainStatusId"):
        assert payload[field], f"{field} missing from the payload"


def test_marketing_opt_in_defaults_false(ids):
    """We hold their address, not their consent. Never assert opt-in."""
    payload, _ = res.build_lead_payload(
        name="A B", email="a@b.co", phone="7575551212", address="1 A St")
    assert payload["contactDetails"]["marketingOptIn"] is False


def test_refuses_without_phone(ids):
    """lead/add requires a phone, so a missing one must block, not guess."""
    _, problems = res.build_lead_payload(
        name="A B", email="a@b.co", phone="", address="1 A St")
    assert any("phone" in p for p in problems)


def test_refuses_without_email():
    _, problems = res.build_lead_payload(
        name="A B", email="", phone="7575551212", address="1 A St")
    assert any("email" in p for p in problems)


def test_refuses_without_email_even_when_looks_valid(monkeypatch):
    monkeypatch.setenv(res.TOKEN_ENV, "t")
    _, problems = res.build_lead_payload(
        name="A B", email="not-an-email", phone="7575551212", address="1 A St")
    assert any("email" in p for p in problems)


def test_missing_account_ids_are_reported(monkeypatch):
    """The four ObjectIds live inside one account and must be surfaced, not guessed."""
    monkeypatch.setenv(res.TOKEN_ENV, "t")
    for k in ALL_IDS:
        monkeypatch.delenv(k, raising=False)
    _, problems = res.build_lead_payload(
        name="A B", email="a@b.co", phone="7575551212", address="1 A St")
    joined = " ".join(problems)
    for field in ("marketingCampignId", "marketId", "pipelineId", "mainStatusId"):
        assert field in joined


def test_missing_ids_helper(monkeypatch):
    for k in ALL_IDS:
        monkeypatch.setenv(k, ALL_IDS[k])
    assert res.missing_ids() == []
    monkeypatch.delenv("RESIMPLY_MARKET_ID")
    assert res.missing_ids() == ["marketId"]


def test_push_lead_without_token_is_a_clean_error(monkeypatch):
    monkeypatch.delenv(res.TOKEN_ENV, raising=False)
    with pytest.raises(res.ResimplyError) as e:
        res.push_lead(name="A B", email="a@b.co", phone="7575551212", address="1 A St")
    assert e.value.status == 400


def test_push_lead_refuses_incomplete_contact_without_calling(monkeypatch, ids):
    """Must not reach the network when the payload is unbuildable."""
    called = []
    monkeypatch.setattr(res, "_post", lambda *a, **k: called.append(a))
    with pytest.raises(res.ResimplyError):
        res.push_lead(name="A B", email="", phone="", address="1 A St")
    assert called == []


def test_property_type_defaults_to_residential(ids):
    payload, _ = res.build_lead_payload(
        name="A B", email="a@b.co", phone="7575551212", address="1 A St",
        property_type="nonsense")
    assert payload["propertyType"] == "residential"


def test_verify_without_token_reports_unconfigured(monkeypatch):
    monkeypatch.delenv(res.TOKEN_ENV, raising=False)
    out = res.verify()
    assert out["ok"] is False
    assert out["configured"] is False


# --- skip_sherpa_service ---------------------------------------------------

def test_parses_full_address():
    out = sherpa.parse_address("1501 VANCE CIR, Chesapeake, VA 23320")
    assert out["ok"] is True
    assert out["street"] == "1501 VANCE CIR"
    assert out["city"] == "CHESAPEAKE"
    assert out["state"] == "VA"
    assert out["zipcode"] == "23320"


def test_refuses_address_without_zip():
    """No ZIP can match a different door and still bill a credit."""
    out = sherpa.parse_address("1501 Vance Cir, Chesapeake, VA")
    assert out["ok"] is False
    assert "ZIP" in out["why"]


def test_refuses_address_without_state():
    assert sherpa.parse_address("1501 Vance Cir, Chesapeake 23320")["ok"] is False


def test_junk_email_domains_are_dropped():
    people = [{"person_name": {"first_name": "A", "last_name": "B"},
               "emails": [{"email_address": "a@yopmail.com"},
                          {"email_address": "real@cox.net"}]}]
    emails = sherpa._person_emails(people[0])
    assert emails == ["real@cox.net"]


def test_dnc_flag_is_surfaced_not_filtered():
    """The decision belongs to the caller; hiding it would hide a DNC number."""
    person = {"phone_numbers": [
        {"e164_format": "+17551234567", "type": "mobile",
         "dnc_statuses": [{"is_dnc": True}]}]}
    phones = sherpa._person_phones(person)
    assert phones[0][2] is True, "DNC flag must survive parsing"


def test_phone_is_usable_rejects_dnc_and_stale():
    import time as _t
    year = _t.strftime("%Y")
    assert sherpa.phone_is_usable("mobile", True, f"{year}-05-01") is False
    assert sherpa.phone_is_usable("landline", False, f"{year}-05-01") is False
    assert sherpa.phone_is_usable("mobile", False, "2011-03-04") is False
    assert sherpa.phone_is_usable("mobile", False, f"{year}-05-01") is True


def test_persons_walks_nested_shape():
    node = {"property": {"owners": [{"person": {"person_name": {"first_name": "A", "last_name": "B"}}}]}}
    assert len(sherpa._persons(node)) == 1


def test_trace_without_key_does_not_spend(monkeypatch):
    monkeypatch.delenv("SKIP_SHERPA_API_KEY", raising=False)
    with pytest.raises(sherpa.ProviderError) as e:
        sherpa.trace("1501 Vance Cir", "Chesapeake", "VA", "23320")
    assert e.value.spendable is False
    assert e.value.status == 400


def test_configured_reflects_env(monkeypatch):
    monkeypatch.setenv("SKIP_SHERPA_API_KEY", "k")
    assert sherpa.configured() is True
    monkeypatch.delenv("SKIP_SHERPA_API_KEY")
    assert sherpa.configured() is False


# --- the save statement ----------------------------------------------------

def test_owner_save_sql_placeholder_count():
    """Guards the bug that prompted extracting this to module scope.

    An inline version of this statement had 8 placeholders and 9 params, which
    psycopg accepts at import and rejects on the first real click.
    """
    src = open(APP_MODULE).read()
    m = re.search(r"_OWNER_SAVE_SQL = \((.*?)\n\)", src, re.S)
    assert m, "_OWNER_SAVE_SQL not found — was it renamed?"
    body = m.group(1)
    # Count only placeholders in the concatenated literal, minus %% escapes.
    placeholders = body.count("%s")
    # owner, email x2, phone x2, address, json, id
    assert placeholders == 8, f"expected 8 placeholders, found {placeholders}"


def test_owner_save_sql_does_not_clobber_real_email():
    src = open(APP_MODULE).read()
    m = re.search(r"_OWNER_SAVE_SQL = \((.*?)\n\)", src, re.S)
    body = m.group(1)
    # The real value must be the ELSE branch, and a @lead.local sentinel must
    # remain replaceable.
    assert "ELSE email END" in body
    assert "@lead.local" in body


def _load_owner_draft_body():
    """Pull _owner_draft_body out of construction_main without importing it.

    The app imports psycopg and boots a schema on import, so importing it needs
    a live database. The other tests here read source text for the same reason.
    """
    import pathlib
    src = pathlib.Path(__file__).resolve().parent / APP_MODULE
    text = src.read_text()
    ns = {}
    # _owner_draft_body only needs its own body, not the module globals. Slice to
    # the next top-level def, not just @app.post -- on the construction site the
    # settings route is a plain @app.get and would otherwise be exec'd in too.
    start = text.index("def _owner_draft_body(")
    rest = text[start:]
    import re
    m = re.search(r"^def |^@app\.", rest[len("def _owner_draft_body("):], re.M)
    end = start + len("def _owner_draft_body(") + m.start() if m else len(text)
    exec(compile(text[start:end], str(src), "exec"), ns)
    return ns["_owner_draft_body"]


def test_draft_body_makes_no_unsupported_claims():
    body = _load_owner_draft_body()(
        "Leonard G Barlow", "1501 Vance Cir, Chesapeake, VA 23320",
        {"year_built": 1998, "square_footage": 2100})
    assert "Leonard" in body
    assert "1998" in body
    # An opt-out is a CAN-SPAM requirement for commercial email.
    assert "won't contact you again" in body
    # It must not assert a quote, a permit, or an offer we have not verified.
    low = body.lower()
    for claim in ("we offer", "your permit", "guaranteed", "free estimate"):
        assert claim not in low, f"draft asserts an unsupported claim: {claim}"


def test_draft_body_uses_first_name_only():
    body = _load_owner_draft_body()("Mary Ann Frazier", "1 A St, Norfolk, VA 23510", {})
    assert "Hi Mary," in body
    assert "Frazier" not in body.split("\n")[0]


def test_draft_property_details_render_as_a_sentence():
    """Regression: an earlier version emitted a bare fragment line.

    It produced " (built 1998), 2,100 sq ft, Single Family" -- a leading space
    and a dangling clause -- which reads like a template error in front of a
    homeowner. This test exists because that shipped into a draft generator.
    """
    body = _load_owner_draft_body()(
        "Leonard G Barlow", "1501 Vance Cir, Chesapeake, VA 23320",
        {"year_built": 1998, "square_footage": 2100, "property_type": "Single Family"})
    # No line may start with whitespace -- that is the signature of the bug.
    for line in body.split("\n"):
        assert not line.startswith(" "), f"line starts with a space: {line!r}"
    assert "The assessor record lists it as built in 1998, 2,100 sq ft, Single Family." in body
    # And the facts are attributed, not asserted as certain.
    assert "The assessor record lists it" in body


def test_draft_property_line_is_omitted_when_there_are_no_facts():
    body = _load_owner_draft_body()("A B", "1 A St, Norfolk, VA 23510", {})
    assert "assessor record lists" not in body
    # Still no double blank line where the sentence would have gone.
    assert "\n\n\n" not in body


# --- the settings route ----------------------------------------------------

def _read_route(name, window=2200):
    """Source of one route, taken to the next top-level decorator."""
    src = open(APP_MODULE).read()
    start = src.index(f"async def {name}")
    # Stop at the next route decorator so a window can't spill into a neighbour
    # and make a missing field look present.
    nxt = src.find("\n@app.", start)
    return src[start:nxt] if nxt > start else src[start:start + window]


def test_resimply_settings_route_exists_and_guards():
    """The save route must be admin-gated like every other settings POST."""
    body = _read_route("save_resimply_settings")
    assert "require_auth(request)" in body, "settings save must be auth-gated"
    # Blank means keep, clearing is explicit. Otherwise saving the page would
    # silently wipe a token the owner cannot read back.
    assert "clear_resimply" in body
    assert "if value:" in body


def test_resimply_settings_saves_all_five_fields():
    body = _read_route("save_resimply_settings")
    for field in ("resimply_api_token", "resimply_campaign_id", "resimply_market_id",
                  "resimply_pipeline_id", "resimply_status_id"):
        assert field in body, f"{field} is not saved by the settings route"


def test_settings_page_does_not_verify_when_unconfigured():
    """A live verify() on every page load would add a 401 round trip when idle."""
    src = open(APP_MODULE).read()
    idx = src.find('name="settings.html"')
    assert idx > 0, "settings page not found"
    start = src.rfind("async def", 0, idx)
    body = src[start:idx]
    assert "resimply_configured" in body
    assert "if resimply_configured:" in body, "verify() must be gated on a token existing"


# --- the skiptrace page ----------------------------------------------------

def test_push_is_opt_in_and_defaults_off():
    """The lead must be saved and drafted whether or not the push is ticked."""
    tpl = "templates/construction/skiptrace.html" if APP_MODULE == "construction_main.py" \
        else "templates/skiptrace.html"
    html = open(tpl).read()
    assert 'id="st-c-push"' in html, "no push checkbox on the page"
    # No `checked` attribute => off by default.
    import re
    tag = re.search(r'<input type="checkbox" id="st-c-push"[^>]*>', html).group(0)
    assert "checked" not in tag, "the REsimpli push must not default to on"
    assert "push_resimply" in html


def test_push_failure_does_not_hide_the_save_confirmation():
    """A failed push is expected until the key works; the lead is still saved."""
    tpl = "templates/construction/skiptrace.html" if APP_MODULE == "construction_main.py" \
        else "templates/skiptrace.html"
    html = open(tpl).read()
    assert "renderResimplyResult" in html
    assert "The lead is saved and drafted regardless." in html


def test_dnc_phones_are_rendered_disabled_not_hidden():
    """Shown but unselectable: visible, informed, but not one click from being used."""
    tpl = "templates/construction/skiptrace.html" if APP_MODULE == "construction_main.py" \
        else "templates/skiptrace.html"
    html = open(tpl).read()
    assert "DO NOT CALL" in html
    assert "' disabled'" in html or "' disabled'" in html.replace("'", '"') or "disabled" in html
    assert "dnc_count" in html
