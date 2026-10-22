#!/usr/bin/env python3
"""Batch direct mail for address-only property owners, ordered as a delivery route.

mail_letters.py can already render one letter per address-only lead. What it
cannot do is treat the batch as a job: nobody had needed to know that the 177
letters of Sept 2026 were ordered, chunked, or routed, so it left them in id
order with no way to work them.

This is that layer, and it changes nothing about the letters themselves. It
selects a stricter audience than ADDRESS_ONLY_SQL, orders them by driving
distance from the owner's residence, splits them into day-sized chunks, and
writes per-chunk route sheets a phone can navigate from.

    venv/bin/python mail_batch.py list
    venv/bin/python mail_batch.py build --out outbox/2026-10-12
    venv/bin/python mail_batch.py fire  --out outbox/2026-10-12

Three subcommands, and the split is the whole design:

  list   Reads only. Prints exactly who would be mailed and who was dropped,
         with the reason for every drop. Safe to run as often as you like.

  build  Renders PDFs + manifest.csv + route sheets to a folder. Spends no
         money and puts nothing in the post. This is the batch you review.

  fire   Actually transmits, via Lob, and only from an already-built folder.
         Gated on a typed count. Postal mail cannot be recalled, so this step
         is deliberately the slowest one in the file.

Offline with respect to the business: like mail_letters.py it never writes
comms_logs and never contacts a lead. Marking a batch as mailed stays an
explicit owner action (lob_send does it on --send, or the four buttons on the
lead card), so a built-but-unmailed batch cannot make the app suppress a
future email or text to somebody by accident.

Three checks here that mail_letters.py does not do, because a batch is where
they start to matter:

  * suppression_list / DNC are honoured. The letter promises the recipient an
    opt-out ("email us and we will remove your address"), and nothing wired
    honor_opt_out to inbound mail -- so a reply of "stop" would previously
    have been re-mailed on the next batch. This checks before every send.

  * The owner's personal mobile can never appear. 252-665-5891 belongs in
    lending material only; 757-908-7121 is the line on customer-facing work.
    .env.example still sets LETTER_BRAND_PHONE to the personal number, and
    mail_letters.py reads that env var, so preflight hard-fails on it.

  * The letterhead is complete. USPS actioning a bounce needs a return street.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

import mail_letters
import open_geo

# ── configuration ───────────────────────────────────────

ORIGIN = os.getenv("MAIL_ROUTE_ORIGIN", "").strip() or ", ".join(
    x for x in (mail_letters.BRAND["street"], mail_letters.BRAND["city"]) if x
)
DEFAULT_CHUNK = int(os.getenv("MAIL_CHUNK_SIZE", "25") or 25)

# Owner's personal mobile. Cold mail must never carry it; see module docstring.
PERSONAL_PHONE_FRAGMENTS = ("252",)

ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
R = 3958.8  # miles

# Street-type and directional spelling. An exclude file written by hand or by a
# different tool spells these differently from the permit feed ("Ross Burn" vs
# "Rossburn", "Avenue" vs "Ave"), and a plain lowercase-and-strip-punctuation
# key silently fails to match across that difference -- which would re-mail
# somebody who was already mailed.
_STREET_TYPES = {
    "street": "st", "str": "st", "avenue": "ave", "av": "ave", "aven": "ave",
    "drive": "dr", "road": "rd", "court": "ct", "lane": "ln", "circle": "cir",
    "boulevard": "blvd", "blv": "blvd", "place": "pl", "plz": "plaza",
    "terrace": "ter", "trace": "trce", "parkway": "pkwy", "pkway": "pkwy",
    "highway": "hwy", "square": "sq", "loop": "loop", "run": "run",
    "walk": "walk", "way": "way", "point": "pt", "ridge": "rdg", "creek": "crk",
    "crossing": "xing", "junction": "jct", "estate": "est",
    "harbor": "hbr", "harbour": "hbr", "landing": "lndg", "mount": "mt",
    "view": "vw", "bend": "bnd", "center": "ctr", "centre": "ctr",
    "beach": "bch", "heights": "hts", "brook": "brk", "burg": "bg",
    "field": "fld", "ford": "frd", "gate": "gt", "grove": "grv",
    "haven": "hvn", "island": "isl", "knoll": "knl", "lake": "lk",
    "manor": "mnr", "meadow": "mdw", "mill": "ml", "mission": "msn",
    "mountain": "mtn", "ramp": "rp", "ranch": "rnch", "river": "rvr",
    "spring": "spg", "station": "sta", "summit": "smit", "valley": "vly",
    "village": "vlg", "port": "pt",
}
_DIRECTIONALS = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}
# Multi-word states have to be collapsed before tokenising, otherwise "north
# carolina" becomes two tokens and never matches "nc".
_STATES = {
    "north carolina": "nc", "virginia": "va", "west virginia": "wv",
    "south carolina": "sc", "new york": "ny", "new jersey": "nj",
    "maryland": "md", "delaware": "de", "pennsylvania": "pa",
}


def door_key(value) -> str:
    """Normalised, punctuation-free key identifying one physical door.

    Lowercase, strip everything non-alphanumeric, collapse whitespace, then
    canonicalise states, street types and directionals. Deliberately does NOT
    expand or remove unit designators, and does NOT rejoin compound street
    names: over-normalising risks merging two genuinely different units in a
    townhouse development, and a letter to a neighbour is cheaper than a lost
    lead. The cost is that "Rossburn Ct" will not match "Ross Burn Ct" -- if
    your exclude file spells it that way, fix the file.
    """
    s = re.sub(r"[^a-z0-9 ]", " ", str(value or "").lower())
    s = re.sub(r"\s+", " ", s).strip()
    for full, abbr in _STATES.items():
        s = s.replace(full, abbr)
    out = []
    for tok in s.split(" "):
        tok = _DIRECTIONALS.get(tok) or _STREET_TYPES.get(tok) or tok
        out.append(tok)
    return " ".join(out)


# ── database ────────────────────────────────────────────

# ADDRESS_ONLY_SQL re-queried with the columns needed to screen, not to render.
# It is copied rather than imported-and-patched because test_mail_letters.py
# pins that constant's exact text; mutating it in place would break that test.
# The %% doubling is required by psycopg paramstyle, same as upstream.
SELECT_SQL = """
    SELECT id, name, address, project_type, source, status, created_at,
           COALESCE(phone, '') AS phone, COALESCE(email, '') AS email,
           COALESCE(do_not_contact, FALSE) AS do_not_contact
    FROM leads
    WHERE company = %(company)s
      AND status = 'new'
      AND COALESCE(source, '') <> 'sam-gov'
      AND address IS NOT NULL AND BTRIM(address) <> ''
      AND (email IS NULL OR BTRIM(email) = '' OR LOWER(email) LIKE '%%@lead.local')
      AND (phone IS NULL OR BTRIM(phone) = ''
           OR LOWER(BTRIM(phone)) IN ('unknown', 'n/a', 'none', '-'))
      AND COALESCE(do_not_contact, FALSE) = FALSE
    ORDER BY id
"""

# do_not_contact arrives from bizstack_leads/schema.py, which construction_main
# applies at startup (:702). A database that has never run the app will not
# have the column, and the query would fail outright rather than degrade.
# Probe once, then drop the clause and say so.
DNC_CLAUSE = "COALESCE(do_not_contact, FALSE) = FALSE"


def _has_dnc(cur) -> bool:
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'leads' AND column_name = 'do_not_contact'
        LIMIT 1
    """)
    return cur.fetchone() is not None


def connect():
    import psycopg
    from psycopg.rows import dict_row

    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise SystemExit("DATABASE_URL is not set; cannot read leads.")
    return psycopg.connect(url, row_factory=dict_row)


def fetch(db, company: str, only_ids=None) -> list:
    with db.cursor() as cur:
        sql = SELECT_SQL
        if not _has_dnc(cur):
            sql = sql.replace("      AND " + DNC_CLAUSE + "\n", "")
        cur.execute(sql, {"company": company})
        rows = [dict(r) for r in cur.fetchall()]
    if only_ids:
        want = {int(i) for i in only_ids}
        rows = [r for r in rows if int(r["id"]) in want]
    return rows


def dedupe(rows: list) -> tuple:
    """Collapse repeat permit rows at one door. Reuses mail_letters' own rule."""
    return mail_letters.dedupe_by_address(rows)


def load_exclusions(paths) -> set:
    """Normalised door keys from any CSV with an address column.

    Needed because a batch mailed by hand leaves no trace the app can see.
    mail_letters.py is offline by design and never writes comms_logs, and Lob
    has never actually sent -- so the Sept 2026 print batch is invisible to
    contact_policy_allows, which would otherwise happily mail the same 58
    doors again. Point this at that old manifest, a USPS mail list, or any
    spreadsheet of addresses already mailed.
    """
    keys = set()

    for p in paths or []:
        path = Path(p)
        if not path.exists():
            raise SystemExit(f"exclude file not found: {path}")
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            cols = {c.lower(): c for c in (reader.fieldnames or [])}
            col = next((cols[c] for c in ("address", "property_address", "street")
                        if c in cols), None)
            if not col:
                raise SystemExit(
                    f"{path} has no address-like column. Saw: {reader.fieldnames}")
            n = 0
            for row in reader:
                k = door_key(row.get(col))
                if k:
                    keys.add(k)
                    n += 1
        print(f"  {n} address(es) loaded from {path}")
    return keys


# ── screening ───────────────────────────────────────────

def zip_of(address: str) -> str:
    m = ZIP_RE.search(mail_letters._clean(address))
    return m.group(1) if m else ""


def screen(db, rows: list, require_person: bool, quiet: bool = False,
           exclude: set = None):
    """Split rows into (keep, drops). Drops carry a reason, always.

    Every exclusion is reported. A silently shorter list is how a batch ends up
    mailing a customer twice, or mailing a federal solicitation, with nothing in
    the output to say so.
    """
    keep, drops = [], []

    def drop(row, why):
        drops.append({"id": row.get("id"),
                      "address": mail_letters._clean(row.get("address")),
                      "name": mail_letters._clean(row.get("name")),
                      "reason": why})

    for row in rows:
        name = mail_letters._clean(row.get("name"))
        address = mail_letters._clean(row.get("address"))
        zc = zip_of(address)

        if row.get("do_not_contact"):
            drop(row, "do_not_contact flag is set")
            continue
        if exclude and door_key(address) in exclude:
            drop(row, "address is in an exclude file (already mailed)")
            continue
        # format_recipient is the single definition of "is this a person name"
        # in this codebase. Reusing it means the screen and the letter can
        # never disagree about who counts as a name.
        if require_person and mail_letters.format_recipient(name) == "the homeowner":
            drop(row, f"no person name (name is {name!r}, a city or a role)")
            continue
        if not zc:
            drop(row, "no ZIP in the address -- USPS cannot deliver it")
            continue

        # The opt-out promise on every letter is enforced here.
        # check_outbound returns (allowed, reason) -- not (blocked, reason).
        try:
            import suppression
            allowed, why = suppression.check_outbound(
                db, suppression.CHANNEL_MAIL, address,
                lead_id=row.get("id"), source=mail_letters._clean(row.get("source")),
                email=mail_letters._clean(row.get("email")),
                phone=mail_letters._clean(row.get("phone")),
                name=name, zipcode=zc)
        except Exception as exc:
            # Fail closed, matching auto_reply.py: an unreachable suppression
            # check must not be read as consent.
            drop(row, f"suppression check unavailable ({type(exc).__name__}: {exc})")
            continue
        if not allowed:
            drop(row, f"blocked: {why}")
            continue

        row["_zip"] = zc
        keep.append(row)

    return keep, drops


def print_drops(drops: list) -> None:
    """Summarise exclusions, grouped by reason.

    Called by each command once every drop is known, not from inside screen():
    the geocode failures are appended after screening finishes, and a summary
    printed at the end of screen() would silently omit them.
    """
    if not drops:
        return
    by_reason = {}
    for d in drops:
        by_reason.setdefault(d["reason"], []).append(d)
    for why, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"  dropped {len(items):>4}  {why}")
        for d in items[:3] if len(items) <= 4 else []:
            print(f"           #{d['id']} {d.get('address', '')[:62]}")


# ── geo ─────────────────────────────────────────────────

def haversine(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def geocode_all(rows: list, cache_path: Path, quiet: bool = False) -> list:
    """Attach lat/lng. Disk-cached so a re-run costs no network and a bad
    geocode does not turn 60 addresses into 60 failed lookups on every try."""
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except Exception:
            cache = {}

    missing = []
    for r in rows:
        key = mail_letters._clean(r.get("address")).lower()
        if key in cache and cache[key].get("ok"):
            r["_lat"], r["_lng"] = cache[key]["lat"], cache[key]["lng"]
            r["_geocode_source"] = cache[key].get("source", "cache")
        else:
            missing.append(r)

    if missing and not quiet:
        print(f"  geocoding {len(missing)} address(es); cache holds {len(rows) - len(missing)}...")

    for r in missing:
        key = mail_letters._clean(r.get("address")).lower()
        hit = open_geo.geocode(mail_letters._clean(r.get("address")))
        if hit.get("ok"):
            cache[key] = {"ok": True, "lat": hit["lat"], "lng": hit["lng"],
                          "source": hit.get("source", "")}
            r["_lat"], r["_lng"] = float(hit["lat"]), float(hit["lng"])
            r["_geocode_source"] = hit.get("source", "")
        else:
            cache[key] = {"ok": False, "error": hit.get("error", "")}
            r["_geo_error"] = hit.get("error", "no match")

    cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True))
    return [r for r in rows if r.get("_lat") is None and r.get("_lng") is None]


def order_route(points: list, origin_pt) -> list:
    """Nearest-neighbour then 2-opt, open-ended (furthest stop last).

    The owner asked for nearest-first, which the initial pass already gives.
    2-opt is here because nearest-neighbour alone leaves two long crossing legs
    in almost every run of 25, and the difference is felt in a car.

    Straight-line, not driving distance: 2-opt over real road miles means an
    OSRM call per candidate edge, which is thousands of requests for one
    batch. Haversine is the standard choice for a first-order pass and the
    ordering it produces is within a few percent of the road-optimal one.
    """
    if len(points) < 3:
        return list(points)

    def total(order):
        legs = [haversine(origin_pt, (order[0]["_lat"], order[0]["_lng"]))]
        for i in range(len(order) - 1):
            legs.append(haversine((order[i]["_lat"], order[i]["_lng"]),
                                  (order[i + 1]["_lat"], order[i + 1]["_lng"])))
        return sum(legs)

    remaining = sorted(points, key=lambda r: haversine(origin_pt, (r["_lat"], r["_lng"])))
    order, cur = [], origin_pt
    while remaining:
        nxt = min(remaining, key=lambda r: haversine(cur, (r["_lat"], r["_lng"])))
        order.append(nxt)
        remaining.remove(nxt)
        cur = (nxt["_lat"], nxt["_lng"])

    best = total(order)
    improved = True
    while improved:
        improved = False
        # i starts at 1, never 0. Reversing a segment that begins at index 0
        # would move the nearest stop to the back of the route, which is the
        # one thing this is not allowed to do: the owner asked for closest
        # first, and the chunk split is built on that ordering. The last stop
        # is allowed to float -- no stop has a claim on being final.
        for i in range(1, len(order) - 1):
            for j in range(i + 2, len(order) + 1):
                cand = order[:i] + order[i:j][::-1] + order[j:]
                t = total(cand)
                if t < best - 1e-9:
                    order, best, improved = cand, t, True
    return order


# ── maps ────────────────────────────────────────────────

def apple_nav(lat, lng, label: str) -> str:
    q = urllib.parse.urlencode({"daddr": f"{lat},{lng}", "q": label, "dirflg": "d"})
    return "https://maps.apple.com/?" + q


def google_nav(lat, lng) -> str:
    return ("https://www.google.com/maps/dir/?api=1&destination="
            + urllib.parse.quote(f"{lat},{lng}"))


def google_route(stops, origin_pt) -> str:
    """Google Maps caps waypoints, so hand it the first nine and let it chain
    the rest; the numbered list on the route sheet covers the remainder."""
    if not stops:
        return ""
    dest = f"{stops[-1]['_lat']},{stops[-1]['_lng']}"
    wp = "|".join(f"{s['_lat']},{s['_lng']}" for s in stops[:9])
    params = {"api": "1",
              "origin": f"{origin_pt[0]},{origin_pt[1]}",
              "destination": dest,
              "travelmode": "driving"}
    if wp:
        params["waypoints"] = wp
    return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(params)


def kml(stops, origin_pt, name: str) -> str:
    def pin(label, lat, lng):
        return (f'  <Placemark><name>{_xml(label)}</name>'
                f'<Point><coordinates>{lng},{lat},0</coordinates></Point></Placemark>')

    body = pin(f"START {ORIGIN}", origin_pt[0], origin_pt[1])
    body += "\n".join(pin(f"{i + 1}. {mail_letters._clean(s.get('name'))}",
                          s["_lat"], s["_lng"]) for i, s in enumerate(stops))
    line = "".join(f"<gx:coord>{x}</gx:coord>" for x in
                   [f"{origin_pt[1]} {origin_pt[0]} 0"]
                   + [f"{s['_lng']} {s['_lat']} 0" for s in stops])
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">\n'
            f'  <Document><name>{_xml(name)}</name>\n'
            f'    <Style id="r"><LineStyle><color>ff2b6cb0</color><width>4</width></LineStyle></Style>\n'
            f'    <Placemark><name>Route</name><styleUrl>#r</styleUrl>\n'
            f'      <LineString><tessellate>1</tessellate><gx:Track>{line}</gx:Track></LineString>\n'
            f'    </Placemark>\n{body}\n  </Document>\n</kml>\n')


def _xml(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


ROUTE_CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#f4f5f7;color:#111827;-webkit-text-size-adjust:100%}
header{background:#111827;color:#fff;padding:18px 16px;position:sticky;top:0;z-index:5}
header h1{margin:0 0 4px;font-size:19px}
header .sub{color:#9ca3af;font-size:13px}
.bar{padding:10px 16px;background:#fff;border-bottom:1px solid #e5e7eb;display:flex;gap:8px;
     flex-wrap:wrap;position:sticky;top:78px;z-index:4}
.bar a{flex:1;text-align:center;background:#2563eb;color:#fff;text-decoration:none;
       padding:11px;border-radius:8px;font-weight:600;font-size:15px;min-width:120px}
.bar a.g{background:#0f9d58}
ol{list-style:none;margin:0;padding:0}
li{background:#fff;border-bottom:1px solid #e5e7eb;padding:13px 16px;display:flex;gap:13px;
   align-items:flex-start}
li.done{opacity:.45}
.num{flex:0 0 34px;height:34px;border-radius:50%;background:#111827;color:#fff;
     display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px}
.who{flex:1;min-width:0}
.name{font-weight:600;font-size:15px}
.addr{color:#4b5563;font-size:14px;word-break:break-word}
.meta{color:#9ca3af;font-size:12px;margin-top:3px}
.go{flex:0 0 auto;display:flex;flex-direction:column;gap:5px}
.go a{font-size:12px;background:#eff6ff;color:#1d4ed8;text-decoration:none;padding:7px 11px;
      border-radius:6px;font-weight:600;text-align:center;white-space:nowrap}
footer{padding:22px 16px;color:#6b7280;font-size:13px;text-align:center}
"""


def route_html(stops, origin_pt, chunk_no: int, total_chunks: int) -> str:
    rows = []
    for i, s in enumerate(stops, 1):
        label = mail_letters._clean(s.get("address"))
        name = mail_letters.format_recipient(s.get("name"))
        rows.append(
            f'<li><div class="num">{i}</div><div class="who">'
            f'<div class="name">{_xml(name)}</div>'
            f'<div class="addr">{_xml(label)}</div>'
            f'<div class="meta">ZIP {s["_zip"]} · lead #{s["id"]} · '
            f'{s.get("_miles_from_origin", 0):.1f} mi out</div></div>'
            f'<div class="go">'
            f'<a href="{apple_nav(s["_lat"], s["_lng"], label)}">Navigate</a>'
            f'<a class="g" href="{google_nav(s["_lat"], s["_lng"])}">Google</a>'
            f'</div></li>')

    title = f"Mail run {chunk_no} of {total_chunks}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{ROUTE_CSS}</style></head><body>
<header>
  <h1>{title}</h1>
  <div class="sub">{len(stops)} stop(s) · from {_xml(ORIGIN)}</div>
</header>
<div class="bar">
  <a href="{google_route(stops, origin_pt)}">Full route in Google Maps</a>
</div>
<ol>{"".join(rows)}</ol>
<footer>Buildstack Construction Co. · {_xml(datetime.now().strftime("%B %-d, %Y"))}<br>
Tap a stop to mark it done. Order is nearest-first, then optimised within the chunk.</footer>
</body></html>
"""


# ── build ───────────────────────────────────────────────

def build(out_dir: Path, company: str, chunk_size: int, only_ids=None,
          require_person: bool = True, drive_times: bool = True,
          exclude_files=None) -> dict:
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SystemExit(
            f"{out_dir} already has files in it.\n"
            "  Use a new folder per batch so a rebuild cannot silently overwrite\n"
            "  a batch that was already printed, or mix two runs together."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    exclude = load_exclusions(exclude_files)

    with connect() as db:
        import suppression
        try:
            suppression.ensure_schema(db)
            db.commit()
        except Exception as exc:
            print(f"  note: suppression schema unavailable ({type(exc).__name__}: {exc}); "
                  "the batch will be screened on name/ZIP only", file=sys.stderr)

        rows = fetch(db, company, only_ids)
        rows, folded = dedupe(rows)
        print(f"  {len(rows)} unique door(s) after folding {len(folded)} repeat permit row(s)")

        keep, drops = screen(db, rows, require_person, exclude=exclude)

        ungeo = geocode_all(keep, out_dir / "geocode_cache.json")
        for r in ungeo:
            drops.append({"id": r.get("id"), "address": mail_letters._clean(r.get("address")),
                          "name": mail_letters._clean(r.get("name")),
                          "reason": f"could not geocode: {r.get('_geo_error', 'no match')}"})
        keep = [r for r in keep if "_lat" in r]

    home = open_geo.geocode(ORIGIN)
    if not home.get("ok"):
        raise SystemExit(f"Cannot geocode the route origin ({ORIGIN!r}): {home.get('error')}\n"
                         "  Set MAIL_ROUTE_ORIGIN to a real address and retry.")
    origin_pt = (float(home["lat"]), float(home["lng"]))

    for r in keep:
        r["_miles_from_origin"] = haversine(origin_pt, (r["_lat"], r["_lng"]))

    # Nearest-first across the whole batch, then a local reorder inside each
    # chunk. Ordering globally before chunking is what makes chunk 1 the 25
    # closest doors; ordering per chunk independently would leave every chunk
    # reaching across the map.
    keep.sort(key=lambda r: r["_miles_from_origin"])
    print_drops(drops)
    chunks = [keep[i:i + chunk_size] for i in range(0, len(keep), chunk_size)]
    chunks = [order_route(c, origin_pt) for c in chunks]

    manifest_rows = []
    for idx, chunk in enumerate(chunks, 1):
        sub = out_dir / f"run-{idx:02d}"
        sub.mkdir(parents=True, exist_ok=True)

        prev_pt, prev = origin_pt, None
        run_manifest = []
        for stop_no, r in enumerate(chunk, 1):
            stem = f"{int(r['id']):05d}-{mail_letters.safe_name(r)}"
            path = sub / f"{stem}.pdf"
            path.write_bytes(mail_letters.render_letter(r))

            leg_miles = leg_minutes = ""
            if drive_times and prev is not None:
                leg = open_geo.route({"lat": prev[0], "lng": prev[1]},
                                     {"lat": r["_lat"], "lng": r["_lng"]})
                if leg.get("ok"):
                    leg_miles, leg_minutes = leg["miles"], leg["minutes"]

            row_out = {
                "run": idx, "stop": stop_no,
                "id": r["id"],
                "name": mail_letters._clean(r.get("name")),
                "address": ", ".join(mail_letters.address_lines(r.get("address"))),
                "zip": r["_zip"],
                "miles_from_origin": f"{r['_miles_from_origin']:.1f}",
                "leg_miles": leg_miles, "leg_minutes": leg_minutes,
                "lat": r["_lat"], "lng": r["_lng"],
                "project_type": mail_letters._clean(r.get("project_type")),
                "source": mail_letters._clean(r.get("source")),
                "status": mail_letters._clean(r.get("status")),
                "pdf": f"{sub.name}/{path.name}",
                "apple_nav": apple_nav(r["_lat"], r["_lng"],
                                       ", ".join(mail_letters.address_lines(r.get("address")))),
            }
            manifest_rows.append(row_out)
            run_manifest.append(row_out)
            prev_pt = (r["_lat"], r["_lng"])
            prev = prev_pt

        (sub / "route.html").write_text(route_html(chunk, origin_pt, idx, len(chunks)))
        (sub / "route.kml").write_text(kml(chunk, origin_pt, f"Mail run {idx}"))

        # Summarise from the run's own manifest rows. `chunk` holds lead rows,
        # which carry _miles_from_origin and no leg_minutes at all.
        miles = [float(m["miles_from_origin"]) for m in run_manifest]
        legs = [int(m["leg_minutes"]) for m in run_manifest if m["leg_minutes"] != ""]
        print(f"  run {idx}: {len(chunk)} letter(s) in {sub.name}/  "
              f"nearest {min(miles):.1f} mi, furthest {max(miles):.1f} mi"
              + (f", ~{sum(legs)} min driving" if legs else ""))

    if manifest_rows:
        mf = out_dir / "manifest.csv"
        with mf.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(manifest_rows[0].keys()))
            w.writeheader()
            w.writerows(manifest_rows)
        # lob_send.load_manifest reads this and plans from it, so keep its
        # required columns present even though we write more.
        print(f"\n  manifest: {mf} ({len(manifest_rows)} piece(s))")

    (out_dir / "dropped.json").write_text(json.dumps(
        {"generated": datetime.now().isoformat(timespec="seconds"),
         "company": company, "origin": ORIGIN, "chunk_size": chunk_size,
         "kept": len(manifest_rows), "dropped": drops,
         "folded_duplicates": [d[0].get("id") for d in folded]}, indent=1))

    summary = {
        "out": str(out_dir), "pieces": len(manifest_rows), "runs": len(chunks),
        "dropped": len(drops), "origin": ORIGIN,
        "chunks": [{"run": i + 1, "stops": len(c)} for i, c in enumerate(chunks)],
    }
    print(f"  dropped {len(drops)} — see dropped.json for the reason on each")
    return summary


# ── preflight ───────────────────────────────────────────

def preflight(out_dir: Path) -> tuple:
    """Every check that must pass before postage is spent. Returns (problems, notes)."""
    problems, notes = [], []

    mf = out_dir / "manifest.csv"
    if not mf.exists():
        return ([f"no manifest.csv in {out_dir} — run `build` first"], [])

    with mf.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return (["manifest.csv is empty"], [])

    for field in ("id", "address", "pdf"):
        if field not in rows[0]:
            problems.append(f"manifest is missing the {field!r} column lob_send needs")
    if problems:
        return (problems, notes)

    # Letterhead. Without a return street a bounce cannot be actioned.
    if not mail_letters.BRAND["street"]:
        problems.append("LETTER_BRAND_STREET is empty — every letter says so in its footer")
    if not mail_letters.BRAND["city"]:
        problems.append("LETTER_BRAND_CITYSTATEZIP is empty")

    # The number on cold mail is the business line. .env.example still sets the
    # personal mobile, and mail_letters.py reads that var verbatim.
    for field in ("phone", "email"):
        val = mail_letters.BRAND.get(field, "")
        for frag in PERSONAL_PHONE_FRAGMENTS:
            if frag in val:
                problems.append(
                    f"LETTER_BRAND_{field.upper()} is {val!r}, which contains {frag} — "
                    "the owner's personal number must not go on cold mail. Set it to "
                    "the business line (757) 908-7121 and rebuild.")

    if not mail_letters.BRAND.get("email"):
        problems.append("LETTER_BRAND_EMAIL is empty — the letter's opt-out names that "
                        "address, so a blank one makes the opt-out undeliverable")

    # Deliverability, decided by the same parser lob_send will use.
    import lob_send
    bad, seen = [], {}
    for r in rows:
        parsed = lob_send.parse_address(r["address"])
        if not parsed.get("ok"):
            bad.append(f"#{r['id']} {r['address'][:52]} — {parsed.get('why', 'unparsable')}")
            continue
        if not (out_dir / r["pdf"]).exists():
            bad.append(f"#{r['id']} letter file is missing: {r['pdf']}")
        key = parsed["address_line1"].lower()
        seen.setdefault(key, []).append(r["id"])
    for key, ids in seen.items():
        if len(ids) > 1:
            bad.append(f"duplicate address {key!r} appears {len(ids)}x (ids {','.join(ids)})")

    if bad:
        problems.append(f"{len(bad)} piece(s) would not be deliverable:")
        problems += [f"    {b}" for b in bad[:10]]
        if len(bad) > 10:
            problems.append(f"    ... and {len(bad) - 10} more")

    notes.append(f"{len(rows)} piece(s)")
    try:
        import lob_send
        notes.append(f"estimated postage ~${len(rows) * lob_send.COST_PER_PIECE:,.2f} "
                     f"at ${lob_send.COST_PER_PIECE}/piece")
    except Exception:
        pass
    if not os.getenv("LOB_API_KEY"):
        notes.append("LOB_API_KEY is not set — build is fine, fire will refuse")
    return (problems, notes)


# ── fire ────────────────────────────────────────────────

def fire(out_dir: Path, limit, assume_yes: bool) -> int:
    problems, notes = preflight(out_dir)
    print("\nPRE-FLIGHT")
    for n in notes:
        print(f"  ok    {n}")
    for p in problems:
        print(f"  BLOCK {p}")
    if problems:
        print("\nRefusing to transmit. Fix the above, rebuild, and try again.")
        return 1

    if not os.getenv("LOB_API_KEY"):
        print("\nRefusing to transmit: LOB_API_KEY is not set.")
        return 1

    pieces = len(list(csv.DictReader((out_dir / "manifest.csv").open(encoding="utf-8"))))
    capped = min(limit, pieces) if limit else pieces

    print("\nThis is the irreversible step. Postal mail cannot be recalled.")
    print(f"  {capped} piece(s) will print, fold, and enter the mail stream.")
    if not assume_yes:
        print("\nType FIRE to confirm, anything else to abort: ", end="", flush=True)
        try:
            answer = sys.stdin.readline().strip()
        except KeyboardInterrupt:
            print("\nAborted.")
            return 1
        if answer != "FIRE":
            print("Aborted.")
            return 1

    cmd = [sys.executable, str(Path(__file__).with_name("lob_send.py")),
           "--out", str(out_dir), "--send"]
    if limit:
        cmd += ["--limit", str(limit)]
    print("\n$ " + " ".join(cmd) + "\n")
    rc = subprocess.run(cmd, cwd=str(Path(__file__).parent)).returncode
    if rc != 0:
        print("\nlob_send reported failures — see lob_send_report.json before re-running. "
              "Re-sending a piece that already went out is the expensive mistake here.")
    return rc


# ── main ────────────────────────────────────────────────

def cmd_list(args) -> int:
    print(f"Route origin: {ORIGIN}\n")
    exclude = load_exclusions(args.exclude)
    with connect() as db:
        import suppression
        try:
            suppression.ensure_schema(db)
            db.commit()
        except Exception as exc:
            print(f"  note: suppression schema unavailable ({type(exc).__name__}: {exc})",
                  file=sys.stderr)
        rows = fetch(db, args.company, args.id)
        rows, folded = dedupe(rows)
        print(f"{len(rows)} unique door(s) after folding {len(folded)} repeat permit row(s)\n")
        keep, drops = screen(db, rows, not args.any_name, exclude=exclude)

    ungeo = geocode_all(keep, Path(os.getenv("MAIL_GEO_CACHE", "/tmp/mail_batch_geo.json")),
                        quiet=False)
    for r in ungeo:
        drops.append({"id": r.get("id"), "address": mail_letters._clean(r.get("address")),
                      "reason": f"could not geocode: {r.get('_geo_error', 'no match')}"})
    # geocode_all returns only the rows it could NOT place; the failures stay
    # in `keep`, and sorting them would KeyError on a missing _lat.
    keep = [r for r in keep if "_lat" in r]

    home = open_geo.geocode(ORIGIN)
    if not home.get("ok"):
        print(f"\nCannot geocode origin ({ORIGIN}): {home.get('error')}")
        return 1
    origin_pt = (float(home["lat"]), float(home["lng"]))

    for r in keep:
        r["_miles_from_origin"] = haversine(origin_pt, (r["_lat"], r["_lng"]))

    keep.sort(key=lambda r: r["_miles_from_origin"])
    print(f"\n{len(keep)} letter(s) would be mailed, nearest first:\n")
    print_drops(drops)
    for i, r in enumerate(keep, 1):
        miles = haversine(origin_pt, (r["_lat"], r["_lng"]))
        print(f"  {i:>4}  {miles:>5.1f} mi  {r['_zip']}  "
              f"{mail_letters.format_recipient(r.get('name'))[:26]:<26} "
              f"{mail_letters._clean(r.get('address'))[:58]}")
    if keep:
        # keep[-1] is the furthest, because keep is sorted by distance.
        furthest = haversine(origin_pt, (keep[-1]["_lat"], keep[-1]["_lng"]))
        print(f"\n  {len(keep)} piece(s); furthest stop is {furthest:.1f} mi out")
        print(f"  chunks of {args.chunk_size}: "
              f"{', '.join(str(len(keep[i:i + args.chunk_size])) for i in range(0, len(keep), args.chunk_size))}")
    return 0


def mark(out_dir: Path, company: str, dry_run: bool) -> int:
    """Record a batch as mailed, so no future batch mails the same doors again.

    Run this AFTER the batch has actually gone out — by Lob, or by hand once
    the print shop has taken it.

    Two records, because they do different jobs:

      * a 'letter' touch in comms_logs, which is what the app's own contact
        policy reads and which flips leads.status off 'new';
      * a suppression entry keyed on name+ZIP with channel=mail.

    The second is the one that actually prevents a repeat. contact_policy_allows
    permits a follow-up touch after LEAD_RETOUCH_DAYS (5), so a recorded letter
    from September is still mailable in October. A KIND_DNC suppression entry
    blocks the mail channel outright, which is the correct outcome for somebody
    who has already had a letter from us and was never recorded as having
    responded.
    """
    mf = out_dir / "manifest.csv"
    if not mf.exists():
        print(f"no manifest.csv in {out_dir}", file=sys.stderr)
        return 1
    with mf.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("manifest.csv is empty", file=sys.stderr)
        return 1

    batch = out_dir.name
    if dry_run:
        print(f"DRY RUN — would mark {len(rows)} lead(s) from {batch} as mailed:")
        for r in rows[:20]:
            print(f"  lead {r['id']:>5}  {r.get('zip', '')}  {r['address'][:58]}")
        if len(rows) > 20:
            print(f"  ... and {len(rows) - 20} more")
        return 0

    import auto_reply
    import suppression

    touched, suppressed, failed = 0, 0, []
    with connect() as db:
        try:
            suppression.ensure_schema(db)
            db.commit()
        except Exception as exc:
            print(f"  note: suppression schema unavailable ({type(exc).__name__}: {exc}); "
                  "only the touch record will be written, and it does NOT block a repeat",
                  file=sys.stderr)

        for r in rows:
            lid = r.get("id")
            try:
                res = auto_reply.record_manual_touch(
                    db, int(lid), "letter", f"mailed in batch {batch}")
                if res.get("ok"):
                    touched += 1
            except Exception as exc:
                failed.append(f"lead {lid}: touch {type(exc).__name__}: {exc}")

            zc = r.get("zip") or zip_of(r.get("address", ""))
            if not zc:
                continue
            # suppression.add() takes the CONNECTION, not a cursor: it calls
            # db.cursor(), db.commit() and db.rollback() itself, and swallows its
            # own failures into an {"ok": False} dict rather than raising.
            # It also rolls the connection back on error, so the touch above
            # must already be committed (record_manual_touch does commit).
            res = suppression.add(
                db, suppression.KIND_DNC, suppression.VALUE_NAME_ZIP,
                suppression.normalize_name_zip(r.get("name", ""), zc),
                channel=suppression.CHANNEL_MAIL,
                reason=f"direct mail batch {batch}",
                source="mail_batch", actor="owner")
            if res.get("ok"):
                suppressed += 1
            else:
                failed.append(f"lead {lid}: suppress: {res.get('error', 'unknown')}")

    print(f"marked {len(rows)} piece(s) from {batch}")
    print(f"  comms_logs touch : {touched}")
    print(f"  mail suppression : {suppressed}")
    if failed:
        print(f"  {len(failed)} failure(s):")
        for f in failed[:10]:
            print(f"    {f}")
    print("\nThis batch is now excluded from future mail runs. To undo, remove the "
          "rows from suppression_list by hand.")
    return 0 if not failed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--company", default="construction")
        p.add_argument("--id", action="append", help="restrict to lead id (repeatable)")
        p.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK)
        p.add_argument("--any-name", action="store_true",
                       help="also mail leads whose name is a city/role, addressed "
                            "'Dear the homeowner'")
        p.add_argument("--exclude", action="append", metavar="CSV",
                       help="CSV of addresses already mailed; those doors are dropped "
                            "(repeatable)")

    p_list = sub.add_parser("list", help="show who would be mailed and who was dropped")
    common(p_list)
    p_list.set_defaults(func=cmd_list)

    p_build = sub.add_parser("build", help="render letters, manifest and route sheets")
    common(p_build)
    p_build.add_argument("--out", required=True, help="output folder for this batch")
    p_build.add_argument("--no-drive-times", action="store_true",
                         help="skip OSRM per-leg driving times (faster, no network)")
    p_build.set_defaults(func=lambda a: build(
        Path(a.out), a.company, a.chunk_size, a.id, not a.any_name,
        not a.no_drive_times, a.exclude))

    p_mark = sub.add_parser("mark", help="record a built batch as mailed (after it ships)")
    p_mark.add_argument("--out", required=True, help="folder produced by `build`")
    p_mark.add_argument("--company", default="construction")
    p_mark.add_argument("--dry-run", action="store_true")
    p_mark.set_defaults(func=lambda a: mark(Path(a.out), a.company, a.dry_run))

    p_fire = sub.add_parser("fire", help="transmit a built batch via Lob")
    p_fire.add_argument("--out", required=True, help="folder produced by `build`")
    p_fire.add_argument("--limit", type=int, help="send at most N pieces")
    p_fire.add_argument("--yes", action="store_true",
                        help="skip the typed confirmation (for scripted use only)")
    p_fire.set_defaults(func=lambda a: fire(Path(a.out), a.limit, a.yes))

    args = ap.parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
