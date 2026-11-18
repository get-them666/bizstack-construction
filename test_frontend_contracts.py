"""Front-end contracts the lead board and call list depend on.

    venv/bin/python -m pytest test_frontend_contracts.py -v
    venv/bin/python test_frontend_contracts.py        # same tests, no pytest flags

Why this file exists
--------------------
The lead UI is assembled from three places that never check each other:

  * `templates/construction/leads.html` and `calllist.html` are Jinja, so a
    missing `<script src=...>` or a renamed `data-call-*` hook is invisible --
    the page returns 200 and renders, it just stops working.
  * `static/js/api-client.js` and `static/js/outbound-calls.js` are static
    files. Jinja does not know they exist; Tailwind does not know whether they
    parse.
  * A `|safe` filter on a phone number or a message body turns customer data
    into HTML. Nothing in FastAPI or Jinja warns you; it renders perfectly.

Each check below fails for a reason a status code cannot show. The template
assertions are gated on the shared bundles: in a checkout that predates them
(they land with the lead-UI work) the check skips and says so, rather than
passing silently or failing on code that is not here yet.

The Jinja compile check uses the application's own environment. A bare
`jinja2.Environment` reports `No filter named 'money'` on backlog.html and
cannot resolve `{% extends "base.html" %}` because the loader root is
`templates/construction`, not `templates/`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "templates" / "construction"
STATIC_DIR = ROOT / "static"
JS_DIR = STATIC_DIR / "js"

LEAD_TEMPLATES = ("leads.html", "calllist.html")
SHARED_SCRIPTS = ("/static/js/api-client.js", "/static/js/outbound-calls.js")
OUTBOUND_SCRIPT = "/static/js/outbound-calls.js"
CALL_HOOKS = ("data-call-lead", "data-call-status", "data-outbound-summary")

# Names that carry a phone number or a message body. These are the fields the
# lead UI renders from inbound lead/call data, i.e. the ones an attacker or a
# confused provider controls. Marking them `|safe` is an XSS hole.
_UNSAFE_EXACT = {"tel", "telephone", "cell", "body", "text", "msg", "reply", "content"}
_UNSAFE_SUBSTRINGS = ("phone", "mobile", "sms", "msisdn", "message", "body")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXPRESSION_RE = re.compile(r"\{\{(?P<expr>.*?)\}\}", re.S)
_SAFE_FILTER_RE = re.compile(r"\|\s*safe\b")


def _read(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Every template must compile in the environment the app actually renders with
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def app_jinja_env():
    """The app's filter set on a loader pinned to this checkout.

    Importing `construction_main` gives the real filters (`money`, `bid_notes`,
    …) so a template that uses an unregistered filter fails here instead of at
    render time. The loader is rebuilt absolute so the check does not depend on
    the process CWD.
    """
    import jinja2

    import construction_main

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
        undefined=jinja2.Undefined,
    )
    env.filters.update(construction_main.templates.env.filters)
    return env


def test_every_template_compiles(app_jinja_env):
    templates = sorted(TEMPLATE_DIR.rglob("*.html"))
    assert templates, f"no templates found under {TEMPLATE_DIR}"

    broken = []
    for path in templates:
        name = str(path.relative_to(TEMPLATE_DIR))
        try:
            app_jinja_env.get_template(name)
        except Exception as exc:  # noqa: BLE001 - collect every break, not the first
            broken.append(f"{name}: {type(exc).__name__}: {exc}")

    assert not broken, "templates that will 500 on render:\n  " + "\n  ".join(broken)


# --------------------------------------------------------------------------
# The lead templates load the shared bundles, exactly once each
# --------------------------------------------------------------------------
@pytest.mark.parametrize("template", LEAD_TEMPLATES)
@pytest.mark.parametrize("script", SHARED_SCRIPTS)
def test_lead_templates_load_shared_script(template, script):
    source = _read(template)
    count = source.count(script)
    bundle = ROOT / script.lstrip("/")

    if count == 0 and not bundle.exists():
        pytest.skip(
            f"{template} does not load {script} yet and {bundle.relative_to(ROOT)} "
            "is not in this checkout -- wiring lands with the lead-UI work"
        )

    assert bundle.exists(), (
        f"{template} references {script} but {bundle.relative_to(ROOT)} does not exist; "
        "the page will 404 the script and every handler on it is dead"
    )
    assert count == 1, (
        f"{template} must load {script} exactly once, found {count}; "
        "a duplicate <script> re-runs the IIFE and rebinds its listeners"
    )


# --------------------------------------------------------------------------
# The call UI's markup hooks exist wherever the outbound bundle is wired
# --------------------------------------------------------------------------
def _outbound_wired() -> bool:
    if (JS_DIR / "outbound-calls.js").exists():
        return True
    return any(OUTBOUND_SCRIPT in _read(t) for t in LEAD_TEMPLATES)


@pytest.mark.parametrize("hook", CALL_HOOKS)
def test_call_ui_markup_hook_present(hook):
    if not _outbound_wired():
        pytest.skip(
            f"{OUTBOUND_SCRIPT} is absent and no lead template loads it -- "
            f"not asserting {hook} before the lead-UI work lands"
        )

    hits = [t for t in LEAD_TEMPLATES if hook in _read(t)]
    assert hits, (
        f"none of {', '.join(LEAD_TEMPLATES)} contains {hook}. "
        "outbound-calls.js binds to it by name; a renamed attribute is a silent no-op "
        "(the button renders, the click does nothing)"
    )


# --------------------------------------------------------------------------
# No `|safe` on a phone number or a message body
# --------------------------------------------------------------------------
def _looks_like_contact_data(identifier: str) -> bool:
    lowered = identifier.lower()
    return lowered in _UNSAFE_EXACT or any(s in lowered for s in _UNSAFE_SUBSTRINGS)


def test_no_safe_filter_on_phone_or_message_fields():
    offenders = []
    for path in sorted(TEMPLATE_DIR.rglob("*.html")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _EXPRESSION_RE.finditer(text):
            expression = match.group("expr")
            if not _SAFE_FILTER_RE.search(expression):
                continue
            names = _IDENTIFIER_RE.findall(expression)
            if any(_looks_like_contact_data(n) for n in names):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}  {{{{ {expression.strip()} }}}}")

    assert not offenders, (
        "phone numbers and message bodies must stay autoescaped; `|safe` there is XSS:\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# Static JS must at least parse -- a SyntaxError kills the whole block
# --------------------------------------------------------------------------
def _static_js_files():
    files = sorted(STATIC_DIR.glob("*.js")) + sorted(JS_DIR.glob("*.js"))
    return files


def _node_check(node: str, path: Path) -> str | None:
    """Return the first error line, or None. Retries as ESM when needed."""
    result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    if result.returncode == 0:
        return None

    detail = (result.stderr or result.stdout or "").strip()
    if "outside a module" in detail or "Unexpected token 'export'" in detail:
        # package.json has no "type": "module", so a browser-ESM file fails
        # `node --check` on its extension alone. Copy it to .mjs and re-check.
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as handle:
            handle.write(path.read_text(encoding="utf-8"))
            tmp = Path(handle.name)
        try:
            result = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
            if result.returncode == 0:
                return None
        finally:
            tmp.unlink(missing_ok=True)

    lines = (result.stderr or result.stdout or "").strip().splitlines()
    return lines[0] if lines else "syntax error"


@pytest.mark.parametrize(
    "path",
    _static_js_files(),
    ids=lambda p: str(Path(p).relative_to(ROOT)),
)
def test_static_js_parses(path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH; cannot parse-check JavaScript")

    error = _node_check(node, Path(path))
    assert error is None, (
        f"{Path(path).relative_to(ROOT)} does not parse: {error}. "
        "One SyntaxError stops the entire <script> block from running and the "
        "page still returns 200"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
