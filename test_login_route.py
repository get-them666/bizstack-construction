"""Regression: the Auth0 bridge must not shadow the owner sign-in form.

POST /api/auth/login is the action of templates/construction/login.html. On
2026-10-15 the Auth0 Action bridge (a7261a5) was registered on that same path.
Starlette matches the first registered route, so every password login hit the
bridge instead, failed its header check, and returned
{"detail": "Bad or missing credentials"} -- 401 for a correct password.

Nothing failed at import, at build time, or in the suite: the route table was
valid, it just had two entries for one path. So this asserts the route table,
which is what actually broke, rather than posting credentials -- no database is
involved, and it stays a fast import-only check.

Keep this passing if the Auth0 path is ever moved again; an Auth0 Action in the
tenant must point at whatever path auth_login_check ends up owning.
"""

from __future__ import annotations

import construction_main


def _handlers(path: str, method: str) -> list:
    return [
        route.endpoint.__name__
        for route in construction_main.app.routes
        if getattr(route, "path", None) == path
        and method in (getattr(route, "methods", None) or set())
    ]


def test_login_form_owns_post_login():
    assert _handlers("/api/auth/login", "POST") == ["api_login"]


def test_auth0_bridge_still_owns_its_own_path():
    assert _handlers("/api/auth/auth0/check", "POST") == ["auth_login_check"]


if __name__ == "__main__":
    test_login_form_owns_post_login()
    test_auth0_bridge_still_owns_its_own_path()
    print("ok: login route ownership")
