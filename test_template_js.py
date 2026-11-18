"""Static check: every <script> block in the construction templates must parse.

WHY THIS EXISTS
---------------
A malformed IIFE shipped to /skiptrace: `(async function loadAddresses() { ... })();`
opens two parens and closes one, which is a SyntaxError. A SyntaxError kills
the ENTIRE script block, so the page loaded 200, the server was healthy, the
API returned 200 with 60 valid rows -- and the dropdown still said
"Loading addresses..." because not one line of that script ever ran.

Nothing caught it:
  - Jinja parsed the template fine; it does not parse JS
  - every endpoint returned the right status
  - the page rendered
  - the browser console said nothing visible

Only counting brackets in the emitted JS found it. That is the whole point:
this is a class of bug that no status code, log line, or HTTP check will ever
reveal, because the failure is in code the server never executes.

WHAT IT CHECKS
--------------
Extracts each <script> block from every template in templates/construction,
strips Jinja tags, and runs the result through a real JS parser (node --check
when available). Blocks that are fragments or reference template variables are
skipped rather than failed -- this reports PARSE BREAKAGE, not style.

Usage
-----
    python -m pytest test_template_js.py -v   # one collected test
    python test_template_js.py                # all construction templates
    python test_template_js.py --quiet

The file was script-only until 2026-11-18: every function lived inside
`if __name__ == "__main__":`, so `pytest test_template_js.py` collected ZERO
tests and reported success. The check now runs both ways.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "templates" / "construction"
SCRIPT_RE = re.compile(r"(?s)<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>")
# Jinja expressions inside JS become values at render time. Replace them with a
# harmless literal so the remainder can be parsed.
JINJA_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)


def script_blocks(path: Path):
    """Yield (line_no, source) for each EXECUTABLE <script> block.

    Skips type="application/ld+json": that is a JSON-LD structured-data blob
    consumed by search engines, not code, and parsing it as JavaScript
    reports a false failure on every schema.org block in the project.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    for match in SCRIPT_RE.finditer(text):
        attrs = (match.group("attrs") or "").lower()
        if "ld+json" in attrs or "application/json" in attrs:
            continue
        line = text[: match.start()].count("\n") + 1
        yield line, match.group("body")


def check_file(path: Path, node: str):
    """Return a list of (line, reason) for each unparseable block."""
    problems = []
    for line, src in script_blocks(path):
        cleaned = JINJA_RE.sub("0", src).strip()
        # An empty block, or one that is only Jinja, has nothing to parse.
        if not cleaned or cleaned == "0":
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(cleaned)
            tmp = Path(handle.name)
        try:
            result = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip().splitlines()
                reason = detail[0] if detail else "syntax error"
                for extra in detail[1:]:
                    if "SyntaxError" in extra or "^" in extra:
                        reason = extra.strip()
                        break
                problems.append((line, reason))
        finally:
            tmp.unlink(missing_ok=True)
    return problems


def collect_failures(node: str):
    """Return (template_count, block_count, [(path, line, reason)])."""
    templates = sorted(TEMPLATE_DIR.rglob("*.html"))
    failures = []
    checked = 0
    for path in templates:
        blocks = list(script_blocks(path))
        checked += len(blocks)
        for line, reason in check_file(path, node):
            failures.append((path, line, reason))
    return len(templates), checked, failures


def test_all_script_blocks_parse():
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH; cannot parse-check JavaScript")

    count, checked, failures = collect_failures(node)
    assert count, f"no templates under {TEMPLATE_DIR}"
    assert not failures, (
        f"{len(failures)} script block(s) will NOT execute in a browser "
        f"({count} templates, {checked} blocks checked):\n  "
        + "\n  ".join(f"{path.relative_to(ROOT)}:{line}  {reason}" for path, line, reason in failures)
        + "\nA SyntaxError here kills the whole block silently -- the page still "
        "returns 200 and every endpoint still works."
    )


def main() -> int:
    quiet = "--quiet" in sys.argv
    node = shutil.which("node")
    if not node:
        print("node not found -- cannot parse-check JavaScript.")
        print("Install Node, or run this in CI where node is present.")
        return 2

    count, checked, failures = collect_failures(node)
    if not count:
        print(f"no templates under {TEMPLATE_DIR}")
        return 1

    if failures:
        for path, line, reason in failures:
            print(f"  FAIL  {path.relative_to(ROOT)}:{line}  {reason}")
    elif not quiet:
        for path in sorted(TEMPLATE_DIR.rglob("*.html")):
            n_blocks = sum(1 for _ in script_blocks(path))
            print(f"  ok    {path.relative_to(ROOT)}  ({n_blocks} script block(s))")

    print(f"\n{count} template(s), {checked} script block(s) parsed.")
    if failures:
        print(f"{len(failures)} block(s) will NOT execute in a browser.")
        print("A SyntaxError here kills the whole block silently -- the page still")
        print("returns 200 and every endpoint still works.")
        return 1
    print("All script blocks parse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
