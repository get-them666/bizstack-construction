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

print("\n[4] lead auto-reply does not crash on a lead with no valid phone")
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
