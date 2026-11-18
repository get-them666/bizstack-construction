"""One-off repair: strip stylesheet debris from mail bodies stored before the fix.

Before `html_text` existed, an inbound HTML email was flattened with a tag-strip
regex. That removed `<style>` tags but kept the CSS between them, so old
`comms_logs.message_body` rows and the `[reply ...]` lines appended to
`leads.notes` still open with a wall of `.externalclass{font-family:Arial}`.
New mail is cleaned at ingestion; this repairs what is already stored.

    python scripts/clean_mail_bodies.py              # dry run: counts + samples
    python scripts/clean_mail_bodies.py --apply      # write, one transaction

Safety:
  * Only rows matching a CSS signature are read, and each candidate is
    re-checked in Python before it is changed.
  * Braces are matched, not regex-slashed, so prose is never guessed at.
  * Every original value is written to a JSONL file before the commit.
  * Idempotent: a second run finds nothing left to do.

Back up first anyway -- this is real mail:
  docker exec bizstack-db-construction-1 pg_dump -U postgres construction \
    -t comms_logs -t leads | gzip > /opt/bizstack/backups/pre_mail_cleanup.sql.gz
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import psycopg
from psycopg.rows import dict_row

# Fetched from Postgres with ~*, then re-checked here. Broad on purpose: an
# over-fetch costs a Python regex, a miss leaves CSS in the owner's mail.
SQL_SIGNATURE = (
    r"@media|@font-face|mso-|!important|font-family[[:space:]]*:|"
    r"\.externalclass|zm_[0-9]+|\{[^{}]{0,200}(margin|padding|color|background|border|width)"
)

_SIGNATURE = re.compile(
    r"@media|@font-face|@import|mso-|\.ExternalClass|zm_\d+|!important|"
    r"font-family\s*:|"
    r"\{[^{}\n]{0,400}"
    r"(?:margin|padding|color|background|width|height|border|font-size|"
    r"font-family|line-height|text-align|display|vertical-align)"
    r"[^{}\n]{0,400}\}",
    re.I)

_AT_IMPORT = re.compile(r"(?im)^\s*@import[^;\n]*;?\s*$")
# A line that is nothing but CSS declarations ("font-family:Arial;color:#333;").
_DECLS_ONLY = re.compile(
    r"(?i)^\s*(?:[-a-z]+\s*:\s*[^;{}\n]+;\s*)+$")
_CSS_VALUE_HINT = re.compile(
    r"(?i)(?:\d+px|#\w{3,8}\b|rgb\(|rgba\(|sans-serif|serif|solid|bold|"
    r"none|auto|block|inline|flex|important|normal|left|right|center|Arial|Helvetica)")
_BRACE_ONLY = re.compile(r"^\s*[{};]+\s*$")
_NEWLINES = re.compile(r"\n{3,}")
# A declaration inside a brace pair, by property name. A bare "word: words" is
# a customer sentence ("note: bring boots"), not CSS; requiring a known property
# is what keeps prose out of the bin.
_CSS_DECL = re.compile(
    r"(?i)(?:^|[;{])\s*(?:-[a-z]+-)?"
    r"(?:margin|padding|color|background|width|height|max-width|min-width|"
    r"border|font|font-size|font-family|font-weight|line-height|letter-spacing|"
    r"text-align|text-decoration|text-transform|vertical-align|display|"
    r"table-layout|word-break|white-space|overflow|mso-[a-z-]+)"
    r"[\w-]*\s*:\s*[^;{}]+")
# One or more simple selectors with combinators (".a", "body", "td, th",
# ".a > .b"). A bare-word descendant ("div p") is rejected on purpose: that
# shape is indistinguishable from prose, and leaving a selector behind is
# cheaper than deleting a sentence.
_SIMPLE_SELECTOR = (
    r"[.#*]?[A-Za-z][\w.-]*(?:\[[^\]]*\])?(?::[a-z-]+(?:\([^)]*\))?)?")
_SEL_LIST = re.compile(
    r"^" + _SIMPLE_SELECTOR +
    r"(?:(?:\s*[,>+~]\s*|\s+)" + _SIMPLE_SELECTOR + r")*$")
# The same shape, anchored to the END of a string: finds the trailing selector
# in "Yelp body, table, td {…}" so the prose before it survives. Only
# combinators separate tokens here: a sentence full of spaces must not match.
_TRAILING_SELECTOR = re.compile(
    r"(" + _SIMPLE_SELECTOR +
    r"(?:\s*[,>+~]\s*" + _SIMPLE_SELECTOR + r")*)\s*$")
_AT_RULE = re.compile(r"@(?:media|font-face|import|keyframes|page|supports)\b", re.I)
# Text that only a generated stylesheet produces. Used for an EMPTY block
# (`div.zm_123 td { }`), where there is no declaration to confirm CSS.
_CSS_SHAPED = re.compile(r"[.#\[\]_:]|-[a-z]|^(?:html|body|table|tbody|thead|tr|td|"
                         r"th|div|p|a|span|img|ul|ol|li|h[1-6]|\*)$", re.I)
# A sentence and a long selector list are the same shape once spaces are
# allowed between tokens; a real generated selector is short. Above this many
# words, treat it as prose and cut only a comma-separated suffix.
_MAX_SELECTOR_TOKENS = 12


def _selector_only(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith("@"):
        return True
    if len(t) > 240 or len(t.split()) > _MAX_SELECTOR_TOKENS:
        return False
    return bool(_SEL_LIST.match(t))


def _cut_point(s: str, i: int, j: int) -> int:
    """Index from which to delete, so the selector goes and prose stays.

    Stored rows glue everything onto one line: a lead note is
    `[reply ...] Roof quote: body{...}` and a bad tag-strip produced
    `New login to your Yelp account Yelp body, table, td {…}`. Cutting at the
    line start would delete the note or the sentence, so keep the prose and cut
    only the trailing selector.
    """
    line_start = s.rfind("\n", i, j)
    start = line_start + 1 if line_start >= 0 else i
    prefix = s[start:j]
    if _selector_only(prefix):
        return start
    at = None
    for m in _AT_RULE.finditer(prefix):
        at = m.start()
    if at is not None:
        return start + at
    m = _TRAILING_SELECTOR.search(prefix)
    if m:
        return start + m.start(1)
    return j


def _css_like(selector: str, inside: str) -> bool:
    """A brace pair is CSS only when a declaration proves it, or it is empty
    and the selector is stylesheet-shaped. Prose keeps its braces."""
    if _CSS_DECL.search(inside or ""):
        return True
    sel = (selector or "").strip()
    if (inside or "").strip():
        return False
    if not sel or sel.startswith("@"):
        return True
    return bool(_CSS_SHAPED.search(sel))


def strip_css(text: str) -> str:
    """Remove `selector{...}` / `@media ...{...}` blocks, keeping the prose."""
    s = text or ""
    if not s:
        return s
    out = []
    i, n = 0, len(s)
    while i < n:
        j = s.find("{", i)
        if j < 0:
            out.append(s[i:])
            break
        depth, k = 0, j
        while k < n:
            if s[k] == "{":
                depth += 1
            elif s[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        line_start = s.rfind("\n", i, j)
        start = line_start + 1 if line_start >= 0 else i
        selector = s[start:j]
        if k >= n:
            # Unbalanced brace: a truncation cut the stylesheet short.
            if _css_like(selector, s[j + 1:]):
                out.append(s[i:_cut_point(s, i, j)])
                i = n
                break
            out.append(s[i:j + 1])
            i = j + 1
            continue
        if _css_like(selector, s[j + 1:k]):
            cut = _cut_point(s, i, j)
            if cut < j:
                out.append(s[i:cut])
                i = k + 1
                continue
        # Not a selector after all: keep the brace pair as text.
        out.append(s[i:j + 1])
        i = j + 1
    s = "".join(out)
    s = _AT_IMPORT.sub("", s)
    lines = []
    for line in s.split("\n"):
        stripped = line.strip()
        if not stripped or _BRACE_ONLY.match(stripped):
            lines.append("")
            continue
        if _DECLS_ONLY.match(stripped) and _CSS_VALUE_HINT.search(stripped):
            lines.append("")
            continue
        lines.append(stripped)
    s = "\n".join(lines)
    s = _NEWLINES.sub("\n\n", s).strip()
    return s


def _preview(value: str, around: bool = False) -> str:
    v = re.sub(r"\s+", " ", value or "")
    return v[:220] + ("…" if len(v) > 220 else "")


def _targets(cur):
    cur.execute(
        "SELECT id, message_body AS body FROM comms_logs "
        "WHERE channel = 'email' AND direction = 'inbound' "
        "AND message_body ~* %s ORDER BY id;", (SQL_SIGNATURE,))
    comms = cur.fetchall()
    cur.execute(
        "SELECT id, company, notes AS body FROM leads "
        "WHERE COALESCE(company, 'construction') = 'construction' "
        "AND notes ~* %s ORDER BY id;", (SQL_SIGNATURE,))
    leads = cur.fetchall()
    return comms, leads


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry run)")
    ap.add_argument("--samples", type=int, default=3,
                    help="how many before/after samples to print (default 3)")
    ap.add_argument("--backup-file", default="",
                    help="where to write the original rows as JSONL "
                         "(default /tmp/mail_body_cleanup_<utc>.jsonl)")
    args = ap.parse_args(argv)

    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if not db_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2
    backup_path = args.backup_file or time.strftime(
        "/tmp/mail_body_cleanup_%Y%m%dT%H%M%SZ.jsonl", time.gmtime())

    with psycopg.connect(db_url, row_factory=dict_row) as db:
        with db.cursor() as cur:
            comms, leads = _targets(cur)

        changed_comms = []
        for row in comms:
            new = strip_css(row["body"])
            if new != row["body"]:
                changed_comms.append((row["id"], row["body"], new))
        changed_leads = []
        for row in leads:
            new = strip_css(row["body"])
            if new != row["body"]:
                changed_leads.append((row["id"], row["body"], new))

        print(f"comms_logs: {len(comms)} candidate(s), {len(changed_comms)} would change")
        print(f"leads.notes: {len(leads)} candidate(s), {len(changed_leads)} would change")

        for label, rows in (("comms_logs", changed_comms), ("leads", changed_leads)):
            for rid, before, after in rows[:max(0, args.samples)]:
                print(f"\n--- {label} #{rid} before ---")
                print(_preview(before))
                print(f"--- {label} #{rid} after ---")
                print(_preview(after))

        if not args.apply:
            print("\nDry run. Nothing written. Re-run with --apply to commit.")
            return 0

        if not changed_comms and not changed_leads:
            print("\nNothing to do.")
            return 0

        with open(backup_path, "w", encoding="utf-8") as fh:
            for rid, before, _after in changed_comms:
                fh.write(json.dumps({"table": "comms_logs", "id": rid,
                                     "message_body": before}) + "\n")
            for rid, before, _after in changed_leads:
                fh.write(json.dumps({"table": "leads", "id": rid,
                                     "notes": before}) + "\n")
        print(f"\nOriginals written to {backup_path}")

        with db.cursor() as cur:
            for rid, _before, after in changed_comms:
                cur.execute("UPDATE comms_logs SET message_body = %s WHERE id = %s;",
                            (after, rid))
            for rid, _before, after in changed_leads:
                cur.execute("UPDATE leads SET notes = %s WHERE id = %s;",
                            (after, rid))
        db.commit()
        print(f"Updated {len(changed_comms)} comms_logs row(s) and "
              f"{len(changed_leads)} leads row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
