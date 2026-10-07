"""Lob sending: address parsing and the one-letter-per-address plan.

Both behaviours here exist to stop money being spent on mail that cannot help:

- The permit feeds emit one row per permit, so a house with a deck permit and a
  roof permit became two leads. The rendered batch was 177 letters covering 63
  distinct doors. Sending all 177 would put eleven identical envelopes in one
  mailbox -- ~$95 of postage for nothing, and eleven touches at one address
  against a policy whose whole point is one.

- 15 of the 177 leads are stored with no ZIP. Lob bills per piece accepted, and
  an undeliverable piece is still billed. Those are listed, never sent.

Offline: no network, no Lob, no database.
"""

if __name__ == "__main__":
    import pathlib
    import re
    import sys

    ROOT = pathlib.Path(__file__).resolve().parent
    sys.path.insert(0, str(ROOT))

    FAILS = []


    def check(name, ok, detail=""):
        detail = str(detail)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if not ok and detail else ""))
        if not ok:
            FAILS.append(name)


    import lob_send as L  # noqa: E402

    print("[1] the confirmed return address is what would be used")
    check("street is 701 Dana Dr", L.FROM["address_line1"] == "701 Dana Dr", L.FROM["address_line1"])
    check("city is Chesapeake", L.FROM["city"] == "Chesapeake", L.FROM["city"])
    check("state is VA", L.FROM["state"] == "VA", L.FROM["state"])
    check("ZIP is five digits, 23321", L.FROM["zip_code"] == "23321", L.FROM["zip_code"])
    check("ZIP is the right width", len(L.FROM["zip_code"]) == 5, L.FROM["zip_code"])

    print("\n[2] a complete address parses into Lob's fields")
    p = L.parse_address("1501 VANCE CIR, Chesapeake, VA 23320")
    check("ok", p["ok"] is True, p)
    check("street", p["address_line1"] == "1501 VANCE CIR", p)
    check("city", p["city"] == "Chesapeake", p)
    check("state", p["state"] == "VA", p)
    check("zip", p["zip_code"] == "23320", p)
    check("name falls back to Homeowner", p["name"] == "Homeowner", p)

    p = L.parse_address("2106 LYNDORA RD, Virginia Beach, VA 23464")
    check("title-cased city survives", p["city"] == "Virginia Beach", p)
    check("zip parsed", p["zip_code"] == "23464", p)

    print("\n[3] a ZIP+4 does not break the parse")
    p = L.parse_address("1501 VANCE CIR, Chesapeake, VA 23320-1234")
    check("plus-4 accepted", p["ok"] is True and p["zip_code"] == "23320-1234", p)

    print("\n[4] an undeliverable address is rejected, not guessed")
    for raw, why in (("36 Duck Road, Corolla, NC", "no ZIP"),
                     ("889 Shore Drive, Virginia Beach, VA", "no ZIP"),
                     ("178 Sowers Street, Elizabeth City, NC", "no ZIP")):
        # The stored state IS recovered; only the ZIP is missing, and the reason
        # must say that rather than blaming the state.
        p = L.parse_address(raw)
        check(f"{raw!r} rejected", p["ok"] is False, p)
        check(f"{raw!r} explains the missing ZIP", str(p.get("why", "")).startswith(why), p)
        check(f"{raw!r} invents no zip", "zip_code" not in p, p)

    check("an empty address is rejected", L.parse_address("")["ok"] is False)
    check("whitespace is rejected", L.parse_address("   ")["ok"] is False)

    print("\n[5] no rejected address can leak a guessed ZIP into a send")
    bad = ["36 Duck Road, Corolla, NC", "889 Shore Drive, Virginia Beach, VA", "12 Oak Ave"]
    for raw in bad:
        p = L.parse_address(raw)
        if not p["ok"]:
            check(f"{raw!r} yields no sendable address", "address_line1" not in p, p)

    print("\n[6] planning folds duplicates to one send per door")


    class _FakeOutDir:
        """Stands in for the output directory: every pdf resolves and exists.

        lob_send.plan() calls out_dir / row['pdf'] then .exists(). Patching Path
        wholesale broke that chain, so this only intercepts the join.
        """

        def __truediv__(self, other):
            return FakePDFPath(str(other))

        def __fspath__(self):
            return "/tmp"


    class FakePDFPath:
        def __init__(self, name):
            self.name = str(name).split("/")[-1]

        def exists(self):
            return True

        def read_bytes(self):
            return b"%PDF-1.4 fake"


    def _plan(rows):
        return L.plan(rows, _FakeOutDir())


    rows = [
        {"id": "182", "address": "1501 VANCE CIR, Chesapeake, VA 23320", "pdf": "a.pdf"},
        {"id": "183", "address": "1501 VANCE CIR, Chesapeake, VA 23320", "pdf": "b.pdf"},
        {"id": "184", "address": "4012 OAK MOSS CT, Chesapeake, VA 23321", "pdf": "c.pdf"},
        {"id": "185", "address": "4012 oak moss ct, chesapeake, va 23321", "pdf": "d.pdf"},
        {"id": "186", "address": "36 Duck Road, Corolla, NC", "pdf": "e.pdf"},
    ]
    p = _plan(rows)
    check("two distinct deliverable addresses = 2 sends",
          len(p["send"]) == 2, f"{len(p['send'])} sends")
    check("duplicates are reported for the touch ledger", len(p["duplicate_lead_ids"]) == 2,
          p["duplicate_lead_ids"])
    check("the no-ZIP lead is rejected, not mailed",
          [r["id"] for r in p["rejected"]] == ["186"], p["rejected"])
    folded = [e for e in p["send"] if len(e["covers"]) > 1]
    check("both folded addresses each got one letter", len(folded) == 2,
          [(e["lead_id"], e["covers"]) for e in p["send"]])
    check("covers keep every lead id",
          all(len(e["covers"]) == 2 for e in folded), folded)
    check("the case-folded duplicate folded into the same letter",
          any("185" in e["covers"] for e in p["send"]), [(e["lead_id"], e["covers"]) for e in p["send"]])
    check("address matching ignores case and spacing",
          len(p["send"]) == 2, "case-folded duplicates should fold")

    print("\n[7] the real batch")
    manifest = pathlib.Path("/tmp/letters_final/manifest.csv")
    if manifest.exists():
        import csv
        with manifest.open() as fh:
            real_rows = list(csv.DictReader(fh))
        p = _plan(real_rows)
        cost = len(p["send"]) * L.COST_PER_PIECE
        # Independently counted from the manifest: 63 distinct addresses, of which 5
        # have no ZIP in any of their lead rows, leaving 58 deliverable doors. The
        # 15 rejected leads are the lead count, not the address count.
        distinct = {re.sub(r"\s+", " ", r["address"]).strip().lower() for r in real_rows}
        deliverable = {a for a in distinct if L.parse_address(a)["ok"]}
        check("177 leads in the manifest", len(real_rows) == 177, len(real_rows))
        check("63 distinct addresses in total", len(distinct) == 63, len(distinct))
        check("58 of them are deliverable", len(deliverable) == 58, len(deliverable))
        check("5 addresses have no ZIP at all", len(distinct - deliverable) == 5,
              sorted(distinct - deliverable))
        check("the plan sends exactly the deliverable doors", len(p["send"]) == 58,
              len(p["send"]))
        check("15 leads are rejected for a missing ZIP", len(p["rejected"]) == 15,
              len(p["rejected"]))
        check("104 duplicate leads are folded into a single letter",
              len(p["duplicate_lead_ids"]) == 104, len(p["duplicate_lead_ids"]))
        check("every send has a state and a ZIP",
              all(e["to"]["state"] and e["to"]["zip_code"] for e in p["send"]))
        check("no two sends share an address",
              len({e["address"].strip().lower() for e in p["send"]}) == len(p["send"]))
        check("no rejected address is in the send set",
              not ({e["address"].strip().lower() for e in p["send"]} & {r["address"].strip().lower() for r in p["rejected"]}))
        check(f"cost is about $48, not $147 ({cost:.2f})", 40 < cost < 60, f"${cost:.2f}")
        # covers is the number of LEADS a single letter settles, not the number of
        # envelopes: 5712 Rossburn Ct has 11 permit records and still receives one
        # letter. What must hold is one letter per door, which is what the
        # no-two-sends-share-an-address check above asserts.
        worst = max(len(e["covers"]) for e in p["send"])
        check("the busiest door still gets exactly one letter",
              len(p["send"]) == len({e["address"].strip().lower() for e in p["send"]}),
              f"worst door covers {worst} leads")
        check("busiest door's leads are all recorded", worst == 11, worst)
    else:
        check("manifest present for the real-batch assertions", False,
              "run mail_letters.py --out /tmp/letters_final first")

    print("\n[8] sending is opt-in")
    import inspect
    src = inspect.getsource(L.main)
    check("--send is required to transmit", '"--send"' in src, "flag missing")
    check("a dry run is the default path", "args.send" in src)
    check("no key means no send", "LOB_API_KEY" in src)
    check("mail type is standard class by default", L.MAIL_TYPE == "usps_standard", L.MAIL_TYPE)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        sys.exit(1)
    print("All lob_send tests passed.")

