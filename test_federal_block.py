"""Federal-domain blocking, and the research loop's contactable-lead filter.

Both defects were found by reading the notes column, not the code.

1. The block matched on the literal suffixes ".gov" and ".mil". Real federal
   contacts in the leads table did not match: us.af.mil, nps.gov, fs.fed.us.
   Those are all federal buyers, which is the exact thing the block exists to
   keep off the automated cadence.

2. lead_research.research_leads selected on "has a phone-less, address-bearing
   new lead" and never checked email, so it researched leads that already had a
   perfectly good addressable email and wrote "unresolved: no business or owner
   found" on them. The note was not true: the lead had a contact, the email bot
   simply had not run yet.
"""

if __name__ == "__main__":
    import os
    import pathlib
    import sys

    ROOT = pathlib.Path(__file__).resolve().parent
    sys.path.insert(0, str(ROOT))

    FAILS = []


    def check(name, ok, detail=""):
        detail = str(detail)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if not ok and detail else ""))
        if not ok:
            FAILS.append(name)


    import auto_reply  # noqa: E402

    os.environ.pop("BLOCK_GOV_MIL_EMAIL", None)
    os.environ.pop("LEAD_SOURCE_EMAIL_BLOCK", None)

    print("[1] the federal domains that were actually in the leads table")
    # Every one of these is in the leads table right now. arlington.va.us is the
    # control: a Virginia county is a state body, not a federal buyer, and the
    # block must not swallow it.
    for addr in ("vanity.wright@us.af.mil", "Wendy_Deleon@nps.gov",
                 "sonny.z.smith@usace.army.mil", "mwalker05@fs.fed.us",
                 "Ellie_Delerme-Velez@ios.doi.gov", "someone@va.gov",
                 "richard.c.klein@usace.army.mil"):
        check(f"{addr} is blocked", auto_reply.blocked_reason(addr) != "", addr)
    check("arlington.va.us is a state body, not federal",
          auto_reply.blocked_reason("buyer@arlington.va.us") == "",
          auto_reply.blocked_reason("buyer@arlington.va.us"))

    print("\n[2] private and state domains are not over-blocked")
    # arlington.va.us matters: .us is not a federal TLD, and a blanket "any label
    # is gov/mil" rule would not catch it -- but neither should a clumsy rule block
    # a Virginia county.
    for addr in ("contractor@roofers.com", "jo@gmail.com", "hello@bizstackperks.com",
                 "info@cityofhampton.gov.us", "someone@arlington.va.us"):
        check(f"{addr} is allowed", auto_reply.blocked_reason(addr) == "",
              auto_reply.blocked_reason(addr))

    print("\n[3] the domain check is not fooled by case or stray dots")
    for dom in ("US.AF.MIL", "NPS.GOV", "VA.GOV", "  fs.fed.us  ", "us.af.mil."):
        check(f"{dom!r} blocked", auto_reply._is_federal_domain(dom), dom)
    check("an empty domain is not federal", auto_reply._is_federal_domain("") is False)
    check("a bare '.gov' is not a domain", auto_reply._is_federal_domain(".gov") is False)
    check("a bare label is not federal", auto_reply._is_federal_domain("localhost") is False)

    print("\n[4] the source block still applies when explicitly configured")
    # SAM.gov was removed on 2026-10-13, so LEAD_SOURCE_EMAIL_BLOCK no longer
    # defaults to "sam-gov". The mechanism itself is retained, so an operator can
    # still name a source to keep out of the automated cadence.
    check("no source is blocked by default",
          auto_reply.blocked_reason("a@b.com", "sam-gov") == "")
    os.environ["LEAD_SOURCE_EMAIL_BLOCK"] = "sam-gov"
    check("sam-gov source blocked once named", auto_reply.blocked_reason("a@b.com", "sam-gov") != "")
    check("a private address on sam-gov is still blocked",
          auto_reply.blocked_reason("contractor@roofers.com", "sam-gov") != "")
    check("permit_finder is not blocked", auto_reply.blocked_reason("a@b.com", "permit_finder") == "")
    os.environ.pop("LEAD_SOURCE_EMAIL_BLOCK")

    print("\n[5] the block can be lifted deliberately")
    os.environ["BLOCK_GOV_MIL_EMAIL"] = "0"
    check("federal domain allowed when the flag is off",
          auto_reply.blocked_reason("buyer@va.gov") == "")
    os.environ["LEAD_SOURCE_EMAIL_BLOCK"] = "permit_finder"
    check("but a source named in LEAD_SOURCE_EMAIL_BLOCK is still blocked",
          auto_reply.blocked_reason("a@b.com", "permit_finder") != "")
    os.environ.pop("LEAD_SOURCE_EMAIL_BLOCK")
    os.environ.pop("BLOCK_GOV_MIL_EMAIL")

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        sys.exit(1)
    print("All federal-block tests passed.")

