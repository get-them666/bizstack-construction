"""Permit imports must not receive an assumed whole-home ballpark.

The six permit rows that polluted the Quoted lane all held the same
$166,250–$306,250 whole-home default for an assumed 1,750 sq ft home, regardless
of whether the permit was electrical, mechanical, or an alteration. The guard
lives in the shared auto-reply stamp path so every caller is covered.
"""

import auto_reply


EST = {
    "model_key": "whole-home",
    "label": "Whole-Home Renovation",
    "low_cents": 16625000,
    "high_cents": 30625000,
    "low_est": 166250,
    "high_est": 306250,
    "assumed_sqft": True,
    "sqft": 1750,
}


class _FakeCursor:
    def __init__(self, source):
        self.source = source
        self.last_sql = ""
        self.last_params = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params

    def fetchone(self):
        return {"source": self.source}


class _FakeDB:
    def __init__(self, source):
        self.cursor_obj = _FakeCursor(source)
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_assumed_whole_home_is_blocked_only_for_permit_imports():
    assert auto_reply._is_permit_assumed_whole_home("permit_finder", EST) is True
    assert auto_reply._is_permit_assumed_whole_home("permit_finder", {**EST, "assumed_sqft": False}) is False
    assert auto_reply._is_permit_assumed_whole_home("permit_finder", {**EST, "model_key": "kitchen"}) is False
    assert auto_reply._is_permit_assumed_whole_home("quotes_page", EST) is False
    assert auto_reply._is_permit_assumed_whole_home("permit_finder", None) is False


def test_stamp_path_skips_permit_default_even_when_caller_says_inbound_email():
    db = _FakeDB("permit_finder")

    stamped = auto_reply._stamp_estimate_on_lead(db, 619, EST, "inbound-email")

    assert stamped is False
    assert db.committed is False
    assert "UPDATE leads SET estimate_low_cents" not in db.cursor_obj.last_sql


def test_stamp_path_allows_explicit_square_footage_on_a_permit():
    db = _FakeDB("permit_finder")

    stamped = auto_reply._stamp_estimate_on_lead(
        db, 619, {**EST, "assumed_sqft": False, "sqft": 2400}, "permit_finder"
    )

    assert stamped is True
    assert db.committed is True
    assert "UPDATE leads SET estimate_low_cents" in db.cursor_obj.last_sql


def test_stamp_path_still_writes_for_a_website_quote():
    db = _FakeDB("quotes_page")

    stamped = auto_reply._stamp_estimate_on_lead(db, 2348, EST, "quotes_page")

    assert stamped is True
    assert db.committed is True
    assert "UPDATE leads SET estimate_low_cents" in db.cursor_obj.last_sql


def test_auto_reply_uses_the_guard_instead_of_a_direct_update():
    import inspect

    source = inspect.getsource(auto_reply.ensure_lead_reply)
    assert "_stamp_estimate_on_lead(db, lead_id, est, source)" in source
    assert "UPDATE leads SET estimate_low_cents" not in source
