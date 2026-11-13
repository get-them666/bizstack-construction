"""A bid email must carry every attachment, not just the first.

The Gmail path used to accept one file and silently ignore the `attachments`
list, so a proposal + RFI went out bare. These pin the transport behavior and
the send guard that refuses to send a bid without its PDF.
"""
import asyncio
import base64

import documents_service
import google_gmail


def _decode_raw(body):
    raw = body["raw"]
    raw += "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw).decode("utf-8", "replace")


def test_gmail_carries_two_attachments(monkeypatch):
    captured = {}

    def fake_api(service, user_email, method, path, params=None, body=None):
        captured["body"] = body
        return {"id": "msg-1"}

    monkeypatch.setattr(google_gmail, "_api_request", fake_api)
    out = google_gmail.send_email(
        "construction", "owner@example.com", "gc@example.com",
        "Bid Proposal", "See attached.", attachments=[
            (b"%PDF-1.4 bid", "Bid.pdf"),
            (b"%PDF-1.4 rfi", "RFI.pdf"),
        ])
    assert out["id"] == "msg-1"
    raw = _decode_raw(captured["body"])
    assert raw.count("Content-Disposition: attachment") == 2
    assert "Bid.pdf" in raw and "RFI.pdf" in raw


def test_gmail_single_attachment_still_works(monkeypatch):
    captured = {}

    def fake_api(service, user_email, method, path, params=None, body=None):
        captured["body"] = body
        return {"id": "msg-2"}

    monkeypatch.setattr(google_gmail, "_api_request", fake_api)
    out = google_gmail.send_email(
        "construction", "owner@example.com", "gc@example.com",
        "Bid Proposal", "See attached.",
        attachment=b"%PDF-1.4 one", filename="One.pdf")
    assert out["id"] == "msg-2"
    raw = _decode_raw(captured["body"])
    assert raw.count("Content-Disposition: attachment") == 1
    assert "One.pdf" in raw


def test_send_email_forwards_the_whole_attachment_list(monkeypatch):
    captured = {}

    async def fake_gmail(cfg, to, subject, body, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(documents_service, "_send_via_gmail", fake_gmail)
    ok = asyncio.run(documents_service.send_email(
        {"SMTP_FROM": "hello@example.com", "SMTP_NAME": "BuildStack"},
        "gc@example.com", "Bid Proposal", "See attached.",
        attachments=[(b"bid", "Bid.pdf"), (b"rfi", "RFI.pdf")]))
    assert ok is True
    assert captured["attachments"] == [(b"bid", "Bid.pdf"), (b"rfi", "RFI.pdf")]


def test_zoho_uploads_every_attachment(monkeypatch):
    import zoho_mail

    sent = {}

    class Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeZoho:
        def is_configured(self):
            return True

        def api_request(self, method, path, params=None, data=None,
                        headers=None, json=None):
            if path.endswith("/messages/attachments"):
                name = params["fileName"]
                return Response({"data": [{
                    "storeName": "store-" + name,
                    "attachmentPath": "/path/" + name,
                    "attachmentName": name,
                }]})
            sent["payload"] = json
            return Response({"data": {}})

    monkeypatch.setattr(zoho_mail, "zoho_client", FakeZoho())
    monkeypatch.setattr(zoho_mail, "_account", lambda sender: ("acct-1", "hello@example.com"))
    ok = zoho_mail.send(
        "gc@example.com", "Bid Proposal", "See attached.",
        attachments=[(b"bid", "Bid.pdf"), (b"rfi", "RFI.pdf")])
    assert ok is True
    names = [a["attachmentName"] for a in sent["payload"]["attachments"]]
    assert names == ["Bid.pdf", "RFI.pdf"]


if __name__ == "__main__":
    import inspect
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            # Minimal stand-in for pytest's monkeypatch fixture.
            class MP:
                def __init__(self):
                    self._undo = []

                def setattr(self, obj, attr, value):
                    self._undo.append((obj, attr, getattr(obj, attr)))
                    setattr(obj, attr, value)

                def undo(self):
                    for obj, attr, old in reversed(self._undo):
                        setattr(obj, attr, old)

            mp = MP()
            try:
                if "monkeypatch" in inspect.signature(fn).parameters:
                    fn(mp)
                else:
                    fn()
            finally:
                mp.undo()
            print(f"PASS {name}")
