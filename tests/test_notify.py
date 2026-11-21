"""Email report: content, trigger, multiple recipients, never crashes the bot (no network)."""
import smtplib

import promoscout.notify as notify
from promoscout.settings import Settings
from promoscout.store import Store

B = "https://www.jumia.com.ng"


def _db(tmp_path, **kw):
    s = Settings(data_dir=tmp_path, email_enabled=True, smtp_user="bot@gmail.com", smtp_password="app-pass",
                 email_to="a@gmail.com, b@gmail.com;a@gmail.com", review_url="http://1.2.3.4:8501", **kw)
    s.ensure_dirs()
    st = Store(s.db_path)
    rid = st.start_run()
    for sku, pct, price, old in (("HOT1", 97.5, 1000, 40000), ("MID1", 85.0, 3000, 20000)):
        st.upsert_product(sku=sku, qualifies=True, discount_pct=pct, url=f"{B}/{sku.lower()}-1.html", run_id=rid,
                          title=f"Item {sku}", price=price, old_price=old, currency="NGN", source_page="x",
                          flags=["old_price_unverified"])
    return s, st, rid


class FakeSMTP:
    sent = []

    def __init__(self, host, port, **kw):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        assert (user, pw) == ("bot@gmail.com", "app-pass")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_recipients_dedup_and_default():
    assert notify.recipients(Settings(email_to="a@x.com, b@x.com;A@x.com", smtp_user="u@x.com")) == ["a@x.com", "b@x.com"]
    assert notify.recipients(Settings(smtp_user="u@x.com")) == ["u@x.com"]


def test_no_email_without_new_hot_find(tmp_path):
    s, st, rid = _db(tmp_path)
    assert notify.build_report(s, rid, st) is None          # nothing alerted this run
    st.close()


def test_email_with_hot_find(tmp_path, monkeypatch):
    s, st, rid = _db(tmp_path)
    st.mark_alerted("HOT1")
    st.close()
    FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    assert notify.notify_run(s, rid) is True
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "a@gmail.com, b@gmail.com"
    assert "1 hot find" in msg["Subject"] and "97.5%" in msg["Subject"]
    text = msg.get_body(("plain",)).get_content()
    assert "http://1.2.3.4:8501" in text and f"{B}/hot1-1.html" in text and f"{B}/mid1-1.html" in text
    html = msg.get_body(("html",)).get_content()
    assert "<a href='http://1.2.3.4:8501'>" in html


def test_email_failure_never_raises(tmp_path, monkeypatch):
    s, st, rid = _db(tmp_path)
    st.mark_alerted("HOT1")
    st.close()

    def boom(*a, **k):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(smtplib, "SMTP_SSL", boom)
    assert notify.notify_run(s, rid) is False


def test_disabled_sends_nothing(tmp_path, monkeypatch):
    s, st, rid = _db(tmp_path)
    st.mark_alerted("HOT1")
    st.close()
    s2 = s.model_copy(update={"email_enabled": False})
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    assert notify.notify_run(s2, rid) is False
