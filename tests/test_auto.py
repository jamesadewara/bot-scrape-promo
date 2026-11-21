"""Auto mode: batch scheduling, cooldown, daily cap, lock (no network)."""
import argparse
import time

import promoscout.cli as cli
from promoscout.crawler import RunSummary
from promoscout.settings import Settings
from promoscout.store import Store


def settings(tmp_path, **kw):
    s = Settings(data_dir=tmp_path, **kw)
    s.ensure_dirs()
    Store(s.db_path).close()
    return s


def _finished_run(s, minutes, pages=10, outcome="ok", attempt=1):
    st = Store(s.db_path)
    rid = st.start_run()
    now = time.time()
    st.db.execute("UPDATE runs SET started_at=?, ended_at=?, pages_fetched=?, state='done' WHERE id=?",
                  (now - minutes * 60, now, pages, rid))
    st.log_request(rid, "https://www.jumia.com.ng/tablets/", "listing", 200, outcome, "allow", 1, 0.1, attempt)
    st.set_kv("checkpoint", '{"entries": [], "visited": {}}')
    st.close()
    return now


def test_first_batch_now(tmp_path):
    s = settings(tmp_path)
    now = time.time()
    start, why = cli.next_batch_at(s, now)
    assert start == now and why == "first batch"


def test_rest_matches_work(tmp_path):
    s = settings(tmp_path)
    end = _finished_run(s, 25)
    start, why = cli.next_batch_at(s, end)
    assert abs(start - (end + 25 * 60)) < 2 and why.startswith("rest 25 min")


def test_rest_floor_and_ceiling(tmp_path):
    s = settings(tmp_path)
    end = _finished_run(s, 1)
    assert abs(cli.next_batch_at(s, end)[0] - (end + 3 * 60)) < 2           # min 3 min
    s2 = settings(tmp_path / "b")
    end2 = _finished_run(s2, 300)
    assert abs(cli.next_batch_at(s2, end2)[0] - (end2 + 60 * 60)) < 2       # max 60 min


def test_pushback_rests_maximum(tmp_path):
    s = settings(tmp_path)
    end = _finished_run(s, 2, outcome="pushback")
    start, why = cli.next_batch_at(s, end)
    assert abs(start - (end + 60 * 60)) < 2 and "pushed back" in why


def test_network_blip_with_real_work_is_normal_rest(tmp_path):
    s = settings(tmp_path)
    end = _finished_run(s, 10, outcome="network_error")
    start, why = cli.next_batch_at(s, end)
    assert abs(start - (end + 10 * 60)) < 2


def test_failed_batch_without_work_retries_soon(tmp_path):
    s = settings(tmp_path)
    end = _finished_run(s, 0.3, pages=0)
    st = Store(s.db_path)
    st.db.execute("UPDATE runs SET state='stopped (error)'")
    st.close()
    start, why = cli.next_batch_at(s, end, failures=1)
    assert abs(start - (end + 2 * 60)) < 2 and why.startswith("retry")
    start3, _ = cli.next_batch_at(s, end, failures=3)
    assert abs(start3 - (end + 8 * 60)) < 2                                  # 2, 4, 8 ... capped at 60


def test_nothing_due_rests_min(tmp_path):
    s = settings(tmp_path)
    end = _finished_run(s, 0.1, pages=0)
    start, why = cli.next_batch_at(s, end)
    assert abs(start - (end + 3 * 60)) < 2 and "nothing" in why


def test_daily_cap_waits_for_midnight(tmp_path):
    s = settings(tmp_path, auto_max_runs_per_day=1)
    _finished_run(s, 1)
    start, why = cli.next_batch_at(s)
    assert "auto_max_runs_per_day" in why and time.localtime(start).tm_hour == 0


def test_lock_reported(tmp_path):
    s = settings(tmp_path)
    st = Store(s.db_path)
    st.lock(24, "HTTP 403")
    st.close()
    start, why = cli.next_batch_at(s)
    assert why.startswith("locked") and start > time.time() + 23 * 3600


def _args():
    return argparse.Namespace(seed=None, no_sitemaps=False, resume=False, quiet=True)


async def test_auto_stops_on_block(tmp_path, monkeypatch):
    s = settings(tmp_path)
    calls = []

    async def fake_run(args, settings_, resume):
        calls.append(resume)
        return RunSummary(len(calls), "stopped (block)", "HTTP 403", 3, 4, 0)

    monkeypatch.setattr(cli, "_one_run", fake_run)
    assert await cli._auto(_args(), s) == 1 and len(calls) == 1


async def test_auto_runs_batches_with_cooldown(tmp_path, monkeypatch):
    s = settings(tmp_path, auto_min_cooldown_minutes=0.0001, auto_max_cooldown_minutes=0.0002,
                 auto_max_batches=3)
    calls = []

    async def fake_run(args, settings_, resume):
        calls.append(resume)
        st = Store(settings_.db_path)
        st.start_run()
        st.set_kv("checkpoint", '{"entries": [], "visited": {}}')
        st.close()
        state = "stopped (network)" if len(calls) == 1 else "done"
        return RunSummary(len(calls), state, "x", 1, 1, 0)

    monkeypatch.setattr(cli, "_one_run", fake_run)
    assert await cli._auto(_args(), s) == 0
    assert calls == [False, True, False]     # a network-stopped batch is resumed after the cooldown


async def test_auto_refuses_when_locked(tmp_path, monkeypatch):
    s = settings(tmp_path)
    st = Store(s.db_path)
    st.lock(24, "captcha")
    st.close()

    async def fake_run(*a):
        raise AssertionError("must not run while locked")

    monkeypatch.setattr(cli, "_one_run", fake_run)
    assert await cli._auto(_args(), s) == 1


async def test_auto_starts_even_after_two_manual_runs_today(tmp_path, monkeypatch):
    s = settings(tmp_path, auto_max_batches=1)          # PRD manual cap stays 2
    _finished_run(s, 1)
    _finished_run(s, 1)
    seen = []

    async def fake_run(args, settings_, resume):
        seen.append(settings_.max_runs_per_day)
        return RunSummary(3, "done", "page cap", 5, 6, 0)

    monkeypatch.setattr(cli, "_one_run", fake_run)
    t0 = time.time()
    assert await cli._auto(_args(), s) == 0
    assert seen == [24] and time.time() - t0 < 5        # started immediately with the auto cap


async def test_auto_resume_starts_now_even_if_rest_pending(tmp_path, monkeypatch):
    s = settings(tmp_path, auto_max_batches=1)
    _finished_run(s, 50)                                  # would normally rest 50 min
    calls = []

    async def fake_run(args, settings_, resume):
        calls.append(resume)
        return RunSummary(9, "done", "page cap", 5, 6, 0)

    monkeypatch.setattr(cli, "_one_run", fake_run)
    t0 = time.time()
    args = _args()
    args.resume = True
    assert await cli._auto(args, s) == 0
    assert calls == [True] and time.time() - t0 < 5


def test_align_to_hour(tmp_path):
    s = settings(tmp_path, auto_align_to_hour=True, auto_align_minute=1)
    end = _finished_run(s, 5)
    start, why = cli.next_batch_at(s, end)
    lt = time.localtime(start)
    assert lt.tm_min == 1 and lt.tm_sec == 0 and start >= end + 5 * 60 and "aligned" in why
