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
import secrets

import auth_service
import photo_store

COOKIE = "portal_session"
ROLE = "customer"
PBKDF2_ITERATIONS = 390_000


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
                client_id INTEGER NOT NULL REFERENCES portal_clients(id) ON DELETE CASCADE,
                sender_role VARCHAR(20) NOT NULL DEFAULT 'client',
                body TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                read_at TIMESTAMPTZ
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_portal_access_client ON portal_project_access(client_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_portal_messages_bid ON portal_messages(bid_id, created_at);")
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
                   description, kind
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


def post_message(conn, bid_id: int, client_id: int, body: str, role: str = "client") -> int:
    text = (body or "").strip()
    if not text:
        raise ValueError("Message is empty.")
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO portal_messages (bid_id, client_id, sender_role, body)
            VALUES (%s, %s, %s, %s) RETURNING id;
        """, (int(bid_id), int(client_id), role, text[:4000]))
        message_id = cur.fetchone()["id"]
    conn.commit()
    return message_id
