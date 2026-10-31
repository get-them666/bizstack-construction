"""End-to-end pipeline tests against a disposable Postgres.

Point DATABASE_URL at an empty database; the schema is created on the first
connection. Every test truncates first, so the database is disposable by design:

    createdb bz_leads_test
    DATABASE_URL=postgresql://localhost/bz_leads_test python -m unittest discover
"""

import os
import unittest
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

import auto_reply
import followups
from bizstack_leads import intake, lifecycle, outreach, queue, schema, store

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/bz_leads_test")


class Brand:
    name = "Broom Service"
    cta = "Call or text us at (757) 908-7121 anytime."


class FakeTransport:
    """Mirrors the real transports (auto_reply.fire_lead_draft): it delivers the
    message *and* records the outbound in comms_logs, which is what advances the
    lead through the lifecycle on the next recompute."""

    def __init__(self, db, sent=True, why=""):
        self.db, self.sent, self.why, self.calls = db, sent, why, []

    def send(self, lead, channel, body):
        self.calls.append((lead["id"], channel, body))
        if self.sent:
            with self.db.cursor() as cur:
                cur.execute(
                    "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body, lead_id) "
                    "VALUES ('outbound', %s, 'system', %s, %s, %s);",
                    (channel, lead.get("email") or "", body, lead["id"]),
                )
        return {"sent": self.sent, "channel": channel, "why": self.why}


class PipelineTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)
        schema.create_schema(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        with self.db.cursor() as cur:
            cur.execute("TRUNCATE leads, comms_logs, payments RESTART IDENTITY;")

    def intake(self, **kw):
        kw.setdefault("company", "broom")
        kw.setdefault("source", "website")
        return intake.intake_lead(self.db, **kw)


class IntakeTests(PipelineTestCase):
    def test_new_reachable_lead_lands_in_first_touch(self):
        r = self.intake(name="Amy Owner", phone="(757) 555-0101")
        self.assertTrue(r["created"])
        self.assertEqual(r["next_action"], lifecycle.ACTION_FIRST_TOUCH)

    def test_phone_is_not_null_so_a_source_without_one_still_inserts(self):
        r = self.intake(name="Permit Only", address="1 Main St", phone="")
        self.assertEqual(r["next_action"], lifecycle.ACTION_ENRICH)
        lead = store.get_lead(self.db, r["lead_id"])
        self.assertEqual(lead["phone"], "")

    def test_the_same_source_ref_updates_instead_of_duplicating(self):
        first = self.intake(name="Permit 123", source="permit", source_ref="vb:123")
        again = self.intake(name="Permit 123", source="permit", source_ref="vb:123")
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(first["lead_id"], again["lead_id"])
        self.assertEqual(len(store.list_leads(self.db, "broom", action=lifecycle.ACTION_ENRICH)), 1)

    def test_a_thin_source_fills_blanks_without_overwriting(self):
        first = self.intake(name="Bob", email="bob@example.com")
        # A later source finds the same human and adds the phone Bob never gave.
        again = self.intake(name="Bob", email="BOB@example.com", phone="7575550102")
        self.assertFalse(again["created"])
        lead = store.get_lead(self.db, first["lead_id"])
        self.assertEqual(lead["phone"], "7575550102")
        self.assertEqual(lead["email"], "bob@example.com")

    def test_enrichment_flips_a_permit_from_needs_info_to_first_touch(self):
        r = self.intake(name="Permit", address="9 Oak", phone="")
        self.assertEqual(r["next_action"], lifecycle.ACTION_ENRICH)
        # Enrichment discovers an email and merges it in.
        merged = self.intake(name="Permit", address="9 Oak", email="owner@mail.com")
        self.assertEqual(merged["lead_id"], r["lead_id"])
        self.assertEqual(merged["next_action"], lifecycle.ACTION_FIRST_TOUCH)


class PlaceholderContactTests(PipelineTestCase):
    def test_an_lead_local_placeholder_email_is_not_reachable(self):
        # The permit importer / reddit scans write a fake @lead.local mailbox so
        # the NOT NULL email column is never NULL. It must not count as a real
        # contact detail, or those leads would be auto-contacted by the sweep.
        self.assertFalse(lifecycle.is_reachable({"phone": "", "email": "con-permit-abc@lead.local"}))
        self.assertTrue(lifecycle.is_reachable({"phone": "7575550101", "email": "con-permit-abc@lead.local"}))
        r = self.intake(name="Permit", address="2 Elm", phone="", email="con-permit-abc@lead.local")
        self.assertEqual(r["next_action"], lifecycle.ACTION_ENRICH)

    def test_intake_persists_app_specific_extra_columns(self):
        from datetime import datetime
        r = self.intake(
            name="Job", phone="7575550199", source="backlog",
            extra={
                "budget": "$12k", "timeline": "asap",
                "estimate_low_cents": 100000, "estimate_high_cents": 150000,
                "created_at": datetime(2024, 5, 1, tzinfo=timezone.utc),
            },
        )
        lead = store.get_lead(self.db, r["lead_id"])
        self.assertEqual(lead["budget"], "$12k")
        self.assertEqual(lead["timeline"], "asap")
        self.assertEqual(lead["estimate_low_cents"], 100000)
        self.assertEqual(lead["created_at"].year, 2024)

    def test_unknown_extra_columns_are_ignored_not_fatal(self):
        r = self.intake(name="Safe", phone="1", extra={"no_such_column": 42})
        self.assertTrue(r["created"])


class QueueTests(PipelineTestCase):
    def test_buckets_group_by_action_most_urgent_first(self):
        self.intake(name="Cold", phone="1")
        replied = self.intake(name="Replied", phone="2")
        store.record_inbound(self.db, replied["lead_id"], "sms", "yes please")
        buckets = queue.buckets(self.db, "broom")
        self.assertEqual(buckets[0]["action"], lifecycle.ACTION_RESPOND)
        self.assertIn(lifecycle.ACTION_FIRST_TOUCH, [b["action"] for b in buckets])

    def test_counts_include_zeroes_for_expected_buckets(self):
        self.intake(name="Cold", phone="1")
        c = queue.counts(self.db, "broom")
        self.assertEqual(c[lifecycle.ACTION_FIRST_TOUCH], 1)
        self.assertEqual(c[lifecycle.ACTION_RESPOND], 0)

    def test_group_is_pure_and_buckets_by_action(self):
        rows = [
            {"id": 1, "next_action": lifecycle.ACTION_RESPOND},
            {"id": 2, "next_action": lifecycle.ACTION_FIRST_TOUCH},
            {"id": 3, "next_action": lifecycle.ACTION_RESPOND},
        ]
        out = queue.group(rows)
        self.assertEqual(out[0]["action"], lifecycle.ACTION_RESPOND)
        self.assertEqual(out[0]["count"], 2)

    def test_closed_leads_are_hidden_unless_asked_for(self):
        won = self.intake(name="Won", phone="1")["lead_id"]
        self.intake(name="Open", phone="2")
        store.set_stage(self.db, won, lifecycle.WON)
        open_names = [b["leads"] for b in queue.buckets(self.db, "broom")]
        self.assertNotIn("Won", [l["name"] for group in open_names for l in group])
        allb = queue.buckets(self.db, "broom", include_closed=True)
        self.assertIn("Won", [l["name"] for group in [b["leads"] for b in allb] for l in group])

    def test_where_extra_layers_an_app_policy_onto_the_query(self):
        self.intake(name="Keep", phone="1", source="website")
        self.intake(name="Hide", phone="2", source="sam-gov")
        buckets = queue.buckets(self.db, "broom", where_extra="source <> %s", extra_params=("sam-gov",))
        names = [l["name"] for b in buckets for l in b["leads"]]
        self.assertIn("Keep", names)
        self.assertNotIn("Hide", names)


class TouchAndStageTests(PipelineTestCase):
    def test_manual_touch_moves_new_to_contacted(self):
        lead_id = self.intake(name="Amy", phone="1")["lead_id"]
        res = store.record_touch(self.db, lead_id, "text", "knocked, no answer")
        self.assertTrue(res["ok"])
        lead = store.recompute(self.db, lead_id)
        self.assertEqual(lead["status"], lifecycle.CONTACTED)
        self.assertEqual(lead["touch_count"], 1)

    def test_follow_up_becomes_due_after_the_interval(self):
        lead_id = self.intake(name="Amy", phone="1")["lead_id"]
        store.record_touch(self.db, lead_id, "phone")
        future = datetime.now(timezone.utc) + timedelta(days=lifecycle.FOLLOW_UP_DAYS, hours=1)
        lead = store.recompute(self.db, lead_id, now=future)
        self.assertEqual(lead["next_action"], lifecycle.ACTION_FOLLOW_UP)

    def test_staging_a_draft_does_not_send_or_change_stage(self):
        lead_id = self.intake(name="Amy", phone="1")["lead_id"]
        outreach.stage_first_touch(self.db, Brand, lead_id)
        lead = store.get_lead(self.db, lead_id)
        self.assertIn("Thanks for reaching out", lead["draft_reply"])
        self.assertEqual(lead["status"], lifecycle.NEW)

    def test_send_through_transport_marks_contacted_and_clears_the_draft(self):
        lead_id = self.intake(name="Amy", email="amy@x.com")["lead_id"]
        transport = FakeTransport(self.db, sent=True)
        res = outreach.send(self.db, transport, lead_id, "Hi", "Body")
        self.assertTrue(res["sent"])
        lead = store.get_lead(self.db, lead_id)
        self.assertIsNone(lead["draft_reply"])
        self.assertEqual(lead["status"], lifecycle.CONTACTED)
        self.assertEqual(transport.calls[0][0], lead_id)

    def test_a_blocked_send_leaves_no_staged_draft_behind(self):
        lead_id = self.intake(name="Amy", email="amy@x.com")["lead_id"]
        res = outreach.send(self.db, FakeTransport(self.db, sent=False, why="daily cap"), lead_id, "Hi", "Body")
        self.assertFalse(res["sent"])
        self.assertEqual(res["why"], "daily cap")
        lead = store.get_lead(self.db, lead_id)
        self.assertIsNone(lead["draft_reply"])

    def test_send_refuses_a_lead_with_no_email(self):
        lead_id = self.intake(name="Amy", phone="1")["lead_id"]
        res = outreach.send(self.db, FakeTransport(self.db), lead_id, "Hi", "Body")
        self.assertFalse(res["sent"])
        self.assertIn("no email", res["why"])


class BackfillTests(PipelineTestCase):
    def test_backfill_tags_legacy_rows_so_counts_match_buckets(self):
        a = self.intake(name="Old One", phone="1")["lead_id"]
        b = self.intake(name="Old Two", phone="2")["lead_id"]
        # Simulate rows that predate the pipeline: untagged, legacy statuses.
        with self.db.cursor() as cur:
            cur.execute(
                "UPDATE leads SET next_action = NULL, status = 'analyzed' WHERE id = ANY(%s);",
                ([a, b],),
            )
        self.assertEqual(queue.counts(self.db, "broom")[lifecycle.ACTION_FIRST_TOUCH], 0)
        self.assertEqual(store.backfill(self.db, "broom"), 2)
        self.assertEqual(queue.counts(self.db, "broom")[lifecycle.ACTION_FIRST_TOUCH], 2)
        self.assertEqual(len(store.list_leads(self.db, "broom", action=lifecycle.ACTION_FIRST_TOUCH)), 2)

    def test_backfill_is_a_noop_once_everything_is_tagged(self):
        self.intake(name="Tagged", phone="1")
        self.assertEqual(store.backfill(self.db, "broom"), 0)


class RecomputeTests(PipelineTestCase):
    def test_recompute_normalises_a_legacy_status_in_place(self):
        lead_id = self.intake(name="Legacy", phone="1")["lead_id"]
        with self.db.cursor() as cur:
            cur.execute("UPDATE leads SET status = 'analyzed' WHERE id = %s;", (lead_id,))
        lead = store.recompute(self.db, lead_id)
        self.assertEqual(lead["status"], lifecycle.NEW)

    def test_do_not_contact_hides_the_lead_from_the_working_queue(self):
        lead_id = self.intake(name="Stop", phone="1")["lead_id"]
        store.set_do_not_contact(self.db, lead_id, True)
        lead = store.recompute(self.db, lead_id)
        self.assertEqual(lead["next_action"], lifecycle.ACTION_CLOSED)


class AutomatedSendRefreshTests(PipelineTestCase):
    """A delivered message must move the queue, not just the comms log.

    The bot used to write ``comms_logs`` and set ``status='contacted'`` without
    recomputing. The lead's ``last_outbound_at`` and follow-up date stayed in
    the past, so the Today call list kept showing the same people as overdue
    however many emails went out. The owner saw "Contacted 97" day after day.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        followups.ensure_schema(cls.db, force=True)

    def test_a_bot_send_refreshes_last_touch_and_next_action(self):
        lead_id = self.intake(name="Bot Send", email="bot@example.com")["lead_id"]
        self.assertTrue(auto_reply._fire_sent_effect(
            self.db, lead_id, "broom", "email", "bot@example.com", "hello",
        ))
        lead = store.get_lead(self.db, lead_id)
        self.assertEqual(lead["status"], lifecycle.CONTACTED)
        self.assertEqual(lead["touch_count"], 1)
        self.assertIsNotNone(lead["last_outbound_at"])
        self.assertEqual(lead["next_action"], lifecycle.ACTION_WAITING)

    def test_a_followup_send_leaves_the_overdue_bucket(self):
        lead_id = self.intake(name="Follow", email="follow@example.com")["lead_id"]
        followups._record(self.db, lead_id, 0, "email", "follow@example.com", "hello")
        lead = store.get_lead(self.db, lead_id)
        self.assertEqual(lead["status"], lifecycle.CONTACTED)
        self.assertEqual(lead["touch_count"], 1)
        self.assertEqual(lead["next_action"], lifecycle.ACTION_WAITING)

    def test_an_owner_touch_moves_a_new_lead_out_of_first_touch(self):
        lead_id = self.intake(name="Called", phone="7575550188")["lead_id"]
        self.assertEqual(
            store.get_lead(self.db, lead_id)["next_action"],
            lifecycle.ACTION_FIRST_TOUCH,
        )
        store.record_touch(self.db, lead_id, "phone", "called from Today")
        lead = store.get_lead(self.db, lead_id)
        self.assertEqual(lead["status"], lifecycle.CONTACTED)
        self.assertEqual(lead["touch_count"], 1)
        self.assertEqual(lead["next_action"], lifecycle.ACTION_WAITING)
        self.assertEqual(store.list_leads(
            self.db, "broom", action=lifecycle.ACTION_FIRST_TOUCH,
        ), [])


if __name__ == "__main__":
    unittest.main()
