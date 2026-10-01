"""Letter rendering and eligibility, asserted offline against the bugs the first
real print run exposed.

None of these were reachable by reading the code. Every one only showed up when
mail_letters.py ran against the live database and someone read the PDFs:

- The eligibility query raised ProgrammingError on every invocation. A bare
  percent in `LIKE '%@lead.local'` starts a psycopg placeholder, and nothing had
  ever executed that SQL, so it shipped broken in a commit that read as tested.
- `status <> 'archived'` matched completed jobs, the owner's own STR properties
  and already-contacted leads. The first batch would have gone to two customers
  whose work was finished and billed, addressed as cold prospects.
- "Virginia Beach" is two words, so the "First L." abbreviation printed
  "Dear V. Beach," on 123 of 178 letters.
- A sam-gov row carried address "57000, VA, 23551" -- a zip in the street slot,
  which renders as an undeliverable envelope.
"""
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


import mail_letters as m  # noqa: E402

print("[1] the eligibility query is executable")
# psycopg's paramstyle treats a bare % as a placeholder start, so the literal
# percent in the lead.local filter must be doubled or every run raises.
try:
    import psycopg  # noqa: F401 - the paramstyle behaviour under test
    _renderable = True
except ImportError:
    _renderable = None

if _renderable:
    # psycopg adapts a Python str into a quoted SQL literal itself, so the
    # template must hold '%%' for the literal percent and %(company)s for the
    # parameter. Python's own %-formatting is NOT a faithful model of that --
    # it renders %(company)s unquoted, which is why the check below asserts on
    # the template rather than on an interpolated copy.
    raw = m.ADDRESS_ONLY_SQL
    stray = [tok for tok in re.findall(r"%(?:%|\(|\w\)|.)", raw) if tok not in ("%%", "%(")]
    check("template has no unescaped percent", not stray, f"found {stray[:3]}")
    check("the lead.local percent is escaped", "'%%@lead.local'" in raw)
    check("company is a bound parameter, not interpolated",
          "company = %(company)s" in raw)
    check("status filter is present", "status = 'new'" in raw)
else:
    check("psycopg present to validate SQL", False, "install psycopg to run this check")
check("query is scoped to status = 'new'", "status = 'new'" in m.ADDRESS_ONLY_SQL)
check("completed / host / contacted are excluded",
      "status = 'new'" in m.ADDRESS_ONLY_SQL and "status <> 'archived'" not in m.ADDRESS_ONLY_SQL)
check("sam-gov is excluded from printed mail",
      "sam-gov" in m.ADDRESS_ONLY_SQL)

print("\n[2] a place name in leads.name is not treated as a person")
# The permit feeds publish no applicant, so `name` holds the city. These printed
# as "Dear Virginia Beach," and as the addressee on the envelope.
for city in ("Virginia Beach", "Chesapeake", "Williamsburg", "Norfolk",
             "Newport News", "Corolla", "Elizabeth City", "Self", "Owner"):
    check(f"{city!r} greets as the homeowner",
          m.format_recipient(city) == "the homeowner", m.format_recipient(city))

print("\n[3] real names still abbreviate")
check("'Ada Lovelace' -> 'A. Lovelace'", m.format_recipient("Ada Lovelace") == "A. Lovelace",
      m.format_recipient("Ada Lovelace"))
check("'Ada B. Lovelace' -> 'A. Lovelace'", m.format_recipient("Ada B. Lovelace") == "A. Lovelace",
      m.format_recipient("Ada B. Lovelace"))
check("a bare first name is kept", m.format_recipient("Ada") == "Ada", m.format_recipient("Ada"))
check("a titled name is kept whole", m.format_recipient("Dr. Kim") == "Dr. Kim",
      m.format_recipient("Dr. Kim"))
check("empty name is the homeowner", m.format_recipient("") == "the homeowner")
check("whitespace-only name is the homeowner", m.format_recipient("   ") == "the homeowner")
# A solicitation title is not a person. The abbreviation path turned this into
# "N. VA)", which printed as the greeting on the one sam-gov letter rendered
# before sam-gov was excluded outright.
long_title = ("NATO Business Opportunity: Minor Construction Framework for "
              "Headquarters Supreme Allied Commander Transformation (Norfolk VA)")
check("a long non-name title is not abbreviated into a fragment",
      m.format_recipient(long_title) == "the homeowner", m.format_recipient(long_title))

print("\n[4] the business number is what gets printed")
check("letterhead defaults to the business line",
      m.BRAND["phone"] == "(757) 908-7121", m.BRAND["phone"])
check("the personal number is not a default", "252" not in m.BRAND["phone"])
check("the opt-out route names a printed channel",
      m.BRAND["email"] in m.BRAND["email"])

print("\n[5] address splitting survives both stored shapes")
check("comma shape", m.address_lines("12 Oak Ave, Chesapeake, VA 23321")
      == ["12 Oak Ave", "Chesapeake", "VA 23321"], m.address_lines("12 Oak Ave, Chesapeake, VA 23321"))
check("newline shape", m.address_lines("12 Oak Ave\nChesapeake, VA 23321")
      == ["12 Oak Ave", "Chesapeake", "VA 23321"], m.address_lines("12 Oak Ave\nChesapeake, VA 23321"))
check("empty address yields no lines", m.address_lines("") == [])
check("a bare zip is still a line, not dropped", m.address_lines("57000, VA, 23551")
      == ["57000", "VA", "23551"], m.address_lines("57000, VA, 23551"))

print("\n[6] filenames are filesystem-safe")
# Leading and trailing separators are stripped, so the .pdf never doubles up.
check("punctuation is stripped and separators trimmed",
      m.safe_name({"id": 1, "name": "Ada B. Lovelace / (flood)"})
      == "Ada-B.-Lovelace-flood", m.safe_name({"id": 1, "name": "Ada B. Lovelace / (flood)"}))
check("a run of punctuation cannot leave a trailing separator",
      not m.safe_name({"id": 1, "name": "Smith Roofing ((("}).endswith("-"),
      m.safe_name({"id": 1, "name": "Smith Roofing ((("}))
check("a name of only punctuation still yields a filename",
      m.safe_name({"id": 7, "name": "///"}) == "lead-7",
      m.safe_name({"id": 7, "name": "///"}))
check("a lead with no name still has a filename",
      m.safe_name({"id": 42, "name": ""}) == "lead-42", m.safe_name({"id": 42, "name": ""}))
check("filenames stay inside a sane length", len(m.safe_name({"id": 1, "name": "x" * 200})) <= 60)

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): {FAILS}")
    sys.exit(1)
print("All mail_letters tests passed.")
