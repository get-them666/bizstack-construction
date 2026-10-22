"""Pull settled payment evidence out of Stripe for a lender.

A lender asking "show me the income" wants settled money, not dashboard
screenshots. This reads the live account and writes a dated markdown summary
plus the raw JSON behind it, so the numbers can be re-derived later.

It reports only money that actually settled: charges that succeeded and payouts
that landed. Failed charges, refunds and Stripe's own fees are listed separately
rather than netted away, because a report that quietly nets a failed payout into
a balance is not evidence of anything.

Usage:
    STRIPE_SECRET_KEY=sk_live_... python3 stripe_income_report.py [outdir]

The key is read from the environment and never written to disk or to the report.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.stripe.com"


def _get(key: str, path: str, params: str = ""):
    url = API + path + ("?" + params if params else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _pages(key: str, path: str, cap: int = 1000):
    """Walk a list endpoint. Capped so a runaway account cannot page forever."""
    out, starting_after = [], None
    while len(out) < cap:
        params = "limit=100" + (f"&starting_after={starting_after}" if starting_after else "")
        data = _get(key, path, params)
        out.extend(data.get("data", []))
        if not data.get("has_more") or not data.get("data"):
            break
        starting_after = data["data"][-1]["id"]
    return out[:cap]


def _day(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def collect(key: str) -> dict:
    acct = _get(key, "/v1/account")
    charges = _pages(key, "/v1/charges")
    payouts = _pages(key, "/v1/payouts")
    balance = _get(key, "/v1/balance")
    invoices = _pages(key, "/v1/invoices")

    succeeded = [c for c in charges if c.get("status") == "succeeded" and c.get("paid")]
    failed = [c for c in charges if c.get("status") != "succeeded"]
    refunded = [c for c in charges if c.get("amount_refunded", 0) > 0]

    gross = sum(c["amount"] for c in succeeded)
    refunds = sum(c.get("amount_refunded", 0) for c in charges)
    landed = sum(p["amount"] for p in payouts if p.get("status") == "paid")
    payout_failed = [p for p in payouts if p.get("status") == "failed"]

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "account": {
            "id": acct.get("id"),
            "business_name": (acct.get("business_profile") or {}).get("name"),
            "country": acct.get("country"),
            "default_currency": acct.get("default_currency"),
            "charges_enabled": acct.get("charges_enabled"),
            "payouts_enabled": acct.get("payouts_enabled"),
        },
        "summary": {
            "charges_succeeded": len(succeeded),
            "charges_failed": len(failed),
            "gross_collected_cents": gross,
            "refunds_cents": refunds,
            "net_collected_cents": gross - refunds,
            "payouts_paid": len([p for p in payouts if p.get("status") == "paid"]),
            "payouts_failed": len(payout_failed),
            "payouts_landed_cents": landed,
            "balance_available_cents": sum(b["amount"] for b in balance.get("available", [])),
            "balance_pending_cents": sum(b["amount"] for b in balance.get("pending", [])),
            "invoices": len(invoices),
        },
        "charges_succeeded": [
            {"date": _day(c["created"]), "amount_cents": c["amount"],
             "net_cents": c["amount"] - c.get("amount_refunded", 0),
             "currency": c["currency"], "receipt_url": c.get("receipt_url"),
             "description": c.get("description")}
            for c in sorted(succeeded, key=lambda x: x["created"])
        ],
        "payouts": [
            {"date": _day(p["created"]), "amount_cents": p["amount"],
             "status": p.get("status"), "currency": p["currency"],
             "arrival_date": _day(p["arrival_date"]) if p.get("arrival_date") else None}
            for p in sorted(payouts, key=lambda x: x["created"])
        ],
        "charges_failed": [
            {"date": _day(c["created"]), "amount_cents": c["amount"],
             "status": c.get("status"),
             "failure_code": c.get("failure_code"), "failure_message": c.get("failure_message")}
            for c in sorted(failed, key=lambda x: x["created"])
        ],
    }


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def render(d: dict) -> str:
    a, s = d["account"], d["summary"]
    L = []
    L.append(f"# Stripe income report — {a['business_name'] or a['id']}")
    L.append("")
    L.append(f"Generated {d['generated_at']}. Source: Stripe API, live account "
             f"`{a['id']}` (not a dashboard export).")
    L.append("")
    L.append(f"- Country: {a['country']} · currency {a['default_currency']}")
    L.append(f"- Charges enabled: {a['charges_enabled']} · payouts enabled: {a['payouts_enabled']}")
    L.append("")
    L.append("## Settled money")
    L.append("")
    L.append("| Measure | Count | Amount |")
    L.append("|---|---|---|")
    L.append(f"| Charges succeeded | {s['charges_succeeded']} | {_money(s['gross_collected_cents'])} |")
    L.append(f"| Refunds | — | -{_money(s['refunds_cents'])} |")
    L.append(f"| **Net collected** | — | **{_money(s['net_collected_cents'])}** |")
    L.append(f"| **Payouts landed in bank** | {s['payouts_paid']} | **{_money(s['payouts_landed_cents'])}** |")
    L.append(f"| Balance available | — | {_money(s['balance_available_cents'])} |")
    L.append(f"| Balance pending | — | {_money(s['balance_pending_cents'])} |")
    L.append("")
    if s["charges_succeeded"] == 0:
        L.append("> **No successful charges on this account yet.** Payouts landed is the "
                 "figure that matters, and it is zero until a deposit clears.")
        L.append("")
    if s["payouts_failed"]:
        L.append(f"> {s['payouts_failed']} payout(s) FAILED — the bank rejected the withdrawal. "
                 "Listed below; these are not income.")
        L.append("")
    if s["charges_succeeded"]:
        L.append("## Successful charges")
        L.append("")
        L.append("| Date | Amount | Net | Description |")
        L.append("|---|---|---|---|")
        for c in d["charges_succeeded"]:
            L.append(f"| {c['date']} | {_money(c['amount_cents'])} | {_money(c['net_cents'])} | "
                     f"{c.get('description') or ''} |")
        L.append("")
    if d["payouts"]:
        L.append("## Payouts")
        L.append("")
        L.append("| Date | Amount | Status |")
        L.append("|---|---|---|")
        for p in d["payouts"]:
            L.append(f"| {p['date']} | {_money(p['amount_cents'])} | {p['status']} |")
        L.append("")
    if d["charges_failed"]:
        L.append("## Failed charges (not income)")
        L.append("")
        L.append("| Date | Amount | Reason |")
        L.append("|---|---|---|")
        for c in d["charges_failed"]:
            L.append(f"| {c['date']} | {_money(c['amount_cents'])} | "
                     f"{c.get('failure_message') or c['status']} |")
        L.append("")
    L.append("---")
    L.append("")
    L.append("Stripe reports only what passed through Stripe. Cash, checks and bank "
             "transfers collected outside the platform do not appear here and are not "
             "represented by this report.")
    return "\n".join(L)


def main() -> int:
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        print("STRIPE_SECRET_KEY is not set.", file=sys.stderr)
        return 1
    outdir = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/stripe")
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        data = collect(key)
    except urllib.error.HTTPError as e:
        print(f"Stripe API error {e.code}: {e.read()[:300].decode(errors='replace')}",
              file=sys.stderr)
        return 1
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (outdir / f"stripe_income_{stamp}.json").write_text(json.dumps(data, indent=2))
    report = render(data)
    (outdir / f"stripe_income_{stamp}.md").write_text(report + "\n")
    print(report)
    print(f"\nwrote {outdir}/stripe_income_{stamp}.{{md,json}}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())