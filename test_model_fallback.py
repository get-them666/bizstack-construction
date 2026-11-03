"""A rate-limited model must fall through to a free one instead of going quiet.

    venv/bin/python -m pytest test_model_fallback.py -v

The Copilot is being retargeted at OpenCode Zen, which is OpenAI-compatible at
https://opencode.ai/zen/v1 -- setting COPILOT_BASE_URL is enough, because the
client property passes it straight to the SDK. Deliberately NOT OPENAI_BASE_URL:
that one is process-global and would drag the live voice agent and web_search
along with it. Zen's free models are free but heavily rate limited: driving the
local server picked ling-3.1-flash-free and got back 429 "Endpoint is
unavailable".

That failure mode is dangerous precisely because it is quiet. An unhandled 429
lands in the generic handler and returns "Message received. Our team will follow
up with you shortly" -- which is the same silent-fallback trap that test_model_unavailable.py
was written for, except a rate limit is transient and would clear on its own if
anybody had looked.

Three guarantees here:
  1. the configured model is tried first, and only then the free ones
  2. the free tier ONLY engages on Zen -- opencode.ai with an OpenAI key is a
     guaranteed 401, so retrying there would just multiply one clear error
  3. an auth failure is NOT retried: a wrong key fails on every model

No network and no database: the OpenAI client is a stub.
"""
import pytest

from con_ai_agent import BusinessAIAgent, ModelUnavailable, guest_fallback


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _Resp:
    def __init__(self, message):
        self.choices = [type("C", (), {"message": message})()]


class _Completions:
    """Records every model asked for, and fails the scripted ones."""

    def __init__(self, script):
        self._script = dict(script)
        self.models = []

    def create(self, **kw):
        model = kw["model"]
        self.models.append(model)
        exc = self._script.get(model)
        if exc:
            raise exc
        return _Resp(_Msg(content=f"answered by {model}"))


class _StubClient:
    def __init__(self, script):
        self.chat = type("C", (), {"completions": _Completions(script)})()


def _agent(script, **env):
    a = BusinessAIAgent(subset="copilot", tool_handlers={"t": lambda: {"ok": True}})
    a._client = _StubClient(script)
    for k, v in env.items():
        a.__dict__[k] = v
    return a


@pytest.fixture(autouse=True)
def _has_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("COPILOT_BASE_URL", raising=False)
    monkeypatch.delenv("COPILOT_FREE_FALLBACK_MODELS", raising=False)


RATE_LIMITED = RuntimeError("Error code: 429 - provider.rate-limit: Endpoint is unavailable")
SERVER_DOWN = RuntimeError("Error code: 503 - service unavailable")
BAD_KEY = RuntimeError("Error code: 401 - invalid_api_key")


# --- classification ----------------------------------------------------

def test_rate_limits_are_retryable_but_auth_is_not():
    for exc in (RATE_LIMITED, SERVER_DOWN, RuntimeError("upstream timeout")):
        assert BusinessAIAgent._unavailable_model(exc), "transient, so retry another model"
    assert BusinessAIAgent._unavailable_model(BAD_KEY) == "", \
        "a wrong key fails on every model; retrying only blurs the error"


def test_billing_is_still_not_a_retry():
    """Out of credits is the owner's problem, not something to route around.

    Silently dropping to a free model here would hide the one fault the owner
    can actually fix.
    """
    exc = RuntimeError("insufficient_quota: credit_balance_exhausted")
    assert BusinessAIAgent._billing_failure(exc)
    assert BusinessAIAgent._unavailable_model(exc) == ""


# --- candidate ordering ------------------------------------------------

def test_no_fallback_off_zen():
    a = BusinessAIAgent(subset="copilot")
    assert a._model_candidates() == [a._model], "an OpenAI key on opencode.ai is a 401"


def test_configured_model_is_always_first(monkeypatch):
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1")
    a = BusinessAIAgent(subset="copilot", model="gpt-5.5")
    assert a._model_candidates()[0] == "gpt-5.5"


def test_free_models_never_repeat_the_configured_one(monkeypatch):
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.setenv("COPILOT_FREE_FALLBACK_MODELS", "space-bunny-free,big-pickle")
    a = BusinessAIAgent(subset="copilot", model="space-bunny-free")
    assert a._model_candidates() == ["space-bunny-free", "big-pickle"]


def test_env_overrides_the_roting_default_list(monkeypatch):
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.setenv("COPILOT_FREE_FALLBACK_MODELS", " one-free , two-free ,")
    a = BusinessAIAgent(subset="copilot")
    assert a._model_candidates()[1:] == ["one-free", "two-free"]


# --- the fallback itself ------------------------------------------------

def test_a_rate_limited_model_falls_through_to_a_free_one(monkeypatch):
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.setenv("COPILOT_FREE_FALLBACK_MODELS", "space-bunny-free")
    a = _agent({"gpt-5.5": RATE_LIMITED})
    a._model = "gpt-5.5"
    out = a.process_conversation([], "hello")
    assert out == "answered by space-bunny-free"
    assert a._client.chat.completions.models == ["gpt-5.5", "space-bunny-free"]


def test_a_healthy_model_is_never_displaced(monkeypatch):
    """The free tier is a fallback, not a default. It must cost nothing when
    the paid model is working."""
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.setenv("COPILOT_FREE_FALLBACK_MODELS", "space-bunny-free")
    a = _agent({})
    a._model = "gpt-5.5"
    assert a.process_conversation([], "hello") == "answered by gpt-5.5"
    assert a._client.chat.completions.models == ["gpt-5.5"]


def test_a_bad_key_is_not_retried_and_is_surfaced_to_the_owner(monkeypatch):
    """A wrong key fails identically on every model, so it is tried once. And
    since 2026-10-28 (a 401 that silently took down the Copilot, site chat and
    phone line together) the Copilot raises it rather than answering with the
    customer fallback."""
    a = _agent({"gpt-5.5": BAD_KEY})
    a._model = "gpt-5.5"
    with pytest.raises(ModelUnavailable) as exc:
        a.process_conversation([], "hello")
    assert "OPENAI_API_KEY" in str(exc.value)
    assert a._client.chat.completions.models == ["gpt-5.5"], "never retried"


def test_every_candidate_failing_tries_them_all_then_falls_back(monkeypatch):
    """Four rate-limited models in a row is still transient, not a billing
    fault, so the polite fallback stands -- same contract as a 500.

    What matters is that all four were actually tried: the free tier is what
    turns a single 429 into a working answer, so a chain that gave up early
    would look identical from here.
    """
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.setenv("COPILOT_FREE_FALLBACK_MODELS", "space-bunny-free,big-pickle")
    a = _agent({"gpt-5.5": RATE_LIMITED, "space-bunny-free": RATE_LIMITED,
                "big-pickle": RATE_LIMITED})
    a._model = "gpt-5.5"
    assert a.process_conversation([], "hello") == guest_fallback()
    assert a._client.chat.completions.models == ["gpt-5.5", "space-bunny-free", "big-pickle"]


def test_the_deadline_stops_the_retry_chain(monkeypatch):
    """A blown deadline must not buy extra round trips by retrying.

    The tool-loop deadline already backstops the request as a whole; this is the
    inner guard, so a slow first model cannot spend the whole budget failing
    over to free ones that will also be too slow.
    """
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.setenv("COPILOT_FREE_FALLBACK_MODELS", "space-bunny-free,big-pickle")
    a = _agent({"gpt-5.5": RATE_LIMITED})
    a._model = "gpt-5.5"
    import con_ai_agent
    orig = con_ai_agent.time.monotonic
    con_ai_agent.time.monotonic = lambda: 99.0
    try:
        with pytest.raises(ModelUnavailable) as exc:
            a._complete([{"role": "user", "content": "hi"}], 100, deadline=90.0)
    finally:
        con_ai_agent.time.monotonic = orig
    assert a._client.chat.completions.models == [], "deadline was already blown"
    assert "No model was available" in str(exc.value)


# --- the owner-facing message -------------------------------------------

def test_the_out_of_credits_message_points_at_the_right_billing_page(monkeypatch):
    """On Zen, "add credit at platform.openai.com" sends the owner to a page
    that cannot possibly fix it."""
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1")
    a = _agent({"gpt-5.5": RuntimeError("insufficient_quota")})
    a._model = "gpt-5.5"
    with pytest.raises(ModelUnavailable) as exc:
        a.process_conversation([], "hello")
    text = str(exc.value)
    assert "opencode.ai/zen" in text
    assert "platform.openai.com" not in text


def test_off_zen_the_message_is_unchanged():
    a = _agent({"gpt-5.5": RuntimeError("insufficient_quota")})
    a._model = "gpt-5.5"
    with pytest.raises(ModelUnavailable) as exc:
        a.process_conversation([], "hello")
    assert "platform.openai.com" in str(exc.value)

def test_a_trailing_slash_still_counts_as_zen(monkeypatch):
    """The fallback turns OFF silently if the URL match is brittle, and a
    rate-limited Zen model would then have nowhere to go."""
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1/")
    a = BusinessAIAgent(subset="copilot")
    assert a._on_zen()
    assert len(a._model_candidates()) > 1


def test_another_host_is_not_zen(monkeypatch):
    """A self-hosted gateway that merely mentions opencode must not inherit
    Zen's free-model list -- those ids do not exist there."""
    monkeypatch.setenv("COPILOT_BASE_URL", "https://proxy.example.com/opencode/v1")
    assert not BusinessAIAgent(subset="copilot")._on_zen()


def test_the_global_openai_base_url_does_not_move_the_copilot(monkeypatch):
    """The scoping that makes this safe.

    OPENAI_BASE_URL is process-global, so the OpenAI SDK picks it up for every
    client in the app -- including the live voice agent and web_search's
    Responses call, neither of which Zen serves. The Copilot must ignore it, so
    setting it cannot silently drag the rest of the app onto Zen.
    """
    monkeypatch.setenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1")
    assert not BusinessAIAgent(subset="copilot")._on_zen()
    assert BusinessAIAgent(subset="copilot")._model_candidates() == [
        BusinessAIAgent(subset="copilot")._model]


def test_copilot_base_url_is_what_reaches_the_sdk(monkeypatch):
    """The retarget has to be real: the client is built with that base_url."""
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1")
    a = BusinessAIAgent(subset="copilot")
    assert "opencode.ai" in str(a.client.base_url)


def test_the_client_defaults_to_openai_when_unset(monkeypatch):
    """Absent the opt-in, nothing changes. Copilot stays exactly where it was."""
    monkeypatch.delenv("COPILOT_BASE_URL", raising=False)
    a = BusinessAIAgent(subset="copilot")
    assert "api.openai.com" in str(a.client.base_url)


# --- keyless Zen -------------------------------------------------------
# Zen's free models are authenticated with an EMPTY bearer. Verified live on
# 2026-10-10: POST /zen/v1/chat/completions returns 200 with no Authorization
# header, and 401 the moment a non-empty one is sent. So the placeholder string
# in ~/.local/share/opencode/auth.json ("opencode auth login") was not a
# missing credential to fill in -- it was actively breaking the request.
#
# This matters because the old gate was `if not os.getenv("OPENAI_API_KEY"):
# return fallback`, which is exactly the silent-no-op that made the Copilot
# look alive while doing nothing.

def test_zen_needs_no_api_key(monkeypatch):
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    a = BusinessAIAgent(subset="copilot")
    assert a.client.base_url == "https://opencode.ai/zen/v1/", "client must build with no key"


def test_zen_sends_an_empty_bearer_not_a_placeholder(monkeypatch):
    """A placeholder string 401s on Zen, so the header must be emptied."""
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    a = BusinessAIAgent(subset="copilot")
    assert a.client.default_headers.get("Authorization") == ""


def test_no_key_still_raises_off_zen(monkeypatch):
    """The zero-key path is Zen-only. Off Zen there is genuinely nothing to
    call, and silently returning a client would hide that."""
    monkeypatch.delenv("COPILOT_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        BusinessAIAgent(subset="copilot").client


def test_a_real_key_is_still_used_when_present(monkeypatch):
    """The empty-bearer trick must not override a genuine credential."""
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key")
    assert BusinessAIAgent(subset="copilot").client.api_key == "sk-real-key"


def test_a_keyless_zen_run_does_not_short_circuit(monkeypatch):
    """The regression this whole change exists for: no key used to return the
    polite fallback without ever calling a model."""
    monkeypatch.setenv("COPILOT_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    a = _agent({})
    a._model = "space-bunny-free"
    assert a.process_conversation([], "hello") == "answered by space-bunny-free"


def test_copilot_model_is_scoped_so_loan_outreach_is_untouched(monkeypatch):
    """OPENAI_MODEL is shared with loan_outreach.py, which drafts real SBA
    lender replies against OpenAI. Pointing it at a Zen model id would 404
    every lender draft, so the Copilot reads its own variable first.
    """
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("COPILOT_MODEL", "space-bunny-free")
    assert BusinessAIAgent(subset="copilot")._model == "space-bunny-free"
    monkeypatch.delenv("COPILOT_MODEL")
    assert BusinessAIAgent(subset="copilot")._model == "gpt-4o-mini"


def test_an_explicit_model_argument_still_wins(monkeypatch):
    monkeypatch.setenv("COPILOT_MODEL", "space-bunny-free")
    assert BusinessAIAgent(subset="copilot", model="gpt-5.5")._model == "gpt-5.5"


# --- parameter shapes the provider rejects ------------------------------
#
# 2026-11-03: COPILOT_MODEL was set to gpt-6-luna. That model rejects
# `max_tokens` exactly as the GPT-5 reasoning models do, but it was not in the
# prefix list, so every turn 400'd, fell into the generic handler and returned
# the "assistant is offline" fallback. The Copilot looked dead for 15 hours and
# the only evidence was one "AI agent fallback triggered" line in the log.
#
# The fix is not a longer prefix list: the shape is learned from the rejection
# and remembered, so the next model rename costs one extra round trip instead of
# an outage.

import con_ai_agent  # noqa: E402

MAX_TOKENS_400 = RuntimeError(
    "Error code: 400 - Unsupported parameter: 'max_tokens' is not supported with "
    "this model. Use 'max_completion_tokens' instead."
)
TEMPERATURE_400 = RuntimeError(
    "Error code: 400 - Unsupported parameter: 'temperature' is not supported with "
    "this model."
)


class _StrictCompletions:
    """Rejects parameters the way gpt-6-luna does, and records every call."""

    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if "max_tokens" in kw:
            raise MAX_TOKENS_400
        if "temperature" in kw:
            raise TEMPERATURE_400
        return _Resp(_Msg(content="answered"))


def _strict_agent(monkeypatch, model="gpt-6-luna"):
    con_ai_agent._MODEL_QUIRKS.clear()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    a = BusinessAIAgent(subset="copilot", model=model,
                        tool_handlers={"t": lambda: {"ok": True}})
    completions = _StrictCompletions()
    a._client = type("C", (), {"chat": type("C", (), {"completions": completions})()})()
    return a, completions


def test_gpt6_uses_the_new_token_parameter_up_front(monkeypatch):
    con_ai_agent._MODEL_QUIRKS.clear()
    assert BusinessAIAgent._uses_completion_token_limit("gpt-6-luna")
    assert not BusinessAIAgent._accepts_temperature("gpt-6-luna")


def test_a_rejected_parameter_is_retried_not_fallen_back(monkeypatch):
    a, calls = _strict_agent(monkeypatch, model="mystery-model-1")
    assert a.process_conversation([], "hello") == "answered", \
        "a 400 on max_tokens must still answer, not return the offline fallback"
    assert "max_completion_tokens" in calls.calls[-1]
    assert "max_tokens" not in calls.calls[-1]
    assert "temperature" not in calls.calls[-1], "reasoning models refuse it too"


def test_the_learned_shape_is_reused_on_the_next_turn(monkeypatch):
    a, calls = _strict_agent(monkeypatch, model="mystery-model-2")
    assert a.process_conversation([], "hello") == "answered"
    settled = len(calls.calls)
    assert a.process_conversation([], "again") == "answered"
    assert len(calls.calls) == settled + 1, "the 400 must not be paid again every turn"


def test_a_genuine_bad_request_is_not_retried(monkeypatch):
    """Only the 'model does not take that parameter' wording counts.

    A plain 400 is a real problem -- retrying it doubles the latency before the
    same failure, and in the Copilot it would hide a bug behind a retry.
    """
    offending = RuntimeError("Error code: 400 - Invalid value for 'messages'")
    assert not BusinessAIAgent._learn_param_quirk(offending, "gpt-6-luna")


class _ToolReasoningCompletions:
    """Mimics gpt-6-luna with tools on /v1/chat/completions.

    It rejects *two* parameters and reports them one at a time: function tools at
    the default reasoning effort first, then `max_tokens`. A single corrected
    retry is not enough here, which is exactly the case the loop is for.
    """

    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if kw.get("tools") and kw.get("reasoning_effort") != "none":
            raise RuntimeError(
                "Error code: 400 - Function tools with reasoning_effort are not "
                "supported for gpt-6-luna in /v1/chat/completions. To use function "
                "tools, use /v1/responses or set reasoning_effort to 'none'."
            )
        if "max_tokens" in kw:
            raise MAX_TOKENS_400
        if "temperature" in kw:
            raise TEMPERATURE_400
        return _Resp(_Msg(content="answered"))


def test_every_rejected_parameter_is_learned_in_one_turn(monkeypatch):
    con_ai_agent._MODEL_QUIRKS.clear()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    a = BusinessAIAgent(subset="copilot", model="mystery-model-3",
                        tool_handlers={"t": lambda: {"ok": True}})
    completions = _ToolReasoningCompletions()
    a._client = type("C", (), {"chat": type("C", (), {"completions": completions})()})()

    assert a.process_conversation([], "hello") == "answered"
    quirks = con_ai_agent._MODEL_QUIRKS["mystery-model-3"]
    assert quirks["reasoning_effort"] == "none"
    assert quirks["completion_tokens"] is True

    settled = len(completions.calls)
    assert a.process_conversation([], "again") == "answered"
    assert len(completions.calls) == settled + 1, "the settled shape must go out first"
    assert completions.calls[-1]["reasoning_effort"] == "none"
    assert "max_completion_tokens" in completions.calls[-1]
