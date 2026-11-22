#!/usr/bin/env python3
"""Put the property-management vendor contacts into Kit, tagged, with no sequence.

These are suppliers and procurement desks, not estimate leads. They must never
enter `estimate-request` / New Estimate Follow-Up -- that funnel is for customers
asking for a quote. This script only subscribes and tags.

    ./venv/bin/python scripts/kit_add_vendors.py --dry-run
    ./venv/bin/python scripts/kit_add_vendors.py --apply

Env: KIT_API_KEY (Kit -> Settings -> Advanced -> API).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

API = "https://api.kit.com/v4"
TAG = "vendor"

# The five vendor desks emailed 2026-11-22. first_name is left blank where the
# address is a shared inbox -- do not invent a person's name.
VENDORS = [
    {"company": "Lawson Realty Corporation", "first_name": "Mike",
     "email": "mfeehery@lawsoncompanies.com"},
    {"company": "Keyrenter Property Management Hampton Roads", "first_name": "Brian",
     "email": "info@keyrenterhamptonroads.com"},
    {"company": "PMI Virginia", "first_name": "Patti",
     "email": "patti@pmivirginia.com"},
    {"company": "Osprey Property Management", "first_name": "",
     "email": "info@ospreypm.com"},
    {"company": "Virginia Beach CDC", "first_name": "",
     "email": "Adminservices@vbcdc.org"},
]


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        os.environ.setdefault(key.strip(), val)


def headers() -> dict:
    return {"X-Kit-Api-Key": (os.getenv("KIT_API_KEY") or "").strip(),
            "Content-Type": "application/json", "Accept": "application/json"}


def tag_id(name: str) -> int | None:
    r = requests.get(f"{API}/tags", headers=headers(), timeout=30)
    if r.status_code == 200:
        for tag in (r.json().get("tags") or []):
            if (tag.get("name") or "").lower() == name.lower():
                return int(tag["id"])
    r = requests.post(f"{API}/tags", headers=headers(), json={"name": name}, timeout=30)
    if r.status_code in (200, 201):
        return int(r.json()["tag"]["id"])
    print("tag create failed:", r.status_code, r.text[:160])
    return None


def subscribe(email: str, first_name: str = "") -> tuple[int | None, bool]:
    """Create-or-find. Returns (subscriber_id, created)."""
    body = {"email_address": email, "state": "active"}
    if first_name:
        body["first_name"] = first_name
    r = requests.post(f"{API}/subscribers", headers=headers(), json=body, timeout=30)
    if r.status_code in (200, 201):
        return int((r.json().get("subscriber") or {})["id"]), True
    # 409/422 means the address is already on the list; look it up instead.
    r = requests.get(f"{API}/subscribers", headers=headers(),
                     params={"email_address": email}, timeout=30)
    for sub in (r.json().get("subscribers") or []):
        if (sub.get("email_address") or "").lower() == email.lower():
            return int(sub["id"]), False
    print("  subscribe failed:", email, r.status_code, r.text[:120])
    return None, False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="preview only (the default)")
    ap.add_argument("--tag", default=TAG)
    args = ap.parse_args()

    load_env(ROOT / ".env")
    if not (os.getenv("KIT_API_KEY") or "").strip():
        print("KIT_API_KEY is not set.", file=sys.stderr)
        return 2

    tid = tag_id(args.tag) if args.apply else None
    if args.apply and not tid:
        return 2
    print(f"{'APPLY' if args.apply else 'DRY RUN'} | tag={args.tag} | contacts={len(VENDORS)}")

    ok = 0
    for v in VENDORS:
        if not args.apply:
            print(f"  would tag {v['email']:<45} {v['company']}")
            continue
        sid, created = subscribe(v["email"], v["first_name"])
        if not sid:
            continue
        tag_resp = requests.post(f"{API}/tags/{tid}/subscribers/{sid}",
                                 headers=headers(), timeout=30)
        tagged = tag_resp.status_code in (200, 201, 204)
        # Deliberately no /sequences call: vendors never enter a sales sequence.
        print(f"  {'new' if created else 'existing':8} id={sid:<10} tagged={tagged}  {v['email']}")
        ok += 1
        time.sleep(0.5)

    if args.apply:
        print(f"done: {ok}/{len(VENDORS)} subscribers tagged '{args.tag}', no sequence enrolled")
    return 0 if ok == len(VENDORS) or not args.apply else 1


if __name__ == "__main__":
    raise SystemExit(main())
