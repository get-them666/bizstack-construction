"""The suppression gate must fail CLOSED, including on the two customer paths.

Background: ``suppression.find()`` returned ``[]`` when the list could not be
read. That is the same value it returns for a clean scrub, so a database error
looked exactly like "not suppressed" and the message went out to someone who
had filed DNC or unsubscribed. These tests pin three things:

* an unreadable list is a *denial*, not an empty result;
* a listed address is denied on the first-touch and owner-send paths;
* a clean address still proceeds -- the fix must not block legitimate outreach.

They use a fake db and fake transports. No network, no PostgreSQL.
"""

import pytest

import auto_reply
import suppression as S


# --------------------------------------------------------------------- fakes

LEAD = {
    "id": 7,
    "email": "homeowner@example.com",
    "phone": "",
    "notes": "",
    "draft_reply": (
        "[BOT DRAFT REPLY – email, not sent] To: homeowner@example.com\n"
        "Hi there,\nThanks for reaching out."
    ),
    "status": "new",
    "company": "construction",
    "opted_out_at": None,
}

LISTED_EMAIL = {
    "id": 1,
    "kind": "dma",
    "value_type": "email",
    "value": "homeowner@example.com",
    "channel": "email",
    "reason": "DMAchoice",
    "source": "test",
    "created_at": None,
}


class _Cursor:
    def __init__(self, db):
        self.db = db
        self._rows = []

    def execute(self, sql, params=None):
        q = " ".join(str(sql).split())
        self.db.queries.append(q)
        if self.db.fail_suppression and "suppression_list" in q:
            raise RuntimeError("suppression db is down")
        if self.db.fail_bookkeeping and (
            "INSERT INTO comms_logs" in q or "UPDATE leads SET notes" in q
        ):
            raise RuntimeError("bookkeeping db is down")
        if "FROM suppression_list" in q:
            self._rows = list(self.db.suppression_rows)
        elif q.startswith("SELECT * FROM leads WHERE id"):
            self._rows = [self.db.lead] if self.db.lead else []
        elif "SELECT notes" in q:
            self._rows = [self.db.lead or {}]
        elif "FROM comms_logs" in q and "COUNT(*)" in q:
            self._rows = [{"n": 0, "c": 0, "last_at": None}]
        elif "FROM comms_logs" in q:
            self._rows = []           # no recent touch
        elif "FROM leads" in q:
            self._rows = [self.db.lead] if self.db.lead else []
        else:
            self._rows = []           # DDL / UPDATE / INSERT
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDb:
    def __init__(self, *, suppression_rows=(), fail_suppression=False,
                 fail_bookkeeping=False, lead=None):
        self.suppression_rows = list(suppression_rows)
        self.fail_suppression = fail_suppression
        self.fail_bookkeeping = fail_bookkeeping
        self.lead = dict(lead) if lead else None
        self.queries = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture()
def transport(monkeypatch):
    """Capture every email the send paths try to transmit."""
    calls = []

    async def fake_send(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(auto_reply.documents_service, "smtp_config_from_env", lambda: {})
    monkeypatch.setattr(auto_reply.documents_service, "smtp_configured", lambda cfg: True)
    monkeypatch.setattr(auto_reply.documents_service, "send_email", fake_send)
    return calls


@pytest.fixture()
def bookkeeping(monkeypatch):
    """Keep _fire_sent_effect's queue recompute out of these fakes."""
    monkeypatch.setattr(auto_reply.lead_store, "recompute", lambda db, lead_id: {})


# -------------------------------------------------------------- read failure

def test_find_raises_when_the_list_cannot_be_read():
    """``[]`` means clean. A failure must be a different value, so raise."""
    db = FakeDb(fail_suppression=True)
    with pytest.raises(S.SuppressionUnavailable):
        S.find(db, email="homeowner@example.com")


def test_is_suppressed_denies_when_the_list_cannot_be_read():
    db = FakeDb(fail_suppression=True)
    blocked, why = S.is_suppressed(db, email="homeowner@example.com", channel="email")
    assert blocked is True
    assert "unavailable" in why.lower()


def test_check_outbound_denies_when_the_list_cannot_be_read():
    db = FakeDb(fail_suppression=True)
    allowed, why = S.check_outbound(db, "email", "homeowner@example.com")
    assert allowed is False
    assert "suppressed" in why.lower()


# -------------------------------------------------------- listed vs. clean

def test_check_outbound_denies_a_listed_address():
    db = FakeDb(suppression_rows=[LISTED_EMAIL])
    allowed, why = S.check_outbound(db, "email", "homeowner@example.com")
    assert allowed is False
    assert "suppressed" in why.lower()


def test_check_outbound_allows_a_clean_address():
    db = FakeDb(suppression_rows=[])
    allowed, why = S.check_outbound(db, "email", "homeowner@example.com",
                                    suppression_only=True)
    assert allowed is True
    assert why == ""


def test_a_phone_scoped_entry_blocks_sms():
    """auto_reply logs SMS as ``text``; the gate must translate it. Passing
    ``text`` straight through would miss a DNC entry filed against ``sms``."""
    phone = {"id": 2, "kind": "dnc", "value_type": "phone",
             "value": "+17579087121", "channel": "phone",
             "reason": "registry", "source": "test", "created_at": None}
    db = FakeDb(suppression_rows=[phone])
    allowed, why = auto_reply._suppression_gate(db, "text", "+17579087121", 7)
    assert allowed is False
    assert "suppressed" in why.lower()


# ------------------------------------------------- first-touch send path

def test_ensure_lead_reply_denies_when_suppression_is_unreadable(monkeypatch, transport):
    monkeypatch.setenv("LEAD_AUTO_SEND", "1")
    db = FakeDb(fail_suppression=True, lead=LEAD)

    result = auto_reply.ensure_lead_reply(
        db, "construction", name="Home Owner", email=LEAD["email"],
        service="", source="web", lead_id=LEAD["id"],
    )

    assert transport == [], "an unreadable suppression list must not transmit"
    assert result["sent"] is False


def test_ensure_lead_reply_denies_a_listed_address(monkeypatch, transport):
    monkeypatch.setenv("LEAD_AUTO_SEND", "1")
    db = FakeDb(suppression_rows=[LISTED_EMAIL], lead=LEAD)

    result = auto_reply.ensure_lead_reply(
        db, "construction", name="Home Owner", email=LEAD["email"],
        service="", source="web", lead_id=LEAD["id"],
    )

    assert transport == []
    assert result["sent"] is False


def test_ensure_lead_reply_sends_a_clean_address(monkeypatch, transport, bookkeeping):
    monkeypatch.setenv("LEAD_AUTO_SEND", "1")
    monkeypatch.setattr(auto_reply, "_fire_sent_effect", lambda *a, **kw: True)
    db = FakeDb(suppression_rows=[], lead=LEAD)

    result = auto_reply.ensure_lead_reply(
        db, "construction", name="Home Owner", email=LEAD["email"],
        service="", source="web", lead_id=LEAD["id"],
    )

    assert len(transport) == 1, "a clean address must still be contacted"
    assert result["sent"] is True


# ------------------------------------------------- owner-send (fire) path

def test_fire_lead_draft_denies_when_suppression_is_unreadable(transport, bookkeeping):
    db = FakeDb(fail_suppression=True, lead=LEAD)

    result = auto_reply.fire_lead_draft(db, "construction", LEAD["id"], force=True)

    assert transport == [], "force must not override an unreadable suppression list"
    assert result["sent"] is False
    assert "suppressed" in result["why"].lower() or "suppression" in result["why"].lower()


def test_fire_lead_draft_denies_a_listed_address(transport, bookkeeping):
    db = FakeDb(suppression_rows=[LISTED_EMAIL], lead=LEAD)

    result = auto_reply.fire_lead_draft(db, "construction", LEAD["id"], force=True)

    assert transport == [], "force must not override a DNC/DMA/STOP entry"
    assert result["sent"] is False
    assert "suppressed" in result["why"].lower()


def test_fire_lead_draft_sends_a_clean_address(transport, bookkeeping):
    db = FakeDb(suppression_rows=[], lead=LEAD)

    result = auto_reply.fire_lead_draft(db, "construction", LEAD["id"], force=True)

    assert len(transport) == 1
    assert result["sent"] is True


def test_blocked_transport_still_reports_not_sent(monkeypatch, bookkeeping):
    """A delivered send is True; a refused transport must stay False."""
    calls = []

    async def refused(*args, **kwargs):
        calls.append(args)
        return False

    monkeypatch.setattr(auto_reply.documents_service, "smtp_config_from_env", lambda: {})
    monkeypatch.setattr(auto_reply.documents_service, "smtp_configured", lambda cfg: True)
    monkeypatch.setattr(auto_reply.documents_service, "send_email", refused)
    db = FakeDb(suppression_rows=[], lead=LEAD)

    result = auto_reply.fire_lead_draft(db, "construction", LEAD["id"], force=True)

    assert len(calls) == 1
    assert result["sent"] is False


def test_delivered_send_survives_a_bookkeeping_failure(monkeypatch):
    """_fire_sent_effect returns True after the transport delivered even if its
    own write failed. The caller must report sent=True; the failure must not be
    misread as "not sent" and retried."""
    async def delivered(*args, **kwargs):
        return True

    monkeypatch.setattr(auto_reply.documents_service, "smtp_config_from_env", lambda: {})
    monkeypatch.setattr(auto_reply.documents_service, "smtp_configured", lambda cfg: True)
    monkeypatch.setattr(auto_reply.documents_service, "send_email", delivered)
    monkeypatch.setattr(auto_reply.lead_store, "recompute", lambda db, lead_id: {})
    db = FakeDb(suppression_rows=[], lead=LEAD, fail_bookkeeping=True)

    result = auto_reply.fire_lead_draft(db, "construction", LEAD["id"], force=True)

    assert result["sent"] is True


# ------------------------------------------------------- wiring regression

def test_both_outbound_paths_call_the_suppression_gate():
    """A comment is not a call. The two paths that previously checked only
    contact_policy_allows must consult suppression for both channels."""
    import inspect

    ensure = inspect.getsource(auto_reply.ensure_lead_reply)
    assert '_suppression_gate(db, "email", email, lead_id)' in ensure
    assert '_suppression_gate(db, "text", to, lead_id)' in ensure

    fire = inspect.getsource(auto_reply.fire_lead_draft)
    assert "_suppression_gate(db, channel, recipient, lead_id)" in fire
