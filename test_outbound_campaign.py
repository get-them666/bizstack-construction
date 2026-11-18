"""The outbound campaign's safety helpers.

    venv/bin/python -m pytest test_outbound_campaign.py -q
"""

from datetime import datetime
from zoneinfo import ZoneInfo


def test_stop_detection_catches_real_opt_outs():
    import outbound_campaign as oc

    for said in ("stop calling me", "please do not call again", "take me off your list",
                 "remove me", "stop", "Stop.", "unsubscribe"):
        assert oc.wants_stop(said) is True, said


def test_stop_detection_ignores_ordinary_sentences():
    import outbound_campaign as oc

    for said in ("stop by Tuesday", "I'll stop at the store", "sounds good, thanks",
                 "can you stop by and look at the roof"):
        assert oc.wants_stop(said) is False, said


def test_phone_normalisation_and_placeholder_screen():
    import outbound_campaign as oc

    assert oc.e164("757-908-7121") == "+17579087121"
    assert oc.e164("+1 (757) 908-7121") == "+17579087121"
    assert oc.e164("12345") == ""
    assert oc._plausible("+17579087121") is True
    assert oc._plausible("+10000000000") is False
    assert oc._plausible("+15551234567") is False


def test_calling_window_is_weekday_business_hours_eastern():
    import outbound_campaign as oc

    tz = ZoneInfo("America/New_York")
    monday_10 = datetime(2026, 11, 16, 10, 0, tzinfo=tz)
    monday_7 = datetime(2026, 11, 16, 7, 0, tzinfo=tz)
    saturday_15 = datetime(2026, 11, 21, 15, 0, tzinfo=tz)
    sunday_12 = datetime(2026, 11, 22, 12, 0, tzinfo=tz)

    assert oc.in_call_window(monday_10)[0] is True
    assert oc.in_call_window(monday_7)[0] is False
    assert oc.in_call_window(saturday_15)[0] is True
    assert oc.in_call_window(sunday_12)[0] is False


def test_dry_run_never_dials(monkeypatch):
    import outbound_campaign as oc

    monkeypatch.setattr(oc, "candidates", lambda *a, **k: [
        {"id": 1, "name": "A", "phone": "757-908-7121", "source": "quotes_page",
         "address": "Norfolk, VA 23510"},
    ])
    monkeypatch.setattr(oc.suppression, "check_outbound", lambda *a, **k: (True, ""))
    monkeypatch.setattr(oc, "_dial", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("dry run must not dial")))

    result = oc.run(None, limit=5, apply=False)
    assert result["ok"] is True
    assert result["dialed"] == []
    assert len(result["would_call"]) == 1
