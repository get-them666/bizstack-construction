"""Client-facing bid PDFs must not print the imported solicitation email.

The notes field holds the Zoho wrapper CSS and the bid-board digest. Printing it
verbatim put ``div.zm_... { background-color: ... }`` into the signed package.
The card still shows it; the PDF does not.
"""
import io

import pypdf

import bid_package_pdf
import build_bid_documents

NOTES = (
    "div.zm_564724795402382273_parse_8770124579341018268 "
    "{ background-color: rgb(236, 234, 234); margin: 0; padding: 0 }\n"
    "11/9/2026 Bid Notification - 3 matches BidPrime\n"
    "--- from email no-reply@bidprime.com ---\n"
    "This is an email sent because you signed up for service from BidPrime."
)

BID = {
    "id": 999,
    "name": "Test Job",
    "location": "1 Main St, Norfolk, VA",
    "bid_amount": 1000,
    "notes": NOTES,
}
COSTS = [{"id": 1, "category": "labor", "description": "Work", "estimated": 1000,
          "qty": 10, "unit": "hr", "unit_cost": 100}]


def _text(raw):
    return "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(raw)).pages)


def test_package_pdf_does_not_print_solicitation_notes():
    text = _text(bid_package_pdf.generate_bid_package_pdf(BID, COSTS, 0))
    assert "zm_" not in text
    assert "background-color" not in text
    assert "BidPrime" not in text


def test_itemized_pdf_does_not_print_solicitation_notes():
    text = _text(build_bid_documents.build_itemized_pricing(BID, COSTS))
    assert "zm_" not in text
    assert "background-color" not in text
    assert "BidPrime" not in text


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
