"""Review requests: one ask per finished customer, never a second."""

import review_requests


def test_review_url_falls_back_and_reports_it(monkeypatch):
    monkeypatch.delenv("GOOGLE_REVIEW_URL", raising=False)
    url, configured = review_requests.review_url()
    assert url.startswith("https://")
    assert configured is False

    monkeypatch.setenv("GOOGLE_REVIEW_URL", "https://g.page/r/abc/review")
    url, configured = review_requests.review_url()
    assert url == "https://g.page/r/abc/review"
    assert configured is True


def test_email_uses_first_name_and_the_review_link(monkeypatch):
    monkeypatch.setenv("GOOGLE_REVIEW_URL", "https://g.page/r/abc/review")
    subject, body = review_requests.email_body(
        {"name": "Ian Scott Hill", "project_type": "Whole-Home Renovation"})
    assert "Ian" in subject
    assert "Whole-Home Renovation" in body
    assert "https://g.page/r/abc/review" in body
    assert "(757) 846-9275" in body


def test_sms_is_short_and_carries_opt_out(monkeypatch):
    monkeypatch.setenv("GOOGLE_REVIEW_URL", "https://g.page/r/abc/review")
    body = review_requests.sms_body({"name": "Ian Hill"})
    assert "STOP" in body
    assert "https://g.page/r/abc/review" in body
    assert len(body) < 320


class _Cursor:
    def execute(self, *a, **k):
        return None

    def fetchall(self):
        return [{"id": 7, "name": "Ian Hill", "email": "ian@example.com",
                 "phone": "+19016862455", "project_type": "Renovation",
                 "status": "completed"}]

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False


class _Conn:
    def cursor(self):
        return _Cursor()

    def commit(self):
        return None


def test_one_send_is_recorded_and_no_second_channel(monkeypatch):
    monkeypatch.setattr(review_requests, "ensure_schema", lambda conn: None)
    sent = []
    recorded = []
    monkeypatch.setattr(review_requests, "_record",
                        lambda conn, lead_id, ch, to, msg: recorded.append((lead_id, ch)))

    def _email(to, subject, body):
        sent.append(("email", to))
        return True

    def _sms(to, body):
        sent.append(("sms", to))
        return True

    summary = review_requests.run_once(
        _Conn(), send_email=_email, send_sms=_sms,
        channel_allowed=lambda ch: (True, ""))

    assert summary["sent"] == 1
    assert sent == [("email", "ian@example.com")]     # email wins; no SMS on top
    assert recorded == [(7, "email")]
