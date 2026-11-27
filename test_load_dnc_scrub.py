"""The DNC download loader must read registry files without suppressing leads.

The FTC download is the list of *registered* numbers; loading it wholesale would
also suppress every clean lead. These tests pin the parser and the intersection
so only a lead number that is actually on the registry is imported.
"""

from __future__ import annotations

import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scripts import load_dnc_scrub as loader  # noqa: E402


def test_numbers_from_text_handles_header_punctuation_and_duplicates():
    text = (
        "phone\n"
        "(757) 555-1234\n"
        "7575551234\n"
        "+1 757 555 9999\n"
        "12345\n"
    )
    assert loader.numbers_from_text(text) == {"7575551234", "7575559999"}


def test_numbers_from_text_finds_a_later_csv_column():
    text = "lead_id,phone\n1,(757) 555-1234\n"
    assert loader.numbers_from_text(text) == {"7575551234"}


def test_numbers_from_zip_reads_every_csv_and_text_member():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("233.csv", "phone\n7575551234\n")
        archive.writestr("234.txt", "7575559999\n")
        archive.writestr("readme.md", "not a phone list")
    assert loader.numbers_from_bytes(buffer.getvalue()) == {
        "7575551234", "7575559999"}


def test_plain_text_bytes_are_accepted():
    assert loader.numbers_from_bytes(b"7575551234\n7575551235\n") == {
        "7575551234", "7575551235"}


def test_ten_digit_ids_that_are_not_us_phones_are_rejected():
    assert loader.last10("1234567890") == ""
    assert loader.last10("0234567890") == ""
    assert loader.last10("7575551234") == "7575551234"


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return [
            {"digits": "7575551234"},
            {"digits": "17575559999"},
            {"digits": ""},
            {"digits": "12345"},
        ]


class _Connection:
    def cursor(self):
        return _Cursor()


def test_lead_numbers_normalises_and_drops_unusable_rows():
    assert loader.lead_numbers(_Connection()) == {
        "7575551234", "7575559999"}
