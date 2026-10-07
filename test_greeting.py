"""Greeting must not address a homeowner by their city.

This fired in real sends. The permit feeds publish no applicant, so leads.name
holds the city -- "Chesapeake" -- until a skip trace or a manual touch supplies a
real person. _person_first had no way to know that, so it returned the city
verbatim and the email opened:

    "Hi Chesapeake, We noticed a recent permit for work at 1501 VANCE CIR"

to George Duport and Fred Kelley, whose names were already on those leads by the
time this was found. Same defect class as the printed letters that greeted
"Dear Virginia Beach," fixed in mail_letters.format_recipient.

Offline: pure functions, no db and no transport.
"""

if __name__ == "__main__":
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

    print("[1] a city in the name field is not a person")
    for place in ("Chesapeake", "Virginia Beach", "Williamsburg", "Norfolk",
                  "Newport News", "Corolla", "Elizabeth City", "Hampton Roads",
                  "self", "Owner", "unknown"):
        check(f"{place!r} yields no first name", auto_reply._person_first(place) == "",
              auto_reply._person_first(place))

    print("\n[2] real people still greet by name")
    for name, want in (("George Duport", "George"),
                       ("Raymond Vj Schrag", "Raymond"),
                       ("Michael Scott", "Michael"),
                       ("Ada Lovelace", "Ada")):
        check(f"{name!r} -> {want!r}", auto_reply._person_first(name) == want,
              auto_reply._person_first(name))

    print("\n[3] the SAM.gov 'title · person' form still resolves to the person")
    # This is the form the two-touch-policy commit was written for; it must not
    # regress while fixing the city case.
    check("'Fire Alarm Upgrade · Richard Klein' -> 'Richard'",
          auto_reply._person_first("Fire Alarm Upgrade · Richard Klein") == "Richard",
          auto_reply._person_first("Fire Alarm Upgrade · Richard Klein"))
    check("the city check applies to the post-· part too",
          auto_reply._person_first("Roof Repair · Chesapeake") == "",
          auto_reply._person_first("Roof Repair · Chesapeake"))

    print("\n[4] the rendered message does not say 'Hi <city>'")
    for place in ("Chesapeake", "Virginia Beach"):
        body = auto_reply._build_message(
            "construction", "Buildstack Construction Co.", name=place,
            source="permit_finder", address="1501 VANCE CIR", service="Reroof")
        check(f"{place} renders 'Hi there,'", body.startswith("Hi there,"), body[:70])
        check(f"{place} never appears as a greeting", f"Hi {place}," not in body, body[:70])

    print("\n[5] a named homeowner gets their name in the rendered message")
    body = auto_reply._build_message(
        "construction", "Buildstack Construction Co.", name="George Duport",
        source="permit_finder", address="1501 VANCE CIR", service="Reroof")
    check("renders 'Hi George,'", body.startswith("Hi George,"), body[:70])
    check("the permit context is preserved", "recent permit" in body, body[:120])

    print("\n[6] empty and odd names degrade instead of greeting nothing")
    for name in ("", None, "   ", "·", "X Y"):
        got = auto_reply._person_first(name)
        check(f"{name!r} is safe ({got!r})", got == "" or len(got) >= 2, got)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        sys.exit(1)
    print("All greeting tests passed.")

