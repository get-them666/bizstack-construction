"""The Copilot and voice prompts must be truthful and behave on a call.

    venv/bin/python -m pytest test_prompts.py -v

Why this file exists
--------------------
A tool refusal is JSON (``ok: false``), and the model is free to retell it as a
success. That is how the owner gets told an email or text went out when nothing
left the building, and how a missing lead/bid field turns into an invented
price. The prompt is the only place that behaviour is specified, so the rules
are pinned here rather than left to drift.

The voice agent reads ``bot_knowledge.md`` and ``construction_knowledge.md``
(``construction_main._voice_prompt`` concatenates both after
``VOICE_AGENT_PROMPT``). Those files are checked as one text, because that is
how the model sees them. ``construction_main`` itself is not imported: it boots
the application and the database, and this suite stays offline.

No network and no database.
"""

import re
from pathlib import Path

import pytest

from con_ai_agent import BusinessAIAgent

ROOT = Path(__file__).resolve().parent
KNOWLEDGE_FILES = [ROOT / "bot_knowledge.md", ROOT / "construction_knowledge.md"]


def flat(text: str) -> str:
    """Collapse whitespace so a rule is assertable across prompt line wraps."""
    return " ".join(text.split())


@pytest.fixture(scope="module")
def copilot() -> str:
    return flat(BusinessAIAgent(subset="copilot")._build_system_prompt())


@pytest.fixture(scope="module")
def guest() -> str:
    return flat(BusinessAIAgent(subset="guest")._build_system_prompt())


@pytest.fixture(scope="module")
def voice_knowledge() -> str:
    """Exactly the knowledge half of ``construction_main._voice_prompt()``."""
    return flat("\n\n".join(p.read_text(encoding="utf-8") for p in KNOWLEDGE_FILES))


# --- Copilot: never claim an action the tool did not confirm ----------------

def test_copilot_has_the_truthfulness_block(copilot):
    assert "TRUTHFULNESS" in copilot
    assert "override every other instruction" in copilot


def test_copilot_only_believes_ok_true(copilot):
    """The tool result is the only evidence an action happened."""
    assert "only if a tool returned ok: true" in copilot
    assert "ok: false" in copilot
    assert "means it did NOT happen" in copilot


def test_copilot_never_reports_an_unconfirmed_call_or_message(copilot):
    assert "Never say or imply that a call was placed" in copilot
    assert "unless a tool confirmed it in this turn" in copilot
    assert "is not a call" in copilot
    assert "is not a send" in copilot


def test_copilot_confirms_destructive_and_irreversible_actions(copilot):
    assert "destructive or irreversible" in copilot
    assert "wait for a clear yes" in copilot
    assert "An ambiguous reply is not consent" in copilot
    assert "Before any database CHANGE" in copilot


def test_copilot_prefers_short_answers(copilot):
    assert "STYLE — the owner is busy" in copilot
    assert "as few words as the answer allows" in copilot
    assert "No preamble" in copilot


def test_copilot_uses_lead_and_bid_data_instead_of_guessing(copilot):
    assert "Answer from the lead, bid or property record" in copilot
    assert "Never substitute an invented price" in copilot
    assert "\"I don't have that\" beats a guess" in copilot


def test_copilot_does_not_present_intent_as_finished_work(copilot):
    assert "Never present intended work as finished work" in copilot
    assert "what is pending" in copilot


def test_guest_gets_the_same_truthfulness_rules(guest):
    """The public widget promises callbacks too, so the block is not owner-only."""
    assert "TRUTHFULNESS" in guest
    assert "only if a tool returned ok: true" in guest


# --- Quoting: no invented price or date -------------------------------------

def test_quote_failure_must_not_be_papered_over(copilot):
    assert "Never substitute a number of your own" in copilot
    assert "never promise a start date or a callback time the tool did not give you" in copilot


# --- Voice: call behaviour and opt-out --------------------------------------

def test_voice_knowledge_has_a_marked_call_behaviour_section(voice_knowledge):
    assert "## 10a. PHONE CALL BEHAVIOUR" in voice_knowledge
    for path in KNOWLEDGE_FILES:
        assert "## 10a. PHONE CALL BEHAVIOUR" in path.read_text(encoding="utf-8"), (
            f"{path.name} is concatenated into the voice prompt and must carry "
            f"the call rules too"
        )


def test_voice_sounds_like_a_person(voice_knowledge):
    assert "Contractions, short sentences" in voice_knowledge
    assert "Ask one question at a time" in voice_knowledge
    assert "read out a menu of options" in voice_knowledge


def test_voice_never_answers_background_noise(voice_knowledge):
    assert "Never answer a background noise" in voice_knowledge
    assert "stay quiet and wait" in voice_knowledge
    assert "never stop mid-sentence" in voice_knowledge


def test_voice_does_not_repeat_didnt_catch_that(voice_knowledge):
    assert "Never say \"I didn't catch that\" twice" in voice_knowledge
    assert "Ask once" in voice_knowledge


def test_voice_hands_off_to_a_human(voice_knowledge):
    assert "If they ask for a human" in voice_knowledge
    assert "stop selling" in voice_knowledge
    assert "(757) 908-7121" in voice_knowledge


def test_voice_discloses_the_automated_assistant(voice_knowledge):
    assert "never claim to be human" in voice_knowledge
    assert "say honestly that you are the company's automated assistant" in voice_knowledge


def test_voice_honours_stop_immediately(voice_knowledge):
    assert "Stop means stop" in voice_knowledge
    assert "ends the call right there" in voice_knowledge
    assert "never call or text that number again" in voice_knowledge
    assert "A STOP text is honoured the same way" in voice_knowledge


def test_voice_does_not_promise_a_send_the_tool_refused(voice_knowledge):
    assert "unless the tool returned ok" in voice_knowledge
    assert "say plainly that it did not send and why" in voice_knowledge


def test_voice_does_not_guess_prices_or_dates(voice_knowledge):
    assert "Never invent a price, date, availability, permit outcome or commitment" in voice_knowledge


def test_no_prompt_permits_guessing_or_false_claims(copilot, voice_knowledge):
    """The old permissive lines must not come back in any prompt."""
    forbidden = [
        "feel free to guess",
        "if you don't know, guess",
        "make up a",
        "estimate from memory",
        "just estimate",
        "claim it was sent",
    ]
    for prompt in (copilot, voice_knowledge):
        for phrase in forbidden:
            assert phrase not in prompt, f"prompt still permits: {phrase}"


# --- Stale hosting names must not survive in any prompt ---------------------

def _voice_prompt_source() -> str:
    """The ``VOICE_AGENT_PROMPT`` literal, read without importing the app."""
    text = (ROOT / "construction_main.py").read_text(encoding="utf-8")
    start = text.index("VOICE_AGENT_PROMPT")
    end = text.index("VOICE_TOOL_URL", start)
    return text[start:end]


def test_no_prompt_names_the_old_hosts():
    sources = {
        "con_ai_agent.py": (ROOT / "con_ai_agent.py").read_text(encoding="utf-8"),
        "VOICE_AGENT_PROMPT": _voice_prompt_source(),
        "copilot prompt": BusinessAIAgent(subset="copilot")._build_system_prompt(),
        "guest prompt": BusinessAIAgent(subset="guest")._build_system_prompt(),
    }
    for path in KNOWLEDGE_FILES:
        sources[path.name] = path.read_text(encoding="utf-8")

    stale = re.compile(r"railway|render\.com|onrender\.com|render cloud", re.I)
    hits = {name: sorted(set(m.group(0).lower() for m in stale.finditer(text)))
            for name, text in sources.items()}
    hits = {name: found for name, found in hits.items() if found}
    assert not hits, f"stale hosting wording in a prompt: {hits}"
