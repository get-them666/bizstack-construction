"""DISABLE_* flags must only switch something OFF when explicitly asked to.

THE BUG THIS GUARDS
Railway stored an unset flag as the STRING "false" on at least one service.
`if not os.getenv("DISABLE_LOAN_OUTREACH")` then evaluated to False, so the
scheduler never started while the log still read "started" -- indistinguishable
from a healthy campaign. It cost weeks before anyone noticed. SESSION_NOTES.md
recorded the fix; this pins it so it cannot regress silently again.

Run: python test_flag_semantics.py
"""

import os
import sys

APP_MODULE = "construction_main.py" if os.path.exists(
    os.path.join(os.path.dirname(__file__), "construction_main.py")) else "main.py"

FAILS = []


def check(name, ok, detail=""):
    detail = str(detail)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def _reload():
    for name in list(sys.modules):
        if name == APP_MODULE:
            del sys.modules[name]
    import importlib
    return importlib.import_module(APP_MODULE[:-3])


def _flag_in_source():
    """The gates must not use bare truthiness any more."""
    src = open(APP_MODULE).read()
    return src


def main():
    import inspect
    import textwrap

    src = _flag_in_source()

    print("\n[1] DISABLE_* gates use the strict helper, not bare truthiness")
    for var in ("DISABLE_LOAN_OUTREACH", "DISABLE_PERMIT_IMPORT", "DISABLE_LEAD_SCAN"):
        if var not in src:
            continue  # that background task isn't started by this app
        check(f"{var} is not read with bare truthiness",
              f'os.getenv("{var}")' not in src,
              f'found os.getenv("{var}") -- the string "false" is truthy')
        check(f"{var} is gated through _disabled()",
              f'_disabled("{var}")' in src, "gate not found")

    print("\n[2] _disabled() semantics")
    m = _reload()
    check("module defines _disabled", hasattr(m, "_disabled"), "missing")
    fn = m._disabled
    saved = {}
    try:
        for raw, expected in [
            (None, False),          # unset -> feature runs
            ("", False),            # empty  -> feature runs
            ("false", False),       # THE BUG: truthy string
            ("False", False),
            ("FALSE", False),
            ("no", False),
            ("off", False),
            ("0", False),
            ("1", True),            # explicit off switch
            ("true", True),
            ("YES", True),
            ("on", True),
        ]:
            if raw is None:
                os.environ.pop("DISABLE_X", None)
            else:
                os.environ["DISABLE_X"] = raw
            got = fn("DISABLE_X")
            check(f"{raw!r} -> disabled={expected}", got is expected, f"got {got}")
    finally:
        for k in ("DISABLE_X",):
            if k in saved:
                os.environ[k] = saved[k]
            else:
                os.environ.pop(k, None)

    print("\n[3] the helper reads one variable, not a hardcoded name")
    body = textwrap.dedent(inspect.getsource(fn))
    check("takes the variable name as an argument", "var" in body, body)
    check("does not hardcode DISABLE_", "DISABLE_" not in body, body)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        return 1
    print("All flag-semantics tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())