"""The phone line must never go silent because the model did.

    venv/bin/python -m pytest test_vapi_model_down.py -v

Vapi answers the business line (757) 908-7121 and asks this app for every
reply via /vapi/llm/chat/completions. When that endpoint fails, Vapi's
behaviour is to speak the assistant's firstMessage and then say nothing for
the rest of the call. On 2026-10-28 OPENAI_API_KEY was rejected (401) and the
handler let the AuthenticationError escape as a 500, so every caller heard
the greeting and then dead air.

The contract pinned here: a model failure produces a 200 with a spoken
fallback -- in both the streaming (SSE) and plain JSON shapes Vapi uses -- and
never a 5xx.

No network and no database: the model call is patched, get_db is overridden.
"""
import json

import pytest
from fastapi.testclient import TestClient

import construction_main as main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("VOICE_BASE_URL", raising=False)

    def boom(*args, **kwargs):
        raise RuntimeError(
            "AuthenticationError: Error code: 401 - Incorrect API key provided: sk-proj-****KmEA."
        )

    monkeypatch.setattr(main, "_vapi_assistant_text", boom)
    main.app.dependency_overrides[main.get_db] = lambda: object()
    try:
        yield TestClient(main.app, raise_server_exceptions=True)
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)


PAYLOAD = {
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Hi, I need a bathroom remodel quote."}],
}


def test_a_model_failure_is_spoken_not_500d(client):
    r = client.post("/vapi/llm/chat/completions", json=PAYLOAD)
    assert r.status_code == 200
    body = r.json()
    text = body["choices"][0]["message"]["content"]
    assert text == main.VOICE_MODEL_DOWN_TEXT
    assert body["choices"][0]["finish_reason"] == "stop"


def test_the_streaming_shape_also_speaks_the_fallback(client):
    r = client.post("/vapi/llm/chat/completions", json={**PAYLOAD, "stream": True})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    chunks = [line[len("data: "):] for line in r.text.splitlines() if line.startswith("data: ")]
    assert chunks[-1] == "[DONE]"
    spoken = "".join(
        json.loads(c)["choices"][0]["delta"].get("content", "")
        for c in chunks[:-1]
    ).strip()
    assert spoken == main.VOICE_MODEL_DOWN_TEXT


def test_the_fallback_gives_the_caller_a_way_forward():
    """Dead air was the bug; a bare apology would be only half a fix."""
    text = main.VOICE_MODEL_DOWN_TEXT.lower()
    assert "call" in text and ("website" in text or "quote" in text)
    # It is spoken aloud, so it must not contain anything a TTS engine would
    # read out awkwardly.
    assert "http" not in text and "@" not in text
