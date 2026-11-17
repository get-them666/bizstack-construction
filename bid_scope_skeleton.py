"""Turn an uploaded spec book into a takeoff skeleton to price.

An interior fit-out cannot be bid off the demolition plan alone. Provost's
scope note on the Chase First Colonial job said so outright: flooring, walls
and finishes are in, not just the demo. Which trades are actually in the job is
written down in the spec book -- CSI section numbers on the uploaded files --
so that is the source of this skeleton rather than a remembered template.

Two things this module deliberately does not do:

1. It does not invent money. Every line lands with no quantity and no rate, so
   the takeoff reads as a checklist of trades to price, not as an estimate.
2. It does not touch a line the owner has already worked on. `seed_bid` only
   ever INSERTs, and it skips any description already on the bid -- including
   removed ones, so re-running cannot resurrect a line that was taken out on
   purpose.
"""
from __future__ import annotations

import os
import re

import bid_costing

# "033505 - CURING, SEALING..." / "072100 _ THERMAL INSULATION_v25.1"
_SECTION = re.compile(r"^(\d{6})\s*[-_]\s*(.+)$")
# A six-digit number followed by nothing but a space is usually a project
# number on a plan set ("254068  Hilltop Colonial Beach ..._BID SET"), not a
# section. Only the division/class-level numbers ending in 00 are accepted that
# way (270000, 099100), where there is no ambiguity worth protecting against.
_LOOSE_SECTION = re.compile(r"^(\d{6})\s+(.+)$")
# Trailing spec-book version stamps: v20.3, V20.7r, 25.1, 20.1r1.
_VERSION = re.compile(r"[_\s]*v?\d{1,2}\.\d+(?:r\d*)?\s*$", re.I)


def parse_spec_sections(filenames) -> list:
    """Pull CSI section number and title out of uploaded filenames.

    Only files that actually start with a six-digit MasterFormat number count.
    A plan sheet, a photo or a bid form dropped in the same folder is ignored,
    which is why the caller can hand over every file on the bid.
    """
    out, seen = [], set()
    for raw in filenames or []:
        name = os.path.basename(str(raw or ""))
        stem = os.path.splitext(name)[0]
        match = _SECTION.match(stem)
        if not match:
            loose = _LOOSE_SECTION.match(stem)
            if not (loose and loose.group(1).endswith("00")):
                continue
            match = loose
        if not match:
            continue
        number, title = match.group(1), match.group(2)
        title = _VERSION.sub("", title)
        title = re.sub(r"\s*[-_]+\s*$", "", title)
        title = re.sub(r"\s{2,}", " ", title).strip()
        if not title or number in seen:
            continue
        seen.add(number)
        out.append({"number": number, "division": int(number[:2]),
                    "title": title, "filename": name})
    return sorted(out, key=lambda s: s["number"])


# One entry per trade package the owner prices. `csi` holds four-digit class
# prefixes so a package survives a spec revision that renumbers a section
# without changing what the trade is (092200 vs 092216 is still drywall);
# `always` is for the demolition scope, which lives on the demo plan and is
# never in the spec book.
_PACKAGES = (
    # --- Demolition, from the demo plan rather than the spec book ----------
    # These carry no division prefix because there is no CSI section behind
    # them; the scope is on the demolition plan.
    {"label": "Demolition — selective interior, per demolition plan",
     "unit": "sf", "category": "labor", "always": True},
    {"label": "Temporary protection, dust control & negative air",
     "unit": "ls", "category": "labor", "always": True},
    {"label": "Demolition debris haul-off & disposal",
     "unit": "ea", "category": "sub", "always": True},
    {"label": "Final clean & punch list",
     "unit": "ls", "category": "labor", "always": True},

    # --- Division 03: concrete ---------------------------------------------
    {"label": "Concrete forming, reinforcing, cast-in-place & grouting",
     "unit": "cy", "category": "sub", "csi": ("0310", "0320", "0330", "0362")},
    {"label": "Concrete floor curing, sealing & hardening",
     "unit": "sf", "category": "material", "csi": ("0335",)},

    # --- Division 05: metals -----------------------------------------------
    {"label": "Structural steel & metal fabrications",
     "unit": "ton", "category": "sub", "csi": ("0511", "0512", "0550")},
    {"label": "Cold-formed metal framing & exterior steel studs",
     "unit": "sf", "category": "sub", "csi": ("0540", "0541")},

    # --- Division 06: wood, plastics, composites ---------------------------
    {"label": "Rough carpentry, blocking & exterior sheathing",
     "unit": "sf", "category": "labor", "csi": ("0610", "0616")},
    {"label": "Finish carpentry, millwork, cabinets & solid surfacing",
     "unit": "lf", "category": "sub", "csi": ("0620", "0641", "0661")},

    # --- Division 07: thermal & moisture protection ------------------------
    {"label": "Thermal & board insulation",
     "unit": "sf", "category": "material", "csi": ("0721", "0724")},
    {"label": "Weather barriers & self-adhering flashing",
     "unit": "sf", "category": "material", "csi": ("0725", "0765")},
    {"label": "Joint sealants & firestopping",
     "unit": "lf", "category": "material", "csi": ("0792",)},

    # --- Division 08: openings ---------------------------------------------
    {"label": "Hollow metal doors & frames",
     "unit": "ea", "category": "sub", "csi": ("0811",)},
    {"label": "Flush wood doors & door hardware",
     "unit": "ea", "category": "sub", "csi": ("0814", "0871")},
    {"label": "Aluminum storefronts, entrances & glazing",
     "unit": "sf", "category": "sub", "csi": ("0841", "0880")},
    {"label": "Access doors & panels",
     "unit": "ea", "category": "material", "csi": ("0831",)},

    # --- Division 09: finishes. The heart of the scope note. ---------------
    {"label": "Metal stud framing & gypsum board partitions",
     "unit": "sf", "category": "labor", "csi": ("0922", "0929")},
    {"label": "Gypsum board ceilings & soffits",
     "unit": "sf", "category": "labor", "csi": ("0929",)},
    {"label": "Acoustical panel ceilings & grid",
     "unit": "sf", "category": "labor", "csi": ("0951",)},
    {"label": "Thin-set tiling",
     "unit": "sf", "category": "sub", "csi": ("0931",)},
    {"label": "Resilient flooring & wall base",
     "unit": "sf", "category": "labor", "csi": ("0965",)},
    {"label": "Carpet tile",
     "unit": "sf", "category": "sub", "csi": ("0968",)},
    {"label": "Wall coverings",
     "unit": "sf", "category": "labor", "csi": ("0972",)},
    {"label": "Acoustic insulation",
     "unit": "sf", "category": "material", "csi": ("0981",)},
    {"label": "Paint & transparent finishes",
     "unit": "sf", "category": "labor", "csi": ("0991", "0993")},

    # --- Division 10: specialties ------------------------------------------
    {"label": "Wall protection, corner guards & toilet accessories",
     "unit": "ea", "category": "material", "csi": ("1026", "1028")},
    {"label": "Fire extinguishers, cabinets, shelving & sun control",
     "unit": "ea", "category": "material", "csi": ("1044", "1056", "1071", "1022")},

    # --- Division 11: equipment --------------------------------------------
    {"label": "Intercom, ATM anti-theft equipment & appliances",
     "unit": "ea", "category": "sub", "csi": ("1117", "1118", "1131")},

    # --- Division 12: furnishings ------------------------------------------
    {"label": "Roller shades, entrance mat & site furnishings",
     "unit": "ea", "category": "sub", "csi": ("1224", "1248", "1293")},

    # --- Division 22: plumbing ---------------------------------------------
    {"label": "Plumbing rough-in & fixtures",
     "unit": "ls", "category": "sub", "csi": ("2201", "2211", "2213")},
    {"label": "Plumbing insulation",
     "unit": "lf", "category": "material", "csi": ("2207",)},

    # --- Division 23: HVAC --------------------------------------------------
    {"label": "HVAC ductwork, insulation & air distribution",
     "unit": "lb", "category": "sub", "csi": ("2307", "2331", "2333", "2337")},
    {"label": "HVAC equipment — split systems, condensing & energy-recovery units",
     "unit": "ea", "category": "sub", "csi": ("2364", "2372", "2381", "2382")},
    {"label": "Testing, adjusting & balancing",
     "unit": "ls", "category": "sub", "csi": ("2305",)},

    # --- Division 26: electrical -------------------------------------------
    {"label": "Electrical rough-in — conduit, boxes & wire",
     "unit": "ls", "category": "sub", "csi": ("2605",)},
    {"label": "Panels, disconnects, devices & grounding",
     "unit": "ea", "category": "sub", "csi": ("2609", "2620", "2624", "2628")},
    {"label": "Lighting & lighting controls",
     "unit": "ea", "category": "sub", "csi": ("2650",)},

    # --- Division 27: communications ---------------------------------------
    {"label": "Structured cabling — horizontal & backbone",
     "unit": "ea", "category": "sub", "csi": ("2700", "2711", "2713", "2715")},
    {"label": "Cabling pathways, grounding, bonding & labeling",
     "unit": "ls", "category": "sub", "csi": ("2705",)},

    # --- Division 28: electronic safety & security --------------------------
    {"label": "Fire detection & alarm",
     "unit": "ls", "category": "sub", "csi": ("2831",)},
)


def build_skeleton(filenames, include_demo: bool = True) -> list:
    """One line per trade package the uploaded spec book actually covers.

    A package is emitted only when a matching CSI section is on the bid, so the
    skeleton never asks for a price on a trade this job does not contain. The
    four demolition lines are the exception: they come from the demo plan.
    """
    present = {s["number"] for s in parse_spec_sections(filenames)}
    lines, seen = [], set()
    for pkg in _PACKAGES:
        if pkg.get("always"):
            if not include_demo:
                continue
            division = None
        else:
            if not any(n.startswith(p) for n in present for p in pkg["csi"]):
                continue
            division = int(next(n for n in sorted(present)
                                if any(n.startswith(p) for p in pkg["csi"]))[:2])
        label = pkg["label"]
        # The division prefix is a pointer back to the section the trade came
        # from. Demolition has no section, so it stands on its own.
        description = (f"Division {division:02d} — {label}" if division else label)
        key = description.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append({"description": description, "unit": pkg["unit"],
                      "category": pkg["category"], "division": division})
    return lines


def _bid_filenames(conn, bid_id: int, company: str) -> list:
    """Every filename attached to the bid, File Cabinet and job documents.

    Spec books land in the File Cabinet when the owner drops a folder in, but
    several bids arrive with the package on the job-documents side instead.
    Both are read, and a missing table is not an error -- the app creates those
    tables at boot and a fresh database may not have them yet.

    The two tables do not agree on much: bid_files keeps the upload name in
    original_filename and has no company column, bid_documents keeps it in
    filename. Guessing one shape for both is how a query 500s on a live card.
    """
    names: list = []
    for table, column, scoped in (
            ("bid_files", "original_filename", False),
            ("bid_documents", "filename", True)):
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) AS t", (table,))
            row = cur.fetchone()
        if not row or not row["t"]:
            continue
        sql = f"SELECT {column} AS name FROM {table} WHERE bid_id = %s"
        params = (bid_id,)
        if scoped:
            sql += " AND company = %s"
            params = (bid_id, company)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            names.extend(r["name"] for r in cur.fetchall() if r.get("name"))
    return names


def seed_bid(conn, bid_id: int, company: str = "construction",
             filenames=None, include_demo: bool = True) -> dict:
    """Add the skeleton to a bid. INSERT only, and idempotent.

    Returns the counts so a route can tell the owner what happened instead of
    silently dropping forty rows on the card.
    """
    names = filenames if filenames is not None else _bid_filenames(conn, bid_id, company)
    lines = build_skeleton(names, include_demo=include_demo)
    with conn.cursor() as cur:
        cur.execute("SELECT description FROM bid_costs WHERE bid_id = %s AND company = %s",
                    (bid_id, company))
        existing = {(r["description"] or "").strip().lower() for r in cur.fetchall()}

    added = skipped = 0
    for line in lines:
        key = line["description"].strip().lower()
        if key in existing:
            skipped += 1
            continue
        bid_costing.add_cost_line(
            conn, bid_id, category=line["category"], description=line["description"],
            unit=line["unit"], company=company)
        existing.add(key)
        added += 1
    return {"added": added, "skipped": skipped,
            "considered": len(lines), "lines": lines}
