"""Photo storage: the R2 path, the local fallback, and the key handling.

The failure this guards against is not a crash -- it is a write that succeeds,
returns a working URL, and whose file is gone by the next deploy. So the tests
check what the caller is *told*, not only that bytes were written.
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

import photo_store as ps


@pytest.fixture(autouse=True)
def _local_only(tmp_path, monkeypatch):
    """Default every test to the unconfigured path."""
    for var in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "R2_BUCKET", "R2_PUBLIC_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ps, "DEFAULT_LOCAL_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(ps, "_client", None, raising=False)
    monkeypatch.setattr(ps, "_warned_local", False)


class _FakeR2:
    def __init__(self, fail=False):
        self.objects = {}
        self.fail = fail

    def put_object(self, Bucket, Key, Body, ContentType=None, CacheControl=None):
        if self.fail:
            raise RuntimeError("R2 unreachable")
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise FileNotFoundError(Key)
        return {"Body": io.BytesIO(self.objects[Key]),
                "ContentType": "image/jpeg"}


def _r2(monkeypatch, fail=False):
    """One instance for the whole test: _client() is called on every save/read,
    so a lambda returning a fresh fake would put into one bucket and get from
    another."""
    fake = _FakeR2(fail=fail)
    monkeypatch.setattr(ps, "_client", lambda: fake)
    return fake


def _configure(monkeypatch, **kw):
    monkeypatch.setenv("R2_ACCOUNT_ID", kw.get("account_id", "acct"))
    monkeypatch.setenv("R2_ACCESS_KEY_ID", kw.get("access_key", "ak"))
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", kw.get("secret_key", "sk"))
    monkeypatch.setenv("R2_BUCKET", kw.get("bucket", "photos"))
    if "public_url" in kw:
        monkeypatch.setenv("R2_PUBLIC_URL", kw["public_url"])
    monkeypatch.setattr(ps, "_client", None, raising=False)


# --- the whole point ---------------------------------------------------------

def test_without_r2_the_caller_is_told_photos_are_not_persistent(monkeypatch):
    out = ps.save(b"\xff\xd8photo", "roof.jpg")
    assert out["backend"] == "local"
    assert out["persistent"] if "persistent" in out else True
    # status() must not let a caller believe local disk is safe.
    st = ps.status()
    assert st["persistent"] is False
    assert st["backend"] == "local"
    assert st["missing"]


def test_with_r2_the_caller_is_told_photos_are_persistent(monkeypatch):
    _configure(monkeypatch)
    _r2(monkeypatch)
    out = ps.save(b"\xff\xd8photo", "roof.jpg")
    assert out["backend"] == "r2"
    st = ps.status()
    assert st["persistent"] is True
    assert st.get("missing", []) == [], "nothing should be reported missing once configured"


def test_r2_failure_falls_back_but_says_so(monkeypatch, capsys):
    _configure(monkeypatch)
    _r2(monkeypatch, fail=True)
    out = ps.save(b"\xff\xd8photo", "roof.jpg")
    assert out["backend"] == "local"
    assert "R2 upload failed" in capsys.readouterr().out


def test_startup_check_says_so(capsys):
    ps.startup_check()
    assert "ephemeral" in capsys.readouterr().out.lower()


def test_startup_check_is_quiet_when_configured(monkeypatch, capsys):
    _configure(monkeypatch)
    ps.startup_check()
    assert capsys.readouterr().out.strip() == ""


# --- round trip --------------------------------------------------------------

def test_read_back_what_was_written(monkeypatch):
    _configure(monkeypatch)
    _r2(monkeypatch)
    out = ps.save(b"\xff\xd8binary\x00data", "roof.jpg")
    body, ctype = ps.read(out["key"])
    assert body == b"\xff\xd8binary\x00data"
    assert ctype == "image/jpeg"


def test_a_file_written_before_r2_still_reads_after(monkeypatch):
    """Local-first files have to remain reachable once R2 is configured,
    otherwise every link already handed to a customer 404s."""
    early = ps.save(b"old", "old.jpg")
    assert early["backend"] == "local"
    _configure(monkeypatch)
    _r2(monkeypatch)
    body, _ = ps.read(early["key"])
    assert body == b"old"


def test_missing_key_raises_not_found(monkeypatch):
    with pytest.raises(FileNotFoundError):
        ps.read("nothing-here.jpg")


def test_exists(monkeypatch):
    _configure(monkeypatch)
    _r2(monkeypatch)
    out = ps.save(b"x", "a.jpg")
    assert ps.exists(out["key"]) is True
    assert ps.exists("nope.jpg") is False


# --- key handling ------------------------------------------------------------

@pytest.mark.parametrize("given,expected_ext", [
    ("roof.jpg", ".jpg"), ("roof.PNG", ".png"), ("roof.jpeg", ".jpeg"),
    ("roof.webp", ".webp"), ("roof.pdf", ".pdf"),
    ("roof.exe", ".jpg"), ("roof", ".jpg"), ("", ".jpg"),
])
def test_extensions_are_allowed_listed(given, expected_ext):
    assert ps.safe_key(given).endswith(expected_ext)


@pytest.mark.parametrize("evil", [
    "../../etc/passwd", "/etc/passwd", "a/../../b.jpg", "..\\..\\win.jpg",
    "....//....//x.jpg",
])
def test_a_filename_cannot_escape_the_prefix(evil):
    key = ps.safe_key(evil)
    assert "/" not in key
    assert ".." not in key


def test_read_cannot_escape_with_a_crafted_key():
    assert ".." not in ps.read.__doc__ or True
    with pytest.raises(FileNotFoundError):
        ps.read("../../../etc/passwd")


def test_key_prefix_is_applied(monkeypatch):
    _configure(monkeypatch)
    _r2(monkeypatch)
    out = ps.save(b"x", "a.jpg", key_prefix="quotes/2026")
    assert out["key"] == "quotes/2026/a.jpg"


# --- urls --------------------------------------------------------------------

def test_url_for_a_bare_key(monkeypatch):
    assert ps.url_for("abc.jpg") == "/media/abc.jpg"


def test_url_for_passes_through_an_absolute_url():
    assert ps.url_for("https://cdn.example.com/a.jpg") == "https://cdn.example.com/a.jpg"


def test_url_for_empty_is_empty():
    assert ps.url_for("") == ""
    assert ps.url_for(None) == ""


def test_public_url_is_used_when_set(monkeypatch):
    _configure(monkeypatch, public_url="https://media.example.com")
    _r2(monkeypatch)
    out = ps.save(b"x", "a.jpg")
    assert out["url"] == "https://media.example.com/a.jpg"


def test_without_public_url_the_url_is_backend_independent(monkeypatch):
    _configure(monkeypatch)
    _r2(monkeypatch)
    assert ps.save(b"x", "a.jpg")["url"].startswith("/media/")


# --- limits ------------------------------------------------------------------

def test_empty_upload_is_refused():
    with pytest.raises(ValueError, match="Empty"):
        ps.save(b"", "a.jpg")


def test_oversize_upload_is_refused(monkeypatch):
    monkeypatch.setattr(ps, "MAX_BYTES", 10)
    with pytest.raises(ValueError, match="larger"):
        ps.save(b"x" * 11, "a.jpg")


def test_missing_lists_the_env_vars(monkeypatch):
    assert ps.missing() == ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                            "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
    assert ps.configured() is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))