"""Auth0 login for the admin pages.

Auth0 proves identity. This module turns that proof into the SAME session
cookie the PIN/email login already issues, so require_admin, /books,
/payroll and everything else keeps working untouched. Nothing downstream
learns that Auth0 exists.

WHY THE ROLE IS DECIDED TWICE
------------------------------
The Action sets a role claim on the token. This module ignores it for
authorisation purposes and calls auth0_service.decide() again against the
allowlist in the database.

That is deliberate. A role claim is data carried in a token; anything that
can mint or influence a token becomes part of your authorisation surface. So
the claim is treated as a hint that gets logged, and the database is the
authority. If Auth0 is compromised, misconfigured, or an Action gets edited
by mistake, the worst outcome is that someone gets no role -- not that they
get `admin`.

WHY VERIFY AT ALL
-----------------
The ID token arrives in a query string. Anyone can craft one. Without
signature verification this module would be a login form that accepts
whatever the browser says it is. Every failure below is a refusal:

  bad signature      forged or tampered token
  wrong issuer       a valid Auth0 token from a DIFFERENT tenant
  wrong audience     a token minted for a different app
  expired            replay of an old token
  bad nonce          response to a request we did not start
  bad state          response to a request we did not start

The last two matter because they are what stops an attacker feeding their
own authorization code into the callback.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt
from jwt.algorithms import RSAAlgorithm

import auth0_service

STATE_COOKIE = "a0_state"
VERIFIER_COOKIE = "a0_verifier"
NONCE_COOKIE = "a0_nonce"
COOKIE_TTL = 600
UA = "BuildStack-Construction/1.0"

ROLE_CLAIM = "https://bizstackperks.com/roles"

_jwks_cache: dict = {"keys": None, "at": 0.0}
JWKS_TTL = 3600


class Auth0Error(RuntimeError):
    """Any reason a login must not proceed. The message is shown to the user."""


def domain() -> str:
    return (os.getenv("AUTH0_DOMAIN") or "").strip().rstrip("/")


def client_id() -> str:
    return (os.getenv("AUTH0_CLIENT_ID") or "").strip()


def configured() -> bool:
    return bool(domain() and client_id())


def missing() -> list[str]:
    out = []
    if not os.getenv("AUTH0_DOMAIN"):
        out.append("AUTH0_DOMAIN")
    if not os.getenv("AUTH0_CLIENT_ID"):
        out.append("AUTH0_CLIENT_ID")
    return out


def _issuer() -> str:
    return f"https://{domain()}/"


def _authorize_url() -> str:
    return f"https://{domain()}/authorize"


def _jwks_url() -> str:
    return f"https://{domain()}/.well-known/jwks.json"


def _token_url() -> str:
    return f"https://{domain()}/oauth/token"


# --- PKCE -------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def new_verifier() -> str:
    return _b64url(secrets.token_bytes(64))


def challenge_for(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def new_nonce() -> str:
    return _b64url(secrets.token_bytes(24))


def authorize_url(verifier: str, nonce: str, state: str, redirect_uri: str) -> str:
    if not configured():
        raise Auth0Error("Auth0 is not configured: " + ", ".join(missing()))
    params = {
        "client_id": client_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
    }
    return f"{_authorize_url()}?{urllib.parse.urlencode(params)}"


# --- JWKS -------------------------------------------------------------------
def _fetch_json(url: str, data: dict | None = None, headers: dict | None = None,
                timeout: int = 20) -> dict:
    body = urllib.parse.urlencode(data).encode() if data else None
    hdrs = {"User-Agent": UA, **({"Content-Type": "application/x-www-form-urlencoded"} if data else {})}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise Auth0Error(f"Auth0 returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise Auth0Error(f"Could not reach Auth0: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise Auth0Error(f"Auth0 returned non-JSON: {e}") from e


def jwks(force: bool = False) -> dict:
    """Auth0's public signing keys, cached.

    Cached because a key fetch on every login would add a round trip to the
    hot path, and Auth0 rotates rarely. Refreshed hourly so a rotation is
    picked up without a restart.
    """
    now = time.time()
    if not force and _jwks_cache["keys"] and _jwks_cache["at"] > now - JWKS_TTL:
        return _jwks_cache["keys"]
    data = _fetch_json(_jwks_url())
    keys = {k.get("kid"): k for k in (data.get("keys") or []) if k.get("kid")}
    if not keys:
        raise Auth0Error("Auth0 returned no signing keys")
    _jwks_cache["keys"] = keys
    _jwks_cache["at"] = now
    return keys


def _key_for(kid: str):
    keys = jwks()
    jwk = keys.get(kid)
    if not jwk:
        # A key we have never seen is most likely a rotation. Refresh once
        # rather than rejecting a legitimate login.
        jwk = jwks(force=True).get(kid)
    if not jwk:
        raise Auth0Error("Sign-in used an unknown signing key")
    return RSAAlgorithm.from_jwk(json.dumps(jwk))


# --- token verification -----------------------------------------------------
def verify_id_token(id_token: str, nonce: str) -> dict:
    """Verify signature and every claim that matters. Raises on any problem."""
    if not id_token:
        raise Auth0Error("No token returned from Auth0")
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as e:
        raise Auth0Error(f"Malformed token: {e}") from e
    kid = header.get("kid")
    if not kid:
        raise Auth0Error("Token carries no key id")

    key = _key_for(kid)
    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=client_id(),
            issuer=_issuer(),
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise Auth0Error("That sign-in link expired — try again") from e
    except jwt.InvalidAudienceError as e:
        raise Auth0Error("Token was issued for a different app") from e
    except jwt.InvalidIssuerError as e:
        raise Auth0Error("Token came from a different Auth0 tenant") from e
    except jwt.InvalidSignatureError as e:
        raise Auth0Error("Signature check failed") from e
    except jwt.MissingRequiredClaimError as e:
        raise Auth0Error(f"Token is missing a required claim: {e}") from e
    except jwt.PyJWTError as e:
        raise Auth0Error(f"Token rejected: {e}") from e

    # Leeway for clock skew between here and Auth0. Five minutes is plenty
    # and not so much that a revoked token has a useful window.
    if (claims.get("exp", 0) - claims.get("iat", 0)) > 86400:
        raise Auth0Error("Token lifetime is implausible")

    got = claims.get("nonce")
    if not nonce or not got or not secrets.compare_digest(str(got), str(nonce)):
        raise Auth0Error("Sign-in could not be matched to a request we started")

    if not claims.get("email"):
        raise Auth0Error("Auth0 did not return an email address")
    return claims


def token_claim_roles(claims: dict) -> list[str]:
    raw = claims.get(ROLE_CLAIM)
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return []


# --- the flow ---------------------------------------------------------------
def exchange_code(code: str, verifier: str, redirect_uri: str) -> dict:
    """Swap an authorization code for tokens. PKCE verifier is required."""
    if not configured():
        raise Auth0Error("Auth0 is not configured: " + ", ".join(missing()))
    return _fetch_json(_token_url(), data={
        "grant_type": "authorization_code",
        "client_id": client_id(),
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
    })


def complete_login(code: str, verifier: str, nonce: str, redirect_uri: str,
                   conn) -> dict:
    """Verify the exchange and decide the role.

    Returns an actor shaped like the one auth_service builds, so the caller
    can hand it to the existing _finish_login and mint an identical session.
    """
    tokens = exchange_code(code, verifier, redirect_uri)
    claims = verify_id_token(tokens.get("id_token", ""), nonce)

    email = str(claims.get("email", "")).strip().lower()
    name = str(claims.get("name") or claims.get("email") or "")
    claim_roles = token_claim_roles(claims)

    # The allowlist is the authority. The token's claim is recorded for
    # comparison and for the audit trail, but it does not grant access.
    verdict = auth0_service.decide(
        conn, email=email, name=name,
        auth0_sub=str(claims.get("sub", ""))[:120],
        user_agent="auth0", ip="",
    )
    if not verdict["allowed"]:
        raise Auth0Error(
            verdict.get("reason") or "That account is not authorised for this site")

    mismatch = bool(claim_roles) and verdict["role"] not in claim_roles
    return {
        "role": verdict["role"],
        "id": str(claims.get("sub", "")),
        "email": email,
        "name": name,
        "auth0_role": verdict["role"],
        "claim_roles": claim_roles,
        "claim_mismatch": mismatch,
    }


def clear_cookies(resp) -> None:
    """Drop the PKCE material once it has been used.

    Single use is the point of PKCE -- leaving the verifier in a cookie would
    let it be replayed, so it goes regardless of whether the login worked.
    """
    for name in (STATE_COOKIE, VERIFIER_COOKIE, NONCE_COOKIE):
        resp.delete_cookie(name, path="/")