"""ABC Supply pricing adapter.

Built against the published Individual Business track
(https://apidocs.abcsupply.com). Client Credentials, no user login, no
redirect URI -- the right shape for a single contractor pricing its own
material.

What it does: given item numbers and quantities, returns the price ABC
Supply would actually charge *this account* at *that branch*. That is the
number worth putting on a takeoff, and it is not the same as a website
shelf price: branches set their own pricing per account and per branch.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
ABC Supply's API Terms of Use prohibit, verbatim:

  - "Direct price comparison tools across competitors or markets"
  - "Packaging or selling of product or pricing data for market analysis
     or intelligence"

So this module prices for the owner's own estimating and never compares
against another supplier. `price_items` takes an item list and returns
prices; there is no competitor field and nothing to compare to. If a
cross-supplier comparison is wanted later it has to come from a source
whose terms allow it.

THE $0.00 TRAP
--------------
Price Items returns HTTP 200 even when a line fails, and a line priced at
0.00 is a SUCCESS meaning "this branch has not entered a price for this
item yet". Treating that as free would understate a takeoff by whatever the
material actually costs. `_read_price` therefore returns a None unit price
and a reason, and callers are expected to show that rather than a zero.

Credentials are read from the environment and never logged. Access tokens
live 30 minutes, so they are cached with a margin.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

SANDBOX = {
    "token": "https://sandbox.auth.partners.abcsupply.com/oauth2/aus1vp07knpuqf6Xz0h8/v1/token",
    "api": "https://partners-sb.abcsupply.com/api",
}
PRODUCTION = {
    "token": "https://auth.partners.abcsupply.com/oauth2/ausvvp0xuwGKLenYy357/v1/token",
    "api": "https://partners.abcsupply.com/api",
}

# Individual Business scopes. pricing.read is the one that matters; the rest
# are here so a later order/invoice feature does not need a new consent.
SCOPES = ("location.read product.read account.read pricing.read "
          "allOrder.read order.write notification.read notification.write "
          "invoice.read invoice.history.read")

# Price Items accepts at most 50 lines per call.
MAX_LINES = 50
TOKEN_TTL = 25 * 60          # tokens live 30 min; refresh early
UA = "Buildstack-Materials/1.0"

_token_cache: dict = {"token": None, "expires": 0.0}


class ABCError(RuntimeError):
    """Anything that stops us getting a price. Shown to the owner, never hidden."""


def config() -> dict:
    """Everything needed, and what is missing. Never returns secret values."""
    env = "ABC_SANDBOX" if os.getenv("ABC_SANDBOX", "").lower() in ("1", "true", "yes") else "ABC_PROD"
    return {
        "client_id": bool(os.getenv("ABC_CLIENT_ID")),
        "client_secret": bool(os.getenv("ABC_CLIENT_SECRET")),
        "ship_to": os.getenv("ABC_SHIP_TO", ""),
        "branch": os.getenv("ABC_BRANCH", ""),
        "environment": env,
        "host": SANDBOX if env == "ABC_SANDBOX" else PRODUCTION,
    }


def configured() -> bool:
    """True when a price request can actually be attempted."""
    c = config()
    return bool(c["client_id"] and c["client_secret"] and c["ship_to"] and c["branch"])


def missing() -> list[str]:
    out = []
    if not os.getenv("ABC_CLIENT_ID"):
        out.append("ABC_CLIENT_ID")
    if not os.getenv("ABC_CLIENT_SECRET"):
        out.append("ABC_CLIENT_SECRET")
    if not os.getenv("ABC_SHIP_TO"):
        out.append("ABC_SHIP_TO (your ABC ship-to account number)")
    if not os.getenv("ABC_BRANCH"):
        out.append("ABC_BRANCH (the branch number you buy from)")
    return out


def _post(url: str, payload: dict, headers: dict, timeout: int = 30) -> dict:
    body = json.dumps(payload).encode() if not isinstance(payload, str) else payload.encode()
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        # Okta returns the reason in a JSON body; surface it rather than a bare code.
        try:
            detail = json.loads(detail).get("error_description") or detail
        except (json.JSONDecodeError, AttributeError):
            pass
        raise ABCError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise ABCError(f"could not reach ABC Supply: {e.reason}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ABCError(f"ABC Supply returned non-JSON: {e}") from e


def _get(url: str, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error_description") or detail
        except (json.JSONDecodeError, AttributeError):
            pass
        raise ABCError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise ABCError(f"could not reach ABC Supply: {e.reason}") from e


def access_token(force: bool = False) -> str:
    """Client-credentials token, cached past its 30-minute life with a margin."""
    now = time.time()
    if not force and _token_cache["token"] and _token_cache["expires"] > now:
        return _token_cache["token"]

    cid, sec = os.getenv("ABC_CLIENT_ID"), os.getenv("ABC_CLIENT_SECRET")
    if not cid or not sec:
        raise ABCError("ABC_CLIENT_ID and ABC_CLIENT_SECRET are not set. Apply at "
                       "partners-developer.abcsupply.com/signup first.")
    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "scope": SCOPES})
    data = _post(config()["host"]["token"], body,
                 {"Authorization": f"Basic {basic}",
                  "Content-Type": "application/x-www-form-urlencoded"})
    tok = data.get("access_token")
    if not tok:
        raise ABCError(f"no access token in response: {str(data)[:200]}")
    _token_cache["token"] = tok
    _token_cache["expires"] = now + TOKEN_TTL
    return tok


def _read_price(line: dict) -> tuple[float | None, str | None]:
    """Unit price, or None plus a reason.

    $0.00 is a successful response meaning the branch has not priced the
    item. It is not free, and it is not zero.
    """
    st = (line.get("status") or {}).get("code")
    msg = (line.get("status") or {}).get("message")
    if st not in (None, "", "OK", "200", 200):
        return None, msg or f"status {st}"
    price = line.get("unitPrice")
    if price in (None, ""):
        return None, msg or "no price returned"
    try:
        val = float(price)
    except (TypeError, ValueError):
        return None, f"unreadable price {price!r}"
    if val == 0:
        return None, "branch has not priced this item yet"
    return val, None


def price_items(items: list[dict], purpose: str = "estimating",
                timeout: int = 30) -> list[dict]:
    """Price a list of {itemNumber, quantity, uom, length} against the account.

    Batches at the documented 50-line ceiling. Every line is returned, with
    unit_price None and reason set where ABC could not price it, so a caller
    can never mistake an unpriced item for a free one.
    """
    if not configured():
        raise ABCError("ABC Supply is not configured; missing " + ", ".join(missing()))
    if purpose not in ("estimating", "quoting", "ordering"):
        purpose = "estimating"

    items = [i for i in items if str(i.get("itemNumber", "")).strip()]
    if not items:
        return []

    c = config()
    out: list[dict] = []
    for start in range(0, len(items), MAX_LINES):
        chunk = items[start:start + MAX_LINES]
        lines = []
        for n, it in enumerate(chunk, start=1):
            line = {"id": str(n), "itemNumber": str(it["itemNumber"]).strip(),
                    "quantity": it.get("quantity", 1)}
            if it.get("uom"):
                line["uom"] = it["uom"]
            # length is required only for dimensional items (lumber, pipe,
            # trim). Sending it on a non-dimensional item is rejected.
            if it.get("length") and it.get("length", {}).get("value") is not None:
                line["length"] = {"value": it["length"]["value"],
                                  "uom": it["length"].get("uom", "in")}
            lines.append(line)

        payload = {"requestId": f"Buildstack-{int(time.time())}-{start}",
                   "shipToNumber": c["ship_to"],
                   "branchNumber": c["branch"],
                   "purpose": purpose,
                   "lines": lines}
        data = _post(f"{c['host']['api']}/pricing/v2/prices", payload,
                     {"Authorization": f"Bearer {access_token()}",
                      "Content-Type": "application/json"}, timeout)
        by_id = {str(l.get("id")): l for l in (data.get("lines") or [])}
        for idx, it in enumerate(chunk, start=1):
            line = by_id.get(str(idx), {})
            unit, reason = _read_price(line)
            qty = float(it.get("quantity") or 0)
            out.append({
                "itemNumber": str(it["itemNumber"]).strip(),
                "description": line.get("description") or it.get("description") or "",
                "quantity": qty,
                "uom": line.get("uom") or it.get("uom") or "",
                "unit_price": unit,
                "extended": round(unit * qty, 2) if unit is not None and qty else None,
                "reason": reason,
            })
    return out


def search_items(term: str, limit: int = 25, branch: str = "") -> list[dict]:
    """Find item numbers by description. This is how a takeoff gets built."""
    if not term.strip():
        return []
    c = config()
    payload = {"searchText": term.strip()[:120], "pageSize": min(int(limit), 50)}
    if branch:
        payload["branchNumbers"] = [branch]
    data = _post(f"{c['host']['api']}/product/v1/items/search", payload,
                 {"Authorization": f"Bearer {access_token()}",
                  "Content-Type": "application/json"})
    rows = data.get("items") or data.get("results") or []
    out = []
    for it in rows[:limit]:
        out.append({
            "itemNumber": it.get("itemNumber") or it.get("itemNumberFull") or "",
            "description": it.get("description") or it.get("itemDescription") or "",
            "uom": it.get("uom") or it.get("unitOfMeasure") or "",
            "branch_price": it.get("branchPrice") or it.get("price"),
        })
    return out


def frequent_items(limit: int = 100) -> list[dict]:
    """What this account actually orders, most frequent first.

    The best possible starting point for a takeoff: real item numbers from
    the account's own history, rather than searching for lumber one sheet at
    a time.
    """
    c = config()
    data = _get(f"{c['host']['api']}/product/v1/items/frequent?pageSize={min(int(limit), 200)}",
                {"Authorization": f"Bearer {access_token()}"})
    rows = data.get("items") or data.get("results") or []
    return [{"itemNumber": i.get("itemNumber", ""),
             "description": i.get("description", ""),
             "uom": i.get("uom", ""),
             "order_count": i.get("orderCount") or i.get("frequency")}
            for i in rows[:limit]]