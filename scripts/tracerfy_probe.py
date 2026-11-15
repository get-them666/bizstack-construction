"""Coverage probe: how often does Tracerfy actually return contact data?

Read-only except for the provider calls. Prints one line per address and a
summary, so the cost of tracing the whole book can be judged before spending.
"""
import json
import os

import psycopg
from psycopg.rows import dict_row

import skip_sherpa_service as s
import skiptrace_service as st

CITY = os.getenv("PROBE_CITY", "Virginia Beach")
N = int(os.getenv("PROBE_N", "8"))

conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
with conn.cursor() as cur:
    cur.execute(
        """
        SELECT address FROM leads
        WHERE company = 'construction' AND address ILIKE %s
          AND address ~ 'VA [0-9]{5}'
          AND coalesce(trim(phone), '') = ''
          AND coalesce(trim(email), '') = ''
        ORDER BY random() LIMIT %s
        """,
        (f"%{CITY}%", N),
    )
    rows = cur.fetchall()

hits = spent = usable = 0
first_full = None
for r in rows:
    p = st.parse_address(r["address"])
    if not p.get("ok"):
        print("skip (unparsed):", r["address"])
        continue
    payload = {"address": p["street"], "city": p["city"], "state": p["state"],
               "zip": p.get("zipcode") or "", "find_owner": True}
    d = s._tracerfy_post(payload)
    persons = d.get("persons") or []
    cred = d.get("credits_deducted") or 0
    hit = bool(d.get("hit"))
    phones = sum(len(x.get("phones") or []) for x in persons)
    emails = sum(len(x.get("emails") or []) for x in persons)
    hits += 1 if hit else 0
    spent += cred
    if phones or emails:
        usable += 1
        if first_full is None:
            first_full = json.dumps(persons[0])[:800]
    print("%-46s hit=%-5s n=%s cred=%s phones=%s emails=%s"
          % (r["address"][:44], hit, d.get("persons_count"), cred, phones, emails))

print("SUMMARY city=%s sampled=%s hits=%s usable=%s credits=%s"
      % (CITY, len(rows), hits, usable, spent))
if first_full:
    print("SAMPLE PERSON:", first_full)
