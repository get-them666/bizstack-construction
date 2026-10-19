"""Bridge the rest of the app into the ledger.

Each function here posts one real-world event into double entry. They are
the only sanctioned way money enters the books, which is what keeps the
figures on /bids and /payroll consistent with the financial statements.

Every one is idempotent on (source_type, source_id), so calling them twice
-- a re-save, a retry, a replayed webhook -- does not double-count. That
matters more than it sounds: a construction app gets re-triggered.

CREW IS ACCRUED, THEN PAID. TWO ENTRIES, NOT ONE.
-----------------------------------------------
When hours are approved, the wage is a liability you owe whether or not you
have sent it:

    approved   Dr 5020 Labor — Direct      (expense)
               Cr 2150 Payroll Payable     (you owe Devon)

When you actually send the money:

    paid       Dr 2150 Payroll Payable    (relief the liability)
               Cr 1010 Cash — Checking     (money leaves)

Collapsing those into a single "paid wages" entry understates labor while
the job is still running, which is exactly when you need the margin to be
true. Keeping them apart is what makes a job's P&L correct mid-project
rather than only at the end.
"""
from __future__ import annotations

from datetime import date

import ledger


def _d(v, field):
    return ledger._c(v, field)


def _as_lines(lines, what: str) -> list:
    """Accept a single dict or a list of them, and reject anything else clearly.

    Without this, passing one dict instead of a list of dicts iterates the
    dict's keys and every one of them fails the same unhelpful way, with the
    first string causing 'str' object has no attribute 'get'. On a money path
    that error message is worse than the mistake.
    """
    if lines is None:
        return []
    if isinstance(lines, dict):
        return [lines]
    if isinstance(lines, (list, tuple)):
        bad = [i for i, x in enumerate(lines) if not isinstance(x, dict)]
        if bad:
            raise ledger.LedgerError(f"{what}: entries {bad} are not objects")
        return list(lines)
    raise ledger.LedgerError(f"{what}: expected a list of objects, got {type(lines).__name__}")


def post_invoice(conn, bid, invoice, company: str = "construction") -> dict:
    """Bill a customer. Debit what they owe, credit revenue."""
    amount = _d(invoice.get("amount"), "invoice amount")
    if amount <= 0:
        return {"posted": False, "reason": "nothing to bill"}
    lines = [
        {"account": "1100", "debit": amount,
         "description": f"Invoice {invoice.get('invoice_number') or ''}".strip(),
         "bid_id": bid.get("id")},
        {"account": "4010", "credit": amount,
         "description": f"Revenue — {(bid.get('name') or 'job')[:120]}",
         "bid_id": bid.get("id")},
    ]
    return ledger.post(conn, lines, entry_date=invoice.get("issued_date"),
                       memo=f"Invoice {invoice.get('invoice_number') or ''} — "
                            f"{(bid.get('name') or '')[:120]}",
                       source_type="invoice", source_id=str(invoice.get("id")),
                       company=company)


def post_payment(conn, bid, payment, company: str = "construction") -> dict:
    """Customer paid. Move money, relieve the receivable."""
    amount = _d(payment.get("amount"), "payment amount")
    if amount <= 0:
        return {"posted": False, "reason": "nothing received"}
    cash = "1020" if str(payment.get("method", "")).lower() in ("card", "stripe", "ach_card") else "1010"
    return ledger.post(conn, [
        {"account": cash, "debit": amount, "description": "Payment received"},
        {"account": "1100", "credit": amount, "description": "Applied to A/R",
         "bid_id": bid.get("id")},
    ], entry_date=payment.get("date"),
        memo=f"Payment — {(bid.get('name') or '')[:120]}",
        source_type="payment", source_id=str(payment.get("id")), company=company)


def post_cost_line(conn, bid, cost_line, company: str = "construction") -> dict:
    """Material, sub, permit or equipment actually incurred on a job.

    Labor is deliberately NOT handled here -- it accrues from approved
    timesheets via accrue_payroll so it cannot be double counted by also
    being typed in here.
    """
    category = str(cost_line.get("category") or "other").lower()
    if category == "labor":
        return {"posted": False, "reason": "labor accrues from timesheets"}
    amount = _d(cost_line.get("actual"), "cost amount")
    if amount <= 0:
        return {"posted": False, "reason": "no actual recorded"}
    account = ledger.CATEGORY_ACCOUNT.get(category, "6010")
    payable = {"material": "2010", "sub": "2010", "permit": "2010"}.get(category, "6010")
    return ledger.post(conn, [
        {"account": account, "debit": amount,
         "description": (cost_line.get("description") or category)[:200],
         "bid_id": bid.get("id")},
        {"account": payable, "credit": amount,
         "description": f"Payable — {(cost_line.get('vendor') or category)[:120]}",
         "bid_id": bid.get("id")},
    ], entry_date=cost_line.get("date"),
        memo=f"{category} — {(cost_line.get('description') or '')[:120]}",
        source_type="cost", source_id=str(cost_line.get("id")), company=company)


def accrue_payroll(conn, run, lines, company: str = "construction") -> dict:
    """Record approved crew wages as an expense and a liability.

    `lines`: [{"crew": "Devon", "hours": 40, "rate": 38.00, "bid_id": 7}]
    """
    lines = _as_lines(lines, "accrue_payroll")
    if not lines:
        return {"posted": False, "reason": "no approved hours"}
    expense_lines, liability_total = [], 0
    for ln in lines:
        rate = _d(ln.get("rate"), "pay rate")
        hours = float(ln.get("hours") or 0)
        if hours <= 0:
            continue
        amt = int(round(rate * hours))
        if amt <= 0:
            continue
        expense_lines.append({
            "account": "5020", "debit": amt,
            "description": f"{ln.get('crew', 'crew')} {hours:g}h @ {rate/100:.2f}"[:200],
            "bid_id": ln.get("bid_id"),
        })
        liability_total += amt
    if not expense_lines:
        return {"posted": False, "reason": "nothing to accrue"}
    expense_lines.append({"account": "2150", "credit": liability_total,
                          "description": "Wages owed to crew"})
    return ledger.post(conn, expense_lines, entry_date=(run or {}).get("period_end"),
                       memo=f"Payroll accrual — {((run or {}).get('period_start') or '')}"
                            f" to {((run or {}).get('period_end') or '')}",
                       source_type="payroll_accrual", source_id=str((run or {}).get("id")),
                       company=company)


def pay_payroll(conn, run, lines, company: str = "construction") -> dict:
    """Money actually sent to crew. Relieves the liability.

    `lines`: [{"crew": "Devon", "gross": 1520.00}]
    """
    lines = _as_lines(lines, "pay_payroll")
    if not lines:
        return {"posted": False, "reason": "nothing to pay"}
    liability_lines, total = [], 0
    for ln in lines:
        amt = _d(ln.get("gross"), "gross pay")
        if amt <= 0:
            continue
        liability_lines.append({"account": "2150", "debit": amt,
                                "description": f"Paid — {ln.get('crew', 'crew')}"[:200]})
        total += amt
    if not liability_lines:
        return {"posted": False, "reason": "nothing to pay"}
    liability_lines.append({"account": "1010", "credit": total,
                            "description": "Payroll funding"})
    return ledger.post(conn, liability_lines, entry_date=(run or {}).get("paid_date"),
                       memo=f"Payroll paid — {((run or {}).get('period_start') or '')}"
                            f" to {((run or {}).get('period_end') or '')}",
                       source_type="payroll_paid", source_id=str((run or {}).get("id")),
                       company=company)


def post_deposit(conn, lead, amount, company: str = "construction") -> dict:
    """Customer deposit to reserve a job. Held, not earned."""
    amt = _d(amount, "deposit amount")
    if amt <= 0:
        return {"posted": False, "reason": "no deposit"}
    return ledger.post(conn, [
        {"account": "1010", "debit": amt, "description": "Deposit received"},
        {"account": "2100", "credit": amt, "description": "Customer deposit held"},
    ], memo=f"Deposit — {str(lead.get('name') or lead.get('email') or '')[:120]}",
        source_type="deposit", source_id=str(lead.get("id")), company=company)


def apply_deposit_to_invoice(conn, lead, amount, invoice_id, company: str = "construction") -> dict:
    """Move a held deposit onto an invoice so it stops looking like income."""
    amt = _d(amount, "amount")
    if amt <= 0:
        return {"posted": False, "reason": "nothing to apply"}
    return ledger.post(conn, [
        {"account": "2100", "debit": amt, "description": "Deposit applied"},
        {"account": "1150", "credit": amt, "description": f"Applied to invoice {invoice_id}"},
    ], memo="Deposit applied to invoice",
        source_type="deposit_apply", source_id=f"{lead.get('id')}:{invoice_id}",
        company=company)


def post_referral_commission(conn, referral, when=None, company: str = "construction") -> dict:
    """A lender paid us a referral fee on a funded business loan.

    Credit 4030 Referral Commission Revenue, debit cash. Idempotent on
    ``(funding_referral, id)`` via :func:`ledger.post`, so re-marking a referral
    as commissioned cannot double-count.

    Refuses a non-business referral outright rather than trusting the caller:
    residential mortgage referral fees are the licensed/RESPA zone and must
    never reach the books. The same guard lives in ``funding_referrals``, and
    keeping it here too means the ledger cannot be used as a bypass.
    """
    purpose = str(referral.get("purpose") or "").lower()
    if purpose != "business":
        raise ledger.LedgerError(
            f"refusing to book a referral commission for purpose {purpose!r}: "
            f"only business-purpose loans may be commissioned")
    amt = _d(referral.get("commission_cents"), "referral commission")
    if amt <= 0:
        return {"posted": False, "reason": "no commission recorded"}
    partner = str(referral.get("partner") or "lender")[:120]
    return ledger.post(conn, [
        {"account": "1010", "debit": amt, "description": f"Referral fee — {partner}"},
        {"account": "4030", "credit": amt, "description": f"Revenue — {partner}"},
    ], entry_date=when, memo=f"Referral commission — {partner}"[:200],
        source_type="funding_referral", source_id=str(referral.get("id")),
        company=company)


def record_operating_expense(conn, code, amount, description="", when=None,
                             company: str = "construction") -> dict:
    """Overhead — insurance, fuel, rent, the things that are not a job."""
    acct = str(code or "").strip()
    if acct not in {a[0] for a in ledger.CHART}:
        raise ledger.LedgerError(f"{acct} is not in the chart")
    amt = _d(amount, "amount")
    if amt <= 0:
        return {"posted": False, "reason": "no amount"}
    return ledger.post(conn, [
        {"account": acct, "debit": amt, "description": (description or "")[:200]},
        {"account": "1010", "credit": amt, "description": "Cash out"},
    ], entry_date=when, memo=f"{acct} — {description}"[:200],
        source_type="opex", source_id=f"{acct}:{when or date.today().isoformat()}:{(description or '')[:40]}",
        company=company)

# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------

def post_quote_accepted(conn, quote, company: str = "construction") -> dict:
    """Record a customer accepting a quote.

    Deliberately not revenue. A quote is an offer; nothing has been earned and
    nothing is owed until an invoice goes out, so posting the full amount here
    would inflate the P&L on every job still sitting in "accepted". This
    carries the value without touching income: the deposit moves to a held
    liability and the balance to a job-in-progress account, both of which are
    cleared by post_invoice() when the work is actually invoiced.
    """
    amt = _d(quote.get("job_price_cents"), "quote job price")
    if amt <= 0:
        return {"posted": False, "reason": "quote has no price"}
    deposit = _d(quote.get("deposit_cents"), "quote deposit")
    if deposit < 0 or deposit > amt:
        deposit = 0
    balance = amt - deposit

    lines = []
    if deposit > 0:
        lines += [
            {"account": "1010", "debit": deposit, "description": "Deposit due on acceptance"},
            {"account": "2100", "credit": deposit, "description": "Customer deposit held"},
        ]
    if balance > 0:
        lines += [
            {"account": "1250", "debit": balance,
             "description": f"Work committed — quote {quote.get('quote_no') or quote.get('id')}"},
            {"account": "2100", "credit": balance,
             "description": "Customer contract balance"},
        ]
    ref = quote.get("quote_no") or quote.get("id")
    return ledger.post(conn, lines,
        memo=f"Quote accepted — {ref}",
        source_type="quote_accepted", source_id=str(quote.get("id")), company=company)
