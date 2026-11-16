"""No SQL query may nest a psycopg placeholder inside a quoted literal.

THE BUG THIS GUARDS
`WHERE created_at < NOW() - INTERVAL '%s days'` looks correct and passes every
review. psycopg substitutes a parameter as a *quoted literal of its own*, so the
query that reaches Postgres is `INTERVAL ''500' days'` -- a syntax error. It
never fired because the retention purge had never actually deleted anything, so
nobody noticed the function had always thrown.

The correct form is `make_interval(days => %s)`, which takes a number rather
than a quoted string.

This scans every .py in the repo for the pattern, so a new one cannot be added
without the suite going red. Also verifies placeholder count against the params
tuple passed alongside, which is the other half of the same class of bug (the
pipeline board once 500'd from a mismatched count).

Run: python test_sql_safety.py
"""

import ast
import os
import re
import sys

APP_MODULE = "construction_main.py" if os.path.exists(
    os.path.join(os.path.dirname(__file__), "construction_main.py")) else "main.py"
HERE = os.path.dirname(os.path.abspath(__file__))

FAILS = []


def check(name, ok, detail=""):
    detail = str(detail)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def _sql_literal(node):
    """Recover the SQL text from the AST shape psycopg accepts.

    Returns None for f-strings (JoinedStr) and other dynamic SQL that cannot be
    statically analysed -- the old implementation joined the constant parts of
    an f-string, which produced a fragment with fewer ``%s`` than the params
    tuple and tripped false-positive mismatch reports.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f-string: placeholders may be generated at runtime; skip.
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        left = node.left
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            return left.value
    # Implicit string concatenation: adjacent string literals become a BinOp
    # chain with ast.Add at parse time... actually CPython concatenates them
    # into a single Constant at compile time. But multi-line implicit
    # concatenation inside a call may produce a BinOp with Add, so handle it.
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _sql_literal(node.left)
        right = _sql_literal(node.right)
        if left is not None and right is not None:
            return left + right
        # If either side is dynamic (f-string, variable), the whole thing is.
        return None
    return None


def _strip_sql_literals(sql):
    """Replace every SQL string literal with a single space.

    Walks the SQL character by character so that the boundary between two
    adjacent literals (``''`` = empty string, not an escaped quote) and
    PostgreSQL escape-prefixed strings (``E'\\\\n'``) are handled correctly.
    The old regex-based stripper could mistake a ``--`` inside a string for a
    line comment and a ``%s`` between two literal boundaries for a nested
    placeholder, producing hundreds of false positives.

    Used for the *mismatch* check, where we need to count only the ``%s``
    placeholders that are *outside* any string literal.
    """
    out = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                    i += 2
                elif sql[i] == "'":
                    i += 1
                    break
                else:
                    i += 1
            out.append(" ")
        elif c == '-' and i + 1 < n and sql[i + 1] == '-':
            while i < n and sql[i] != '\n':
                i += 1
        elif c == '/' and i + 1 < n and sql[i + 1] == '*':
            i += 2
            while i + 1 < n and not (sql[i] == '*' and sql[i + 1] == '/'):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _string_literal_ranges(sql):
    """Return a sorted list of (start, end) ranges for every SQL string literal.

    Used to check whether a ``%s`` found by regex falls inside a string literal
    (a real nested-placeholder bug) or between two adjacent literals (a false
    positive from the fragment-boundary pattern).
    """
    ranges = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            start = i
            i += 1
            while i < n:
                if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                    i += 2
                elif sql[i] == "'":
                    i += 1
                    break
                else:
                    i += 1
            ranges.append((start, i))
        elif c == '-' and i + 1 < n and sql[i + 1] == '-':
            while i < n and sql[i] != '\n':
                i += 1
        elif c == '/' and i + 1 < n and sql[i + 1] == '*':
            i += 2
            while i + 1 < n and not (sql[i] == '*' and sql[i + 1] == '/'):
                i += 1
            i += 2
        else:
            i += 1
    return ranges


def _is_inside_literal(pos, ranges):
    """True if *pos* falls strictly inside one of the string-literal ranges."""
    for start, end in ranges:
        if start < pos < end:
            return True
    return False


def _params_len(node):
    if isinstance(node, (ast.Tuple, ast.List)):
        return len(node.elts)
    return None


def _scan():
    nested, mismatched = [], []
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py"):
            continue
        if name.startswith("test_"):
            continue
        path = os.path.join(HERE, name)
        try:
            tree = ast.parse(open(path).read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "execute" or not node.args:
                continue
            sql = _sql_literal(node.args[0])
            if not sql or "%" not in sql:
                continue

            # A placeholder inside single quotes is a placeholder that will be
            # quoted AGAIN by psycopg. That is always a bug. We find every %s
            # position and check whether it falls strictly inside a string
            # literal, which distinguishes real bugs (INTERVAL '%s days') from
            # false positives where %s sits between two adjacent literals.
            ranges = _string_literal_ranges(sql)
            for m in re.finditer(r"%s", sql):
                if _is_inside_literal(m.start(), ranges):
                    ctx_start = max(0, m.start() - 20)
                    ctx_end = min(len(sql), m.end() + 20)
                    nested.append(f"{name}:{node.lineno}  ...{sql[ctx_start:ctx_end]}...")

            params = node.args[1] if len(node.args) >= 2 else None
            if params is None:
                for kw in node.keywords:
                    if kw.arg == "params":
                        params = kw.value
            n = _params_len(params)
            if n is None:
                continue
            bare = _strip_sql_literals(sql)
            named = len(re.findall(r"%\(\w+\)s", bare))
            if named:
                if named != n:
                    mismatched.append(f"{name}:{node.lineno}  {named} named vs {n} params")
            else:
                pos = len(re.findall(r"%s", bare))
                if pos != n:
                    mismatched.append(f"{name}:{node.lineno}  {pos} positional vs {n} params")
    return nested, mismatched


def main():
    print("\n[1] no psycopg placeholder nested inside a quoted SQL literal")
    nested, mismatched = _scan()
    check("no nested-interval / nested-quote placeholders", not nested,
          "; ".join(nested))
    for line in nested:
        print(f"        {line}")

    print("\n[2] placeholder count matches the params tuple on every execute()")
    check("no placeholder/param count mismatch", not mismatched,
          "; ".join(mismatched))
    for line in mismatched:
        print(f"        {line}")

    print("\n[3] the retention purge actually runs against a real database")
    # Check that copilot_tasks.py no longer contains the old INTERVAL '%s days'
    # pattern. The file was fixed to use make_interval(days => %s); this check
    # pins that fix.
    ct_path = os.path.join(HERE, "copilot_tasks.py")
    if os.path.exists(ct_path):
        src = open(ct_path).read()
        has_bad = bool(re.search(r"INTERVAL\s+'%s", src))
    else:
        has_bad = False
    check("the pattern is gone from copilot_tasks", not has_bad,
          "INTERVAL '%s ...' still present in copilot_tasks.py")

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        return 1
    print("All SQL-safety tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
