"""The founder's own copy must survive a deploy.

Background: the hero paragraph, the "32 years" badge, the meta description and
the About page all lost their founder copy once, when a manual `railway up` from
an uncommitted working tree was replaced by a build from git. Nothing had it
committed, so it was gone. These assertions exist so that cannot happen quietly
again -- if someone edits this copy, they have to mean it.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "")

import construction_main as m  # noqa: E402

FOUNDER = "Shaun O'Leary"
LINE = "32 years in the trade"
CRED = "federal construction inspector"


def template_text(name):
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "construction")
    with open(os.path.join(root, name), encoding="utf-8") as fh:
        return fh.read()


def main():
    index = template_text("index.html")
    about = template_text("about.html")
    base = template_text("base.html")

    print("homepage hero:")
    assert FOUNDER in index, "founder name missing from the homepage hero"
    print(f"  PASS  names {FOUNDER}")
    assert LINE in index, "'32 years in the trade' missing from the homepage"
    print(f"  PASS  states '{LINE}'")
    assert CRED in index, "federal inspector credential missing from the homepage"
    print(f"  PASS  keeps the '{CRED}' credential")
    assert "32 years, one crew, all trades" in index, "hero badge lost the 32-years line"
    print("  PASS  hero badge still reads '32 years, one crew, all trades'")

    print("\nabout page:")
    assert FOUNDER in about, "founder name missing from /about"
    assert LINE in about, "'32 years in the trade' missing from /about"
    assert CRED in about, "federal inspector credential missing from /about"
    print(f"  PASS  /about carries the founder story ({FOUNDER}, {LINE}, {CRED})")

    print("\nmeta description:")
    assert LINE in base, "meta description dropped the experience line"
    print(f"  PASS  meta description leads with '{LINE}'")

    print("\ncontradiction check:")
    # "Founded 2026" next to "32 years in the trade" reads as nonsense to a
    # homeowner, so the About page states experience rather than a founding date.
    assert "Founded</dt>" not in about, "About still shows a bare 'Founded' date"
    assert "32 years in the trade</dd>" in about, "About lost the experience stat"
    print("  PASS  About shows experience, not a founding date that contradicts it")

    # Every page that renders the founder line must not carry the old promise.
    all_templates = index + about
    assert re.search(r"24/7 AI call answering", all_templates) is None, \
        "copy still promises '24/7 AI call answering'"
    print("  PASS  no '24/7 AI call answering' promise left in the copy")

    print("\nALL founder-copy checks PASSED")


if __name__ == "__main__":
    main()