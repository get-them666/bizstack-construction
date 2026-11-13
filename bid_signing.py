"""Electronic signature for a bid's paperwork.

The owner signs once, on the bid card, by drawing in a canvas. The strokes are
stored with an audit record -- who signed, when, from what address, and the
SHA-256 of the document that was on screen at the time -- and stamped into the
proposal, the package and the contract whenever they are generated.

Two decisions worth recording:

* **Drawn, not typed.** This repo ships no TTF, so FPDF's core fonts cannot
  render handwriting. Setting "Shaun David OLeary" in Helvetica Bold is not a
  signature, it is a name in a box. The strokes the owner actually draws are the
  only honest option.
* **Nothing is stored as a finished PDF.** The signature is applied at
  generation time, so a bid that is re-priced after signing cannot go out
  carrying a signature that was put to the old numbers. The audit row keeps the
  hash of what was actually signed, which is the part that matters if the
  question is ever asked.
"""

from __future__ import annotations

import base64
import hashlib
import io

from PIL import Image

# A signature is a few KB. Anything larger is a photograph that arrived in the
# wrong field, and it should not go into a BYTEA column.
MAX_PNG_BYTES = 400_000
# Below this the stroke has no ink worth stamping -- an empty canvas, or a
# single stray tap.
MIN_INK_PIXELS = 40


def ensure_schema(conn) -> None:
    """Idempotent. Safe on every boot."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bid_signatures (
                id SERIAL PRIMARY KEY,
                bid_id INTEGER NOT NULL REFERENCES bid_opportunities(id) ON DELETE CASCADE,
                signer_name VARCHAR(160) NOT NULL,
                signer_email VARCHAR(255),
                signed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ip VARCHAR(64),
                user_agent VARCHAR(400),
                method VARCHAR(20) DEFAULT 'drawn',
                image_png BYTEA,
                document_sha256 CHAR(64),
                company VARCHAR(40) DEFAULT 'construction',
                signer_role VARCHAR(20) NOT NULL DEFAULT 'owner'
            )
        """)
        # The homeowner signs through a link now. Both signatures live in this
        # table, but only the owner's belongs on the contractor line of a
        # generated PDF -- separating them stops a client signature replacing
        # the owner's on every document built afterwards.
        cur.execute("ALTER TABLE bid_signatures ADD COLUMN IF NOT EXISTS "
                    "signer_role VARCHAR(20) NOT NULL DEFAULT 'owner';")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_signatures_bid "
                    "ON bid_signatures(bid_id, company);")
    conn.commit()


def latest(conn, bid_id: int, company: str = "construction", role: str = "owner"):
    """The signature currently in force for this bid, or None.

    History is kept rather than overwritten: if a signature is replaced, the
    earlier row is still the record of what was signed and when.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, bid_id, signer_name, signer_email, signed_at, ip, "
            "       user_agent, method, document_sha256, company, signer_role, "
            "       octet_length(image_png) AS png_bytes "
            "FROM bid_signatures WHERE bid_id = %s AND company = %s "
            "AND signer_role = %s ORDER BY id DESC LIMIT 1",
            (bid_id, company, role))
        return cur.fetchone()


def latest_client(conn, bid_id: int, company: str = "construction"):
    """The homeowner's acceptance signature, which is not the owner's."""
    return latest(conn, bid_id, company, role="client")


def _decode_png(data: str) -> bytes:
    """Accept a canvas data URL or bare base64, and return PNG bytes."""
    raw = (data or "").strip()
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    try:
        png = base64.b64decode(raw, validate=False)
    except Exception:
        raise ValueError("That signature could not be read. Try drawing it again.")
    if not png:
        raise ValueError("The signature was empty. Draw it again.")
    if len(png) > MAX_PNG_BYTES:
        raise ValueError("That image is too large for a signature. Draw it again.")
    return png


def _trim(png: bytes) -> bytes:
    """Crop to the ink and keep transparency, so the stamp sits on the line.

    A canvas is mostly empty. Stamping it untrimmed leaves the signature floating
    wherever the owner happened to start drawing, and scaled down by the empty
    margins.
    """
    try:
        im = Image.open(io.BytesIO(png)).convert("RGBA")
    except Exception:
        raise ValueError("That signature is not a readable image. Draw it again.")
    alpha = im.getchannel("A")
    box = alpha.getbbox()
    if not box:
        raise ValueError("The signature was blank. Draw it again.")
    # Count opaque pixels from the histogram rather than iterating: getdata is
    # deprecated in Pillow 14 and this runs on every save.
    ink = sum(alpha.histogram()[33:])
    if ink < MIN_INK_PIXELS:
        raise ValueError("That is too small to be a signature. Draw it again.")
    out = io.BytesIO()
    im.crop(box).save(out, "PNG", optimize=True)
    return out.getvalue()


def save(conn, bid_id: int, *, signature_data: str, signer_name: str,
         signer_email: str = "", document_sha256: str = "", ip: str = "",
         user_agent: str = "", company: str = "construction",
         signer_role: str = "owner") -> dict:
    """Validate, trim and store one signature. Raises ValueError with a message
    that can be shown to the owner as-is."""
    name = (signer_name or "").strip()
    if not name:
        raise ValueError("A signature needs a printed name beside it.")
    png = _trim(_decode_png(signature_data))
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO bid_signatures
                (bid_id, signer_name, signer_email, ip, user_agent, method,
                 image_png, document_sha256, company, signer_role)
            VALUES (%s,%s,%s,%s,%s,'drawn',%s,%s,%s,%s) RETURNING id, signed_at
        """, (bid_id, name[:160], (signer_email or "")[:255] or None,
              (ip or "")[:64] or None, (user_agent or "")[:400] or None,
              png, (document_sha256 or "")[:64] or None, company,
              "client" if signer_role == "client" else "owner"))
        row = cur.fetchone()
    conn.commit()
    return {"id": row["id"], "signed_at": row["signed_at"], "signer_name": name}


def image_png(conn, signature_id: int) -> bytes | None:
    with conn.cursor() as cur:
        cur.execute("SELECT image_png FROM bid_signatures WHERE id = %s",
                    (signature_id,))
        row = cur.fetchone()
    return bytes(row["image_png"]) if row and row["image_png"] else None


def draw(pdf, png: bytes, x: float, y: float, w: float, h: float) -> None:
    """Place the signature image. Keeps its aspect ratio inside the box."""
    pdf.image(io.BytesIO(png), x=x, y=y, w=w, h=h, keep_aspect_ratio=True)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def describe(row: dict | None) -> dict:
    """What the bid card shows about the signature, without the image."""
    if not row:
        return {"signed": False}
    when = row.get("signed_at")
    return {
        "signed": True,
        "id": row.get("id"),
        "name": row.get("signer_name"),
        "email": row.get("signer_email"),
        "signed_at": when.strftime("%Y-%m-%d %H:%M UTC") if hasattr(when, "strftime") else str(when or ""),
        "method": row.get("method") or "drawn",
        "document_sha256": (row.get("document_sha256") or "")[:16],
    }
