#!/usr/bin/env python3
"""Send the rendered letters through Lob: print, envelope, address, mail.

USPS has no print-and-mail API. It sells postage; the printing, the envelope,
the window addressing and the induction into the mail stream are what you pay
someone else for. Lob is the one with an API for it, and its letter format is
8.5x11 -- which is exactly what mail_letters.py already renders, so the PDFs
from that script go in as-is.

Two things this refuses to do, both of which cost money:

  - Mail the same address twice. The permit feeds emit one row per permit, so a
    house with a deck permit and a roof permit became two leads. The 177 letters
    cover only 63 distinct doors; mailing all of them would put eleven identical
    envelopes in one mailbox and read as desperation. One letter per address, and
    the duplicate leads are recorded as covered by that send.

  - Send an address that cannot be delivered. Lob bills per piece accepted, and
    the 15 leads stored without a ZIP are undeliverable as written. They are
    listed for a human to fix, never sent.

Dry run by default. This spends real money the moment it is not a dry run, so
`--send` is required and nothing happens without it.

    python lob_send.py --out outbox            # what would send, at what cost
    python lob_send.py --out outbox --send     # actually send
    python lob_send.py --out outbox --id 164   # one specific lead

Requires LOB_API_KEY. The account needs a live key and a payment method on file;
there is no way to send from this script without both.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LOB_LETTERS = "https://api.lob.com/v1/letters"

# The return address prints on the letter, and USPS routes undeliverable mail
# back to it. Confirmed by the owner: 701 Dana Dr, Chesapeake, VA 23321.
FROM = {
    "name": os.getenv("LOB_FROM_NAME", "Shaun O'Leary"),
    "company": os.getenv("LOB_FROM_COMPANY", "Buildstack Construction"),
    "address_line1": os.getenv("LOB_FROM_ADDRESS1", "701 Dana Dr"),
    "city": os.getenv("LOB_FROM_CITY", "Chesapeake"),
    "state": os.getenv("LOB_FROM_STATE", "VA"),
    "zip_code": os.getenv("LOB_FROM_ZIP", "23321"),
}

# Lob's published per-piece rates for a black-and-white letter. Startup-tier
# subscription is not required: the Developer tier has no monthly fee and covers
# up to 500 pieces a month, which is above this run. Standard Class is used
# because these are cold prospecting letters with no reply expected inside a
# week, and it is ~$0.23 cheaper per piece than First Class.
COST_PER_PIECE = float(os.getenv("LOB_COST_PER_PIECE", "0.828") or 0.828)
MAIL_TYPE = os.getenv("LOB_MAIL_TYPE", "usps_standard")

# "VA 23320", "VA 23320-1234" or a bare "VA". The ZIP group is optional and so is
# the space before it, because a chunk that is only a state is how these rows
# store a missing ZIP -- requiring the space made a bare "NC" fail to match and
# report "no state" instead of the accurate "no ZIP".
_STATE_ZIP = re.compile(r"^([A-Za-z]{2})(?:\s+(\d{5}(?:-\d{4})?))?$")
_ZIP_ANY = re.compile(r"\b\d{5}(?:-\d{4})?\b")


def parse_address(raw: str) -> dict:
    """'36 Duck Road, Corolla, NC 27948' -> Lob's address fields.

    Both stored shapes appear: permit leads use commas, hand-entered leads use
    newlines, and some rows have the ZIP in a different position than others.
    A row that does not yield a state AND a ZIP comes back with ok=False rather
    than a best guess, because a guessed ZIP sends the letter to the wrong
    county and Lob still charges for it.
    """
    text = str(raw or "").replace("\r\n", " ").replace("\r", " ").replace("\n", ", ")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return {"ok": False, "why": "no address"}

    street = parts[0]
    city = state = zip_code = ""
    for chunk in parts[1:]:
        m = _STATE_ZIP.fullmatch(chunk.strip())
        if m and m.group(1):
            state = m.group(1).upper()
            zip_code = m.group(2) or ""
            continue
        if _ZIP_ANY.search(chunk) and not zip_code:
            city = city or chunk
            found_zip = _ZIP_ANY.search(chunk)
            zip_code = found_zip.group(0)
            continue
        if not city and not state:
            city = chunk
            continue
        # Anything left over belongs on the street line, not the city.
        street = f"{street} {chunk}"

    if not state:
        m2 = _STATE_ZIP.search(text)
        if m2:
            state = m2.group(1).upper()
            zip_code = zip_code or (m2.group(2) or "")
    if not zip_code:
        m3 = _ZIP_ANY.search(text)
        if m3:
            zip_code = m3.group(0)
    if not state:
        return {"ok": False, "why": "no state"}
    if not zip_code:
        return {"ok": False, "why": "no ZIP -- undeliverable as stored", "street": street}
    return {"ok": True, "address_line1": street, "city": city, "state": state,
            "zip_code": zip_code, "name": "Homeowner"}


def load_manifest(out_dir: Path) -> list:
    path = out_dir / "manifest.csv"
    if not path.exists():
        raise SystemExit(f"No manifest.csv in {out_dir}. Run mail_letters.py --out {out_dir} first.")
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def plan(leads: list, out_dir: Path) -> dict:
    """One letter per distinct address, with every duplicate lead attached.

    The winner is the lowest lead id so the choice is stable across runs, and the
    PDF that gets sent is that lead's.
    """
    send, duplicates, rejected = {}, {}, []
    for row in leads:
        raw = (row.get("address") or "").strip()
        key = re.sub(r"\s+", " ", raw).strip().lower()
        parsed = parse_address(raw)
        if not parsed["ok"]:
            rejected.append({"id": row.get("id"), "address": raw, "why": parsed["why"]})
            continue
        pdf = out_dir / (row.get("pdf") or "")
        if not pdf.exists():
            rejected.append({"id": row.get("id"), "address": raw, "why": f"missing {pdf.name}"})
            continue
        entry = {"lead_id": row.get("id"), "pdf": pdf, "address": raw,
                 "to": parsed, "covers": [row.get("id")]}
        if key in send:
            send[key]["covers"].append(row.get("id"))
            duplicates.setdefault(key, []).append(row.get("id"))
        else:
            send[key] = entry
    return {"send": list(send.values()), "rejected": rejected,
            "duplicate_lead_ids": [i for v in duplicates.values() for i in v]}


def _post(pdf: Path, to: dict, description: str) -> dict:
    key = (os.getenv("LOB_API_KEY") or "").strip()
    if not key:
        return {"ok": False, "error": "LOB_API_KEY not set"}
    payload = {
        "file": pdf.name,
        "description": description[:200],
        "to": {"name": to["name"], "address_line1": to["address_line1"],
               "city": to["city"], "state": to["state"], "zip_code": to["zip_code"]},
        "from": dict(FROM),
        "mail_type": MAIL_TYPE,
    }
    boundary = "----bizstacklob"
    body = json.dumps(payload).encode()

    def field(name, value):
        return (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n").encode()

    parts = [field("description", payload["description"]),
             field("to", json.dumps(payload["to"])),
             field("from", json.dumps(payload["from"])),
             field("mail_type", MAIL_TYPE),
             (f"--{boundary}\r\n"
              f'Content-Disposition: form-data; name="file"; filename="{pdf.name}"\r\n'
              "Content-Type: application/pdf\r\n\r\n").encode(),
             pdf.read_bytes(), f"\r\n--{boundary}--\r\n".encode()]
    req = urllib.request.Request(
        LOB_LETTERS, data=b"".join(parts),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "bizstack-construction/1.0 (lob send)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "error": f"HTTP {exc.code}: {body_txt}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, "id": data.get("id"), "status": data.get("status"),
            "tracking": data.get("tracking_events") or [], "date": data.get("date_created")}


def record_touch(lead_ids, channel: str, detail: str) -> str:
    """Mark the mailed leads as touched, using the owner's own path.

    Best effort: the mail is already gone, so a failure here must not stop the
    run or make it look unsent.
    """
    if not lead_ids or not os.getenv("DATABASE_URL"):
        return "skipped (no DATABASE_URL)"
    try:
        import psycopg
        import auto_reply
        with psycopg.connect(os.environ["DATABASE_URL"]) as db:
            n = 0
            for lid in lead_ids:
                if lid and auto_reply.record_manual_touch(db, int(lid), channel, detail).get("ok"):
                    n += 1
        return f"recorded {n} lead(s)"
    except Exception as exc:
        return f"failed: {str(exc)[:160]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="directory holding the PDFs + manifest.csv")
    ap.add_argument("--send", action="store_true", help="actually send (costs money)")
    ap.add_argument("--id", action="append", help="restrict to specific lead id (repeatable)")
    ap.add_argument("--limit", type=int, default=0, help="cap pieces this run")
    ap.add_argument("--no-record", action="store_true", help="do not mark leads as touched")
    args = ap.parse_args()

    out_dir = Path(args.out)
    leads = load_manifest(out_dir)
    if args.id:
        wanted = {str(i) for i in args.id}
        leads = [l for l in leads if str(l.get("id")) in wanted]

    planned = plan(leads, out_dir)
    send, rejected = planned["send"], planned["rejected"]
    dupes = planned["duplicate_lead_ids"]
    if args.limit > 0:
        send = send[: args.limit]

    cost = len(send) * COST_PER_PIECE
    print(f"{'DRY RUN — nothing sent' if not args.send else 'SENDING'}")
    print(f"  leads in manifest : {len(leads)}")
    print(f"  distinct addresses: {len(send)}")
    print(f"  duplicate leads   : {len(dupes)} (covered by the same letter, not mailed again)")
    print(f"  rejected          : {len(rejected)}")
    print(f"  estimated cost    : ${cost:.2f} at ${COST_PER_PIECE}/piece, {MAIL_TYPE}")
    print(f"  return address    : {FROM['address_line1']}, {FROM['city']}, {FROM['state']} {FROM['zip_code']}")
    print()

    for e in send:
        t = e["to"]
        print(f"  MAIL  lead {e['lead_id']:>5}  {t['address_line1']}, {t['city']} {t['state']} {t['zip_code']}"
              + (f"   (+{len(e['covers']) - 1} dup)" if len(e["covers"]) > 1 else ""))
    for r in rejected:
        print(f"  SKIP  lead {r['id']:>5}  {r['address']}  -- {r['why']}")

    if not args.send:
        print(f"\nNothing sent. Re-run with --send to mail {len(send)} piece(s) for ~${cost:.2f}.")
        return 0

    if not (os.getenv("LOB_API_KEY") or "").strip():
        print("\nLOB_API_KEY is not set; cannot send.", file=sys.stderr)
        return 1

    print(f"\nSending {len(send)} piece(s)...")
    results, ok_pieces = [], 0
    for i, e in enumerate(send, 1):
        t = e["to"]
        desc = f"lead {e['lead_id']} · {t['address_line1']}, {t['city']}"
        r = _post(e["pdf"], t, desc)
        results.append({"lead_ids": e["covers"], "address": e["address"],
                        "ok": r.get("ok"), "id": r.get("id"), "error": r.get("error")})
        if r.get("ok"):
            ok_pieces += 1
            print(f"  [{i}/{len(send)}] sent {r.get('id')} lead {e['lead_id']}")
        else:
            print(f"  [{i}/{len(send)}] FAILED lead {e['lead_id']}: {r.get('error')}")
        if i < len(send):
            time.sleep(1.0)  # stay well inside Lob's rate limit

    # Every lead a successful letter reached, including the duplicate rows that
    # were folded into it.
    covered = [lid for r in results if r["ok"] for lid in r["lead_ids"]]
    note = (f"mailed via Lob {ok_pieces} piece(s)"
            if not args.no_record else "not recorded (--no-record)")
    print(f"\nsent {ok_pieces}/{len(send)}  ·  leads covered: {len(covered)}")
    print(f"touch accounting: {record_touch(covered, 'letter', note)}")

    report = out_dir / "lob_send_report.json"
    report.write_text(json.dumps({"results": results, "rejected": rejected,
                                  "covered_lead_ids": covered,
                                  "cost_per_piece": COST_PER_PIECE,
                                  "estimated_cost": round(cost, 2)}, indent=2))
    print(f"report: {report}")
    return 0 if ok_pieces == len(send) else 1


if __name__ == "__main__":
    raise SystemExit(main())
