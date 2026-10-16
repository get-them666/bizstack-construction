"""Routes for bid_opportunities. Mounted from construction_main.

Every route is admin-gated. None of them sends email, dials, or touches `leads` --
this board tracks solicitations we are pursuing, not customers.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import bid_opportunities as bo
import psycopg

router = APIRouter()

_COMPANY = "construction"


def _today() -> date:
    return date.today()


def _fmt(d) -> str:
    if d is None:
        return "—"
    if isinstance(d, date):
        return d.strftime("%b %d")
    return str(d)[:10]


def _money(v) -> str:
    if v is None:
        return "—"
    return f"${float(v):,.0f}"


def _days_out(d) -> int | None:
    if d is None:
        return None
    return (d - _today()).days


@router.get("/bids", response_class=HTMLResponse)
async def bids_board(
    request: Request,
    status: str = "",
    db=Depends(lambda: None),  # replaced at include time
):
    raise HTTPException(status_code=500, detail="router not mounted")