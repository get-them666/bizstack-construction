"""Client portal accounts, project access, and the read model behind /portal.

The marketing site's demo portal kept its session in localStorage and showed
sample data. This module is the real version: an email/password account, a
signed customer session, and an explicit client -> bid access list. A client can
only see bids linked to their account or bids whose contact email matches the
verified login email.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import string
from datetime import datetime, timezone

import auth_service
import photo_store

COOKIE = "portal_session"
PENDING_COOKIE = "portal_otp_pending"
ROLE = "customer"
PBKDF2_ITERATIONS = 390_000

OTP_LOGIN = "portal_login"
OTP_RESET = "portal_reset"
OTP_TTL_SECONDS = 10 * 60
MAX_OTP_ATTEMPTS = 6


# --- schema ------------------------------------------------------------------
def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS portal_clients (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) UNIQUE NOT NULL,
                name VARCHAR(255),
                password_hash TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_login_at TIMESTAMPTZ
            );
        """)
        # A temporary password is exactly that: it must be replaced on first
        # use. The flag is checked by /portal, not by a custom form, so it
        # cannot be skipped by navigating straight to a project.
        cur.execute("ALTER TABLE portal_clients ADD COLUMN IF NOT EXISTS "
                    "must_change_password BOOLEAN NOT NULL DEFAULT FALSE;")
        cur.execute("ALTER TABLE portal_clients ADD COLUMN IF NOT EXISTS "
                    "password_changed_at TIMESTAMPTZ;")
        # Sign-in codes and password resets share the owner's login_otps table.
        # `purpose` keeps a reset code from working as a login code.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS login_otps (
                id SERIAL PRIMARY KEY,
                role VARCHAR(50) NOT NULL,
                actor_id INTEGER,
                email VARCHAR(255),
                code_hash VARCHAR(255) NOT NULL,
                attempts INTEGER DEFAULT 0,
                consumed BOOLEAN DEFAULT FALSE,
                expires_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("ALTER TABLE login_otps ADD COLUMN IF NOT EXISTS "
                    "purpose VARCHAR(40) NOT NULL DEFAULT 'login';")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_login_otps_lookup "
                    "ON login_otps(role, actor_id, purpose, created_at DESC);")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS portal_project_access (
                id SERIAL PRIMARY KEY,
                client_id INTEGER NOT NULL REFERENCES portal_clients(id) ON DELETE CASCADE,
                bid_id INTEGER NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (client_id, bid_id)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS portal_messages (
                id SERIAL PRIMARY KEY,
                bid_id INTEGER NOT NULL,
                client_id INTEGER REFERENCES portal_clients(id) ON DELETE CASCADE,
                sender_role VARCHAR(20) NOT NULL DEFAULT 'client',
                body TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                read_at TIMESTAMPTZ
            );
        """)
        # Office replies have no client id of their own; the thread is keyed by
        # bid_id. Drop the old NOT NULL for databases created before this.
        cur.execute("ALTER TABLE portal_messages ALTER COLUMN client_id DROP NOT NULL;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_portal_access_client ON portal_project_access(client_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_portal_messages_bid ON portal_messages(bid_id, created_at);")
        # One row per card payment, keyed by Zoho's payment id. The unique key
        # is what makes the browser callback, a page refresh and a webhook
        # converge on one recorded payment instead of three.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS portal_payments (
                id SERIAL PRIMARY KEY,
                bid_id INTEGER NOT NULL,
                invoice_id INTEGER NOT NULL,
                payments_session_id VARCHAR(80),
                payment_id VARCHAR(80) NOT NULL UNIQUE,
                amount_cents INTEGER NOT NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'succeeded',
                raw TEXT,
                zoho_pushed_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_portal_payments_invoice "
                    "ON portal_payments(invoice_id, created_at DESC);")
        # A client saying "I sent Zelle" is not a payment. It is a dated claim
        # with a confirmation number that the office can reconcile.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS portal_payment_claims (
                id SERIAL PRIMARY KEY,
                bid_id INTEGER NOT NULL,
                invoice_id INTEGER NOT NULL,
                reference VARCHAR(40) NOT NULL UNIQUE,
                method VARCHAR(20) NOT NULL,
                amount_cents INTEGER NOT NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'claimed',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        # Every checkout session is issued for one invoice and one amount. The
        # confirm step must find its session here, so a payment id from another
        # invoice cannot be replayed against this one.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS portal_payment_sessions (
                id SERIAL PRIMARY KEY,
                session_id VARCHAR(80) NOT NULL UNIQUE,
                bid_id INTEGER NOT NULL,
                invoice_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL,
                used_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
    conn.commit()


# --- passwords ---------------------------------------------------------------
def hash_password(password: str) -> str:
    raw = (password or "").encode("utf-8")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw, salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, expected = (stored or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"),
            bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (TypeError, ValueError):
        return False


# --- accounts ----------------------------------------------------------------
def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def client_by_email(conn, email: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM portal_clients WHERE LOWER(email) = %s AND is_active LIMIT 1;",
            (normalize_email(email),))
        return cur.fetchone()


def client_by_id(conn, client_id: int):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM portal_clients WHERE id = %s AND is_active;", (client_id,))
        return cur.fetchone()


def create_client(conn, email: str, name: str, password: str) -> int:
    email = normalize_email(email)
    if "@" not in email or " " in email:
        raise ValueError("Enter a valid email address.")
    if len(password or "") < 8:
        raise ValueError("The password must be at least 8 characters.")
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO portal_clients (email, name, password_hash)
            VALUES (%s, %s, %s)
            ON CONFLICT (email) DO UPDATE SET
                name = EXCLUDED.name,
                password_hash = EXCLUDED.password_hash,
                is_active = TRUE
            RETURNING id;
        """, (email, (name or "").strip()[:255], hash_password(password)))
        client_id = cur.fetchone()["id"]
    conn.commit()
    return client_id


def set_password(conn, client_id: int, password: str, *, must_change: bool = False) -> None:
    if len(password or "") < 8:
        raise ValueError("The password must be at least 8 characters.")
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE portal_clients
               SET password_hash = %s, must_change_password = %s,
                   password_changed_at = now()
             WHERE id = %s;
        """, (hash_password(password), bool(must_change), int(client_id)))
    conn.commit()


def temporary_password(length: int = 12) -> str:
    """Readable over the phone, but not guessable.

    Excludes 0/O/1/l/I because a temporary password gets read aloud and typed
    from a text message.
    """
    letters = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz"
    digits = "23456789"
    alphabet = letters + digits
    while True:
        raw = "".join(secrets.choice(alphabet) for _ in range(max(10, length)))
        if any(c.isdigit() for c in raw) and any(c.isalpha() for c in raw):
            return raw


def ensure_client_for_bid(conn, bid: dict) -> dict:
    """The portal account for this bid's contact, creating one if needed.

    Returns ``{"client_id", "email", "password", "created", "must_change"}``.
    ``password`` is only ever set for a newly created account -- resending a
    link must not silently reset a password the client has already chosen.
    """
    email = normalize_email(bid.get("contact_email"))
    if "@" not in email or " " in email:
        raise ValueError("This bid has no usable contact email, so there is no "
                         "login to create. Add one under Contact first.")
    name = (bid.get("contact_name") or bid.get("name") or "").strip()[:255]
    existing = client_by_email(conn, email)
    if existing:
        with conn.cursor() as cur:
            cur.execute("UPDATE portal_clients SET is_active = TRUE, "
                        "name = COALESCE(NULLIF(%s, ''), name) WHERE id = %s;",
                        (name, existing["id"]))
        conn.commit()
        return {"client_id": existing["id"], "email": email, "password": None,
                "created": False, "must_change": bool(existing.get("must_change_password"))}
    password = temporary_password()
    client_id = create_client(conn, email, name, password)
    with conn.cursor() as cur:
        cur.execute("UPDATE portal_clients SET must_change_password = TRUE WHERE id = %s;",
                    (client_id,))
    conn.commit()
    return {"client_id": client_id, "email": email, "password": password,
            "created": True, "must_change": True}


def link_bid(conn, client_id: int, bid_id: int) -> None:
    link_project(conn, client_id, bid_id)


# --- sign-in codes (portal only) ---------------------------------------------
def _env_flag(name: str):
    raw = (os.getenv(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def otp_enabled(conn) -> bool:
    """Clients get an emailed code by default. A deploy can force it either way."""
    forced = _env_flag("PORTAL_OTP_ENABLED")
    if forced is not None:
        return forced
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'portal_otp_enabled';")
            row = cur.fetchone()
        raw = (row.get("value") if row else "true") or "true"
        return str(raw).strip().lower() not in ("0", "false", "no", "off")
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return True


def disabled_otp_reason() -> str:
    return ("Sign-in codes are turned off for the client portal. "
            "Set PORTAL_OTP_ENABLED=true or use Settings → Security.")


def store_otp(conn, client_id: int, email: str, code: str,
              purpose: str = OTP_LOGIN) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE login_otps SET consumed = TRUE
             WHERE role = 'customer' AND actor_id = %s
               AND purpose = %s AND consumed = FALSE;
        """, (int(client_id), purpose))
        cur.execute("""
            INSERT INTO login_otps (role, actor_id, email, code_hash, purpose, expires_at)
            VALUES ('customer', %s, %s, %s, %s, now() + interval '10 minutes');
        """, (int(client_id), normalize_email(email), auth_service.hash_otp(code), purpose))
    conn.commit()


def verify_otp_code(conn, client_id: int, purpose: str, code: str) -> tuple[bool, str]:
    """Consume one code. Returns (ok, customer-facing reason when not)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM login_otps
             WHERE role = 'customer' AND actor_id = %s AND purpose = %s
               AND consumed = FALSE
             ORDER BY id DESC LIMIT 1;
        """, (int(client_id), purpose))
        row = cur.fetchone()
        if not row:
            return False, "That code has expired. Request a new one."
        expires = row.get("expires_at")
        if expires is not None and getattr(expires, "tzinfo", None) is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires and expires < datetime.now(timezone.utc):
            return False, "That code has expired. Request a new one."
        if int(row.get("attempts") or 0) >= MAX_OTP_ATTEMPTS:
            return False, "Too many attempts. Request a new code."
        if not auth_service.verify_otp((code or "").strip(), row.get("code_hash") or ""):
            cur.execute("UPDATE login_otps SET attempts = attempts + 1 WHERE id = %s;",
                        (row["id"],))
            conn.commit()
            return False, "That code is not correct."
        cur.execute("UPDATE login_otps SET consumed = TRUE WHERE id = %s;", (row["id"],))
    conn.commit()
    return True, ""


def issue_login_code(conn, client: dict) -> str:
    code = auth_service.generate_otp()
    store_otp(conn, int(client["id"]), client.get("email") or "", code, OTP_LOGIN)
    return code



def link_project(conn, client_id: int, bid_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO portal_project_access (client_id, bid_id)
            VALUES (%s, %s) ON CONFLICT (client_id, bid_id) DO NOTHING;
        """, (client_id, int(bid_id)))
    conn.commit()


def touch_login(conn, client_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE portal_clients SET last_login_at = now() WHERE id = %s;", (client_id,))
    conn.commit()


# --- project access ----------------------------------------------------------
def accessible_bids(conn, client_id: int, email: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT b.*
            FROM bid_opportunities b
            WHERE COALESCE(b.company, 'construction') = 'construction'
              AND (
                EXISTS (
                    SELECT 1 FROM portal_project_access a
                    WHERE a.client_id = %s AND a.bid_id = b.id
                )
                OR (
                    b.contact_email IS NOT NULL
                    AND LOWER(BTRIM(b.contact_email)) = LOWER(%s)
                )
              )
            ORDER BY COALESCE(b.updated_at, b.created_at) DESC NULLS LAST, b.id DESC;
        """, (int(client_id), normalize_email(email)))
        return cur.fetchall()


def can_view_bid(conn, client_id: int, email: str, bid_id: int) -> bool:
    return any(int(b["id"]) == int(bid_id) for b in accessible_bids(conn, client_id, email))


# --- portal data -------------------------------------------------------------
def portal_data(conn, bid_id: int, client_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM bid_opportunities WHERE id = %s;", (int(bid_id),))
        bid = cur.fetchone() or {}

        project = None
        try:
            cur.execute(
                "SELECT * FROM projects WHERE bid_id = %s ORDER BY id DESC LIMIT 1;",
                (int(bid_id),))
            project = cur.fetchone()
        except Exception:
            pass

        cur.execute("""
            SELECT id, invoice_number, issued_date, due_date, status, amount, collected,
                   description, kind, zoho_invoice_url
            FROM bid_invoices WHERE bid_id = %s ORDER BY id;
        """, (int(bid_id),))
        invoices = cur.fetchall()

        cur.execute("""
            SELECT id, filename, key, url, content_type, bytes, document_type, uploaded_at
            FROM bid_documents
            WHERE bid_id = %s AND superseded_at IS NULL
            ORDER BY uploaded_at DESC NULLS LAST, id DESC;
        """, (int(bid_id),))
        documents = cur.fetchall()

        photos = []
        if project:
            cur.execute("""
                SELECT id, filename, caption, uploaded_at
                FROM job_photos WHERE project_id = %s
                ORDER BY uploaded_at DESC NULLS LAST, id DESC;
            """, (project["id"],))
            photos = cur.fetchall()

        cur.execute("""
            SELECT id, sender_role, body, created_at
            FROM portal_messages WHERE bid_id = %s ORDER BY created_at, id;
        """, (int(bid_id),))
        messages = cur.fetchall()

    # Old local-disk uploads were deleted by deploys. The bid card keeps those
    # rows as history, but the client portal must not show a 404 as a document.
    documents = [
        d for d in documents
        if (not d.get("key") and (d.get("url") or "").startswith("http"))
        or (d.get("key") and photo_store.exists(d["key"]))
    ]
    photos = [
        p for p in photos
        if not p.get("filename") or photo_store.exists(p["filename"])
    ]
    for doc in documents:
        doc["href"] = photo_store.url_for(doc.get("key") or doc.get("url") or "")
    for photo in photos:
        photo["href"] = photo_store.url_for(photo.get("filename") or "")

    contract = float(bid.get("contract_value") or bid.get("bid_amount") or 0)
    paid = sum(float(i.get("collected") or 0) for i in invoices)
    balance = max(0.0, contract - paid)
    progress = min(100, round((paid / contract) * 100)) if contract else 0
    next_invoice = next(
        (i for i in invoices if (i.get("status") or "").lower() not in ("paid", "void")),
        None)

    return {
        "bid": bid,
        "project": project,
        "invoices": invoices,
        "documents": documents,
        "photos": photos,
        "messages": messages,
        "contract": contract,
        "paid": paid,
        "balance": balance,
        "progress": progress,
        "next_invoice": next_invoice,
    }


def post_message(conn, bid_id: int, client_id, body: str, role: str = "client") -> int:
    text = (body or "").strip()
    if not text:
        raise ValueError("Message is empty.")
    cid = int(client_id) if client_id not in (None, "", 0) else None
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO portal_messages (bid_id, client_id, sender_role, body)
            VALUES (%s, %s, %s, %s) RETURNING id;
        """, (int(bid_id), cid, role, text[:4000]))
        message_id = cur.fetchone()["id"]
    conn.commit()
    return message_id


def messages_for_bid(conn, bid_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, sender_role, body, created_at
            FROM portal_messages WHERE bid_id = %s ORDER BY created_at, id;
        """, (int(bid_id),))
        return cur.fetchall()


def linked_client_for_bid(conn, bid_id: int):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.* FROM portal_clients c
            JOIN portal_project_access a ON a.client_id = c.id
            WHERE a.bid_id = %s ORDER BY c.id LIMIT 1;
        """, (int(bid_id),))
        return cur.fetchone()


def inbox_threads(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT b.id AS bid_id, b.name, b.contact_name, b.contact_email,
                   COUNT(m.id) AS message_count, MAX(m.created_at) AS last_at,
                   (SELECT body FROM portal_messages m2
                    WHERE m2.bid_id = b.id
                    ORDER BY m2.created_at DESC, m2.id DESC LIMIT 1) AS last_body
            FROM bid_opportunities b
            JOIN portal_messages m ON m.bid_id = b.id
            GROUP BY b.id, b.name, b.contact_name, b.contact_email
            ORDER BY last_at DESC;
        """)
        return cur.fetchall()


# --- money owed ---------------------------------------------------------------
def amount_due(invoice: dict) -> float:
    """What is still owed on one invoice. A void invoice owes nothing."""
    if str((invoice or {}).get("status") or "").lower() == "void":
        return 0.0
    try:
        amount = float((invoice or {}).get("amount") or 0)
        collected = float((invoice or {}).get("collected") or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, amount - collected), 2)


def open_invoices(invoices: list) -> list:
    """Invoices a client can still pay, oldest first."""
    return [i for i in (invoices or [])
            if str(i.get("status") or "").lower() not in ("paid", "void")
            and amount_due(i) > 0]


def invoice_for_client(conn, client_id: int, email: str, bid_id: int, invoice_id: int):
    """One invoice, only if this client may see the bid it belongs to."""
    if not can_view_bid(conn, client_id, email, bid_id):
        return None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM bid_invoices
            WHERE id = %s AND bid_id = %s AND company = 'construction';
        """, (int(invoice_id), int(bid_id)))
        return cur.fetchone()


# --- settings ------------------------------------------------------------------
def set_setting(conn, key: str, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
        """, (key, value))
    conn.commit()


# --- card payments -------------------------------------------------------------
def existing_payment(conn, payment_id: str):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM portal_payments WHERE payment_id = %s;",
                    ((payment_id or "").strip(),))
        return cur.fetchone()


def record_payment(conn, *, bid_id: int, invoice_id: int, payment_id: str,
                   amount_cents: int, payments_session_id: str = "",
                   status: str = "succeeded", raw: str = "") -> tuple[dict, bool]:
    """Insert one payment. Returns (row, created). An existing id is never re-posted."""
    pid = (payment_id or "").strip()
    if not pid:
        raise ValueError("A card payment needs a Zoho payment id.")
    existing = existing_payment(conn, pid)
    if existing:
        return existing, False
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO portal_payments
                (bid_id, invoice_id, payments_session_id, payment_id,
                 amount_cents, status, raw)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (payment_id) DO NOTHING
            RETURNING *;
        """, (int(bid_id), int(invoice_id), (payments_session_id or "")[:80] or None,
              pid[:80], int(amount_cents), (status or "succeeded")[:30],
              (raw or "")[:4000] or None))
        row = cur.fetchone()
    conn.commit()
    if not row:
        return existing_payment(conn, pid), False
    return row, True


def mark_zoho_pushed(conn, payment_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE portal_payments SET zoho_pushed_at = now() "
                    "WHERE payment_id = %s;", ((payment_id or "").strip(),))
    conn.commit()


def confirmation_number(payment_id: str) -> str:
    """A short, spoken-aloud confirmation that points back at the payment."""
    tail = "".join(ch for ch in str(payment_id or "") if ch.isalnum())[-6:].upper() or "000000"
    return f"PAY-{tail}"


def record_claim(conn, *, bid_id: int, invoice_id: int, method: str,
                 amount_cents: int) -> dict:
    ref = "SENT-" + secrets.token_hex(3).upper()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO portal_payment_claims
                (bid_id, invoice_id, reference, method, amount_cents)
            VALUES (%s, %s, %s, %s, %s) RETURNING *;
        """, (int(bid_id), int(invoice_id), ref, (method or "zelle")[:20],
              int(amount_cents)))
        row = cur.fetchone()
    conn.commit()
    return row


def record_session(conn, *, session_id: str, bid_id: int, invoice_id: int,
                   amount_cents: int) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO portal_payment_sessions
                (session_id, bid_id, invoice_id, amount_cents)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (session_id) DO NOTHING;
        """, ((session_id or "")[:80], int(bid_id), int(invoice_id), int(amount_cents)))
    conn.commit()


def session_for_invoice(conn, session_id: str, bid_id: int, invoice_id: int):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM portal_payment_sessions
             WHERE session_id = %s AND bid_id = %s AND invoice_id = %s
               AND used_at IS NULL AND created_at > now() - interval '30 minutes';
        """, ((session_id or "").strip(), int(bid_id), int(invoice_id)))
        return cur.fetchone()


def mark_session_used(conn, session_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE portal_payment_sessions SET used_at = now() "
                    "WHERE session_id = %s;", ((session_id or "").strip(),))
    conn.commit()
