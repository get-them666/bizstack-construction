"""send_bid_inquiry must respect the federal block and must not fake a send.

Two real defects, both found by reading the database rather than the code:

1. The federal block was armed but not consulted on this path. blocked_reason()
   exists and auto_reply_to_lead calls it -- the emailbot correctly logged
   "withheld" for sam-gov leads -- but send_bid_inquiry never did. SAM.gov leads
   were emailed straight to .gov and .mil points of contact, which is precisely
   what the block exists to prevent.

2. documents_service.send_email returns False when the Gmail transport is
   unavailable, and that return value was discarded. The comms_logs row was
   written and True returned no matter what. That produced 194,660 "sent" rows
   against 85 distinct recipients, and since every owner-facing number is a
   COUNT over comms_logs, the dashboard showed a working bot while nothing had
   left the building.

Both are checked here against a fake db and a stubbed transport. No network.
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


class FakeCursor:
    def __init__(self, log, rows=None):
        self.log = log
        self._rows = rows if rows is not None else [None]

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDb:
    def __init__(self, rows=None):
        self.log = []
        self._rows = rows

    def cursor(self):
        return FakeCursor(self.log, list(self._rows) if self._rows else None)

    def commit(self):
        pass

    def inserts(self):
        return [(s, p) for s, p in self.log if s.upper().startswith("INSERT")]

    def touched(self):
        return [p for s, p in self.log if s.upper().startswith("INSERT")]


import auto_reply  # noqa: E402
import documents_service  # noqa: E402

# The block is on by default. Both tests below depend on that.
os.environ.pop("LEAD_SOURCE_EMAIL_BLOCK", None)
os.environ.pop("BLOCK_GOV_MIL_EMAIL", None)
os.environ.pop("LEAD_EMAIL_DAILY_CAP", None)


def _stub_transport(results, sent=None):
    """Replace the Gmail path. `results` is the per-call True/False queue;
    `sent` collects the addresses the transport was actually handed.

    The two must be separate lists: one call earlier this reused a single list
    for both, and resetting the recorded addresses also reset the pending return
    values, so a call meant to fail returned True instead.

    Must be async: the caller wraps this in run_coro, which rejects a plain
    value with "an asyncio.Future, a coroutine or an awaitable is required".
    """
    sent = [] if sent is None else sent

    async def fake_send(cfg, to, subject, body, **kw):
        sent.append(to)
        return results.pop(0) if results else True

    documents_service.send_email = fake_send
    auto_reply.documents_service.send_email = fake_send
    auto_reply.documents_service.smtp_configured = lambda cfg: True
    auto_reply.documents_service.smtp_config_from_env = lambda: {"SMTP_FROM": "x@y.com",
                                                                 "SMTP_NAME": "BizStack"}
    return sent


print("[1] a federal point of contact is never emailed")


def _gov_attempt(addr):
    """send_bid_inquiry is the SAM.gov-only path, so it has no source param:
    every address it is handed is a federal solicitation POC by definition."""
    sent = _stub_transport([True])
    db = FakeDb()
    res = auto_reply.send_bid_inquiry(db, "construction", title="Roof", email=addr,
                                      lead_id=99, source="sam-gov")
    return res, sent, db


for addr in ("chamel.r.adams.civ@us.navy.mil", "Ellie_Delerme-Velez@ios.doi.gov",
             "someone@va.gov", "buyer@arlington.va.us"):
    res, sent, db = _gov_attempt(addr)
    check(f"{addr} is refused", res is False, res)
    # A refused recipient must not even reach the transport. `sent` collects the
    # address, so it must stay empty.
    check(f"{addr} never reached the transport", sent == [], sent)
    check(f"{addr} logged no touch", not db.inserts(), db.inserts())

res, sent, db = _gov_attempt("contractor@roofers.com")
check("this path refuses every recipient, not just .gov ones", res is False, res)
check("...and nothing was sent", sent == [], sent)

print("\n[2] the refusal is recorded so it is auditable")
res, sent, db = _gov_attempt("buyer@va.gov")
check("the lead's notes get the reason",
      any("UPDATE leads SET notes" in s for s, _ in db.log), [s for s, _ in db.log])
check("the reason names the federal domain",
      any("federal" in str(p).lower() or "sam-gov" in str(p).lower()
          for s, p in db.log if "UPDATE leads" in s),
      [p for s, p in db.log if "UPDATE leads" in s])

print("\n[3] the block can still be lifted deliberately, and then mail flows")
os.environ["BLOCK_GOV_MIL_EMAIL"] = "0"
os.environ["LEAD_SOURCE_EMAIL_BLOCK"] = ""
sent = _stub_transport([True])
db = FakeDb()
os.environ["BLOCK_GOV_MIL_EMAIL"] = "0"
os.environ["LEAD_SOURCE_EMAIL_BLOCK"] = ""
res = auto_reply.send_bid_inquiry(db, "construction", title="Roof",
                                  email="buyer@va.gov", lead_id=7, source="sam-gov")
check("with the block off, a federal address may be emailed", res is True, res)
check("...and exactly that address reached the transport",
      sent == ["buyer@va.gov"], sent)
os.environ["BLOCK_GOV_MIL_EMAIL"] = "1"
os.environ["LEAD_SOURCE_EMAIL_BLOCK"] = "sam-gov"

print("\n[4] a send that did not deliver is not logged as sent")
# The defect: send_email returning False still produced a comms_logs row and a
# True return, which is how 194,660 phantom sends got counted as real.
sent = _stub_transport([False])
db = FakeDb()
res = auto_reply.send_bid_inquiry(db, "construction", title="Deck", email="jo@example.com",
                                  lead_id=5, source="permit_finder")
check("a failed send returns False", res is False, res)
check("a failed send writes NO comms_logs row", not db.inserts(), db.inserts())
check("the transport was called with that address", sent == ["jo@example.com"], sent)

print("\n[5] a send that did deliver is logged exactly once")
sent = _stub_transport([True])
db = FakeDb()
res = auto_reply.send_bid_inquiry(db, "construction", title="Deck", email="jo@example.com",
                                  lead_id=5, source="permit_finder")
check("a delivered send returns True", res is True, res)
check("exactly one comms_logs row", len(db.inserts()) == 1, db.inserts())
check("the row is the recipient, outbound, email",
      db.inserts() and "comms_logs" in db.inserts()[0][0]
      and "'outbound'" in db.inserts()[0][0] and "'email'" in db.inserts()[0][0],
      db.inserts())

print("\n[6] the count that matters: 85 real recipients, not 194,660")
# Sanity check on the shape of the fix rather than the data. Every logged row
# must correspond to a recipient that was actually mailed, so the dashboard can
# no longer overstate.
sends = []
db = FakeDb()
for i, ok in enumerate([True, True, False, True]):
    _stub_transport([ok])
    # The block is lifted for this section so the delivery accounting can be
    # tested without a real transport.
    os.environ["BLOCK_GOV_MIL_EMAIL"] = "0"
    auto_reply.send_bid_inquiry(db, "construction", title="T",
                                email=f"lead{i}@example.com", lead_id=i,
                                source="permit_finder")
# The insert params are (recipient, body) -- p[0] is the address.
logged = {p[0] for _, p in db.inserts() if p}
check("3 of 4 attempts logged", len(logged) == 3, sorted(logged))
check("the failed one is absent", "lead2@example.com" not in logged, sorted(logged))
check("the three delivered ones are present",
      logged == {"lead0@example.com", "lead1@example.com", "lead3@example.com"}, sorted(logged))

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): {FAILS}")
    sys.exit(1)
print("All bid-inquiry tests passed.")
