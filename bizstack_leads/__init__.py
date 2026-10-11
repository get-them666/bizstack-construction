"""bizstack_leads -- the shared lead pipeline for BizStack apps.

One lifecycle, one intake, one work queue, shared by Broom and Buildstack so a
fix lands in both instead of being copied between two ``main.py`` files.

    from bizstack_leads import schema, intake, queue, store, outreach, lifecycle

    schema.ensure_schema(db)                     # once, at boot
    intake.intake_lead(db, company="broom", source="website", ...)
    queue.buckets(db, company="broom")           # what the owner sees
"""
from . import intake, lifecycle, outreach, queue, schema, store

__all__ = ["intake", "lifecycle", "outreach", "queue", "schema", "store"]

__version__ = "0.1.0"
