"""Give a generated PDF a creation date taken from the record, not the clock.

fpdf2 writes ``datetime.now()`` into two places in every file it produces: the
``/CreationDate`` header field and the document ``/ID``. Two renders of the same
bid, seconds apart, are therefore different bytes even though not one character
of the document changed.

That defeats the point of the hash stored against a signing request. The hash is
supposed to answer "is the PDF the client signed the PDF we sent?". If every
render is unique it can only answer "a render happened".

Stamping from the bid row instead makes a document reproducible: the same record
always renders to the same bytes, and editing the bid moves the stamp along with
the content, which is exactly when it should become a different document.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

# A record with no timestamps still has to render deterministically. The epoch is
# deliberately obviously-not-a-real-date rather than today, so a document stamped
# 1970 is visible in a PDF viewer instead of quietly looking correct.
_UNKNOWN = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def document_date(record: dict | None) -> datetime:
    """The stamp for a document generated from ``record``.

    ``updated_at`` first: the document belongs to the bid as it stood when it was
    last edited. ``created_at`` covers a row nobody has touched since insert.
    """
    for key in ("updated_at", "created_at"):
        resolved = _as_datetime((record or {}).get(key))
        if resolved is not None:
            return resolved
    return _UNKNOWN


def stamp(pdf, record: dict | None):
    """Pin ``pdf``'s creation date so repeated renders produce identical bytes."""
    pdf.set_creation_date(document_date(record))
    return pdf


def day_text(record: dict | None) -> str:
    """The date a generated document is dated, ready to print."""
    return document_date(record).strftime("%B %d, %Y")
