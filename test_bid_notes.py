"""Bid notes must render as a readable card, never as raw email HTML."""
import bid_notes

SAMPLE = """
<html><head><style>.zm_1234 { color:#333; font-family:Arial } @media only screen and (max-width:600px){ .x_1 { width:100% } }</style></head>
<body><div class="zm_1234"><p>Loberg Construction sent a message about VEG.
SUB BIDS DUE: Friday 11/13 by 1:00 PM INVITATION TO BID B26-0248</p>
<p>Project Address: 551 Hilltop Plaza, Virginia Beach, VA 23454
Project Summary: Renovation of existing tenant space
Trades Involved: 024119 - Selective Demolition, 093000 - Ceramic Tile, 099100 - Painting</p>
<p>Reply to Painting Reply to Flooring</p>
<p>Bid Due: Friday 11/13</p></div></body></html>
"""


def test_strips_the_zoho_stylesheet():
    out = bid_notes.format_notes(SAMPLE)
    assert "zm_" not in out
    assert "x_1" not in out
    assert "@media" not in out
    assert "font-family" not in out


def test_escapes_user_and_email_markup():
    out = bid_notes.format_notes(
        '<script>alert(1)</script><b>Bid Due:</b> Friday '
        '<img src=x onerror="alert(2)">')
    assert "<script" not in out
    assert "onerror" not in out
    assert "<img" not in out


def test_known_labels_are_bold_and_not_walls_of_text():
    out = bid_notes.format_notes(SAMPLE)
    assert "<strong>Project Address:</strong>" in out
    assert "<strong>Project Summary:</strong>" in out
    assert "<strong>Bid Due:</strong>" in out


def test_trades_and_reply_links_become_bullets():
    out = bid_notes.format_notes(SAMPLE)
    assert "<li>093000 - Ceramic Tile</li>" in out
    assert "<li>099100 - Painting</li>" in out
    assert "<li>Reply to Painting</li>" in out


def test_quoted_older_email_is_collapsed():
    out = bid_notes.format_notes(
        "Bid Due: Friday\n--- from email ---\nEarlier Body: hello")
    assert out.count("<details") == 1
    assert "Earlier email:" in out


def test_empty_and_plain_text():
    assert bid_notes.format_notes("") == ""
    assert bid_notes.format_notes(None) == ""
    plain = bid_notes.format_notes("Just a note about the job walk.")
    assert plain.startswith("<p ")
    assert "Just a note about the job walk." in plain


def test_address_fragment_stays_with_the_address():
    out = bid_notes.format_notes(
        "Project Address: 551 Hilltop Plaza, Virginia Beach,\nVA. 23454")
    assert "Virginia Beach, VA. 23454" in out


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
