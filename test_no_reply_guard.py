"""Notification robots must not be treated as leads or answered.

The Proton daily notice arrived from no-reply@mail.proton.me, was recorded as
mail, and the lead reply bot answered it. Proton then answered the robot.
These pin the guard at every layer: the send policy, the reply builder, and the
inbound matcher.
"""
import auto_reply
import inbound_email
import mail_inbox


def test_no_reply_addresses_are_recognised():
    assert auto_reply.is_no_reply("no-reply@mail.proton.me")
    assert auto_reply.is_no_reply("noreply@notifications.example.com")
    assert auto_reply.is_no_reply("do-not-reply@example.com")
    assert not auto_reply.is_no_reply("cathi@lobergconstruction.com")
    assert not auto_reply.is_no_reply("")


def test_blocked_reason_refuses_no_reply():
    assert auto_reply.blocked_reason("no-reply@mail.proton.me") == "no-reply address"


def test_ensure_lead_reply_returns_before_sending(monkeypatch):
    monkeypatch.setattr(auto_reply, "_ensure_draft_column", lambda db: None)
    monkeypatch.setattr(auto_reply, "note_withheld", lambda db, lead_id, reason: None)
    out = auto_reply.ensure_lead_reply(
        object(), "construction", email="no-reply@mail.proton.me")
    assert out == {"sent": False, "staged": False, "msg": ""}


def test_inbound_matcher_skips_no_reply():
    assert inbound_email._NO_REPLY_RE.match("no-reply@mail.proton.me")
    assert inbound_email._NO_REPLY_RE.match("notifications@example.com")
    assert not inbound_email._NO_REPLY_RE.match("cathi@lobergconstruction.com")


def test_proton_notification_is_hidden_from_the_inbox_list():
    assert mail_inbox._PROTON_NOTIFICATION_RE.match("no-reply@mail.proton.me")
    assert not mail_inbox._PROTON_NOTIFICATION_RE.match(
        "team@buildingconnected.com")
    assert mail_inbox._NO_REPLY_RE.match("no-reply@mail.proton.me")
    assert not mail_inbox._NO_REPLY_RE.match("cathi@lobergconstruction.com")


if __name__ == "__main__":
    import inspect

    class MP:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, attr, value):
            self._undo.append((obj, attr, getattr(obj, attr)))
            setattr(obj, attr, value)

        def undo(self):
            for obj, attr, old in reversed(self._undo):
                setattr(obj, attr, old)

    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            mp = MP()
            try:
                fn(mp) if "monkeypatch" in inspect.signature(fn).parameters else fn()
            finally:
                mp.undo()
            print(f"PASS {name}")
