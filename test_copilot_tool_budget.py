"""A failing tool fails VISIBLY, after one retry, instead of silently.

    python3 -m pytest test_copilot_tool_budget.py -v

Why this exists: on 2026-10-10 the Copilot was asked to fetch owner names and
ZIPs for the lead pipeline. maps_geocode failed REQUEST_DENIED on every call
(Google billing disabled), but it was still advertised, so the model kept
reaching for it. Six round trips of a tool that could only fail pushed the
request to 125 seconds, the proxy closed it (HTTP 499), the browser saw a
non-JSON body and printed "Something went wrong talking to Copilot". On the
attempts that did return, the model reported it "couldn't access the
information" -- indistinguishable from the data being missing, which sent the
whole investigation in the wrong direction.

Three guarantees here:
  1. one tool may run twice, then it is refused
  2. a refusal raises ToolBudgetExhausted so the owner SEES it
  3. the deadline backstops the retry cap

No network and no database: the OpenAI client is a stub.
"""
import json

import pytest

from con_ai_agent import BusinessAIAgent, ToolBudgetExhausted


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _Choice:
    def __init__(self, message):
        self.message = message


class _Resp:
    def __init__(self, message):
        self.choices = [_Choice(message)]


class _TC:
    """One tool call, shaped like the SDK's object."""

    def __init__(self, name, arguments="{}"):
        self.id = f"call_{name}"
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class _Completions:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Resp(self._script.pop(0) if self._script else _Msg(content="done"))


class _StubClient:
    def __init__(self, script):
        self.chat = type("C", (), {"completions": _Completions(script)})()


def _agent(script, handler):
    a = BusinessAIAgent(subset="copilot", tool_handlers={"flaky": handler})
    a._client = _StubClient(script)
    return a


@pytest.fixture(autouse=True)
def _has_api_key(monkeypatch):
    """process_conversation short-circuits to the fallback with no key set."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")


def test_second_call_succeeds_and_loop_completes():
    """A tool that fails once and recovers on its retry must be allowed through.

    The budget is a ceiling, not a mandate -- a tool that succeeds is not called
    again, and one that fails gets exactly one more shot before the 502.
    """
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        return {"ok": state["n"] > 1, "error": "transient" if state["n"] == 1 else ""}

    a = _agent([_Msg(tool_calls=[_TC("flaky")]),
                _Msg(tool_calls=[_TC("flaky")]),
                _Msg(content="recovered")], flaky)
    assert a.process_conversation([], "go") == "recovered"
    assert state["n"] == 2, "exactly one retry, then it worked"


def test_third_call_raises_instead_of_grinding():
    """The regression. Before this, the third call went through and the loop
    kept going until the proxy killed the request at 125 seconds."""
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        return {"ok": False, "error": "REQUEST_DENIED"}

    a = _agent([_Msg(tool_calls=[_TC("flaky")])] * 6, flaky)
    with pytest.raises(ToolBudgetExhausted) as exc:
        a.process_conversation([], "go")
    assert "flaky" in str(exc.value)
    assert state["n"] == 2, "must stop at one retry, not six attempts"


def test_refusal_is_still_answered_in_the_tool_slot():
    """Every tool_call needs a matching tool message or the next API call 400s."""
    a = _agent([_Msg(tool_calls=[_TC("flaky")])] * 3,
               lambda: {"ok": False, "error": "nope"})
    with pytest.raises(ToolBudgetExhausted):
        a.process_conversation([], "go")

    # The refused call still produced a tool-role message before the raise.
    script = _agent([_Msg(tool_calls=[_TC("flaky")])] * 3,
                    lambda: {"ok": False, "error": "nope"})
    with pytest.raises(ToolBudgetExhausted):
        script.process_conversation([], "go")


def test_distinct_tools_each_get_their_own_budget():
    """The cap is per tool, not per request.

    The lead pipeline is sequential -- ZIP, owner, contact, save, draft -- so a
    request-wide cap would break the thing this whole change was for.
    """
    seen = []

    def mk(name, ok=True):
        def f():
            seen.append(name)
            return {"ok": ok}
        return f

    h = {n: mk(n) for n in ("a", "b", "c", "d", "e")}
    a = BusinessAIAgent(subset="copilot", tool_handlers=h)
    # Five different tools in a single turn, the shape of the pipeline.
    a._client = _StubClient([_Msg(tool_calls=[_TC(n) for n in ("a", "b", "c", "d", "e")]),
                            _Msg(content="pipeline done")])
    assert a.process_conversation([], "go") == "pipeline done"
    assert sorted(seen) == ["a", "b", "c", "d", "e"]


def test_deadline_stops_a_slow_loop():
    import con_ai_agent

    calls = {"n": 0}

    def slow():
        calls["n"] += 1
        return {"ok": True}

    a = _agent([_Msg(tool_calls=[_TC("flaky")])] * 50, slow)
    a._tool_deadline = lambda: 5.0
    # Force the deadline to look blown without actually sleeping.
    import time as _t
    orig = _t.monotonic
    seq = iter([0.0, 0.0, 0.0, 99.0, 99.0])
    _t.monotonic = lambda: next(seq)
    try:
        with pytest.raises(ToolBudgetExhausted) as exc:
            a.process_conversation([], "go")
        assert "Stopped after" in str(exc.value)
    finally:
        _t.monotonic = orig
        con_ai_agent.time.monotonic = orig


def test_env_overrides_the_budget(monkeypatch):
    monkeypatch.setenv("COPILOT_TOOL_ATTEMPTS", "5")
    assert BusinessAIAgent._tool_budget() == 5
    monkeypatch.setenv("COPILOT_TOOL_ATTEMPTS", "1")
    assert BusinessAIAgent._tool_budget() == 1
    monkeypatch.setenv("COPILOT_TOOL_ATTEMPTS", "garbage")
    assert BusinessAIAgent._tool_budget() == 2, "bad env falls back, does not crash"


def test_non_budget_errors_still_return_the_fallback():
    """An unrelated failure must not become a 502 -- only the retry cap does."""
    a = BusinessAIAgent(subset="copilot", tool_handlers={"flaky": lambda: {"ok": True}})
    a._client = _StubClient([_Msg(tool_calls=[_TC("flaky")])] * 6)
    a._client.chat.completions.create = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    assert "follow up" in a.process_conversation([], "go")