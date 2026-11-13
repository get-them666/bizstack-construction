"""Click-and-sign links for a bid's renovation agreement.

The owner's own signature (``bid_signing``) is drawn once on the bid card. This
module is the other half: a single-use, expiring link the homeowner opens with
no login, reads the agreement, draws *their* signature and accepts it. When that
happens the bid becomes won, a job card exists with nobody assigned, the deposit
invoice reaches the books, the signed PDF comes back to the owner and the client
is given portal credentials.

Why a database token rather than a signed-only token: a link that lives in a
row can be revoked, shows the owner whether it was opened, and makes the accept
step idempotent. A forwarded link is limited by expiry and by the fact that a
second accept finds ``signed_at`` already set and is refused.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

DEFAULT_DAYS = 30


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bid_sign_requests (
                id SERIAL PRIMARY KEY,
                bid_id INTEGER NOT NULL REFERENCES bid_opportunities(id) ON DELETE CASCADE,
                company VARCHAR(40) NOT NULL DEFAULT 'construction',
                token VARCHAR(120) NOT NULL UNIQUE,
                document_sha256 CHAR(64),
                sent_to VARCHAR(255),
                sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL,
                signer_name VARCHAR(160),
                signer_email VARCHAR(255),
                signed_at TIMESTAMPTZ,
                ip VARCHAR(64),
                user_agent VARCHAR(400),
                signature_id INTEGER,
                accepted BOOLEAN NOT NULL DEFAULT FALSE,
                revoked_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_sign_requests_bid "
                    "ON bid_sign_requests(bid_id, company, sent_at DESC);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_sign_requests_token "
                    "ON bid_sign_requests(token);")
    conn.commit()


def create_request(conn, bid_id: int, *, sent_to: str, document_sha256: str = "",
                   days: int = DEFAULT_DAYS, company: str = "construction") -> dict:
    """Create a link and revoke any earlier unsigned link for the same bid."""
    ensure_schema(conn)
    token = secrets.token_urlsafe(32)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bid_sign_requests SET revoked_at = now()
            WHERE bid_id = %s AND company = %s AND signed_at IS NULL AND revoked_at IS NULL;
        """, (int(bid_id), company))
        cur.execute("""
            INSERT INTO bid_sign_requests
                (bid_id, company, token, document_sha256, sent_to,
                 expires_at)
            VALUES (%s, %s, %s, %s, %s, now() + (%s || ' days')::interval)
            RETURNING *;
        """, (int(bid_id), company, token, (document_sha256 or "")[:64] or None,
              (sent_to or "")[:255], int(days)))
        row = cur.fetchone()
    conn.commit()
    return row


def latest(conn, bid_id: int, company: str = "construction"):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM bid_sign_requests
            WHERE bid_id = %s AND company = %s
            ORDER BY id DESC LIMIT 1;
        """, (int(bid_id), company))
        return cur.fetchone()


def active(conn, bid_id: int, company: str = "construction"):
    """The newest link that can still be used, or None."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM bid_sign_requests
            WHERE bid_id = %s AND company = %s
              AND signed_at IS NULL AND revoked_at IS NULL AND expires_at > now()
            ORDER BY id DESC LIMIT 1;
        """, (int(bid_id), company))
        return cur.fetchone()


def by_token(conn, token: str):
    """A link that may still be opened. Expired/revoked links return None."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM bid_sign_requests
            WHERE token = %s AND revoked_at IS NULL AND expires_at > now()
            LIMIT 1;
        """, ((token or "").strip(),))
        return cur.fetchone()


def mark_signed(conn, request_id: int, *, signature_id: int, signer_name: str,
                signer_email: str = "", ip: str = "", user_agent: str = "") -> bool:
    """Claim the link. Returns False if somebody already claimed it."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bid_sign_requests
               SET signed_at = now(), accepted = TRUE, signature_id = %s,
                   signer_name = %s, signer_email = %s, ip = %s, user_agent = %s
             WHERE id = %s AND signed_at IS NULL AND revoked_at IS NULL
            RETURNING id;
        """, (int(signature_id), (signer_name or "")[:160],
              (signer_email or "")[:255] or None, (ip or "")[:64] or None,
              (user_agent or "")[:400] or None, int(request_id)))
        row = cur.fetchone()
    conn.commit()
    return bool(row)


def revoke(conn, bid_id: int, company: str = "construction") -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bid_sign_requests SET revoked_at = now()
            WHERE bid_id = %s AND company = %s AND signed_at IS NULL AND revoked_at IS NULL;
        """, (int(bid_id), company))
    conn.commit()


def describe(conn, bid_id: int, company: str = "construction") -> dict:
    row = latest(conn, bid_id, company)
    if not row:
        return {"sent": False}
    when = row.get("signed_at")
    expires = row.get("expires_at")
    if expires is not None and getattr(expires, "tzinfo", None) is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return {
        "sent": True,
        "id": row.get("id"),
        "token": row.get("token"),
        "sent_to": row.get("sent_to"),
        "sent_at": row.get("sent_at"),
        "expires_at": expires,
        "signed_at": when,
        "signed": bool(when),
        "signer_name": row.get("signer_name"),
        "signer_email": row.get("signer_email"),
        "revoked": bool(row.get("revoked_at")),
        "usable": bool(not when and not row.get("revoked_at")
                       and expires and expires > datetime.now(timezone.utc)),
    }
