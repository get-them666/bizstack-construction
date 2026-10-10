"""A dead credit balance must not look like a working assistant.

    python3 -m pytest test_model_unavailable.py -v

On 2026-10-10 the Copilot answered every message with "Message received. Our
team will follow up with you shortly." The cause was an OpenAI 429
insufficient_quota -- the account was out of credits. That string is the
customer-facing lead-capture fallback, and it was being returned to the owner
because the generic exception handler swallowed the billing failure.

It reads like an assistant that is working and simply has nothing to say, which
is why it went unquestioned. The Copilot now raises ModelUnavailable and the
endpoint returns 503 with the reason.

The customer-facing paths must NOT change: a homeowner texting in should still
get a polite answer, not an HTTP error.

No network: the model call is stubbed.
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


def _client_that_raises(exc):
    class C:
        def create(self, **kw):
            raise exc
    return type("Client", (), {"chat": type("Ch", (), {"completions": C()})()})()


def _agent(exc, subset="copilot"):
    a = BusinessAIAgent(subset=subset, tool_handlers={"t": lambda: {"ok": True}})
    a._client = _client_that_raises(exc)
    return a


QUOTA_ERRORS = [
    "Error code: 429 - {'error': {'message': 'You have no credits remaining. "
    "Add credits to continue using the API at https://platform.openai.com/settings/"
    "organization/billing/.', 'type': 'insufficient_quota', 'code': 'credit_balance_exhausted'}}",
    "Error code: 429 - insufficient_quota",
    "You exceeded your current quota, please check your plan and billing details.",
    "billing_hard_limit_reached",
]


@pytest.mark.parametrize("message", QUOTA_ERRORS)
def test_billing_failure_is_detected(monkeypatch, message):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert BusinessAIAgent._billing_failure(Exception(message))


def test_transient_errors_are_not_billing():
    """A 5xx or a timeout is worth retrying, not paging the owner about billing."""
    for msg in ("Error code: 500 - internal server error",
                "Connection timed out",
                "Rate limit reached for gpt-4o-mini"):
        assert BusinessAIAgent._billing_failure(Exception(msg)) == ""


def test_copilot_raises_on_empty_credits(monkeypatch):
    """The regression: this used to return the polite fallback."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(ModelUnavailable) as exc:
        _agent(RuntimeError(QUOTA_ERRORS[0])).process_conversation([], "hello")
    text = str(exc.value)
    assert "credits" in text.lower()
    assert "platform.openai.com" in text, "must say where to fix it"


def test_copilot_still_falls_back_on_other_errors(monkeypatch):
    """Only billing is surfaced. A transient fault keeps the old behaviour so a
    blip does not turn into a scary 503."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    out = _agent(RuntimeError("Error code: 500 - internal error")).process_conversation([], "hi")
    assert "follow up" in out


def test_customer_assistant_keeps_its_fallback(monkeypatch):
    """A homeowner texting in must get a polite answer, not an HTTP error."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    out = _agent(RuntimeError(QUOTA_ERRORS[0]), subset="guest").process_conversation([], "hi")
    assert "follow up" in out


def test_endpoint_maps_it_to_503():
    """The wiring, checked without a database."""
    import construction_main
    import inspect
    src = inspect.getsource(construction_main.copilot_chat)
    assert "ModelUnavailable" in src
    assert "status_code=503" in src
    assert "billing" in src


# --- the retry classifier ----------------------------------------------------
# A false positive here is worse than a miss. Dropping into the free Zen loop
# burns the remaining candidates and degrades the answer to a general chat
# model, so a failure that was never transient must never be treated as one.

RETRYABLE = [
    "Error code: 503 - {'error': {'message': 'The server is overloaded'}}",
    "APIStatusError: Error code: 503 - {'error': {'message': 'Service Unavailable'}}",
    "Error code: 502 - Bad Gateway",
    "RateLimitError: Rate limit reached",
    "APITimeoutError: Request timed out",
    "APIConnectionError: Connection reset by peer",
]

NOT_RETRYABLE = [
    # An auth failure is never retried: a wrong key fails on every model, so
    # retrying only multiplies one clear error across several confusing ones.
    "AuthenticationError: {'error': {'message': 'Incorrect API key provided'}}",
    "Error code: 401 - {'error': {'message': 'Incorrect API key'}}",
    # A caller-side error is not transient either.
    "BadRequestError: Error code: 400 - too many tokens",
]


@pytest.mark.parametrize("message", RETRYABLE)
def test_transient_failures_are_retried(message):
    assert BusinessAIAgent._unavailable_model(Exception(message))


@pytest.mark.parametrize("message", NOT_RETRYABLE)
def test_permanent_failures_are_not_retried(message):
    assert not BusinessAIAgent._unavailable_model(Exception(message))