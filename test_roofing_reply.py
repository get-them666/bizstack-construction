"""A roofing inquiry gets a roofing first touch, not the generic one.

    venv/bin/python -m pytest test_roofing_reply.py -v

The inbound email that prompted this asked for an inspection and a reroof
quotation and gave nothing else -- no name, no address, no photos. The generic
first touch answered politely and asked for nothing, so the only way to price it
was a second round of email. The roofing branch asks for the address and a
couple of photos in the first message, which is what actually turns the inquiry
into a scheduled inspection.

Two things it must NOT do:

  1. quote a number. A re-roof varies more than any other line we sell (pitch,
     layers, decking, penetrations), and `_auto_quote` only has a usable range
     when square footage is known. A blind range invites the owner to argue
     against a figure nobody measured.
  2. hijack the permit-finder copy. Those leads are already in a different
     conversation (we contacted them, not the other way round) and
     test_greeting pins that wording.

Offline: pure function, no db and no transport.
"""
import auto_reply


def _msg(**kw):
    kw.setdefault("name", "")
    kw.setdefault("source", "website")
    return auto_reply._build_message("construction", auto_reply.brand.short_name(), **kw)


def test_a_roofing_inquiry_is_recognised():
    for service in ("Roofing", "roof replacement", "REROOF", "roof inspection"):
        body = _msg(service=service)
        assert "roof inspections and full reroofs" in body, service


def test_it_asks_for_the_address_and_photos():
    body = _msg(service="roofing")
    assert "property address" in body
    assert "photos" in body
    assert "inspection" in body


def test_it_does_not_quote_blind():
    """No dollar figure unless the estimate service supplied one from a real
    square footage; a bare 'roofing' inquiry has neither."""
    body = _msg(service="roofing")
    assert "$" not in body, body


def test_it_keeps_the_business_line_in_the_footer():
    body = _msg(service="roofing")
    assert "(757) 908-7121" in body
    assert "252-665-5891" not in body, "the personal mobile is lending material only"


def test_a_known_address_is_quoted_back():
    body = _msg(service="roofing", address="1501 Vance Cir")
    assert "1501 Vance Cir" in body


def test_permit_finder_copy_is_untouched():
    body = _msg(service="Roof Repair", source="permit_finder", address="1501 VANCE CIR")
    assert "recent permit" in body
    assert "roof inspections and full reroofs" not in body, \
        "permit leads are our outreach, not their inquiry"


def test_other_trades_still_get_the_generic_reply():
    body = _msg(service="kitchen remodel")
    assert "roof" not in body.lower()
    assert "project estimator" in body
