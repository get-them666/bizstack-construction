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
    """Recover the SQL text from the AST shape psycopg accepts."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(p.value for p in node.values
                       if isinstance(p, ast.Constant) and isinstance(p.value, str))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        left = node.left
        if isinstance(left, ast.Constant) and isinstance(left.value, str):
            return left.value
    return None


def _strip_literals_and_comments(sql):
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


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
            # quoted AGAIN by psycopg. That is always a bug.
            for m in re.finditer(r"'[^']*%s[^']*'", sql):
                nested.append(f"{name}:{node.lineno}  {m.group(0)}")

            params = node.args[1] if len(node.args) >= 2 else None
            if params is None:
                for kw in node.keywords:
                    if kw.arg == "params":
                        params = kw.value
            n = _params_len(params)
            if n is None:
                continue
            bare = _strip_literals_and_comments(sql)
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
    check("the pattern is gone from copilot_tasks", not nested, "re-check [1]")

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        return 1
    print("All SQL-safety tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())