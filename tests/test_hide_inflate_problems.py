"""Auto-hide + memo, fake-discount (inflated old price) flag, problem emails. No network."""
import smtplib
import time

import promoscout.cli as cli
import promoscout.notify as notify
from promoscout.crawler import RunSummary
from promoscout.extract import Candidate
from promoscout.scoring import inflated_old_price
from promoscout.settings import Settings
from promoscout.store import Store

B = "https://www.jumia.com.ng"


def _store(tmp_path, **kw):
    s = Settings(data_dir=tmp_path, **kw)
    s.ensure_dirs()
    return s, Store(s.db_path)


def _add(st, sku, pct, first_seen_h_ago=0, last_seen_h_ago=0, price=1000, old=10000):
    rid = st.start_run()
    st.upsert_product(sku=sku, qualifies=True, discount_pct=pct, url=f"{B}/{sku}-1.html", run_id=rid,
                      title=sku, price=price, old_price=old, currency="NGN", source_page="x", flags=[])
    now = time.time()
    st.db.execute("UPDATE products SET first_seen=?, last_seen=? WHERE sku=?",
                  (now - first_seen_h_ago * 3600, now - last_seen_h_ago * 3600, sku))


# ------------------------------------------------------------------ auto-hide
def test_auto_hide_rules(tmp_path):
    s, st = _store(tmp_path)
    _add(st, "old_low", 84, first_seen_h_ago=50)          # under 90, older than 48 h -> hidden
    _add(st, "old_high", 93, first_seen_h_ago=50)         # 90%+ stays
    _add(st, "new_low", 84, first_seen_h_ago=2)           # still fresh -> stays
    _add(st, "stale_high", 97, first_seen_h_ago=80, last_seen_h_ago=80)   # not seen 72 h -> hidden
    _add(st, "reviewed", 84, first_seen_h_ago=50)
    st.set_review("reviewed", "approved")                 # reviewed items are never auto-hidden
    assert st.auto_hide(48, 90, 72) == 2
    status = {r["sku"]: r["review_status"] for r in st.db.execute("SELECT sku, review_status FROM products")}
    assert status == {"old_low": "hidden", "old_high": "pending", "new_low": "pending",
                      "stale_high": "hidden", "reviewed": "approved"}


def test_hidden_returns_only_if_price_changes(tmp_path):
    s, st = _store(tmp_path)
    _add(st, "x", 84, first_seen_h_ago=50)
    st.auto_hide(48, 90, 72)
    rid = st.start_run()
    kw = dict(qualifies=True, discount_pct=84, url=f"{B}/x-1.html", run_id=rid, title="x", currency="NGN",
              source_page="x", flags=[])
    st.upsert_product(sku="x", price=1000, old_price=10000, **kw)          # same price: stays hidden
    assert st.get_product("x")["review_status"] == "hidden"
    st.upsert_product(sku="x", price=800, old_price=10000, **kw)           # cheaper now: back in queue
    assert st.get_product("x")["review_status"] == "pending"


# ------------------------------------------------------------------ inflated old price
def _c(sku, title, price, old=None):
    return Candidate(sku=sku, title=title, price=price, old_price=old, url=None, source="card")


def test_inflated_old_price_flag():
    nodizz = _c("N", "NODIZZ Npad 702 Kids Tablet 7 Inch", 69984, 385920)
    peers = [_c("a", "Kids Tablet 7 Inch Android", 65000), _c("b", "Modio Kids Tablet 7 Inch", 72000),
             _c("c", "Atouch Kids Tablet 7 Inch Wifi", 80000), _c("d", "Office chair", 20000)]
    assert inflated_old_price(nodizz, peers + [nodizz], 3.0)        # 385,920 > 3 x 72,000
    honest = _c("H", "Smart Kids Tablet 7 Inch", 30000, 150000)
    assert not inflated_old_price(honest, peers, 3.0)                # 150,000 < 3 x 72,000
    assert not inflated_old_price(nodizz, peers[:2], 3.0)            # fewer than 3 similar: no verdict
    assert not inflated_old_price(nodizz, peers, 0)                  # disabled


# ------------------------------------------------------------------ problem emails
class FakeSMTP:
    sent = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *a):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def _mail_settings(tmp_path, **kw):
    s = Settings(data_dir=tmp_path, email_enabled=True, smtp_user="bot@gmail.com", smtp_password="p",
                 email_to="a@gmail.com,b@gmail.com", **kw)
    s.ensure_dirs()
    Store(s.db_path).close()
    return s


def test_problem_email_rate_limited_then_recovered(tmp_path, monkeypatch):
    s = _mail_settings(tmp_path)
    FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    assert notify.notify_problem(s, "unreachable", "Jumia unreachable", "timeout")
    assert not notify.notify_problem(s, "unreachable", "Jumia unreachable", "timeout")   # within 6 h
    assert notify.notify_problem(s, "block", "blocked", "403", force=True)               # blocks always
    assert FakeSMTP.sent[0]["To"] == "a@gmail.com, b@gmail.com"
    assert notify.notify_recovered(s, "20 pages")
    assert not notify.notify_recovered(s, "20 pages")                                    # only once
    assert [m["Subject"] for m in FakeSMTP.sent] == [
        "PromoScout problem: Jumia unreachable", "PromoScout problem: blocked", "PromoScout: working again"]


def test_block_batch_emails_everyone(tmp_path, monkeypatch):
    s = _mail_settings(tmp_path)
    FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    st = Store(s.db_path)
    st.lock(24, "HTTP 403")
    st.close()
    cli._problem_emails(s, RunSummary(1, "stopped (block)", "HTTP 403", 3, 4, 0))
    assert len(FakeSMTP.sent) == 1 and "block" in FakeSMTP.sent[0]["Subject"].lower()
    assert "Locked until" in FakeSMTP.sent[0].get_content()


async def test_auto_emails_after_three_failed_batches(tmp_path, monkeypatch):
    s = _mail_settings(tmp_path, auto_retry_minutes=0.0001, auto_max_cooldown_minutes=0.0002, auto_max_batches=4)
    FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)

    async def fake_run(args, settings_, resume):
        st = Store(settings_.db_path)
        rid = st.start_run()
        st.end_run(rid, "stopped (error)", "ConnectionTimeoutError", pages=0)
        st.close()
        return RunSummary(rid, "stopped (error)", "ConnectionTimeoutError", 0, 1, 0)

    monkeypatch.setattr(cli, "_one_run", fake_run)
    import argparse
    await cli._auto(argparse.Namespace(seed=None, no_sitemaps=False, resume=False, quiet=True), s)
    subjects = [m["Subject"] for m in FakeSMTP.sent]
    assert subjects == ["PromoScout problem: Jumia unreachable: 3 batches failed in a row"]


async def test_auto_crash_is_emailed(tmp_path, monkeypatch):
    s = _mail_settings(tmp_path)
    FakeSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)

    async def boom(*a):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cli, "_one_run", boom)
    import argparse

    import pytest
    with pytest.raises(RuntimeError):
        await cli._auto(argparse.Namespace(seed=None, no_sitemaps=False, resume=False, quiet=True), s)
    assert "crashed" in FakeSMTP.sent[0]["Subject"] and "disk full" in FakeSMTP.sent[0].get_content()
