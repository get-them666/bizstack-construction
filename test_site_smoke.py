"""Smoke test: every template and every page route must actually render.

Two regressions this locks down:

1. Template include paths. `Jinja2Templates(directory="templates/construction")`
   sets the loader root to that dir, so `{% include "construction/x.html" %}`
   cannot resolve and the page 500s at request time (not import time, and not
   at `get_template()` time either -- includes load during render).
2. Page routes blowing up against a real schema. We boot the app against a
   scratch database so `get_template`/render and the route bodies both run.

Set DATABASE_URL to a disposable Postgres database. The schema is created on
startup, so an empty database is fine.
"""
import os
import sys
import pathlib

import re

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


print("[1] every {% include %}/{% extends %} resolves against the real loader root")
from jinja2 import Environment, FileSystemLoader  # noqa: E402

TPL_DIR = ROOT / "templates" / "construction"
env = Environment(loader=FileSystemLoader(str(TPL_DIR)))
env.globals.update(
    company=lambda: {"name": "Test"},
    current_actor=lambda r: None,
    site_theme_state=lambda: {"css": "", "decor": ""},
)
env.filters["money"] = lambda v: f"${v}"

targets = []
for f in sorted(TPL_DIR.glob("*.html")):
    src = f.read_text()
    for ref in re.findall(r'\{%-?\s*(?:include|extends|import|from)\s+"([^"]+)"', src):
        targets.append((f.name, ref))

broken = [(src, ref) for src, ref in targets if not (TPL_DIR / ref).is_file()]
check(f"{len(targets)} include/extends targets all resolve", not broken,
      "; ".join(f"{s} -> {r}" for s, r in broken))

# Includes only load at render time, so compile alone proves nothing.
unrenderable = []
for f in sorted(TPL_DIR.glob("*.html")):
    try:
        env.get_template(f.name)
    except Exception as e:
        unrenderable.append((f.name, str(e)))
check(f"all {len(list(TPL_DIR.glob('*.html')))} templates compile", not unrenderable,
      "; ".join(f"{n}: {e}" for n, e in unrenderable))

print("\n[2] app imports and schema builds")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/postgres")
try:
    import construction_main as m
    import auth_service
    check("construction_main imports", True)
except Exception as e:
    check("construction_main imports", False, repr(e))
    print("\n" + "=" * 60)
    print("ABORT: cannot test routes without the app importing")
    raise SystemExit(1)

print("\n[3] every page route renders (no 5xx) against a real database")
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(m.app, raise_server_exceptions=False)
try:
    with client:
        client.get("/health")
    check("schema builds on startup", True)
except Exception as e:
    check("schema builds on startup", False, repr(e))
    raise SystemExit(1)

tok = auth_service.issue_session("admin", 1, "test@example.com", "Test Admin")
client.cookies.set(auth_service.SESSION_COOKIE, tok)
client.cookies.set("user_email", "test@example.com")

PAGES = [
    "/", "/services", "/about", "/portfolio", "/quote", "/contact", "/legal",
    "/dashboard", "/leads", "/leads?lane=contactable", "/leads?lane=permits",
    "/pipeline", "/pipeline?stream=permits", "/pipeline?stream=leads", "/pipeline?q=Ann",
    "/leads?status=new", "/leads?q=Ann", "/inventory", "/payments", "/payroll",
    "/projects", "/backlog", "/acquisition", "/crew", "/crew/admin", "/crew/access",
    "/appearance", "/copilot", "/app", "/instant-quote", "/get-started",
    "/health", "/api/business/summary", "/lead-sources/status",
]

bad = []
for p in PAGES:
    try:
        r = client.get(p, follow_redirects=False)
    except Exception as e:
        bad.append((p, f"EXC {e!r}")); continue
    if r.status_code >= 500:
        bad.append((p, f"{r.status_code}"))
check(f"{len(PAGES)} page routes return <500", not bad,
      "; ".join(f"{p} -> {c}" for p, c in bad))

print("\n[4] pipeline board moves cards and validates stages")
# Regression: the board's SQL originally interpolated a hand-built WHERE
# fragment with a mismatched placeholder count, which raised "no result
# available" and 500'd the page.
g = m.get_db()
pdb = next(g)
_KEEP_PDB = g          # hold the generator so psycopg doesn't close the conn
with pdb.cursor() as cur:
    cur.execute("""INSERT INTO job_leads (address, city, work_type, status, found_at)
                   VALUES ('77 Board Test Ave','Norfolk','deck','new',NOW()) RETURNING id;""")
    permit_id = cur.fetchone()["id"]
pdb.commit()

r = client.post("/api/pipeline/move", data={"stream": "permits", "item_id": permit_id, "status": "knocked"})
check("moving a card returns ok", r.status_code == 200, f"got {r.status_code}")
with pdb.cursor() as cur:
    cur.execute("SELECT status FROM job_leads WHERE id = %s;", (permit_id,))
    check("status persisted to the database", cur.fetchone()["status"] == "knocked")

r = client.post("/api/pipeline/move", data={"stream": "permits", "item_id": permit_id, "status": "bogus"})
check("unknown permit stage is rejected", r.status_code == 400, f"got {r.status_code}")
r = client.post("/api/pipeline/move", data={"stream": "nope", "item_id": permit_id, "status": "new"})
check("unknown stream is rejected", r.status_code == 400, f"got {r.status_code}")

r = client.get("/pipeline?q=Board Test")
check("board search finds the card", r.status_code == 200 and "Board Test" in r.text, f"got {r.status_code}")

# Regression: the board used require_admin(), which raises a bare 401 and left a
# signed-out user on a dead end. Every other owner page redirects to /login.
signed_out = TestClient(m.app, raise_server_exceptions=False)
r = signed_out.get("/pipeline", follow_redirects=False)
check("signed-out board redirects to login, not 401",
      r.status_code == 303 and "/login" in (r.headers.get("location") or ""),
      f"got {r.status_code} -> {r.headers.get('location')}")
signed_out.close()

# Regression: backlog rows are source='backlog' and have their own board
# stream. The leads query used to include them too, so every back-logged job
# rendered twice and was double-counted in the header totals.
with pdb.cursor() as cur:
    cur.execute("""INSERT INTO leads (name, phone, address, project_type, source, status,
                                  company, created_at)
                   VALUES ('Board Dup Job', '', '5 Dup St', 'Deck', 'backlog', 'new',
                           'construction', NOW()) RETURNING id;""")
    dup_id = cur.fetchone()["id"]
pdb.commit()
r = client.get("/pipeline?stream=leads&q=Board Dup Job")
check("back-log job is not in the leads stream", r.text.count('data-id="%d"' % dup_id) == 0,
      "back-log job leaked into the leads stream")
r = client.get("/pipeline?stream=backlog&q=Board Dup Job")
check("back-log job is in the back-log stream", r.text.count('data-id="%d"' % dup_id) == 1,
      f"expected exactly 1, got {r.text.count('data-id=\"%d\"' % dup_id)}")

print("\n[5] a disabled lead source is hidden everywhere, and reversible")
# The operator switched SAM.gov off for both companies. Leads must disappear
# from every owner-facing view without being deleted, and come back on one flag.
import os  # noqa: E402
os.environ["LEAD_SOURCES_SAM_GOV"] = "0"
check("sam-gov is hidden when disabled",
      "sam-gov" in m.hidden_lead_sources(), str(m.hidden_lead_sources()))
check("visibility clause is emitted",
      "source" in m.source_visibility_clause(), m.source_visibility_clause())
check("params line up with placeholders",
      m.source_visibility_clause().count("%s") == len(m.source_visibility_params()))

with pdb.cursor() as cur:
    cur.execute("""INSERT INTO leads (name, phone, email, address, project_type, source,
                                  status, company, created_at)
                   VALUES ('Sam Gov Hidden Co', '', 'x@lead.local', '1 Fed Way', 'Roof',
                           'sam-gov', 'new', 'construction', NOW()) RETURNING id;""")
    sam_id = cur.fetchone()["id"]
    cur.execute("""INSERT INTO leads (name, phone, email, address, project_type, source,
                                  status, company, created_at)
                   VALUES ('Visible Local Co', '757-555-9999', 'y@lead.local', '2 Local Way',
                           'Deck', 'reddit', 'new', 'construction', NOW()) RETURNING id;""")
    vis_id = cur.fetchone()["id"]
pdb.commit()

for url in ("/leads", "/pipeline", "/dashboard"):
    body = client.get(url).text
    check(f"{url} hides the sam-gov lead", "Sam Gov Hidden Co" not in body)
    check(f"{url} still shows other leads", "Visible Local Co" in body)

# And it must come back when re-enabled. Asserted per-source, not on the whole
# list: scrap-io is independently gated and off by default (it spends credits,
# and SCRAP_IO_API_KEY is unset here), so the list is legitimately non-empty and
# asserting `not hidden_lead_sources()` only passed while sam-gov was the sole
# gated source. That assertion failed the moment a second gated source landed.
os.environ["LEAD_SOURCES_SAM_GOV"] = "1"
check("sam-gov is no longer hidden when re-enabled",
      "sam-gov" not in m.hidden_lead_sources())
check("leads list shows the sam-gov lead again",
      "Sam Gov Hidden Co" in client.get("/leads").text)
os.environ["LEAD_SOURCES_SAM_GOV"] = "0"
check("sam-gov is hidden again once switched off",
      "sam-gov" in m.hidden_lead_sources())

print("\n[6] lead auto-reply does not crash on a lead with no valid phone")
# Regression: `to` was only bound inside the SMS branch but read unconditionally,
# so every lead lacking a valid phone raised UnboundLocalError and silently lost
# its email auto-reply too.
import auto_reply  # noqa: E402
import inspect  # noqa: E402

src = inspect.getsource(auto_reply.ensure_lead_reply)
check("`to` is initialised before the SMS branch",
      re.search(r'^\s*to = ""', src, re.M) is not None)

dbgen = m.get_db()
db = next(dbgen)
with db.cursor() as cur:
    cur.execute("""INSERT INTO leads (name, phone, email, project_type, address, budget,
                      timeline, description, source, status, company, created_at)
                   VALUES ('NoPhone','','','Roofing','1 A St','','','',
                   'web','new','construction',NOW()) RETURNING id;""")
    no_phone_id = cur.fetchone()["id"]
db.commit()

try:
    auto_reply.ensure_lead_reply(
        db, "construction",
        name="NoPhone", phone="", email="nobody@invalid.test",
        service="Roofing", address="1 A St", budget="", timeline="",
        message="", source="web", lead_id=no_phone_id,
    )
    check("lead with no phone does not raise", True)
except Exception as e:
    check("lead with no phone does not raise", False, repr(e))

print("\n" + "=" * 60)
if FAILS:
    print(f"{len(FAILS)} FAILING:")
    for f in FAILS:
        print(f"  - {f}")
    raise SystemExit(1)
print("site renders end to end")
