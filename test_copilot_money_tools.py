"""Money answers the Copilot got wrong because the tools could not see the data.

    python3 -m pytest test_copilot_money_tools.py -v

Why this file exists
--------------------
Asked "did the direct deposit to Shaun OLeary go through", the Copilot answered
that the latest payroll run was open with no pay lines. That was true of the
wrong run: `get_payroll_summary` only queried `status IN ('open','closed')`, so
a run that had already been paid was invisible to it, and `ach_sent_at` was
never returned at all. Asked which leads were waiting for a deposit, it read
`leads.deposit_status` and missed four deposit invoices on `bid_invoices`.

Those are capability bugs, not model bugs: nothing was advertised that could
answer the question. The fix added four tools, and this file holds the wiring
to the same contract as test_lead_pipeline_tools.py -- every one advertised AND
handled. Nothing here touches a database or the network.
"""

import pytest

from con_ai_agent import BusinessAIAgent
import construction_main as main

MONEY_TOOLS = [
    "get_payroll_summary",     # recent runs including paid + ach_sent_at
    "crew_pay_history",        # one person across every run
    "get_deposit_invoices",    # deposits sent and not collected
    "list_addresses",          # deduped properties for flyer runs
]


class _FakeDb:
    """The handlers only close over db; building the dict must not query."""

    def cursor(self):
        raise AssertionError("build_copilot_handlers must not touch the database")

    def commit(self):
        pass

    def rollback(self):
        pass


def _advertised():
    return {t["function"]["name"] for t in BusinessAIAgent(subset="copilot")._tools()}


def _handlers():
    return set(main.build_copilot_handlers(_FakeDb(), "owner@example.com"))


@pytest.mark.parametrize("name", MONEY_TOOLS)
def test_money_tool_is_advertised_and_handled(name):
    assert name in _advertised(), f"{name} has a handler but no schema: unreachable"
    assert name in _handlers(), f"{name} is advertised but has no handler: it can only fail"


def test_list_leads_returns_the_address():
    """The Copilot could not produce any address list because list_leads did not
    select the column -- the refusal was a policy problem, this was a code one."""
    import inspect
    src = inspect.getsource(main.build_tool_handlers)
    start = src.index("def list_leads(")
    block = src[start:start + 900]
    assert "address" in block, "list_leads no longer selects the postal address"


def test_payroll_summary_is_not_limited_to_unpaid_runs():
    """The original bug in one line: it filtered the run table to open/closed."""
    import inspect
    src = inspect.getsource(main.build_tool_handlers)
    start = src.index("def get_payroll_summary(")
    block = src[start:start + 900]
    assert "status IN ('open','closed')" not in block
    assert "ach_sent_at" in src[start:start + 2000]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
