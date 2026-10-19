"""Photo and file storage, on Cloudflare R2 when available.

Why this exists
---------------
Uploads were written straight to ``uploads/`` on the app's own disk. On
Railway that disk is ephemeral unless a volume is mounted, and the only volume
attached was Postgres -- so every deploy deleted the crew's site photos. The
bug was invisible because the write succeeded and the URL came back fine; the
file was simply gone by the next deploy.

R2 is the fix: 10 GB free, no egress fee, and S3-compatible, so ``boto3`` --
already in requirements.txt -- talks to it with no new dependency.

The fallback is deliberately loud
---------------------------------
When R2 is not configured this writes to local disk and still works, because a
hard failure would take photo upload down entirely. But it is never silent:
:func:`save` returns ``backend="local"``, :func:`status` reports the same, and
the process says so once at startup. Local storage is a known-lossy mode, not
a supported destination, and the response tells the caller which one they got.

Serving
-------
Keys are stored, not URLs, so a photo uploaded before R2 was configured keeps
working and keeps resolving after it is. ``/media/{key}`` serves from whichever
backend holds the object, which is why a quote's ``photo_path`` is still valid a
year later.

env: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET,
     R2_PUBLIC_URL (optional; enables direct browser delivery)
"""

from __future__ import annotations

import os
import re
import time

DEFAULT_LOCAL_DIR = os.getenv("UPLOAD_DIR", "uploads")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".pdf"}
MAX_BYTES = int(os.getenv("UPLOAD_MAX_BYTES", str(20 * 1024 * 1024)))

_warned_local = False
_client = None


def config() -> dict:
    return {
        "account_id": (os.getenv("R2_ACCOUNT_ID") or "").strip(),
        "access_key": (os.getenv("R2_ACCESS_KEY_ID") or "").strip(),
        "secret_key": (os.getenv("R2_SECRET_ACCESS_KEY") or "").strip(),
        "bucket": (os.getenv("R2_BUCKET") or "").strip(),
        "public_url": (os.getenv("R2_PUBLIC_URL") or "").strip().rstrip("/"),
    }


def configured() -> bool:
    c = config()
    return bool(c["account_id"] and c["access_key"] and c["secret_key"] and c["bucket"])


def missing() -> list[str]:
    c = config()
    return [name for name, key in
            (("R2_ACCOUNT_ID", "account_id"), ("R2_ACCESS_KEY_ID", "access_key"),
             ("R2_SECRET_ACCESS_KEY", "secret_key"), ("R2_BUCKET", "bucket"))
            if not c[key]]


def _client():
    """Lazy so an unconfigured deployment never imports boto3."""
    global _client
    if _client is not None:
        return _client
    c = config()
    import boto3
    from botocore.config import Config
    _client = boto3.client(
        "s3",
        endpoint_url=f"https://{c['account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=c["access_key"],
        aws_secret_access_key=c["secret_key"],
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )
    return _client


def safe_key(filename: str) -> str:
    """A key that cannot escape the prefix or carry a path.

    Crew-supplied filenames are untrusted input. Anything not matching is
    replaced rather than sanitised into something that still looks like the
    original, so a crafted name cannot become ``../`` or an absolute path.
    """
    base = os.path.basename(str(filename or "")).strip()
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in ALLOWED_EXT:
        ext = ".jpg"
    stem = re.sub(r"[^A-Za-z0-9_-]", "", stem)[:40]
    return f"{stem or 'photo'}{ext}"


def validate(raw: bytes, filename: str) -> None:
    if not raw:
        raise ValueError("Empty file")
    if len(raw) > MAX_BYTES:
        raise ValueError(f"File is larger than {MAX_BYTES // (1024 * 1024)} MB")


def save(raw: bytes, filename: str, *, key_prefix: str = "") -> dict:
    """Store bytes and return where they went.

    ``key`` is the durable handle to store against a record. ``url`` is what a
    browser should fetch, and is stable across a backend change because
    ``/media/`` resolves either way.
    """
    global _warned_local
    validate(raw, filename)
    key = f"{key_prefix.strip('/')}/{safe_key(filename)}" if key_prefix else safe_key(filename)
    public = config()["public_url"]
    url = f"{public}/{key}" if public else f"/media/{key}"

    if configured():
        try:
            _client().put_object(
                Bucket=config()["bucket"], Key=key, Body=raw,
                ContentType=_content_type(key),
                CacheControl="public, max-age=31536000, immutable")
            return {"key": key, "url": url, "backend": "r2", "bytes": len(raw)}
        except Exception as e:
            # Fall through to local rather than losing the photo, but the
            # caller is told, because a silent fallback here is how the
            # original bug survived.
            print(f"⚠️ R2 upload failed ({e}); writing {key} to local disk, "
                  f"which is lost on redeploy", flush=True)

    os.makedirs(DEFAULT_LOCAL_DIR, exist_ok=True)
    flat = key.replace("/", "_")
    with open(os.path.join(DEFAULT_LOCAL_DIR, flat), "wb") as fh:
        fh.write(raw)
    if not _warned_local:
        _warned_local = True
        print(f"⚠️ photo storage is LOCAL DISK ({DEFAULT_LOCAL_DIR}), not R2. "
              f"Set {', '.join(missing())} -- files here are deleted on every "
              f"deploy.", flush=True)
    return {"key": key, "url": f"/media/{key}", "backend": "local", "bytes": len(raw)}


def read(key: str) -> tuple[bytes, str]:
    """Fetch an object from whichever backend holds it.

    R2 first, then local. A key uploaded before R2 was configured is only on
    disk; one uploaded after is only in R2. Both have to work for links that
    were already handed out.
    """
    safe = os.path.basename(str(key or "")).replace("..", "")
    if configured():
        try:
            obj = _client().get_object(Bucket=config()["bucket"], Key=safe)
            return obj["Body"].read(), obj.get("ContentType") or _content_type(safe)
        except Exception:
            pass
    path = os.path.join(DEFAULT_LOCAL_DIR, safe.replace("/", "_"))
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read(), _content_type(safe)
    raise FileNotFoundError(key)


def exists(key: str) -> bool:
    try:
        read(key)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def status() -> dict:
    """Whether photos will survive a deploy, and if not, why not."""
    if configured():
        return {"ok": True, "backend": "r2", "bucket": config()["bucket"],
                "persistent": True, "note": "photos survive redeploys"}
    return {"ok": False, "backend": "local", "dir": DEFAULT_LOCAL_DIR,
            "persistent": False, "missing": missing(),
            "note": "photos are on the app's ephemeral disk and are deleted "
                    "on every deploy"}


def _content_type(key: str) -> str:
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".gif": "image/gif",
            ".pdf": "application/pdf"}.get(key.lower()[-4:], "application/octet-stream")


def startup_check() -> dict:
    """Say so once at boot rather than discovering it after a deploy."""
    s = status()
    if not s["persistent"]:
        print(f"⚠️ photo storage: {s['note']} (missing {', '.join(s['missing'])})",
              flush=True)
    return s


_started = time.time()

def url_for(key: str) -> str:
    """A browser URL for a stored key, whether or not R2 is configured.

    A filename column written before R2 was configured holds a bare name; a new
    one holds a key. Both resolve to /media/ so a link handed out earlier
    still works after a backend change.
    """
    k = str(key or "").strip()
    if not k:
        return ""
    if k.startswith(("http://", "https://")):
        return k
    return f"/media/{k}"
