#!/usr/bin/env python3
"""Render the print backlog into dated batches, and record each batch.

    python render_letter_batches.py --out outbox              # all eligible, 100 per batch
    python render_letter_batches.py --out outbox --limit 30   # a single route
    python render_letter_batches.py --out outbox --dry-run    # what would be written

Why batches: printing is free, postage is not, and delivery is the owner's own
walk. A batch is the unit that gets printed, carried and then confirmed on
/letters -- one click after the last door, and every lead in it is recorded as a
`letter` touch and moved to `contacted`.

Nothing is recorded at render time. Rendering a PDF is not mailing it; the touch
is written only when the owner says the batch was delivered.

A door already in a batch (delivered or not) is skipped, so re-running this does
not print the same house twice.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

import mail_letters as ml


def _mailable(leads: list[dict]) -> tuple[list[dict], list[dict]]:
    good, held = [], []
    for ld in leads:
        if re.search(r"\b\d{5}(?:-\d{4})?\b", ml._clean(ld.get("address"))):
            good.append(ld)
        else:
            held.append(ld)
    return good, held


def _route_key(lead: dict) -> str:
    return " ".join((lead.get("address") or "").lower().split())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="outbox", help="root directory for batch folders")
    ap.add_argument("--company", default="construction")
    ap.add_argument("--batch-size", type=int, default=100)
    ap.add_argument("--limit", type=int, default=0, help="stop after N letters")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-trace-names", action="store_true",
                    help="skip the cached skip-trace names (letters read 'the homeowner')")
    args = ap.parse_args()

    import psycopg
    from psycopg.rows import dict_row
    import letter_batches

    url = os.getenv("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2

    leads = ml.fetch_leads(args.company)
    leads, dropped = ml.dedupe_by_address(leads)
    print(f"{len(leads)} unique door(s) before filtering "
          f"({len(dropped)} duplicate permit row(s) collapsed).")

    with psycopg.connect(url, row_factory=dict_row) as db:
        letter_batches.ensure_schema(db)
        already = letter_batches.batched_lead_ids(db, args.company)
        if already:
            before = len(leads)
            leads = [ld for ld in leads if int(ld["id"]) not in already]
            print(f"Skipped {before - len(leads)} lead(s) already in a batch.")
        if not leads:
            print("Nothing new to print.")
            return 0

        if not args.no_trace_names:
            try:
                named = ml.apply_skip_traced_names(leads)
                print(f"skip-trace names applied to {named}/{len(leads)} letter(s) "
                      f"(cache read only, nothing spent)")
            except Exception as exc:
                print(f"  skip-trace names unavailable ({type(exc).__name__}: {exc}); "
                      f"letters will read 'the homeowner'", file=sys.stderr)

        leads, held = _mailable(leads)
        if held:
            print(f"HOLDING BACK {len(held)} address(es) with no ZIP (USPS cannot deliver):")
            for ld in held[:10]:
                print(f"  #{ld['id']:<6} {ml._clean(ld.get('address'))[:70]}")
            if len(held) > 10:
                print(f"  ... and {len(held) - 10} more")
        if args.limit:
            leads = leads[:args.limit]
        leads.sort(key=_route_key)
        if not leads:
            print("Nothing mailable.")
            return 0

        size = max(1, args.batch_size)
        chunks = [leads[i:i + size] for i in range(0, len(leads), size)]
        print(f"{len(leads)} letter(s) in {len(chunks)} batch(es) of up to {size}.")
        if args.dry_run:
            for n, chunk in enumerate(chunks, start=1):
                print(f"  batch {n}: {len(chunk)} — {ml._clean(chunk[0].get('address'))[:50]} ..")
            print("Dry run: nothing written.")
            return 0

        root = Path(args.out)
        root.mkdir(parents=True, exist_ok=True)
        stamp = date.today().isoformat()
        made = 0
        for n, chunk in enumerate(chunks, start=1):
            out = root / f"batch-{stamp}-{n:02d}"
            out.mkdir(parents=True, exist_ok=True)
            rows = []
            for ld in chunk:
                stem = f"{int(ld['id']):05d}-{ml.safe_name(ld)}"
                path = out / f"{stem}.pdf"
                try:
                    path.write_bytes(ml.render_letter(ld))
                except Exception as exc:
                    print(f"  FAILED #{ld['id']} {ml.safe_name(ld)}: {exc}", file=sys.stderr)
                    continue
                rows.append({
                    "id": int(ld["id"]),
                    "addressee": ml.format_recipient(ld.get("name")),
                    "greeting": ml.greeting(ld.get("name")),
                    "name": ml._clean(ld.get("name")),
                    "address": ", ".join(ml.address_lines(ld.get("address"))),
                    "status": ml._clean(ld.get("status")),
                    "pdf": path.name,
                })
            if not rows:
                print(f"  batch {n}: nothing rendered, not recorded.")
                continue
            with (out / "manifest.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            name = f"{stamp} · batch {n} · {len(rows)} doors"
            batch_id = letter_batches.create_batch(
                db, name, str(out),
                [{"lead_id": r["id"], "address": r["address"], "pdf": r["pdf"]} for r in rows],
                args.company)
            made += len(rows)
            print(f"  batch {batch_id}: {len(rows)} letter(s) -> {out}")
        print(f"\n{made} letter(s) rendered and recorded in {len(chunks)} batch(es).")
        print("Confirm each batch on /letters after you deliver it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
