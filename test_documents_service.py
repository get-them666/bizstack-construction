"""Tests for the outbound mail ladder in documents_service.send_email.

The regression these guard: Resend's daily quota is shared by every consumer in
the Railway project and the lead auto-reply bot can exhaust it on its own. The
mailer used to hard-stop all external sends for the rest of the day, which
silently disabled the business-critical lender sequence. It must now degrade to
the SES/SMTP ladder instead.

Note: RESEND_API_KEY must be present in the environment for the Resend branch to
run at all, so every case sets it. Without it the quota logic is never reached.
"""

import asyncio
import types
import unittest
from unittest import mock

import documents_service as ds


BASE_CFG = {
    "SMTP_HOST": "smtp.example.net",
    "SMTP_PORT": "587",
    "SMTP_TLS": "starttls",
    "SMTP_USER": "user@example.net",
    "SMTP_PASS": "secret",
    "SMTP_FROM": "hello@example.net",
    "SMTP_NAME": "BizStack",
}

TO = "smallbusiness@lisc.org"


def _run(
    env=None,
    resend_result=True,
    ses_result=True,
    ses_configured=True,
    quota_blocked=True,
    smtp_fails=False,
    sink=None,
):
    """Call send_email with every network layer stubbed out.

    Defaults model the failing case seen in production: Resend quota exhausted,
    sandbox SES rejecting the unverified external recipient.
    """
    cfg = dict(BASE_CFG)
    smtp_attempts = []

    async def _fake_smtp(msg, **kwargs):
        smtp_attempts.append(kwargs)
        if sink is not None:
            sink.append(kwargs)
        if smtp_fails:
            raise RuntimeError("SMTP 587/starttls attempt failed: connection refused")

    mocks = types.SimpleNamespace(
        resend=mock.AsyncMock(return_value=resend_result),
        # _send_via_ses is invoked through asyncio.to_thread, so it must be a
        # *synchronous* mock. An AsyncMock here returns an un-awaited coroutine
        # object, which is truthy and would make every SES failure look like a
        # success -- exactly the bug this suite exists to prevent.
        ses=mock.MagicMock(return_value=ses_result),
        smtp=mock.AsyncMock(side_effect=_fake_smtp),
    )

    base_env = {"RESEND_API_KEY": "re_test_key"}
    # These cases cover the Resend/SES/SMTP ladder, which the operator retired.
    # It is still reachable behind EMAIL_TRANSPORT=legacy, so pin it explicitly
    # rather than letting the Gmail-only default short-circuit every test.
    base_env["EMAIL_TRANSPORT"] = "legacy"
    base_env.update(env or {})

    with mock.patch.dict("os.environ", base_env, clear=True), mock.patch.object(
        ds, "_send_via_resend_api", mocks.resend
    ), mock.patch.object(ds, "_send_via_ses", mocks.ses), mock.patch.object(
        ds.aiosmtplib, "send", mocks.smtp
    ), mock.patch.object(
        ds, "ses_configured", return_value=ses_configured
    ), mock.patch.object(
        ds, "_resend_quota_blocked", return_value=quota_blocked
    ):
        result = asyncio.run(ds.send_email(cfg, TO, "Subject", "Body"))
    return result, mocks, smtp_attempts


class QuotaFallbackTests(unittest.TestCase):
    def test_quota_exhausted_still_delivers(self):
        """The core regression: an exhausted quota must not drop the message."""
        result, _, smtp = _run(ses_result=False)
        self.assertTrue(result)
        self.assertEqual(len(smtp), 1, "SMTP should have been attempted after the quota block")

    def test_quota_exhausted_does_not_call_resend(self):
        _, mocks, _ = _run(ses_result=False)
        mocks.resend.assert_not_awaited()

    def test_sandbox_ses_rejection_falls_through_to_smtp(self):
        """SES is a sandbox identity and rejects unverified recipients."""
        result, mocks, smtp = _run(ses_result=False)
        mocks.ses.assert_called_once()
        self.assertTrue(result)
        self.assertTrue(smtp)

    def test_hard_stop_restorable_via_env_opt_out(self):
        """EMAIL_FALLBACK_SES_ON_QUOTA=0 restores the old fail-fast behaviour."""
        result, _, smtp = _run(env={"EMAIL_FALLBACK_SES_ON_QUOTA": "0"}, ses_configured=False)
        self.assertFalse(result)
        self.assertEqual(smtp, [], "opt-out must not attempt SMTP")

    def test_degrading_on_quota_is_the_default(self):
        """Regression guard: this must never become opt-in again."""
        result, _, smtp = _run(ses_result=False)
        self.assertTrue(result)
        self.assertTrue(smtp)


class SesFallThroughTests(unittest.TestCase):
    def test_ses_failure_falls_through_to_smtp(self):
        """The SES branch used to `return` unconditionally, stranding mail."""
        result, mocks, smtp = _run(ses_result=False)
        mocks.ses.assert_called_once()
        self.assertTrue(smtp, "SMTP ladder must run after SES fails")

    def test_ses_success_short_circuits(self):
        result, _, smtp = _run(ses_result=True)
        self.assertTrue(result)
        self.assertEqual(smtp, [], "a successful SES send must not also send over SMTP")

    def test_resend_success_short_circuits_everything(self):
        result, mocks, smtp = _run(quota_blocked=False, resend_result=True, ses_result=False)
        self.assertTrue(result)
        mocks.resend.assert_awaited_once()
        mocks.ses.assert_not_called()
        self.assertEqual(smtp, [])

    def test_ses_exception_is_caught(self):
        """An SES client error must not abort the send."""

        def _boom(*a, **k):
            raise RuntimeError("Email address is not verified")

        cfg = dict(BASE_CFG)
        with mock.patch.dict(
            "os.environ", {"RESEND_API_KEY": "k", "EMAIL_TRANSPORT": "legacy"}, clear=True
        ), mock.patch.object(
            ds, "_resend_quota_blocked", return_value=True
        ), mock.patch.object(ds, "ses_configured", return_value=True), mock.patch.object(
            ds, "_send_via_ses", new=mock.MagicMock(side_effect=_boom)
        ), mock.patch.object(
            ds.aiosmtplib, "send", new=mock.AsyncMock(return_value=None)
        ):
            result = asyncio.run(ds.send_email(cfg, TO, "S", "B"))
        self.assertTrue(result)


class DegradedLadderBoundTests(unittest.TestCase):
    def test_degraded_send_does_not_walk_the_full_ladder(self):
        """Sends are awaited in a loop; a multi-minute send stalls the queue.

        SMTP is forced to fail so the ladder is actually walked, and the
        attempts are collected through a sink because the call raises. Without
        the bound this tries 587, 465 and 25 -- a single degraded email would burn
        ~45s of connect timeouts and serialise the whole outbound queue behind it.
        """
        attempts = []
        with self.assertRaises(Exception):
            _run(ses_result=False, smtp_fails=True, sink=attempts)
        ports = sorted({a.get("port") for a in attempts})
        self.assertLessEqual(len(attempts), 2, "degraded path must try at most two candidates")
        self.assertNotIn(25, ports, "port 25 must be dropped from the degraded ladder")

    def test_degraded_send_skips_ipv4_sweep(self):
        """The IPv4 retry sweep doubles the cost of an already-degraded send."""
        with mock.patch.object(
            ds.asyncio,
            "getaddrinfo",
            new=mock.AsyncMock(side_effect=AssertionError("must not resolve")),
            create=True,
        ) as gai:
            result, _, _ = _run(ses_result=False)
        self.assertTrue(result)
        gai.assert_not_called()

    def test_degraded_send_uses_shorter_connect_timeout(self):
        with self.assertRaises(Exception):
            _run(env={"SMTP_FALLBACK_CONNECT_TIMEOUT": "7"}, ses_result=False, smtp_fails=True)
        _, _, smtp = _run(env={"SMTP_FALLBACK_CONNECT_TIMEOUT": "7"}, ses_result=False)
        self.assertTrue(smtp)
        self.assertEqual(smtp[0].get("timeout"), 7)


class GmailTransportTests(unittest.TestCase):
    """The default transport is now Gmail only, since SES/Resend/Zoho are retired."""

    def _call(self, gmail_ok):
        cfg = dict(BASE_CFG)
        gmail = mock.AsyncMock(return_value=gmail_ok)
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            ds, "_send_via_gmail", gmail
        ), mock.patch.object(ds, "_send_via_resend_api", mock.AsyncMock()) as resend, \
             mock.patch.object(ds, "_send_via_ses", mock.MagicMock()) as ses, \
             mock.patch.object(ds.aiosmtplib, "send", mock.AsyncMock()) as smtp:
            result = asyncio.run(ds.send_email(cfg, TO, "Subject", "Body"))
        return result, gmail, resend, ses, smtp

    def test_gmail_is_the_default_transport(self):
        result, gmail, resend, ses, smtp = self._call(True)
        self.assertTrue(result)
        gmail.assert_awaited_once()
        resend.assert_not_awaited()
        ses.assert_not_called()
        smtp.assert_not_awaited()

    def test_no_fallback_when_gmail_fails(self):
        """No retired provider may quietly take over and throttle us again."""
        result, gmail, resend, ses, smtp = self._call(False)
        self.assertFalse(result)
        gmail.assert_awaited_once()
        resend.assert_not_awaited()
        ses.assert_not_called()
        smtp.assert_not_awaited()

    def test_legacy_ladder_still_reachable_explicitly(self):
        cfg = dict(BASE_CFG)
        with mock.patch.dict(
            "os.environ",
            {"EMAIL_TRANSPORT": "legacy", "RESEND_API_KEY": "k"},
            clear=True,
        ), mock.patch.object(ds, "_send_via_gmail", mock.AsyncMock()) as gmail, \
             mock.patch.object(ds, "_send_via_resend_api", mock.AsyncMock(return_value=True)) as resend, \
             mock.patch.object(ds, "_send_via_ses", mock.MagicMock()) as ses, \
             mock.patch.object(ds.aiosmtplib, "send", mock.AsyncMock()):
            result = asyncio.run(ds.send_email(cfg, TO, "Subject", "Body"))
        self.assertTrue(result)
        gmail.assert_not_awaited()
        resend.assert_awaited_once()


class TotalFailureTests(unittest.TestCase):
    def test_all_providers_failing_raises(self):
        """A genuine total failure must still surface, not be silently swallowed."""
        with self.assertRaises(Exception):
            _run(ses_result=False, smtp_fails=True)

    def test_smtp_success_when_ses_unconfigured(self):
        """No AWS creds: SMTP is the only path and it must work."""
        result, mocks, smtp = _run(ses_configured=False, ses_result=False)
        mocks.ses.assert_not_called()
        self.assertTrue(result)
        self.assertEqual(len(smtp), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
