"""Email must only be logged as sent when the transport actually accepted it.

Two bugs, both in the Copilot's send_email_message handler:

1. It gated on documents_service.smtp_configured(), which requires SMTP_HOST.
   This deployment runs EMAIL_TRANSPORT=gmail, where SMTP_HOST is legitimately
   unset and the Gmail API is the working transport. So every send was refused
   with "SMTP not configured." even though mail could have gone out.

2. It then discarded the return value of documents_service.send_email(), which
   returns False on failure and never raises. So had the gate passed, it would
   have written to comms_logs and returned ok=True for mail that was never
   delivered -- the same phantom rows that had to be purged from comms_logs.

The heavy third-party deps (signalwire, psycopg, openai, fastapi) are stubbed so
construction_main imports offline. No network, no database, no SMTP.
"""

import os
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

FAILS = []


def check(name, ok, detail=""):
    detail = str(detail)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def _install_stubs():
    for name in ("signalwire", "signalwire.rest", "signalwire.rest.client"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["signalwire.rest.client"].RestClient = object
    sys.modules.setdefault("aiosmtplib", types.ModuleType("aiosmtplib"))
    if "openai" not in sys.modules:
        m = types.ModuleType("openai")
        m.OpenAI = object
        sys.modules["openai"] = m
    if "psycopg" not in sys.modules:
        pg = types.ModuleType("psycopg")
        pg.__path__ = []
        pg.connect = None
        rows = types.ModuleType("psycopg.rows")
        rows.dict_row = dict
        pg.rows = rows
        sys.modules["psycopg"] = pg
        sys.modules["psycopg.rows"] = rows


_install_stubs()

import construction_main as main  # noqa: E402


class FakeCursor:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.db.writes.append((" ".join(sql.split()), params))


class FakeDb:
    def __init__(self):
        self.writes = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass


def _handler(db):
    """Build just the send_email_message closure from build_tool_handlers."""
    # build_tool_handlers closes over many services; only the email path matters,
    # so grab the closure without invoking the rest.
    handlers = main.build_tool_handlers(db, None)
    return handlers["send_email_message"]


def _patch_transport(delivered):
    """Force documents_service.send_email to return `delivered`."""
    import documents_service

    class _DS:
        @staticmethod
        def smtp_config_from_env():
            return {"SMTP_FROM": "hello@bizstackperks.com", "SMTP_NAME": "BizStack"}

        @staticmethod
        async def send_email(cfg, to, subject, body, *a, **kw):
            return delivered

    sys.modules["documents_service"] = _DS
    return documents_service


def test_transport_rejection_is_not_logged():
    print("\n--- transport refuses: must NOT write to comms_logs ---")
    db = FakeDb()
    _patch_transport(False)          # Gmail declines (bad/absent OAuth token)
    out = _handler(db)("a@b.com", "Hi", "body")
    check("reports failure", out.get("ok") is False, out)
    check("explains the likely cause", "token" in out.get("error", "").lower(), out)
    check("no phantom comms_logs row", db.writes == [], f"logged anyway: {db.writes}")


def test_exception_is_not_logged():
    print("\n--- transport raises: must NOT write to comms_logs ---")
    db = FakeDb()

    class _DS:
        @staticmethod
        def smtp_config_from_env():
            return {"SMTP_FROM": "hello@bizstackperks.com"}

        @staticmethod
        async def send_email(*a, **kw):
            raise RuntimeError("socket timeout")

    sys.modules["documents_service"] = _DS
    out = _handler(db)("a@b.com", "Hi", "body")
    check("reports failure", out.get("ok") is False, out)
    check("no phantom comms_logs row", db.writes == [], f"logged anyway: {db.writes}")


def test_real_send_is_logged():
    print("\n--- transport accepts: must write to comms_logs ---")
    db = FakeDb()
    _patch_transport(True)
    out = _handler(db)("a@b.com", "Hi", "body")
    check("reports success", out.get("ok") is True, out)
    check("logged exactly once", len(db.writes) == 1, db.writes)
    if db.writes:
        sql, params = db.writes[0]
        check("logs sender, recipient and body", len(params) == 3, params)
        check("sender is the real from-address", params[0] == "hello@bizstackperks.com", params)


def test_missing_from_address_is_rejected():
    print("\n--- no SMTP_FROM: refuse rather than guess ---")
    db = FakeDb()

    class _DS:
        @staticmethod
        def smtp_config_from_env():
            return {"SMTP_FROM": "", "SMTP_NAME": ""}

        @staticmethod
        async def send_email(*a, **kw):
            raise AssertionError("must not attempt a send with no from-address")

    sys.modules["documents_service"] = _DS
    out = _handler(db)("a@b.com", "Hi", "body")
    check("reports failure", out.get("ok") is False, out)
    check("mentions the missing config", "smtp_from" in out.get("error", "").lower(), out)


def test_no_smtp_gate_under_gmail_transport():
    print("\n--- EMAIL_TRANSPORT=gmail must not require SMTP_HOST ---")
    # Reload the real module: earlier tests replaced it with a stub.
    import importlib

    for name in [m for m in sys.modules if m.startswith("documents_service")]:
        del sys.modules[name]
    real_ds = importlib.import_module("documents_service")

    os.environ["EMAIL_TRANSPORT"] = "gmail"
    for stale in ("SMTP_HOST", "SMTP_FROM"):
        os.environ.pop(stale, None)
    cfg = real_ds.smtp_config_from_env()
    cfg["SMTP_FROM"] = "hello@bizstackperks.com"   # the only mail var actually set

    # The guard that produced "SMTP not configured." on every send, because it
    # demanded an SMTP_HOST the Gmail transport never uses.
    check("SMTP_HOST is unset, which is correct for the Gmail transport",
          not cfg.get("SMTP_HOST"), cfg)
    check("smtp_configured() now accepts the Gmail transport",
          real_ds.smtp_configured(cfg), cfg)

    # It must still refuse when there is genuinely nothing to send from --
    # otherwise this becomes a guard that never fires.
    no_from = dict(cfg, SMTP_FROM="")
    check("but a missing sending address is still refused",
          not real_ds.smtp_configured(no_from), no_from)

    # And the legacy ladder still needs its host.
    os.environ["EMAIL_TRANSPORT"] = "legacy"
    check("legacy transport still requires SMTP_HOST",
          not real_ds.smtp_configured(cfg), cfg)
    cfg["SMTP_HOST"] = "smtp.example.com"
    check("legacy transport accepts a host when present",
          real_ds.smtp_configured(cfg), cfg)
    os.environ["EMAIL_TRANSPORT"] = "gmail"


if __name__ == "__main__":
    for fn in (
        test_transport_rejection_is_not_logged,
        test_exception_is_not_logged,
        test_real_send_is_logged,
        test_missing_from_address_is_rejected,
        test_no_smtp_gate_under_gmail_transport,
    ):
        fn()
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'All email send-accuracy tests passed.'}")
    sys.exit(1 if FAILS else 0)