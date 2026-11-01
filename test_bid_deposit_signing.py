"""Deposit requests, e-signature, and the bid-to-project link.

Pure tests: no database, no network, no SMTP. The parts that need a real
Postgres are exercised against production separately; what is pinned here is the
arithmetic, the validation, and the one SQL shape that had already gone wrong.
"""

from __future__ import annotations

import base64
import io
from datetime import date, timedelta

import pytest
from PIL import Image, ImageDraw

import bid_costing
import bid_deposit
import bid_signing


# --- deposit arithmetic ------------------------------------------------------
def test_deposit_is_thirty_percent_to_the_cent():
    bid = {"bid_amount": 8255.99}
    assert bid_deposit.deposit_amount(bid) == 2476.80


def test_deposit_and_balance_add_back_to_the_bid():
    """A cent lost in rounding becomes a reconciliation problem on the final
    invoice, so the two halves must reconstruct the whole exactly."""
    for total in (8255.99, 1000.01, 3.33, 99999.99, 0.05):
        bid = {"bid_amount": total}
        parts = bid_deposit.deposit_amount(bid) + bid_deposit.balance_after_deposit(bid)
        assert round(parts, 2) == round(total, 2)


def test_deposit_is_zero_without_a_price():
    """Never invent a deposit for an unpriced bid."""
    for bid in ({}, {"bid_amount": None}, {"bid_amount": ""},
                {"bid_amount": 0}, {"bid_amount": "not a number"}):
        assert bid_deposit.deposit_amount(bid) == 0.0
        assert bid_deposit.balance_after_deposit(bid) == 0.0


def test_deposit_due_date_is_net_fifteen():
    assert bid_deposit.deposit_due_date(date(2026, 10, 31)) == date(2026, 11, 15)
    assert (bid_deposit.deposit_due_date() - date.today()).days == 15


# --- documents ---------------------------------------------------------------
BID = {
    "id": 18350,
    "name": "Hallway, Runner and Bathroom Renovation",
    "location": "520 King George Rd, Virginia Beach, VA 23462",
    "bid_amount": 8255.99,
    "contact_name": "Turnkey Asset Management",
    "contact_email": "contractors@turnkeyassetmgt.com",
    "contact_phone": "954-228-0518",
}
INVOICE = {"invoice_number": "INV-2026-0007", "amount": 2476.80,
           "issued_date": "2026-10-31", "due_date": "2026-11-15"}


def test_deposit_pdf_is_a_pdf():
    pdf = bid_deposit.build_deposit_pdf(BID, INVOICE)
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000


def test_deposit_pdf_survives_a_bid_with_no_contact():
    """A bid with no contact must still render, not 500 the route."""
    pdf = bid_deposit.build_deposit_pdf(
        {"id": 1, "name": "Job", "bid_amount": 1000}, {})
    assert pdf.startswith(b"%PDF")


def test_deposit_email_names_the_amount_and_the_invoice():
    subject, body = bid_deposit.build_deposit_email(BID, INVOICE)
    assert "INV-2026-0007" in subject
    assert "2,476.80" in body
    assert "520 King George Rd" in body


def test_deposit_email_is_plain_ascii():
    """The transport is not guaranteed to carry anything else."""
    subject, body = bid_deposit.build_deposit_email(BID, INVOICE)
    (subject + body).encode("ascii")


# --- signature ---------------------------------------------------------------
def _stroke_png(padding: int = 40) -> bytes:
    """A transparent canvas with one diagonal stroke, like the signature pad."""
    im = Image.new("RGBA", (300 + padding * 2, 120 + padding * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    draw.line([(padding, padding + 90), (padding + 300, padding + 10)],
              fill=(15, 23, 42, 255), width=4)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def test_signature_accepts_a_canvas_data_url():
    assert bid_signing._decode_png(_data_url(_stroke_png())) == _stroke_png()


def test_signature_accepts_bare_base64():
    bare = base64.b64encode(_stroke_png()).decode("ascii")
    assert bid_signing._decode_png(bare) == _stroke_png()


def test_signature_trim_crops_the_empty_canvas():
    """A canvas is mostly empty. Stamped untrimmed, the signature floats and
    scales down by the margin instead of sitting on the line."""
    trimmed = bid_signing._trim(_stroke_png(padding=40))
    im = Image.open(io.BytesIO(trimmed))
    assert im.width < 300 + 80
    assert im.height < 120 + 80
    assert im.mode == "RGBA"


def test_blank_signature_is_rejected():
    blank = Image.new("RGBA", (300, 120), (0, 0, 0, 0))
    out = io.BytesIO()
    blank.save(out, "PNG")
    with pytest.raises(ValueError, match="blank"):
        bid_signing._trim(out.getvalue())


def test_a_single_tap_is_not_a_signature():
    tiny = Image.new("RGBA", (300, 120), (0, 0, 0, 0))
    ImageDraw.Draw(tiny).point((10, 10), fill=(0, 0, 0, 255))
    out = io.BytesIO()
    tiny.save(out, "PNG")
    with pytest.raises(ValueError, match="too small"):
        bid_signing._trim(out.getvalue())


def test_unreadable_signature_is_rejected_with_a_usable_message():
    with pytest.raises(ValueError):
        bid_signing._trim(b"this is not a png")


def test_signature_requires_a_printed_name():
    """The image alone is not a signature; the name beside it is what makes it
    attributable."""
    with pytest.raises(ValueError, match="printed name"):
        bid_signing.save(None, 1, signature_data=_data_url(_stroke_png()),
                         signer_name="   ")


def test_describe_reports_unsigned_when_there_is_no_row():
    assert bid_signing.describe(None) == {"signed": False}


def test_describe_formats_the_audit_line():
    import datetime as dt
    row = {"id": 3, "signer_name": "Shaun David OLeary",
           "signer_email": "soleary0006@gmail.com",
           "signed_at": dt.datetime(2026, 10, 31, 23, 45),
           "method": "drawn", "document_sha256": "a" * 64}
    out = bid_signing.describe(row)
    assert out["signed"] is True
    assert out["signed_at"].startswith("2026-10-31")
    assert len(out["document_sha256"]) == 16


# --- bid -> project ----------------------------------------------------------
class _FakeCursor:
    def __init__(self, script):
        self.script = list(script)
        self.executed = []
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((" ".join(str(sql).split()), params))
        self._last = self.script.pop(0) if self.script else None

    def fetchone(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, script):
        self.cur = _FakeCursor(script)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def _project_script(*, existing=None, bid=None):
    return [
        {"t": "projects"},                      # _table_exists
        bid or {"id": 7, "name": "Job", "location": "1 Test Rd",
                "bid_amount": 1000, "trade": "carpentry", "start_date": None},
        existing,                               # projects WHERE bid_id
        {"column_name": "company"},             # company column probe
        {"id": 42},                             # INSERT RETURNING id
    ]


def test_convert_writes_bid_id_so_hours_land_on_the_bid(monkeypatch):
    """The original bug: the project was inserted without bid_id, so
    open_job_for_bid and labor_actual (both keyed on bid_id) never found it and
    the bid reported zero labour cost -- a 100% margin."""
    monkeypatch.setattr(bid_costing, "ensure_accounting_schema", lambda conn: None)
    conn = _FakeConn(_project_script())
    assert bid_costing.convert_bid_to_project(conn, 7) == 42

    insert_sql, insert_params = [e for e in conn.cur.executed if "INSERT INTO projects" in e[0]][0]
    assert "bid_id" in insert_sql
    assert insert_params[0] == 7


def test_convert_is_idempotent_on_bid_id(monkeypatch):
    """Pressing it twice must not create a second project and split the hours."""
    monkeypatch.setattr(bid_costing, "ensure_accounting_schema", lambda conn: None)
    conn = _FakeConn(_project_script(existing={"id": 9}))
    assert bid_costing.convert_bid_to_project(conn, 7) == 9
    assert not any("INSERT INTO projects" in sql for sql, _ in conn.cur.executed)


def test_convert_rejects_a_missing_bid(monkeypatch):
    monkeypatch.setattr(bid_costing, "ensure_accounting_schema", lambda conn: None)
    conn = _FakeConn([{"t": "projects"}, None])
    with pytest.raises(ValueError):
        bid_costing.convert_bid_to_project(conn, 999)


# --- invoice kind ------------------------------------------------------------
def test_deposit_invoice_is_created_with_kind_deposit(monkeypatch):
    """The card uses kind='deposit' to decide whether it has already asked.
    Without it, a second press raises a second deposit."""
    captured = {}

    def fake_create(conn, bid_id, amount, company="construction", description="",
                    issued_date="", due_date="", invoice_number="", status="draft",
                    kind="invoice"):
        captured.update(bid_id=bid_id, amount=amount, kind=kind, due_date=due_date)
        return {"id": 1, "invoice_number": "INV-1"}

    monkeypatch.setattr(bid_costing, "create_invoice", fake_create)
    monkeypatch.setattr(bid_costing, "_table_exists", lambda conn, name: True)
    monkeypatch.setattr(bid_deposit, "find_deposit_invoice", lambda *a, **k: None)

    bid_deposit.ensure_deposit_invoice(None, {"id": 7, "bid_amount": 1000})
    assert captured["kind"] == "deposit"
    assert captured["amount"] == 300.0
    assert captured["due_date"] == bid_deposit.deposit_due_date().isoformat()


def test_deposit_invoice_refuses_an_unpriced_bid(monkeypatch):
    monkeypatch.setattr(bid_costing, "_table_exists", lambda conn, name: True)
    monkeypatch.setattr(bid_deposit, "find_deposit_invoice", lambda *a, **k: None)
    with pytest.raises(ValueError, match="no price"):
        bid_deposit.ensure_deposit_invoice(None, {"id": 7, "bid_amount": 0})


def test_a_sent_deposit_is_not_re_priced(monkeypatch):
    """The client agreed to a figure, not to a formula. Re-pricing a bid after
    the request went out must not change what they were asked for."""
    sent = {"id": 5, "status": "sent", "amount": 300.0}
    monkeypatch.setattr(bid_costing, "_table_exists", lambda conn, name: True)
    monkeypatch.setattr(bid_deposit, "find_deposit_invoice", lambda *a, **k: sent)
    monkeypatch.setattr(bid_costing, "create_invoice",
                        lambda *a, **k: pytest.fail("must not create a second invoice"))

    out = bid_deposit.ensure_deposit_invoice(None, {"id": 7, "bid_amount": 5000})
    assert out["amount"] == 300.0
