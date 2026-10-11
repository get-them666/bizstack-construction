"""Pure lifecycle rules -- no database."""

import unittest
from datetime import datetime, timedelta, timezone

from bizstack_leads import lifecycle


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def lead(**kw):
    base = {"status": "new", "phone": "5551234", "email": "", "created_at": NOW,
            "last_outbound_at": None, "do_not_contact": False}
    base.update(kw)
    return base


class StageTranslationTests(unittest.TestCase):
    def test_legacy_vocabularies_collapse_onto_stages(self):
        self.assertEqual(lifecycle.to_stage("analyzed"), lifecycle.NEW)
        self.assertEqual(lifecycle.to_stage("quote"), lifecycle.QUOTED)
        self.assertEqual(lifecycle.to_stage("host"), lifecycle.WON)
        self.assertEqual(lifecycle.to_stage("deposit"), lifecycle.QUOTED)
        self.assertEqual(lifecycle.to_stage("in_progress"), lifecycle.WON)
        self.assertEqual(lifecycle.to_stage("completed"), lifecycle.WON)

    def test_unknown_status_is_treated_as_new_not_buried(self):
        self.assertEqual(lifecycle.to_stage("weird-old-value"), lifecycle.NEW)
        self.assertEqual(lifecycle.to_stage(None), lifecycle.NEW)


class NextActionTests(unittest.TestCase):
    def test_closed_and_do_not_contact_are_closed(self):
        self.assertEqual(lifecycle.next_action(lead(status="won"), now=NOW)[0],
                         lifecycle.ACTION_CLOSED)
        self.assertEqual(lifecycle.next_action(lead(status="lost"), now=NOW)[0],
                         lifecycle.ACTION_CLOSED)
        self.assertEqual(lifecycle.next_action(lead(do_not_contact=True), now=NOW)[0],
                         lifecycle.ACTION_CLOSED)

    def test_reply_beats_everything(self):
        self.assertEqual(lifecycle.next_action(lead(status="engaged"), now=NOW)[0],
                         lifecycle.ACTION_RESPOND)

    def test_unreachable_goes_to_enrich_even_when_new(self):
        action, due = lifecycle.next_action(lead(phone="", email=""), now=NOW)
        self.assertEqual(action, lifecycle.ACTION_ENRICH)
        self.assertIsNone(due)

    def test_new_and_reachable_needs_first_touch(self):
        action, due = lifecycle.next_action(lead(), now=NOW)
        self.assertEqual(action, lifecycle.ACTION_FIRST_TOUCH)
        self.assertEqual(due, NOW)

    def test_contacted_is_waiting_until_the_follow_up_is_due(self):
        recent = NOW - timedelta(days=1)
        action, due = lifecycle.next_action(lead(status="contacted", last_outbound_at=recent), now=NOW)
        self.assertEqual(action, lifecycle.ACTION_WAITING)
        self.assertEqual(due, recent + timedelta(days=lifecycle.FOLLOW_UP_DAYS))

    def test_contacted_follow_up_becomes_due(self):
        old = NOW - timedelta(days=lifecycle.FOLLOW_UP_DAYS, hours=1)
        action, due = lifecycle.next_action(lead(status="contacted", last_outbound_at=old), now=NOW)
        self.assertEqual(action, lifecycle.ACTION_FOLLOW_UP)
        self.assertEqual(due, old + timedelta(days=lifecycle.FOLLOW_UP_DAYS))

    def test_quoted_leads_also_get_follow_ups(self):
        old = NOW - timedelta(days=5)
        action, _ = lifecycle.next_action(lead(status="quoted", last_outbound_at=old), now=NOW)
        self.assertEqual(action, lifecycle.ACTION_FOLLOW_UP)


if __name__ == "__main__":
    unittest.main()
