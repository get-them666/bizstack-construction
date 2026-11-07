"""The permit-lead enrichment pipeline is reachable by the Copilot.

    python3 -m pytest test_lead_pipeline_tools.py -v

Why this file exists
--------------------
The Copilot answered "I am unable to access the property information and names
for the leads." That was not a provider outage. Four of the five steps of the
pipeline were **unreachable**: skip_trace_owner and save_contact had handlers
registered in build_copilot_handlers but no schema in the tool list, and
lookup_zip, contact_lookup and draft_lead_email did not exist at all.

The failure mode is quiet and total. A handler in _tool_handlers is just a dict
entry; the API only ever offers the model the functions named in the schema list.
A handler with no schema is dead code, and the model reports the capability as
missing rather than erroring. So the wiring "looked" finished and was not.

These tests pin both halves of the contract -- every advertised tool has a
handler, and every pipeline handler is advertised -- so the next schema added
without a handler fails here instead of in production.

No network and no database.
"""

import inspect

import pytest

from pathlib import Path

from con_ai_agent import BusinessAIAgent
import copilot_ops

# The pipeline, in the order the Copilot is told to run it. enrich_leads is
# the batch form of steps 1-5 and is held to the same contract.
PIPELINE = ["lookup_zip", "skip_trace_owner", "contact_lookup", "save_contact",
            "draft_lead_email", "enrich_leads"]


class _FakeDb:
    """build_contact_tools only closes over db inside the handlers."""

    def cursor(self):
        raise AssertionError("schema build must not touch the database")

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def agent():
    return BusinessAIAgent(subset="copilot")


@pytest.fixture
def handlers():
    return copilot_ops.build_contact_tools(_FakeDb(), "owner@example.com")


@pytest.fixture
def advertised(agent):
    return {t["function"]["name"] for t in agent._tools()}


# --- reachability: the actual bug -------------------------------------------

def test_every_pipeline_step_has_a_schema(advertised):
    """Each step must be a tool the model can see.

    This is the regression. Before the fix, skip_trace_owner and save_contact
    were handler-only and invisible to the API.
    """
    missing = [name for name in PIPELINE if name not in advertised]
    assert not missing, f"handler exists but no schema: {missing}"


def test_every_pipeline_step_has_a_handler(handlers):
    """A schema with no handler returns 'No handler for tool' at runtime."""
    missing = [name for name in PIPELINE if name not in handlers]
    assert not missing, f"advertised but no handler: {missing}"


def test_pipeline_is_copilot_only_not_public_widget():
    """These tools write to leads, so they must not reach the public widget."""
    public = {t["function"]["name"] for t in BusinessAIAgent(subset="widget")._tools()}
    leaked = [name for name in PIPELINE if name in public]
    assert not leaked, f"exposed to the public widget: {leaked}"


def test_schema_params_match_handler_signature(handlers, agent):
    """Drift here fails at call time as 'Invalid tool arguments', not at build."""
    schemas = {t["function"]["name"]: t for t in agent._tools()}
    for name in PIPELINE:
        sig = set(inspect.signature(handlers[name]).parameters)
        props = set(schemas[name]["function"]["parameters"]["properties"])
        assert sig == props, f"{name}: handler{sorted(sig)} != schema{sorted(props)}"


def test_create_deposit_links_batch_is_copilot_only_and_wired():
    """The batch deposit-link tool must be visible to the Copilot and handled.

    It also creates real Stripe sessions, so it must never reach the public
    widget. The generic dispatch test below would catch a missing handler, but
    not a leak to the widget or a silently dropped schema.
    """
    import construction_main

    class _NoDb:
        def cursor(self):
            raise AssertionError("building the schema map must not touch the database")

        def commit(self):
            pass

        def rollback(self):
            pass

    handlers = construction_main.build_copilot_handlers(_NoDb(), "owner@example.com")
    copilot = {t["function"]["name"] for t in BusinessAIAgent(subset="copilot")._tools()}
    public = {t["function"]["name"] for t in BusinessAIAgent(subset="widget")._tools()}
    assert "create_deposit_links" in copilot
    assert "create_deposit_links" in handlers
    assert "create_deposit_links" not in public


def test_every_advertised_tool_is_dispatchable():
    """No advertised tool may resolve to a dead handler.

    The mirror of the original bug, found while fixing it: send_email_message's
    handler was popped from the Copilot on 2026-10-03 after it looped and sent
    three identical emails to a public office, but its schema was left in place,
    so the model was still offered a send tool that could only ever fail.

    Asserted against the real handler map rather than a stand-in, because the
    schema list and the handler dict are two separate lists kept in step by hand.
    """
    import construction_main

    class _NoDb:
        def cursor(self):
            raise AssertionError("building the schema map must not touch the database")

        def commit(self):
            pass

        def rollback(self):
            pass

    handlers = construction_main.build_copilot_handlers(_NoDb(), "owner@example.com")
    advertised = {t["function"]["name"] for t in BusinessAIAgent(subset="copilot")._tools()}

    dead = advertised - set(handlers)
    assert not dead, f"advertised with no handler, so they can only fail: {sorted(dead)}"


# --- safety invariants ------------------------------------------------------

def test_copilot_email_is_one_initiated_per_day_but_replies_reopen_the_thread(monkeypatch):
    """One cold email per day; once they answer, the conversation can continue.

    The 2026-10-03 loop was three emails to one address in ~30 seconds with no
    reply. The gate is on *initiated* mail, not on answering someone.
    """
    import construction_main

    sent = []

    def _fake_raw(**kw):
        sent.append(kw)
        return {"ok": True, "sent_to": kw.get("to")}

    monkeypatch.setattr(construction_main, "build_tool_handlers",
                        lambda db, svc: {"send_email_message": _fake_raw})
    monkeypatch.setattr(
        construction_main.auto_reply, "_recent_send_guard",
        lambda db, recipient, channel, days=1: (True, ""))
    monkeypatch.setattr(construction_main, "_copilot_email_min_gap_seconds", lambda: 60)

    handlers_map = construction_main.build_copilot_handlers(_FakeDb(), "owner@example.com")
    send = handlers_map["send_email_message"]

    # First email of the day: allowed.
    first = send(to="gc@example.com", subject="Plan set", body="Please send the RFI response.")
    assert first["ok"] is True
    assert len(sent) == 1

    # Second email the same day, no reply: refused.
    monkeypatch.setattr(
        construction_main.auto_reply, "_recent_send_guard",
        lambda db, recipient, channel, days=1: (False, "already emailed today"))
    monkeypatch.setattr(construction_main, "_copilot_conversation_open",
                        lambda db, address: False)
    denied = send(to="gc@example.com", subject="Follow-up", body="Any update?")
    assert denied["ok"] is False
    assert "until they reply" in denied["error"].lower()
    assert len(sent) == 1

    # They replied: the thread is open, so another email is allowed.
    monkeypatch.setattr(construction_main, "_copilot_conversation_open",
                        lambda db, address: True)
    monkeypatch.setattr(construction_main, "_copilot_seconds_since_outbound",
                        lambda db, address: 120.0)
    reply = send(to="gc@example.com", subject="Re: Plan set", body="Tuesday works.")
    assert reply["ok"] is True
    assert len(sent) == 2

    # Open thread, but the loop floor still applies.
    monkeypatch.setattr(construction_main, "_copilot_seconds_since_outbound",
                        lambda db, address: 10.0)
    too_soon = send(to="gc@example.com", subject="Re: Plan set", body="One more thing.")
    assert too_soon["ok"] is False
    assert "wait" in too_soon["error"].lower()
    assert len(sent) == 2


def test_copilot_chat_can_resolve_the_tool_logger_module():
    """The route calls con_ai_agent.copilot_agent_tool_logger.

    Only names were imported from con_ai_agent, so `con_ai_agent` itself was
    undefined in construction_main's globals. Every /api/copilot request died
    with NameError before the model was reached -- the Copilot was 500ing, not
    refusing. This pins the module binding the route actually uses.
    """
    import construction_main
    assert hasattr(construction_main, "con_ai_agent")
    assert callable(construction_main.con_ai_agent.copilot_agent_tool_logger)


def test_copilot_can_place_an_outbound_call():
    """Restored with the email tool; the schema must contain it."""
    names = {t["function"]["name"] for t in BusinessAIAgent(subset="copilot")._tools()}
    assert "make_outbound_call" in names


def test_the_operating_manual_does_not_forbid_email():
    """The schema is useless if the manual says the tool does not exist.

    construction_knowledge.md is concatenated into the system prompt, so a
    stale "no outbound email tool" sentence overrides the schema and makes the
    Copilot refuse a capability it actually has.
    """
    text = Path("construction_knowledge.md").read_text(encoding="utf-8")
    low = text.lower()
    assert "no outbound email tool" not in low
    assert "you cannot send email" not in low
    assert "send_email_message" in text

    schemas = {t["function"]["name"]: t
               for t in BusinessAIAgent(subset="copilot")._tools()}
    desc = schemas["send_email_message"]["function"]["description"].lower()
    assert "real email" in desc
    assert "ok=false" in desc, "the model must know a refusal is possible"


def test_contact_lookup_refuses_before_spending(monkeypatch, handlers):
    """No key or no ZIP means nothing may be billed.

    Skip Sherpa charges one credit per address, misses included, and this tool
    is the Copilot's only way to spend one. So an unconfigured provider or an
    unresolvable ZIP must return a refusal with the provider never reached.
    """
    import skip_sherpa_service

    called = []

    def boom(*a, **kw):
        called.append(a)
        return {"found": True, "email": "x@y.com", "phone": "7575551234"}

    monkeypatch.setattr(skip_sherpa_service, "trace", boom)
    monkeypatch.setattr(skip_sherpa_service, "configured", lambda: False)
    result = handlers["contact_lookup"](address="8494 Lynn River Road, Norfolk, VA")
    assert result["ok"] is False
    assert not called, "must not reach Skip Sherpa when unconfigured"


def test_draft_lead_email_is_staged_not_sent(handlers):
    """The tool's docstring promises it cannot transmit. Enforce the shape.

    A draft is the reviewable artifact; a send is not. This asserts the handler
    only ever returns the staged payload, and never a send confirmation.
    """
    import inspect as _inspect
    src = _inspect.getsource(handlers["draft_lead_email"])
    assert "draft_reply" in src
    for forbidden in ("documents_service", "send_email", "_fire_sent_effect"):
        assert forbidden not in src, f"draft tool must not transmit: {forbidden}"


def test_people_search_and_existing_lead_quote_are_copilot_only():
    copilot_tools = {t["function"]["name"]: t for t in BusinessAIAgent(subset="copilot")._tools()}
    public_tools = {t["function"]["name"]: t for t in BusinessAIAgent(subset="widget")._tools()}
    assert "find_people" in copilot_tools
    assert "find_people" not in public_tools
    copilot_quote = copilot_tools["quote_project"]["function"]["parameters"]
    public_quote = public_tools["quote_project"]["function"]["parameters"]
    assert "lead_id" in copilot_quote["properties"]
    assert "lead_id" not in public_quote["properties"]


def test_quote_tables_are_created_during_application_startup():
    """The leads board reads quotes before any quote route may have run.

    Production regression: ``quotes`` was only created lazily, so every leads
    page logged a missing-table error and silently hid quote-backed deposits.
    """
    import construction_main

    src = inspect.getsource(construction_main.lifecycle)
    assert "quoting.ensure_schema(conn)" in src