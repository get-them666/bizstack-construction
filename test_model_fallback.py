"""A rate-limited model must fall through to a free one instead of going quiet.

    venv/bin/python -m pytest test_model_fallback.py -v

The Copilot is being retargeted at OpenCode Zen, which is OpenAI-compatible at
https://opencode.ai/zen/v1 -- setting OPENAI_BASE_URL is enough, because the
OpenAI SDK reads that variable itself. Zen's free models are free but heavily
rate limited: driving the local server picked ling-3.1-flash-free and got back
429 "Endpoint is unavailable".

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

from con_ai_agent import BusinessAIAgent, ModelUnavailable


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
    monkeypatch.setenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1")
    a = BusinessAIAgent(subset="copilot", model="gpt-5.5")
    assert a._model_candidates()[0] == "gpt-5.5"


def test_free_models_never_repeat_the_configured_one(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.setenv("COPILOT_FREE_FALLBACK_MODELS", "space-bunny-free,big-pickle")
    a = BusinessAIAgent(subset="copilot", model="space-bunny-free")
    assert a._model_candidates() == ["space-bunny-free", "big-pickle"]


def test_env_overrides_the_roting_default_list(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.setenv("COPILOT_FREE_FALLBACK_MODELS", " one-free , two-free ,")
    a = BusinessAIAgent(subset="copilot")
    assert a._model_candidates()[1:] == ["one-free", "two-free"]


# --- the fallback itself ------------------------------------------------

def test_a_rate_limited_model_falls_through_to_a_free_one(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.setenv("COPILOT_FREE_FALLBACK_MODELS", "space-bunny-free")
    a = _agent({"gpt-5.5": RATE_LIMITED})
    a._model = "gpt-5.5"
    out = a.process_conversation([], "hello")
    assert out == "answered by space-bunny-free"
    assert a._client.chat.completions.models == ["gpt-5.5", "space-bunny-free"]


def test_a_healthy_model_is_never_displaced(monkeypatch):
    """The free tier is a fallback, not a default. It must cost nothing when
    the paid model is working."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.setenv("COPILOT_FREE_FALLBACK_MODELS", "space-bunny-free")
    a = _agent({})
    a._model = "gpt-5.5"
    assert a.process_conversation([], "hello") == "answered by gpt-5.5"
    assert a._client.chat.completions.models == ["gpt-5.5"]


def test_a_bad_key_is_not_retried_and_still_falls_back_quietly(monkeypatch):
    """Off Zen there is nowhere to go, so the customer-facing string stands --
    that is the pre-existing behaviour for a non-billing failure."""
    a = _agent({"gpt-5.5": BAD_KEY})
    a._model = "gpt-5.5"
    assert "follow up" in a.process_conversation([], "hello")
    assert a._client.chat.completions.models == ["gpt-5.5"]


def test_every_candidate_failing_tries_them_all_then_falls_back(monkeypatch):
    """Four rate-limited models in a row is still transient, not a billing
    fault, so the polite fallback stands -- same contract as a 500.

    What matters is that all four were actually tried: the free tier is what
    turns a single 429 into a working answer, so a chain that gave up early
    would look identical from here.
    """
    monkeypatch.setenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.setenv("COPILOT_FREE_FALLBACK_MODELS", "space-bunny-free,big-pickle")
    a = _agent({"gpt-5.5": RATE_LIMITED, "space-bunny-free": RATE_LIMITED,
                "big-pickle": RATE_LIMITED})
    a._model = "gpt-5.5"
    assert "follow up" in a.process_conversation([], "hello")
    assert a._client.chat.completions.models == ["gpt-5.5", "space-bunny-free", "big-pickle"]


def test_the_deadline_stops_the_retry_chain(monkeypatch):
    """A blown deadline must not buy extra round trips by retrying.

    The tool-loop deadline already backstops the request as a whole; this is the
    inner guard, so a slow first model cannot spend the whole budget failing
    over to free ones that will also be too slow.
    """
    monkeypatch.setenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1")
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
    monkeypatch.setenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1")
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
    monkeypatch.setenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1/")
    a = BusinessAIAgent(subset="copilot")
    assert a._on_zen()
    assert len(a._model_candidates()) > 1


def test_another_host_is_not_zen(monkeypatch):
    """A self-hosted gateway that merely mentions opencode must not inherit
    Zen's free-model list -- those ids do not exist there."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example.com/opencode/v1")
    assert not BusinessAIAgent(subset="copilot")._on_zen()
