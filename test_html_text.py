"""HTML mail bodies must arrive as prose, not as a stylesheet.

The owner inbox used to print every message's `<style>` rules above the text,
because the cleaner stripped tags only. These pin the fix on all three paths
that flatten a body (Zoho Mail API, inbound ingestion, IMAP fallback).

Run: venv/bin/python -m pytest test_html_text.py -v
"""

import email.message

import html_text
import inbound_email
import zoho_mail

MARKETING = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Quick update</title>
<style type="text/css">
@media only screen and (max-width:600px){.container{width:100%!important;}}
body{margin:0;padding:0;background:#f4f4f4;}
.externalclass{font-family:Arial,sans-serif;color:#333333;}
</style></head>
<body class="externalclass">
<div>Hi Shaun,<br><br>Can you send the SOW for the Kempsville job?&nbsp;Thanks.</div>
<div>Barbara</div>
<!--[if mso]><style>td{mso-line-height-rule:exactly;}</style><![endif]-->
</body></html>"""


def _html_msg(body):
    msg = email.message.EmailMessage()
    msg.set_content(body, subtype="html")
    return msg


def test_stylesheet_and_title_never_reach_the_reader():
    text = html_text.to_text(MARKETING)
    for leak in ("@media", "margin:0", "font-family", "externalclass", "Quick update"):
        assert leak not in text, f"{leak!r} leaked out of <style>/<title>"
    assert text.startswith("Hi Shaun,")


def test_paragraphs_and_line_breaks_survive():
    text = html_text.to_text(MARKETING)
    assert "Hi Shaun,\n\nCan you send the SOW for the Kempsville job? Thanks.\n\nBarbara" in text


def test_entities_are_decoded_not_left_raw():
    text = html_text.to_text("<p>Tom &amp; Jerry &mdash; 30% &ldquo;deposit&rdquo;&nbsp;due</p>")
    assert text == "Tom & Jerry — 30% “deposit” due"


def test_links_keep_their_words_and_drop_the_href():
    text = html_text.to_text('<p>See the <a href="https://tracker.example/x?utm=1">walkthrough</a>.</p>')
    assert text == "See the walkthrough."
    assert "tracker.example" not in text


def test_table_rows_do_not_run_together():
    text = html_text.to_text("<table><tr><td>Deposit</td><td>30%</td></tr>"
                             "<tr><td>Final</td><td>30%</td></tr></table>")
    assert "Deposit 30%" in text and "Final 30%" in text
    assert "30%Final" not in text


def test_unclosed_style_does_not_print_its_rules():
    text = html_text.to_text("<div>Hello</div><style>.a{color:red}")
    assert "color:red" not in text


def test_inbound_ingestion_sees_prose_not_css():
    text = inbound_email._body_text(_html_msg(MARKETING))
    assert "margin:0" not in text and "@media" not in text
    assert "Can you send the SOW" in text


def test_zoho_mail_api_content_is_cleaned_the_same_way():
    text = zoho_mail._html_to_text(MARKETING)
    assert "font-family" not in text and "Quick update" not in text
    assert "Barbara" in text


def test_plain_text_bodies_are_left_alone():
    msg = email.message.EmailMessage()
    msg.set_content("5 < 10 and Tom & Jerry, see https://example.com/a?b=1")
    text = inbound_email._body_text(msg)
    assert "5 < 10 and Tom & Jerry" in text
    assert "https://example.com/a?b=1" in text


def test_inbox_page_cleans_a_body_even_if_the_provider_forgot():
    import mail_inbox

    body = mail_inbox._plain_body(MARKETING)
    assert "margin:0" not in body and "@media" not in body
    assert body.startswith("Hi Shaun,")


def test_inbox_page_leaves_angle_bracket_prose_alone():
    import mail_inbox

    assert mail_inbox._plain_body("If 5 < 10 then the wall is 12 ft") == \
        "If 5 < 10 then the wall is 12 ft"


# ── the one-off repair for rows stored before the fix ───────────────────────

def _strip_css():
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
    import clean_mail_bodies
    return clean_mail_bodies.strip_css


def test_repair_removes_stored_css_but_keeps_the_message():
    strip_css = _strip_css()
    stored = ("Quick update\n\n @media only screen and (max-width:600px){"
              ".container{width:100%!important;}}\nbody{margin:0;padding:0}\n"
              ".externalclass{font-family:Arial;color:#333}\n"
              "Hi Shaun, Can you send the SOW?")
    out = strip_css(stored)
    assert "margin" not in out and "@media" not in out and "font-family" not in out
    assert "Hi Shaun, Can you send the SOW?" in out


def test_repair_keeps_a_lead_note_prefix_intact():
    strip_css = _strip_css()
    stored = ("[reply 2026-11-17 09:14 from barbara@example.com] Roof quote: "
              "body{margin:0;padding:0}\n\nTuesday at 9 works.")
    out = strip_css(stored)
    assert out.startswith("[reply 2026-11-17 09:14 from barbara@example.com] Roof quote:")
    assert "margin" not in out and "Tuesday at 9 works." in out


def test_repair_never_eats_prose_with_braces_or_labels():
    strip_css = _strip_css()
    for prose in ("Hi Shaun, use {braces} in the template. Thanks!",
                  "Note: bring boots and gloves. Gate code 4412.",
                  "Tom & Jerry said 5 < 10; see https://example.com/a?b=1"):
        assert strip_css(prose) == prose


def test_repair_handles_generated_selectors_and_glued_prose():
    strip_css = _strip_css()
    zoho_empty = ("div.zm_2509405415985501405_parse_7386470989916945559 table, "
                  "div.zm_2509405415985501405_parse_7386470989916945559 td { }")
    zoho_attr = ("div.zm_6793997775284739519_parse_3749184777660313051 "
                 "a[x-apple-data-detectors] { color: inherit !important }")
    yelp = ("New login to your Yelp account Yelp body, table, td, a, p, span, ul, "
            "li {font-family: Arial, sans-serif!important;} 96 Your account was used.")
    for raw in (zoho_empty, zoho_attr):
        out = strip_css(raw)
        assert "zm_" not in out and "color: inherit" not in out
    out = strip_css(yelp)
    assert out.startswith("New login to your Yelp account")
    assert "font-family" not in out and out.endswith("Your account was used.")


def test_repair_handles_hyphenated_properties_and_keeps_the_body():
    strip_css = _strip_css()
    stored = ("Tomorrow: MongoDB 9.0 in action\n\n"
              "div.zm_3095277270518060795_parse_3873648759517561530 table td "
              "{ border-collapse: collapse }\n"
              "div.zm_3095277270518060795_parse_3873648759517561530 table "
              "{ border-spacing: 0 }\nThen the real body starts here.")
    out = strip_css(stored)
    assert "border-collapse" not in out and "border-spacing" not in out
    assert out.startswith("Tomorrow: MongoDB 9.0 in action")
    assert out.endswith("Then the real body starts here.")


if __name__ == "__main__":
    import sys
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception:
                failures += 1
                print(f"FAILED {name}")
                traceback.print_exc()
    print(f"{'FAILED' if failures else 'ok'} — {failures} failure(s)")
    sys.exit(1 if failures else 0)
