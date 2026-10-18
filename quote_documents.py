"""Customer-facing documents built from a quote: estimate, bill of sale, receipt.

``documents_service.render_document`` takes a declarative ``form`` dict of
sections and fields; this module supplies those forms and fills them from a
quote row. No form was defined anywhere before, which is why
``render_document`` had no callers.

Provenance is not decoration. An estimate has to say what its numbers are:

* ``supplier`` -- a price a supplier gave us. The only thing a customer can
  rely on, and the only one where ``price_verified`` is true.
* ``estimationpro`` -- a public regional benchmark. Real data, dated and
  traceable, but not something anyone was invoiced.
* ``estimate`` -- our own allowance, escalated by BLS PPI. Movement is
  official; the level is judgement.

Presenting the second or third as the first is how a quote becomes a dispute,
so the wording is pinned by a test rather than left to whoever edits the form.
"""

from __future__ import annotations

import time

import documents_service

FORM_ESTIMATE = {
    "title": "Project Estimate",
    "sections": [
        {"heading": "Prepared for", "fields": [
            {"key": "customer_name", "label": "Customer"},
            {"key": "customer_email", "label": "Email"},
            {"key": "address", "label": "Property address"},
        ]},
        {"heading": "Estimate", "fields": [
            {"key": "quote_no", "label": "Estimate number"},
            {"key": "date", "label": "Date"},
            {"key": "expires", "label": "Valid until"},
            {"key": "trade", "label": "Scope"},
            {"key": "dimensions", "label": "Dimensions"},
            {"key": "sqft", "label": "Area"},
        ]},
        {"heading": "Scope of work", "fields": [
            {"key": "scope_text", "label": "Included"},
        ]},
        {"heading": "Pricing basis", "fields": [
            {"key": "price_basis", "label": "Prices"},
            {"key": "index_note", "label": "Index"},
            {"key": "deposit_line", "label": "Deposit due at signing"},
            {"key": "total_words", "label": "Total in words"},
        ]},
    ],
}

FORM_BILL_OF_SALE = {
    "title": "Bill of Sale",
    "sections": [
        {"heading": "Sold by", "fields": [
            {"key": "seller", "label": "Seller"},
            {"key": "seller_phone", "label": "Phone"},
        ]},
        {"heading": "Sold to", "fields": [
            {"key": "customer_name", "label": "Customer"},
            {"key": "customer_email", "label": "Email"},
            {"key": "address", "label": "Delivery / property address"},
        ]},
        {"heading": "Sale", "fields": [
            {"key": "quote_no", "label": "Reference"},
            {"key": "date", "label": "Date"},
            {"key": "item_count", "label": "Items"},
            {"key": "total_line", "label": "Total"},
            {"key": "paid_line", "label": "Paid"},
            {"key": "balance_line", "label": "Balance due"},
            {"key": "total_words", "label": "Total in words"},
        ]},
        {"heading": "Terms", "fields": [
            {"key": "terms", "label": "Terms"},
        ]},
    ],
}

FORM_RECEIPT = {
    "title": "Payment Receipt",
    "sections": [
        {"heading": "Receipt", "fields": [
            {"key": "receipt_no", "label": "Receipt number"},
            {"key": "date", "label": "Date received"},
        ]},
        {"heading": "Received from", "fields": [
            {"key": "customer_name", "label": "Customer"},
            {"key": "customer_email", "label": "Email"},
        ]},
        {"heading": "Payment", "fields": [
            {"key": "method", "label": "Method"},
            {"key": "reference", "label": "Applied to"},
            {"key": "amount_line", "label": "Amount received"},
            {"key": "amount_words", "label": "Amount in words"},
            {"key": "balance_line", "label": "Balance remaining"},
        ]},
    ],
}

FORMS = {
    "estimate": FORM_ESTIMATE,
    "bill_of_sale": FORM_BILL_OF_SALE,
    "receipt": FORM_RECEIPT,
}

BILL_OF_SALE_TERMS = (
    "Title passes on receipt of full payment. Materials are sold as listed and "
    "are not returnable once delivered. Quantities are as shown; shortages must "
    "be raised on the day of delivery. This sale is separate from any labour or "
    "installation agreement between the parties."
)

# What the customer is entitled to be told about where a number came from.
PRICE_BASIS = {
    "supplier": "Supplier price — a price our supplier quoted for these items.",
    "estimationpro": (
        "Regional market benchmark from a public cost service, regionalised to "
        "this ZIP code. Current as of the date shown and updated monthly, but it "
        "is a benchmark rather than a quote from a yard. Your final price is "
        "confirmed before we order."
    ),
    "estimate": (
        "Planning estimate. These are our own allowances for this scope, indexed "
        "to current construction material costs. Treat them as a range and "
        "expect the final invoice to follow the actual purchase price."
    ),
}


def _money(cents: int) -> str:
    return f"${int(cents or 0) / 100:,.2f}"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _date(value) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value or "")


def values_from_quote(quote: dict, lines: list[dict] | None = None) -> dict:
    """Fill the estimate / bill-of-sale fields from a quote row."""
    q = quote or {}
    rows = lines if lines is not None else (q.get("lines") or [])
    total = int(q.get("job_price_cents") or 0)
    if not total:
        total = sum(int(r.get("total_cents") or 0) for r in rows)
    deposit = int(q.get("deposit_cents") or 0)
    source = str(q.get("price_source") or "estimate")

    dims = ""
    if q.get("width_ft") and q.get("length_ft"):
        dims = f"{q['width_ft']} ft x {q['length_ft']} ft"

    index_note = ""
    if q.get("bls_series"):
        index_note = f"{q['bls_series']} @ {q.get('bls_period') or 'n/a'}"
        if q.get("escalated"):
            index_note += " (escalated)"
        else:
            index_note += " (not escalated — treat as dated)"

    return {
        "date": _today(),
        "customer_name": q.get("customer_name") or "",
        "customer_email": q.get("customer_email") or "",
        "address": q.get("address") or "",
        "quote_no": q.get("quote_no") or f"#{q.get('id')}",
        "expires": _date(q.get("expires_at")),
        "trade": q.get("trade") or "",
        "dimensions": dims,
        "sqft": (f"{q['sqft']:,} sq ft" if q.get("sqft") else ""),
        "scope_text": q.get("scope_text") or "",
        "price_basis": PRICE_BASIS.get(source, PRICE_BASIS["estimate"]),
        "index_note": index_note,
        "deposit_line": _money(deposit) if deposit else "None",
        "total_line": _money(total),
        "total_words": documents_service.amount_to_words(total),
        "seller": "Buildstack Construction Co.",
        "seller_phone": "757-908-7121",
        "item_count": f"{len(rows)} item(s)",
        "paid_line": _money(0),
        "balance_line": _money(total),
        "terms": BILL_OF_SALE_TERMS,
    }


def values_from_payment(payment: dict, quote: dict | None = None) -> dict:
    q = quote or {}
    amount = int(payment.get("amount_cents") or 0)
    balance = int(payment.get("balance_cents") or 0)
    return {
        "date": _today(),
        "receipt_no": payment.get("receipt_no") or f"R-{payment.get('id', '')}",
        "customer_name": payment.get("customer_name") or q.get("customer_name") or "",
        "customer_email": payment.get("customer_email") or q.get("customer_email") or "",
        "method": payment.get("method") or "",
        "reference": payment.get("reference") or (q.get("quote_no") or ""),
        "amount_line": _money(amount),
        "amount_words": documents_service.amount_to_words(amount),
        "balance_line": _money(balance),
    }


def render(kind: str, values: dict, fmt: str = "pdf") -> bytes:
    """Render one of the three documents. Unknown kinds raise rather than
    falling back to a blank page that looks like a document."""
    form = FORMS.get(str(kind or "").lower())
    if form is None:
        raise ValueError(f"unknown document kind {kind!r}; have {sorted(FORMS)}")
    return documents_service.render_document(form, values, fmt)