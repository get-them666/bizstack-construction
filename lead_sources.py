"""Scheduled lead sources for BizStack (no API keys).

SAM.gov was removed on 2026-10-13. The operator does not want federal or
government work, so the federal scanner, its NAICS presets and the
LEAD_SOURCES_SAM_GOV kill switch are all gone rather than merely disabled --
a disabled scanner is one env var away from resurrecting itself.

Scrap.io remains. It is supply-side: local trade businesses scraped from Google
Maps, which is where residential and light-commercial customers come from.
"""

import scrap_io


def scan(limit=None, preset=None):
    """Aggregate the enabled lead sources. Scrap.io only.

    Kept the same shape the caller expects: matches, errors and disabled.
    """
    matches, errors, disabled = [], [], []

    if scrap_io.enabled():
        local = scrap_io.scan_scrap_io(preset=preset)
        matches.extend(local.get("matches") or [])
        errors.extend(local.get("errors") or [])
        for d in local.get("disabled") or []:
            disabled.append(d)
    else:
        print("[lead-source] Scrap.io disabled (LEAD_SOURCES_SCRAP_IO=0); skipping maps scan", flush=True)
        disabled.append("scrap-io")

    if limit:
        matches = matches[:limit]
    return {"matches": matches, "errors": errors, "disabled": disabled}