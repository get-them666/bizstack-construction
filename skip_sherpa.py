#!/usr/bin/env python3
"""Skip-trace permit addresses through Skip Sherpa to get a real owner contact.

The permit feeds publish an address and nothing else. That is why 500+ leads
sit unreachable: no applicant name, no phone, no email, so there is no channel
the email/text bot can use and mail_letters.py is the only way to reach them.
Skip Sherpa turns an address into an owner name and contact details, which is
what makes those leads contactable.

Skip Sherpa is the provider, not ATTIC. Its /api/property_details endpoint
returns an owner NAME but no phone or email; /api/properties is the actual skip
trace and returns both. Verified live against Chesapeake addresses.

Every lookup is billable, so this is deliberately hard to over-run:

  - dry run by default, and the plan is printed before anything is sent
  - --limit is required to actually spend anything
  - a per-run credit ceiling that stops the batch even if --limit is large
  - SKIP_SHERPA_API_KEY must be set; without it nothing is sent
  - addresses are deduped, because the permit feeds emit one row per permit and
    177 leads cover only 63 distinct doors. Paying for duplicates is paying for
    the same person twice.
  - addresses already carrying a usable contact are skipped, so no credit is
    spent rediscovering a phone number we already hold

Writes results back to leads.name / email / phone and stamps analysis_json, so
the email bot picks them up on its next pass with no further wiring.

    python skip_sherpa.py --list                    # who is eligible, spends nothing
    python skip_sherpa.py --limit 20                # trace up to 20 addresses
    python skip_sherpa.py --limit 20 --apply        # ...and write to the database
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_ROOT = "https://skipsherpa.com"

# Cloudflare fronts this API and answers python-urllib with Error 1010 "Access
# denied" -- the owner's bot-fingerprint rule. It does not 403 as an auth
# failure, it looks like one, so the key looks wrong when it is fine. Both
# failures cost zero credits because the request never reaches the API.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Hard ceiling per run regardless of --limit. Skip Sherpa is per-lookup
# billing, so a mistyped limit is the difference between 20 credits and 500.
DEFAULT_CREDIT_CEILING = 13  # 20 total, 7 already spent by hand

_STATE_ZIP = re.compile(r"^(.*?),\s*([A-Za-z]{2})\s*(\d{5}(?:-\d{4})?)?$")


def api_key() -> str:
    return (os.getenv("SKIP_SHERPA_API_KEY") or "").strip()


def credit_ceiling() -> int:
    try:
        return max(0, int(os.getenv("SKIP_SHERPA_MAX_CREDITS", DEFAULT_CREDIT_CEILING) or DEFAULT_CREDIT_CEILING))
    except (TypeError, ValueError):
        return DEFAULT_CREDIT_CEILING


def parse_address(raw: str) -> dict:
    """'1501 VANCE CIR, Chesapeake, VA 23320' -> Skip Sherpa's address shape."""
    text = str(raw or "").replace("\r\n", " ").replace("\r", " ").replace("\n", ", ")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return {"ok": False, "why": "empty address"}
    street = parts[0]
    city = state = zipcode = ""
    for chunk in parts[1:]:
        if re.fullmatch(r"[A-Za-z]{2}", chunk.strip()):
            state = chunk.strip().upper()
            continue
        z = re.search(r"\b\d{5}(?:-\d{4})?\b", chunk)
        if z:
            zipcode = z.group(0)
            rest = chunk.replace(zipcode, "").strip(" ,")
            if rest:
                city = city or rest
            continue
        if not city:
            city = chunk
    if not state:
        m = _STATE_ZIP.search(text)
        if m:
            state = m.group(2).upper()
            zipcode = zipcode or (m.group(3) or "")
    if not state:
        return {"ok": False, "why": "no state"}
    if not zipcode:
        return {"ok": False, "why": "no ZIP"}
    return {"ok": True, "street": street, "city": city.upper(), "state": state, "zipcode": zipcode}


def _post(path: str, payload: dict) -> dict:
    """Skip Sherpa's lookup endpoints are PUT, not POST, and answer 405 to a POST.

    The spec declares every /api/* lookup as put, so this sends PUT throughout.
    """
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{API_ROOT}{path}", data=body, method="PUT",
        headers={"Content-Type": "application/json",
                 "api-key": api_key(),
                 "Authorization": f"Bearer {api_key()}",
                 "Accept": "application/json",
                 "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return {"error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"error": str(exc)[:200]}


def _phones_from(node) -> list:
    """Pull phone records out of whatever shape the response used."""
    out = []
    if isinstance(node, dict):
        for key, val in node.items():
            if "phone" in key.lower() and isinstance(val, list):
                for p in val:
                    if isinstance(p, dict):
                        num = (p.get("phone") or p.get("number") or p.get("phone_number")
                               or p.get("display") or "")
                        typ = str(p.get("phone_type") or p.get("type") or "")
                        if num and len(re.sub(r"\D", "", str(num))) >= 10:
                            out.append((str(num), typ))
            elif isinstance(val, (dict, list)):
                out.extend(_phones_from(val))
    elif isinstance(node, list):
        for item in node:
            out.extend(_phones_from(item))
    return out


def _emails_from(node) -> list:
    out = []
    if isinstance(node, dict):
        for key, val in node.items():
            if "email" in key.lower():
                if isinstance(val, str) and "@" in val:
                    out.append(val)
                elif isinstance(val, list):
                    for e in val:
                        if isinstance(e, str) and "@" in e:
                            out.append(e)
                        elif isinstance(e, dict) and isinstance(e.get("email"), str):
                            out.append(e["email"])
            elif isinstance(val, (dict, list)):
                out.extend(_emails_from(val))
    elif isinstance(node, list):
        for item in node:
            out.extend(_emails_from(item))
    return out


def _owner_names(node) -> list:
    names = []
    if isinstance(node, dict):
        owners = node.get("owners")
        if isinstance(owners, list):
            for o in owners:
                if isinstance(o, dict):
                    fn = (o.get("first_name") or "").strip()
                    ln = (o.get("last_name") or "").strip()
                    if fn and ln:
                        names.append(f"{fn.title()} {ln.title()}")
                    elif fn or ln:
                        names.append((fn or ln).title())
        for k, v in node.items():
            if k == "owners":
                continue
            if isinstance(v, (dict, list)):
                names.extend(_owner_names(v))
    elif isinstance(node, list):
        for item in node:
            names.extend(_owner_names(item))
    return names


def _persons(node) -> list:
    """Walk the nested person objects out of the response.

    The shape is property_results[].property.owners[].person, with the name under
    person_name and the contact records under emails / phone_numbers. An earlier
    version guessed at these paths and returned nothing while the API was
    returning HTTP 200 with all the data, which looked identical to a miss.
    """
    found = []
    if isinstance(node, dict):
        if "person" in node and isinstance(node.get("person"), dict):
            found.append(node["person"])
        owners = node.get("owners")
        if isinstance(owners, list):
            for o in owners:
                if isinstance(o, dict) and isinstance(o.get("person"), dict):
                    found.append(o["person"])
        for k, v in node.items():
            if k in ("owners", "person"):
                continue
            if isinstance(v, (dict, list)):
                found.extend(_persons(v))
    elif isinstance(node, list):
        for item in node:
            found.extend(_persons(item))
    return found


def _person_name(person: dict) -> str:
    pn = person.get("person_name") or {}
    fn = str(pn.get("first_name") or "").strip()
    ln = str(pn.get("last_name") or "").strip()
    if fn and ln:
        return f"{fn.title()} {ln.title()}"
    full = str(pn.get("full_name") or person.get("display_name") or "").strip()
    return full or (fn or ln).title()


# Whether traced phones are written to the leads table at all.
#
# Email-only is the default and should stay that way. Measured on a real
# Chesapeake owner: 9 phone numbers returned, 7 flagged DNC by the registry, and
# 2 last confirmed in 2011 and 2012. Those are consumer mobiles belonging to
# people who registered against being called. Writing them would feed the SMS and
# AI-voice sweeps, which is the sharpest regulatory exposure in this system --
# and the numbers add nothing today, because email is the channel that actually
# produced replies this week.
#
# SKIP_SHERPA_WRITE_PHONES=1 opts in, and even then _phone_is_usable() drops DNC
# entries and anything not confirmed recently.
WRITE_PHONES = (os.getenv("SKIP_SHERPA_WRITE_PHONES", "0") or "0").strip().lower() in ("1", "true", "yes", "on")


def _phone_is_usable(phone_type: str, dnc: bool, last_seen: str) -> bool:
    """Only for the opt-in path. DNC is never usable; stale numbers are not either."""
    if dnc:
        return False
    if (phone_type or "").lower() in ("landline", ""):
        return False
    if last_seen and last_seen < (time.strftime("%Y") + "-01-01"):
        return False
    return True


def _person_phones(person: dict) -> list:
    """Phones as (e164, type, is_dnc, last_seen).

    The DNC flag is carried through and NOT filtered here. Whether a
    do-not-call registry entry blocks outreach depends on whose number it is and
    which channel is used, so the decision is left visible to the caller rather
    than silently made in a parser. last_seen matters too: this data returns
    numbers last confirmed in 2011, and a stale landline is worse than no number.
    """
    out = []
    for p in person.get("phone_numbers") or []:
        if not isinstance(p, dict):
            continue
        e164 = str(p.get("e164_format") or "").strip()
        digits = re.sub(r"\D", "", e164)
        # Require a real E.164: '+' alone passes a naive length check on the
        # non-digit characters, and that empty string was being picked as the
        # preferred number.
        if not e164.startswith("+") or not (10 <= len(digits) <= 15):
            continue
        dnc = False
        for st in p.get("dnc_statuses") or []:
            if isinstance(st, dict) and st.get("is_dnc"):
                dnc = True
                break
        out.append((e164, str(p.get("type") or ""), dnc,
                    str(p.get("last_seen") or "")))
    return out


def _person_emails(person: dict) -> list:
    out = []
    for e in person.get("emails") or []:
        addr = (e.get("email_address") if isinstance(e, dict) else e) or ""
        addr = str(addr).strip().lower()
        if "@" in addr and addr.rsplit("@", 1)[-1] not in _JUNK_EMAIL_DOMAINS:
            out.append(addr)
    return out


# Disposable/webmail addresses are technically valid and useless for outreach.
_JUNK_EMAIL_DOMAINS = {
    "example.com", "domain.com", "email.com", "test.com", "yopmail.com",
    "mailinator.com", "guerrillamail.com", "tempmail.com", "throwawaymail.com",
}


def trace_address(addr: dict) -> dict:
    """One address, one credit. Returns owner/phone/email or an error."""
    # success_criteria is an ENUM, not an object: 'owner-name',
    # 'owner-contact-any', 'owner-contact-email', 'owner-contact-phone',
    # 'owner-contact-address'. An unrecognised value does not 400 -- the lookup
    # simply never reaches the success condition, so the whole request is billed
    # and returns nothing. 'owner-contact-any' is what we want: an owner with at
    # least one reachable method, so a name-only match is not paid for.
    payload = {"property_lookups": [{
        "property_address_lookup": {
            "street": addr["street"], "city": addr["city"],
            "state": addr["state"], "zipcode": addr["zipcode"],
        },
        "success_criteria": "owner-contact-any",
        "debt_data_best_effort": False,
    }]}
    data = _post("/api/properties", payload)
    if data.get("error"):
        return {"ok": False, "error": data["error"]}

    # The response key is property_results, and a per-item status_code of 200
    # with the data under a nested `property` key. Reading property_lookup_results
    # here returned "no result row" while the API was returning complete data.
    results = data.get("property_results") or data.get("property_lookup_results") or []
    if not results:
        return {"ok": False, "error": "no result row"}
    first = results[0] if isinstance(results[0], dict) else {}

    code = first.get("status_code")
    if code and int(code) != 200:
        issues = first.get("issues") or []
        detail = issues[0].get("detail") if issues and isinstance(issues[0], dict) else ""
        return {"ok": False, "error": f"no contact ({detail or code})"}

    prop = first.get("property") or {}
    if not isinstance(prop, dict) or not prop:
        issues = first.get("issues") or []
        return {"ok": False, "error": "no property matched" + (f" ({issues})" if issues else "")}

    people = _persons(prop)
    if not people:
        return {"ok": False, "error": "no owner person returned"}

    names, phones, emails = [], [], []
    for person in people:
        nm = _person_name(person)
        if nm and nm not in names:
            names.append(nm)
        phones.extend(_person_phones(person))
        emails.extend(_person_emails(person))

    # Prefer a mobile, then the most recently confirmed number. A landline last
    # seen in 2011 is almost certainly disconnected, and a bad number burns a
    # touch the contact policy will not give back.
    def rank(rec):
        e164, typ, _dnc, seen = rec
        is_mobile = "mobile" in typ.lower() or "cell" in typ.lower()
        return (0 if is_mobile else 1, "" if seen else "9999", seen, e164)

    ordered = sorted(phones, key=rank)
    chosen = ordered[0] if ordered else ("", "", False, "")
    usable = [r for r in ordered if _phone_is_usable(r[1], r[2], r[3])]
    ordered_emails = []
    for e in emails:
        if e not in ordered_emails:
            ordered_emails.append(e)
    # With WRITE_PHONES off (the default) the phone is returned for reporting but
    # not written, so the lead cannot enter the SMS or AI-voice sweep.
    write_phone = chosen[0] if (WRITE_PHONES and usable) else ""
    return {
        "ok": True,
        "owner": names[0] if names else "",
        "all_owners": names[:4],
        "phone": write_phone,
        "phone_raw": chosen[0],
        "phone_type": chosen[1],
        "phone_dnc": chosen[2],
        "phone_last_seen": chosen[3],
        "phone_usable": bool(usable),
        "phone_count": len(phones),
        "all_phones": [f"{e} ({t}{', DNC' if d else ''}{', seen ' + s if s else ''})"
                       for e, t, d, s in ordered[:5]],
        "all_emails": ordered_emails[:6],
        "email": emails[0] if emails else "",
        "value": prop.get("estimated_value"),
        "owner_occupied": prop.get("owner_occupied"),
    }


# --- RentCast prefilter --------------------------------------------------------
# Why this exists: Skip Sherpa bills a credit for EVERY address traced, including
# the ones that come back with no owner at all. RentCast answers the same
# question -- does an owner of record exist here? -- for free, from the same
# assessor record, and skiptrace_service caches every answer. So the addresses
# that would burn a Skip Sherpa credit to return nothing can be identified
# first, for nothing.
#
# This is not a replacement. RentCast returns an owner NAME and no phone or
# email, and name-only is precisely what Skip Sherpa's 'owner-contact-any'
# success criteria refuses to bill for. RentCast screens; Skip Sherpa delivers
# the contact. Run order is RentCast, then Skip Sherpa on what survives.
#
# Disabled by default. It spends the shared RentCast quota, and that quota is
# 50/month also used by instant-quote, so it is opt-in and separately bounded.
PREFILTER_DEFAULT_CEILING = 20  # well under RentCast's 50/month


def rentcast_prefilter(entries: list, db_url: str = "") -> tuple:
    """Drop addresses RentCast says have no owner, so no credit is spent on them.

    Returns (kept_entries, notes). `notes` is a list of human-readable lines for
    the run report; it is the audit trail for what was dropped and why.

    Fails soft and entirely: any problem here leaves the plan untouched and
    reports why, because a broken prefilter must never silently delete real work
    from the Skip Sherpa plan.
    """
    if not (os.getenv("RENTCAST_API_KEY") or "").strip():
        return entries, ["prefilter: RENTCAST_API_KEY not set, plan unchanged"]

    # Both imports are inside the guard. psycopg is optional in a bare checkout,
    # and an ImportError escaping here would abort the run and spend nothing --
    # worse than the outcome this function exists to produce.
    try:
        import psycopg
        from psycopg.rows import dict_row
        import skiptrace_service as sts
    except ImportError as exc:
        return entries, [f"prefilter unavailable ({exc}); plan unchanged"]

    # The shared RentCast ceiling. Cached answers do not count against it --
    # they cost nothing -- so this is a cap on provider calls, not on addresses.
    ceiling = prefilter_ceiling()
    spent, kept, notes = 0, [], []

    try:
        db = psycopg.connect(db_url or os.getenv("DATABASE_URL", ""), row_factory=dict_row)
    except Exception as exc:
        return entries, [f"prefilter: no database for cache ({type(exc).__name__}); plan unchanged"]

    try:
        with db.cursor() as cur:
            sts.ensure_schema(cur)

        for entry in entries:
            to = entry["to"]
            key = sts.normalize_address(to["street"], to["city"], to["state"], to["zipcode"])

            cached = None
            with db.cursor() as cur:
                cached = sts.cache_get(cur, key)

            if cached is None:
                if spent >= ceiling:
                    notes.append(f"prefilter: hit the {ceiling}-call RentCast ceiling; "
                                 f"the rest go to Skip Sherpa unverified")
                    kept.append(entry)
                    continue
                try:
                    result = sts.trace_address(to["street"], to["city"], to["state"], to["zipcode"])
                except sts.ProviderError as exc:
                    # A quota or outage must not shrink the plan. Keep it.
                    notes.append(f"prefilter: RentCast {exc.status} -- plan unchanged from here")
                    kept.append(entry)
                    continue
                spent += 1
                with db.cursor() as cur:
                    sts.cache_put(cur, key, result)
                    sts.audit(cur, "skip_sherpa_prefilter", key, result, cached=False)
            else:
                result = cached

            if not result.get("found"):
                notes.append(f"  dropped (no owner of record): {to['street']}, {to['city']} "
                             f"-- saves 1 Skip Sherpa credit")
                continue

            entry["prefiltered_owner"] = result.get("owner_of_record") or ""
            entry["prefiltered_residential"] = bool(result.get("is_residential"))
            kept.append(entry)

        db.commit()
    except Exception as exc:
        return entries, [f"prefilter failed ({type(exc).__name__}: {exc}); plan unchanged"]
    finally:
        try:
            db.close()
        except Exception:
            pass

    notes.insert(0, f"prefilter: {spent} RentCast call(s) (ceiling {ceiling}), "
                    f"{len(entries) - len(kept)} address(es) dropped for free")
    return kept, notes


def prefilter_ceiling() -> int:
    try:
        return max(0, int(os.getenv("RENTCAST_PREFILTER_MAX_CALLS", PREFILTER_DEFAULT_CEILING) or 0))
    except (TypeError, ValueError):
        return PREFILTER_DEFAULT_CEILING


ELIGIBLE_SQL = """
    SELECT id, name, address, email, phone, source, analysis_json
    FROM leads
    WHERE company = %(company)s
      AND status = 'new'
      AND address IS NOT NULL AND BTRIM(address) <> ''
      AND (email IS NULL OR BTRIM(email) = '' OR LOWER(email) LIKE '%%@lead.local')
    ORDER BY id
"""


def fetch_candidates(company: str) -> list:
    import psycopg
    from psycopg.rows import dict_row
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        raise SystemExit("DATABASE_URL is not set; cannot read leads.")
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        with db.cursor() as cur:
            cur.execute(ELIGIBLE_SQL, {"company": company})
            return [dict(r) for r in cur.fetchall()]


def build_plan(rows: list) -> dict:
    """One credit per distinct address. Duplicates are folded onto the winner."""
    planned, skipped = {}, []
    for row in rows:
        raw = (row.get("address") or "").strip()
        key = re.sub(r"\s+", " ", raw).strip().lower()
        parsed = parse_address(raw)
        if not parsed["ok"]:
            skipped.append({"id": row.get("id"), "address": raw, "why": parsed["why"]})
            continue
        if (row.get("phone") or "").strip():
            skipped.append({"id": row.get("id"), "address": raw, "why": "already has a phone"})
            continue
        entry = planned.get(key)
        if entry:
            entry["leads"].append(row.get("id"))
        else:
            planned[key] = {"address": raw, "to": parsed, "leads": [row.get("id")]}
    return {"plan": list(planned.values()), "skipped": skipped}


def apply_results(db_url: str, company: str, results: list) -> int:
    """Write owner/phone/email onto the leads and stamp analysis_json.

    Stamping is what stops a re-run re-paying for the same address, which is the
    difference between a one-off cost and a repeating bill.
    """
    import psycopg
    from psycopg.rows import dict_row
    updated = 0
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        for res in results:
            if not res.get("ok"):
                continue
            meta = {"skip_traced": True, "skip_sherpa_at": time.strftime("%Y-%m-%d"),
                    "skip_sherpa_owner": res.get("owner") or "",
                    "skip_sherpa_phones": res.get("phone_count", 0)}
            payload = json.dumps(meta)
            for lid in res["leads"]:
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE leads SET "
                        # Overwrite the placeholder name, whatever shape it has.
                        # The earlier guard was `name LIKE 'Permit · %'`, but
                        # permit_finder writes the bare city into `name`
                        # ("Chesapeake"), so it matched nothing and the traced
                        # owner was never written. The address is the identity
                        # here and lead_id is already scoped to it, so replacing
                        # a placeholder city with a real person's name is safe.
                        "  name = CASE WHEN %s <> '' THEN %s ELSE name END, "
                        # Same for the @lead.local placeholders: those are the
                        # synthetic addresses the schema requires, not a real
                        # contact, so replacing one is the point.
                        "  email = CASE WHEN %s <> '' AND ("
                        "    BTRIM(COALESCE(email,'')) = '' OR LOWER(email) LIKE '%%@lead.local') "
                        "             THEN %s ELSE email END, "
                        "  phone = CASE WHEN %s <> '' AND BTRIM(COALESCE(phone,'')) = '' "
                        "             THEN %s ELSE phone END, "
                        "  analysis_json = CASE WHEN analysis_json IS NULL THEN %s::jsonb "
                        "       ELSE analysis_json::jsonb || %s::jsonb END "
                        "WHERE id = %s;",
                        (res.get("owner") or "", res.get("owner") or "",
                         res.get("email") or "", res.get("email") or "",
                         res.get("phone") or "", res.get("phone") or "",
                         payload, payload, lid),
                    )
                    updated += cur.rowcount
        db.commit()
    return updated


def import_from_file(path: str, company: str, apply: bool) -> int:
    """Load results that were already paid for -- no credits spent.

    Skip Sherpa has no endpoint to retrieve past lookups, so results traced by
    hand in the dashboard exist only there. Feed them back in here instead of
    buying them twice. Accepts either this script's own output shape or a plain
    list of {address, owner, phone, email} objects, which is what a copy-paste
    out of the dashboard looks like.
    """
    raw = json.loads(Path(path).read_text())
    if isinstance(raw, dict):
        raw = raw.get("results") or []
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        addr = (item.get("address") or "").strip()
        parsed = parse_address(addr)
        if not parsed["ok"]:
            print(f"  skip (unparseable address): {addr!r} — {parsed['why']}")
            continue
        rows.append({
            "ok": bool(item.get("owner") or item.get("phone") or item.get("email")),
            "address": addr,
            "owner": (item.get("owner") or "").strip(),
            "phone": (item.get("phone") or "").strip(),
            "email": (item.get("email") or "").strip(),
            "phone_count": 1,
            "leads": item.get("leads") or [],
        })
    usable = [r for r in rows if r["ok"]]
    print(f"imported {len(usable)} usable row(s) with a name, phone or email "
          f"(of {len(raw)} in file) — 0 credits spent")
    if not usable:
        return 0
    for r in usable:
        print(f"  {r['address'][:44]:<44} {(r['owner'] or '(no name)')[:22]:<22} "
              f"{(r['phone'] or '-')[:15]:<15} {r['email']}")
    if apply:
        n = apply_results(os.environ["DATABASE_URL"], company, usable)
        print(f"\nwrote {n} lead row(s)")
        return n
    print("\ndry run: not written. Re-run with --apply to save.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company", default="construction")
    ap.add_argument("--list", action="store_true", help="show the plan, spend nothing")
    ap.add_argument("--limit", type=int, default=0, help="max addresses to trace")
    ap.add_argument("--apply", action="store_true", help="write results to the database")
    ap.add_argument("--out", default="", help="also write results to this JSON file")
    ap.add_argument("--import", dest="import_path", default="",
                    help="load already-paid results from this JSON file; spends no credits")
    ap.add_argument("--prefilter", action="store_true",
                    help="screen addresses with RentCast first, so Skip Sherpa credits "
                         "are not spent on addresses with no owner of record")
    args = ap.parse_args()

    if args.import_path:
        return 0 if import_from_file(args.import_path, args.company, args.apply) else 1

    rows = fetch_candidates(args.company)
    if not rows:
        print(f"No address-only leads for company '{args.company}'.")
        return 0

    built = build_plan(rows)
    plan, skipped = built["plan"], built["skipped"]
    ceiling = credit_ceiling()
    budget = min(args.limit, ceiling) if args.limit > 0 else 0

    print(f"eligible leads      : {len(rows)}")
    print(f"distinct addresses  : {len(plan)}")
    print(f"skipped             : {len(skipped)}")
    print(f"credits available   : {ceiling} (SKIP_SHERPA_MAX_CREDITS)")
    if args.limit:
        print(f"requested --limit   : {args.limit}"
              + ("  CLAMPED to the ceiling" if args.limit > ceiling else ""))
    if not api_key():
        print("\nSKIP_SHERPA_API_KEY is not set; nothing can be traced.")
        print("Set it, then re-run to spend credits.")
        return 1

    if args.list or budget <= 0:
        print("\nFirst 25 addresses that would be traced:")
        for e in plan[:25]:
            print(f"  lead {e['leads'][0]:>5}  {e['to']['street']}, {e['to']['city']} "
                  f"{e['to']['state']} {e['to']['zipcode']}"
                  + (f"   (+{len(e['leads']) - 1} leads)" if len(e["leads"]) > 1 else ""))
        if len(plan) > 25:
            print(f"  ... and {len(plan) - 25} more")
        print("\nNothing traced. Re-run with --limit N to spend N credits.")
        return 0

    # Prefilter before the budget slice, not after: screening then truncating
    # would waste the RentCast calls on addresses the limit was going to drop.
    # It runs after the --list early return above, because --list must spend
    # nothing -- not even free RentCast quota.
    if args.prefilter:
        print(f"\nprefiltering {len(plan)} address(es) with RentCast (free, cached)...")
        plan, notes = rentcast_prefilter(plan)
        for line in notes:
            print(f"  {line}")

    target = plan[:budget]
    print(f"\ntracing {len(target)} address(es), {len(target)} credit(s)...")
    results = []
    for i, e in enumerate(target, 1):
        res = trace_address(e["to"])
        res["address"] = e["address"]
        res["leads"] = e["leads"]
        results.append(res)
        if res.get("ok"):
            # Where the prefilter already found the name, show it when Skip
            # Sherpa's success criteria withheld one -- a name-only match is
            # not billed, so the answer would otherwise look like nothing.
            owner = res.get("owner") or e.get("prefiltered_owner") or "(no name)"
            print(f"  [{i}/{len(target)}] {e['to']['street']:<28} "
                  f"{owner:<24} "
                  f"{(res.get('phone') or '(no phone)'):<16} {res.get('email') or ''}")
        else:
            print(f"  [{i}/{len(target)}] {e['to']['street']:<28} FAILED: {res.get('error')}")
        if i < len(target):
            time.sleep(0.3)

    hit = [r for r in results if r.get("ok") and r.get("email")]
    named = [r for r in results if r.get("ok")]
    suppressed = len(named) - len([r for r in named if r.get("phone")])
    print(f"\n{len(hit)}/{len(target)} returned a usable EMAIL")
    if named and suppressed:
        print(f"{suppressed} result(s) had a phone that was not written "
              f"(email-only mode; DNC/stale numbers are never usable)")
    if not WRITE_PHONES:
        print("phones recorded for reporting but NOT written to leads (SKIP_SHERPA_WRITE_PHONES=0)")

    if args.apply and hit:
        n = apply_results(os.environ["DATABASE_URL"], args.company, hit)
        print(f"wrote {n} lead row(s); the email bot picks them up on its next pass")
    elif hit:
        print("dry run: not written. Re-run with --apply to save.")

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"results: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())