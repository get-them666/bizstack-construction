"""Zelle / ACH deposit instructions.

    venv/bin/python -m pytest test_deposit_instructions.py -v

The important properties: the client sees the Zelle handle first and the ACH
details second, the full account number is encrypted at rest and never
rendered by ``details()``, and an unconfigured account says so instead of
printing a blank or a guess.
"""
from cryptography.fernet import Fernet

import deposit_instructions as di


def _store(monkeypatch):
    monkeypatch.setenv("BANK_ENCRYPTION_KEY", Fernet.generate_key().decode())
    data = {}
    return data, (lambda k: data.get(k, "")), (lambda k, v: data.__setitem__(k, v))


def test_zelle_handle_accepts_a_phone_or_an_email():
    assert di.normalize_zelle("(757) 908-7121") == "7579087121"
    assert di.normalize_zelle("pay@bizstackperks.com") == "pay@bizstackperks.com"
    assert di.normalize_zelle("12345") == ""
    assert di.normalize_zelle("not an email@") == ""
    assert di.format_phone("7579087121") == "(757) 908-7121"


def test_save_encrypts_the_account_and_shows_only_the_last_four(monkeypatch):
    data, get, put = _store(monkeypatch)
    di.save(put, "7579087121", "051000017", "000123456789")
    raw = data[di.KEY_ACCOUNT_ENC]
    assert "000123456789" not in raw, "the account number must not sit in app_settings in the clear"
    d = di.details(get)
    assert d["account_last4"] == "6789" and d["has_account"] is True
    assert d["ach_ready"] is True and d["ready"] is True
    assert d["zelle_display"] == "(757) 908-7121"
    assert di.ach_details(get)["account"] == "000123456789"


def test_nothing_configured_reads_as_not_ready():
    d = di.details(lambda k: "")
    assert d["ready"] is False and d["zelle"] == "" and d["account_last4"] == ""
    lines = di.payment_lines("BuildStack Construction LLC", lambda k: "",
                             phone="757-908-7121", reference="invoice 1042")
    assert len(lines) == 1 and "invoice 1042" in lines[0]
    assert "routing" not in lines[0].lower()


def test_payment_lines_put_zelle_first_then_ach(monkeypatch):
    _, get, put = _store(monkeypatch)
    di.save(put, "7579087121", "051000017", "000123456789")
    lines = di.payment_lines("BuildStack Construction LLC", get, reference="invoice 1042")
    assert lines[0].startswith("Zelle")
    assert "051000017" in lines[1] and "000123456789" in lines[1]
    assert "BuildStack Construction LLC" in lines[1]
    assert all("invoice 1042" in line for line in lines)


def test_link_round_trips_and_rejects_other_token_types():
    token = di.link(42)
    assert token.startswith("/pay/deposit/")
    assert di.lead_from_token(token.rsplit("/", 1)[-1]) == 42
    assert di.lead_from_token("garbage") is None
    assert di.reference(42) == "DEP-00042"


def test_an_expired_link_does_not_resolve():
    token = di.link(7, days=-1)
    assert di.lead_from_token(token.rsplit("/", 1)[-1]) is None
