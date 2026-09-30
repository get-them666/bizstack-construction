"""Tests for the SBA microloan outreach cadence and lender-reply path.

No network and no database: every test patches the mailer and the connection.
"""

import asyncio
import datetime as _dt
import unittest
import warnings
from unittest import mock

import loan_outreach as lo


class FakeCursor:
    """Minimal cursor: `result` feeds fetchone()/fetchall(), `insert_returns`
    simulates an INSERT ... RETURNING that yields no row on conflict."""

    def __init__(self, store):
        self.store = store
        self.rowcount = 0
        self._one = None
        self._all = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        self.store.append((flat, params))
        if "RETURNING" in flat:
            if self.store.rowcounts is not None:
                # explicit per-statement claim outcomes, in order
                rc = self.store.rowcounts.pop(0) if self.store.rowcounts else 0
                self._one = {"id": rc} if rc else None
                self._all = []
                self.rowcount = rc
            elif "INSERT" in flat:
                self._one = self.store.take_insert()
                self._all = []
                self.rowcount = 1 if self._one is not None else 0
            else:
                self._one = self.store.result
                self._all = []
                self.rowcount = self.store.result_rowcount
        else:
            self._one = self.store.result
            self._all = self.store.results
            self.rowcount = self.store.result_rowcount
        return self

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeConn:
    def __init__(self, rows=None, results=None, rowcount=0, rowcounts=None):
        self.store = FakeStore(rows, results, rowcount, rowcounts)
        self.commits = 0

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        self.commits += 1


class FakeStore(list):
    def __init__(self, rows, results, rowcount, rowcounts=None):
        super().__init__()
        self.result = rows
        self.results = results if results is not None else []
        self.result_rowcount = rowcount
        self.rowcounts = list(rowcounts) if rowcounts is not None else None
        self.inserted = set()

    def take_insert(self):
        """First INSERT of a given message_id wins; repeats conflict to None."""
        for sql, params in reversed(self):
            if "INSERT" not in sql or not params:
                continue
            if params[0] in self.inserted:
                return None
            self.inserted.add(params[0])
            return {"id": len(self.inserted)}
        return None


class TouchpointKindTests(unittest.TestCase):
    def test_every_touchpoint_fires_for_every_lender(self):
        """Regression: Day 1 and Day 7 were tagged 'text' while every lender is
        email-only, so the channel check skipped them forever and the lenders
        only ever received the Day 3 email."""
        for lender in lo.LENDERS:
            for day in (1, 3, 7, 14):
                spec = lo._day_body(day)
                self.assertTrue(spec, f"day {day} has no body")
                self.assertIn(
                    spec["kind"],
                    lender["channels"],
                    f"day {day} ({spec['kind']}) can never be sent to {lender['name']}",
                )

    def test_touchpoints_have_subject_and_body(self):
        for day in (1, 3, 7, 14):
            spec = lo._day_body(day)
            self.assertTrue(spec["subject"].strip())
            self.assertGreater(len(spec["body"]), 80)

    def test_unknown_day_has_no_body(self):
        self.assertEqual(lo._day_body(99), {})


class LenderAllowlistTests(unittest.TestCase):
    def test_known_lender_domains_match(self):
        # A formatted From header must still resolve to the address.
        for addr in ("smallbusiness@lisc.org", "wmartin@lisc.org", "jbarnes@vccva.org",
                     "VSBFA@sbsd.virginia.gov", "someone@sba.gov"):
            self.assertTrue(lo._is_reply_from_lender(addr), addr)

    def test_outsiders_do_not_match(self):
        for addr in ("", "someone@gmail.com", "lisc.org.evil.com@attacker.net",
                     "nope@notsba.gov", "sba.gov.evil.net", "lisc.org@attacker.net"):
            self.assertFalse(lo._is_reply_from_lender(addr), addr)

    def test_auto_messages_are_detected(self):
        self.assertTrue(lo._is_auto_message({"Subject": "Out of Office"}))
        self.assertTrue(lo._is_auto_message({"From": "noreply@lisc.org"}))
        self.assertTrue(lo._is_auto_message({"Auto-Submitted": "auto-replied"}))
        self.assertFalse(lo._is_auto_message({"From": "smallbusiness@lisc.org",
                                              "Subject": "Re: $50K microloan"}))


class SendEmailTests(unittest.TestCase):
    def test_delegates_to_shared_mailer_with_cc(self):
        """Outreach must ride the app mailer (Resend/SES/SMTP ladder) because
        direct smtplib egress is firewalled on the host."""
        cfg = lo._mailer_cfg()
        self.assertEqual(cfg["SMTP_NAME"], lo.FROM_NAME)
        with mock.patch.object(lo, "_shared_send_email", new=mock.AsyncMock(return_value=True)) as shared:
            ok = asyncio.run(lo.send_email("a@lisc.org", "b@lisc.org", "Subject", "Body"))
        self.assertTrue(ok)
        self.assertEqual(shared.call_args.kwargs["cc"], "b@lisc.org")
        self.assertEqual(shared.call_args.args[1], "a@lisc.org")
        self.assertEqual(shared.call_args.args[2], "Subject")

    def test_mailer_failure_is_not_an_exception(self):
        with mock.patch.object(lo, "_shared_send_email", new=mock.AsyncMock(side_effect=RuntimeError("boom"))):
            self.assertFalse(asyncio.run(lo.send_email("a@lisc.org", "", "S", "B")))
        with mock.patch.object(lo, "_shared_send_email", new=mock.AsyncMock(return_value=False)):
            self.assertFalse(asyncio.run(lo.send_email("a@lisc.org", "", "S", "B")))


class HandleInboundTests(unittest.TestCase):
    PAYLOAD = {
        "type": "email.received",
        "data": {
            "email_id": "abc-123",
            "from": "WMartin <wmartin@lisc.org>",
            "subject": "Re: $50K microloan",
            "text": "Can you send the full package?",
        },
    }

    def test_claims_allowlisted_sender(self):
        with mock.patch.object(lo, "_reply_to_lender", return_value={"status": "sent"}) as reply:
            out = asyncio.run(lo.handle_inbound(self.PAYLOAD))
        self.assertEqual(len(out), 1)
        args = reply.call_args.args
        self.assertEqual(args[0], "wmartin@lisc.org")
        self.assertEqual(args[1], "Re: $50K microloan")
        self.assertEqual(args[2], "Can you send the full package?")
        self.assertEqual(args[3], "wh|abc-123")

    def test_ignores_non_lender_senders(self):
        with mock.patch.object(lo, "_reply_to_lender") as reply:
            out = asyncio.run(lo.handle_inbound(
                {"from": "lead@example.com", "subject": "hi", "text": "hi"}))
        self.assertEqual(out, [])
        reply.assert_not_called()

    def test_ignores_autoreplies(self):
        with mock.patch.object(lo, "_reply_to_lender") as reply:
            out = asyncio.run(lo.handle_inbound(
                {"from": "jbarnes@vccva.org", "subject": "Out of Office", "text": "away"}))
        self.assertEqual(out, [])
        reply.assert_not_called()

    def test_handles_bare_list_payload(self):
        with mock.patch.object(lo, "_reply_to_lender", return_value={"status": "sent"}) as reply:
            out = asyncio.run(lo.handle_inbound(
                [{"from": "jbarnes@vccva.org", "subject": "s", "text": "t"}]))
        self.assertEqual(len(out), 1)
        reply.assert_called_once()

    def test_reply_failure_does_not_break_the_webhook(self):
        with mock.patch.object(lo, "_reply_to_lender", side_effect=RuntimeError("db down")):
            self.assertEqual(asyncio.run(lo.handle_inbound(self.PAYLOAD)), [])

    def test_claim_happens_before_send(self):
        """A redelivery must not send a second reply, so the message_id is
        claimed before the mailer is touched."""
        conn = FakeConn()
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = conn
        order = []
        with mock.patch.object(lo.psycopg, "connect", return_value=ctx), \
             mock.patch.object(lo, "_record_reply", side_effect=lambda *a, **k: order.append("claim") or True), \
             mock.patch.object(lo, "_draft_reply", side_effect=lambda *a: order.append("draft") or "r"), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(side_effect=lambda *a, **k: order.append("send") or True)):
            out = asyncio.run(lo._reply_to_lender("a@lisc.org", "s", "b", "m1"))
        self.assertEqual(out["status"], "sent")
        self.assertEqual(order, ["claim", "draft", "send"])

    def test_duplicate_claim_skips_draft_and_send(self):
        conn = FakeConn()
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = conn
        with mock.patch.object(lo.psycopg, "connect", return_value=ctx), \
             mock.patch.object(lo, "_draft_reply") as draft, \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock()) as send:
            first = asyncio.run(lo._reply_to_lender("a@lisc.org", "s", "b", "m1"))
            draft.reset_mock()
            send.reset_mock()
            second = asyncio.run(lo._reply_to_lender("a@lisc.org", "s", "b", "m1"))
        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "duplicate")
        draft.assert_not_called()
        send.assert_not_called()

    def test_falls_back_to_composite_message_id(self):
        with mock.patch.object(lo, "_reply_to_lender", return_value={"status": "sent"}) as reply:
            asyncio.run(lo.handle_inbound(
                {"from": "jbarnes@vccva.org", "subject": "s", "text": "t",
                 "created_at": "2026-09-28T00:00:00Z"}))
        self.assertEqual(reply.call_args.args[3], "jbarnes@vccva.org|s|2026-09-28T00:00:00Z")


class RecordReplyTests(unittest.TestCase):
    def test_duplicate_message_id_is_not_recorded_twice(self):
        # Same store across calls: a webhook redelivery opens a new connection
        # but hits the same unique index.
        conn = FakeConn()
        self.assertTrue(lo._record_reply(conn, "m1", "a@lisc.org", "s", "b", "r", "sent"))
        self.assertFalse(lo._record_reply(conn, "m1", "a@lisc.org", "s", "b", "r", "sent"))
        self.assertTrue(lo._record_reply(conn, "m2", "a@lisc.org", "s2", "b", "r", "sent"))


class CadenceTests(unittest.TestCase):
    def _conn_with(self, touch):
        conn = FakeConn(rows=touch, rowcount=1)
        return conn

    def test_already_sent_touch_is_skipped(self):
        conn = self._conn_with({"status": "sent", "sent_at": None})
        with mock.patch.object(lo, "_campaign_start", return_value=_dt.date.today() - _dt.timedelta(days=5)), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            asyncio.run(lo._run_cadence(conn))
        send.assert_not_called()

    def test_missed_touch_is_never_retried(self):
        conn = self._conn_with({"status": "missed", "sent_at": None})
        with mock.patch.object(lo, "_campaign_start", return_value=_dt.date.today() - _dt.timedelta(days=5)), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            asyncio.run(lo._run_cadence(conn))
        send.assert_not_called()

    def test_late_touch_is_recorded_missed_not_sent(self):
        """A touchpoint outside its grace window must be recorded, never mailed
        late: the copy is time-relative ("I emailed you yesterday")."""
        start = _dt.date.today() - _dt.timedelta(days=20)
        conn = FakeConn(rows=None, rowcount=0)
        with mock.patch.object(lo, "_campaign_start", return_value=start), \
             mock.patch.object(lo, "mail_ready", return_value=True), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            asyncio.run(lo._run_cadence(conn))
        send.assert_not_called()
        statuses = [p[6] for sql, p in conn.store if "INSERT INTO outreach_touches" in sql]
        self.assertTrue(statuses)
        self.assertEqual(set(statuses), {"missed"})

    def test_dead_mail_does_not_burn_the_cadence(self):
        """Regression: `missed` is terminal, so marking it while mail is
        undeliverable permanently gave up on the whole campaign while the log
        still looked healthy. Touchpoints must stay open when nothing can send.
        """
        start = _dt.date.today() - _dt.timedelta(days=20)
        conn = FakeConn(rows=None, rowcount=0)
        with mock.patch.object(lo, "_campaign_start", return_value=start), \
             mock.patch.object(lo, "mail_ready", return_value=False), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            asyncio.run(lo._run_cadence(conn))
        send.assert_not_called()
        statuses = [p[6] for sql, p in conn.store if "INSERT INTO outreach_touches" in sql]
        self.assertEqual(statuses, [], f"touches were burned while mail was dead: {statuses}")

    def test_mail_preflight_reports_broken_without_a_token(self):
        """The preflight must never raise, even with no Google module or token."""
        lo._MAIL_READY_CACHE.clear()
        with mock.patch.object(lo, "FROM_ADDR", "hello@example.com"), \
             mock.patch.dict(lo.os.environ, {"GMAIL_SEND_SERVICES": "construction"}, clear=False):
            ok = lo.mail_ready()
        self.assertIsInstance(ok, bool)

    def test_due_touch_is_sent_and_recorded(self):
        start = _dt.date.today() - _dt.timedelta(days=1)
        conn = FakeConn(rows=None, rowcount=0)
        with mock.patch.object(lo, "_campaign_start", return_value=start), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            asyncio.run(lo._run_cadence(conn))
        send.assert_called()
        inserts = [p for sql, p in conn.store
                   if "INSERT INTO outreach_touches" in sql and len(p) == 8]
        self.assertEqual(len(inserts), len(lo.LENDERS))
        for p in inserts:
            self.assertEqual(p[6], "sent")

    def test_send_failure_is_recorded_as_failed(self):
        start = _dt.date.today() - _dt.timedelta(days=1)
        conn = FakeConn(rows=None, rowcount=0)
        with mock.patch.object(lo, "_campaign_start", return_value=start), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=False)):
            asyncio.run(lo._run_cadence(conn))
        for sql, p in conn.store:
            if "INSERT INTO outreach_touches" in sql and len(p) == 8:
                self.assertEqual(p[6], "failed")

    def test_cadence_does_not_start_before_day_one(self):
        conn = FakeConn(rows=None, rowcount=0)
        with mock.patch.object(lo, "_campaign_start", return_value=_dt.date.today()), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            asyncio.run(lo._run_cadence(conn))
        send.assert_not_called()


class RetryFailedRepliesTests(unittest.TestCase):
    def test_only_failed_rows_are_retried(self):
        rows = [{"message_id": "m1", "sender": "a@lisc.org", "subject": "s", "reply_body": "r"}]
        conn = FakeConn(results=rows, rowcount=1)
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = conn
        with mock.patch.object(lo.psycopg, "connect", return_value=ctx), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            out = asyncio.run(lo.retry_failed_replies())
        self.assertEqual(out, [{"sender": "a@lisc.org", "status": "sent"}])
        send.assert_called_once()
        selects = [sql for sql, _ in conn.store if "SELECT message_id" in sql]
        self.assertTrue(selects)
        self.assertIn("status IN ('failed', 'pending')", selects[0])

    def test_database_error_is_swallowed(self):
        with mock.patch.object(lo.psycopg, "connect", side_effect=RuntimeError("no db")):
            self.assertEqual(asyncio.run(lo.retry_failed_replies()), [])


class ClaimTouchTests(unittest.TestCase):
    """Two apps run this scheduler against one database, so a touchpoint must
    be claimed atomically or both apps email the same lender."""

    LENDER = {"name": "LISC", "to": "smallbusiness@lisc.org", "cc": "wmartin@lisc.org",
              "channels": [lo.CHANNEL_EMAIL]}

    def test_claim_flip_is_conditional(self):
        conn = FakeConn(rowcounts=[1])
        self.assertTrue(lo._claim_touch(conn, self.LENDER, 7))
        sqls = [s for s, _ in conn.store]
        self.assertEqual(len(sqls), 1, "existing row must be claimed by UPDATE alone")
        self.assertIn("RETURNING id", sqls[0])
        self.assertIn("status <> 'sending'", sqls[0])
        self.assertIn("SET status = 'sending'", sqls[0])

    def test_second_claim_is_refused(self):
        # existing 'sending' row -> UPDATE misses, INSERT conflicts, refused
        conn = FakeConn(rowcounts=[0, 0])
        self.assertFalse(lo._claim_touch(conn, self.LENDER, 7))
        self.assertIn("ON CONFLICT", conn.store[1][0])

    def test_fresh_touch_is_claimed_via_insert(self):
        conn = FakeConn(rowcounts=[0, 1])
        self.assertTrue(lo._claim_touch(conn, self.LENDER, 7))
        self.assertTrue(any("INSERT" in s and "'sending'" in s and len(p) == 4
                            for s, p in conn.store))

    def test_stale_sending_is_taken_over(self):
        conn = FakeConn(rowcounts=[1])
        self.assertTrue(lo._claim_touch(conn, self.LENDER, 7))
        sql, params = conn.store[0]
        self.assertIn("sent_at >", sql)
        cutoff = params[-1]
        delta = lo._dt.datetime.now(lo._dt.timezone.utc) - cutoff
        self.assertAlmostEqual(delta.total_seconds() / 60, lo.STALE_SENDING_MINUTES, delta=1)

    def test_cadence_sends_once_when_claim_is_refused(self):
        conn = FakeConn(rowcounts=[0])
        with mock.patch.object(lo, "_campaign_start", return_value=lo._dt.date.today() - lo._dt.timedelta(days=3)), \
             mock.patch.object(lo, "_claim_touch", return_value=False), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            asyncio.run(lo._run_cadence(conn))
        send.assert_not_called()

    def test_cadence_claims_before_sending(self):
        conn = FakeConn(rowcounts=[1])
        order = []
        with mock.patch.object(lo, "_campaign_start", return_value=lo._dt.date.today() - lo._dt.timedelta(days=3)), \
             mock.patch.object(lo, "_claim_touch", side_effect=lambda *a: order.append("claim") or True), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(side_effect=lambda *a, **k: order.append("send") or True)):
            asyncio.run(lo._run_cadence(conn))
        self.assertTrue(order)
        # every send must be immediately preceded by a claim, and they alternate
        self.assertEqual(order[::2], ["claim"] * (len(order) // 2))
        self.assertEqual(order[1::2], ["send"] * (len(order) // 2))


class AsyncWebhookTests(unittest.TestCase):
    """Regression: the reply path used a sync function calling asyncio.run().

    The inbound-email webhook is `async def`, so it runs inside a live event
    loop where asyncio.run() raises RuntimeError. handle_inbound swallowed it
    per-message, so every lender reply failed silently and the whole point of
    the fix (answering lenders) never worked. These tests call the entry points
    from inside a running loop, exactly as the route does.
    """

    PAYLOAD = {"type": "email.received", "data": {
        "email_id": "e-real", "from": "jbarnes@vccva.org",
        "subject": "Re: $50K microloan", "text": "Send the package."}}

    def test_handle_inbound_works_inside_a_running_loop(self):
        conn = FakeConn()
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = conn

        async def route():
            return await lo.handle_inbound(self.PAYLOAD)

        with mock.patch.object(lo.psycopg, "connect", return_value=ctx), \
             mock.patch.object(lo, "_draft_reply", return_value="Sending now."), \
             mock.patch.object(lo, "_reply_body_for", return_value="Send the package."), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            out = asyncio.run(route())

        self.assertEqual([r["status"] for r in out], ["sent"])
        send.assert_called_once()

    def test_reply_to_lender_works_inside_a_running_loop(self):
        conn = FakeConn()
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = conn

        async def route():
            return await lo._reply_to_lender("jbarnes@vccva.org", "s", "b", "wh|1")

        with mock.patch.object(lo.psycopg, "connect", return_value=ctx), \
             mock.patch.object(lo, "_draft_reply", return_value="r"), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)):
            out = asyncio.run(route())
        self.assertEqual(out["status"], "sent")

    def test_retry_works_inside_a_running_loop(self):
        conn = FakeConn(results=[{"message_id": "m", "sender": "a@lisc.org",
                                  "subject": "s", "reply_body": "r"}])
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = conn

        async def route():
            return await lo.retry_failed_replies()

        with mock.patch.object(lo.psycopg, "connect", return_value=ctx), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            out = asyncio.run(route())
        send.assert_called_once()
        self.assertEqual(out[0]["status"], "sent")

    def test_no_awaited_coroutine_is_dropped(self):
        """A dropped coroutine means the mail was never sent."""
        conn = FakeConn()
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = conn

        async def route():
            return await lo.handle_inbound(self.PAYLOAD)

        with mock.patch.object(lo.psycopg, "connect", return_value=ctx), \
             mock.patch.object(lo, "_draft_reply", return_value="r"), \
             mock.patch.object(lo, "_reply_body_for", return_value="b"), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)), \
             warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            asyncio.run(route())
        self.assertEqual([w for w in caught if "never awaited" in str(w.message)], [])


class ReplySubjectTests(unittest.TestCase):
    def test_does_not_double_prefix(self):
        self.assertEqual(lo._reply_subject("Re: $50K microloan"), "Re: $50K microloan")
        self.assertEqual(lo._reply_subject("RE: hello"), "RE: hello")
        self.assertEqual(lo._reply_subject("$50K microloan"), "Re: $50K microloan")
        self.assertEqual(lo._reply_subject(""), "Re: ")

    def test_reply_is_sent_with_a_single_prefix(self):
        conn = FakeConn()
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = conn
        with mock.patch.object(lo.psycopg, "connect", return_value=ctx), \
             mock.patch.object(lo, "_draft_reply", return_value="r"), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            asyncio.run(lo._reply_to_lender("a@lisc.org", "Re: $50K microloan", "b", "m1"))
        self.assertEqual(send.call_args.args[2], "Re: $50K microloan")


class CatchupTests(unittest.TestCase):
    def _ctx(self, conn):
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = conn
        return ctx

    def test_catchup_is_recorded_as_day_zero(self):
        """Day 0 keeps the catch-up out of the 1/3/7/14 cadence, and must not
        reset the campaign clock (that would re-send Day 3 to lenders who
        already received it)."""
        conn = FakeConn()
        with mock.patch.object(lo.psycopg, "connect", return_value=self._ctx(conn)), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)):
            asyncio.run(lo.send_catchup())
        days = {p[2] for sql, p in conn.store
                if "INSERT INTO outreach_touches" in sql and len(p) == 8}
        self.assertEqual(days, {lo.CATCHUP_DAY})
        self.assertNotIn(1, days)
        self.assertNotIn(3, days)
        self.assertFalse([s for s, _ in conn.store if "UPDATE app_settings" in s],
                         "catch-up must not move the campaign start date")

    def test_catchup_copy_owns_the_gap(self):
        low = lo.CATCHUP_BODY.lower()
        self.assertIn("went quiet", low)
        self.assertNotIn("yesterday", low)
        self.assertIn("[Lender Name]", lo.CATCHUP_BODY)

    def test_catchup_does_not_resend_when_already_sent(self):
        conn = FakeConn(rows={"status": "sent"})
        with mock.patch.object(lo.psycopg, "connect", return_value=self._ctx(conn)), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            out = asyncio.run(lo.send_catchup())
        send.assert_not_called()
        self.assertEqual({r["status"] for r in out}, {"already-sent"})

    def test_force_resends(self):
        conn = FakeConn(rows={"status": "sent"})
        with mock.patch.object(lo.psycopg, "connect", return_value=self._ctx(conn)), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            out = asyncio.run(lo.send_catchup(force=True))
        self.assertEqual(send.call_count, len(lo.LENDERS))
        self.assertEqual({r["status"] for r in out}, {"sent"})

    def test_catchup_reaches_every_lender_with_cc(self):
        conn = FakeConn()
        with mock.patch.object(lo.psycopg, "connect", return_value=self._ctx(conn)), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=True)) as send:
            asyncio.run(lo.send_catchup())
        self.assertEqual(send.call_count, len(lo.LENDERS))
        for lender in lo.LENDERS:
            self.assertTrue(any(c.args[0] == lender["to"] and c.args[1] == lender["cc"]
                                for c in send.call_args_list), lender["name"])

    def test_catchup_records_failure(self):
        conn = FakeConn()
        with mock.patch.object(lo.psycopg, "connect", return_value=self._ctx(conn)), \
             mock.patch.object(lo, "send_email", new=mock.AsyncMock(return_value=False)):
            out = asyncio.run(lo.send_catchup())
        self.assertEqual({r["status"] for r in out}, {"failed"})
        for sql, p in conn.store:
            if "INSERT INTO outreach_touches" in sql and len(p) == 8:
                self.assertEqual(p[6], "failed")


if __name__ == "__main__":
    unittest.main()
