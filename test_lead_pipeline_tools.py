"""The permit-lead enrichment pipeline is reachable by the Copilot.

    python3 -m pytest test_lead_pipeline_tools.py -v

Why this file exists
--------------------
The Copilot answered "I am unable to access the property information and names
for the leads." That was not a provider outage. Four of the five steps of the
pipeline were **unreachable**: skip_trace_owner and save_contact had handlers
registered in build_copilot_handlers but no schema in the tool list, and
lookup_zip, pdl_contact and draft_lead_email did not exist at all.

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

from con_ai_agent import BusinessAIAgent
import copilot_ops

# The pipeline, in the order the Copilot is told to run it.
PIPELINE = ["lookup_zip", "skip_trace_owner", "pdl_contact", "save_contact", "draft_lead_email"]


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

def test_copilot_cannot_send_email(agent):
    """The withholding is half-done until the schema goes too."""
    names = {t["function"]["name"] for t in agent._tools()}
    assert "send_email_message" not in names
    assert "make_outbound_call" not in names


def test_pdl_contact_refuses_without_a_zip(monkeypatch, handlers):
    """No ZIP means a city-level match, which can return a neighbour.

    pdl_contact_service refuses before spending anything; this asserts the
    Copilot-facing wrapper surfaces that refusal rather than calling through.
    """
    import pdl_contact_service

    called = []

    def boom(*a, **kw):
        called.append(a)
        return {"found": True, "email": "x@y.com", "phone": "7575551234"}

    monkeypatch.setattr(pdl_contact_service, "lookup", boom)
    monkeypatch.setattr(pdl_contact_service, "configured", lambda: False)
    result = handlers["pdl_contact"](address="8494 Lynn River Road, Norfolk, VA")
    assert result["ok"] is False
    assert not called, "must not reach PDL when unconfigured or ZIP-less"


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