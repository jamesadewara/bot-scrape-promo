"""Command line: run a crawl, show status, export approved products (F1, F13)."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import signal
import sys
import time

import structlog

from .crawler import Crawler
from .fetcher import Fetcher
from .notify import build_report, configured, notify_problem, notify_recovered, notify_run, send
from .scoring import review_sort_key
from .settings import Settings
from .store import Store


def configure_logging(settings: Settings, to_console: bool) -> None:
    """Every request is logged (time, URL, status, robots decision) as JSON lines for audit."""
    settings.ensure_dirs()
    handlers: list[logging.Handler] = [logging.FileHandler(settings.log_path, encoding="utf-8")]
    if to_console:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=handlers, force=True)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def _one_run(args: argparse.Namespace, settings: Settings, resume: bool):
    store = Store(settings.db_path)
    try:
        async with Fetcher(settings) as fetcher:
            crawler = Crawler(settings, store, fetcher, live=not args.quiet)
            summary = await crawler.run(seed=args.seed, use_sitemaps=not args.no_sitemaps, resume=resume)
    finally:
        store.close()
    # one email per batch with new hot finds (off unless configured); runs off the event loop
    await asyncio.to_thread(notify_run, settings, summary.run_id)
    await asyncio.to_thread(_problem_emails, settings, summary)
    return summary


def _problem_emails(settings: Settings, summary) -> None:
    """Email every recipient when a batch hits a problem; and once more when it works again."""
    if summary.state == "stopped (block)":
        store = Store(settings.db_path)
        try:
            until = store.lock_until()
        finally:
            store.close()
        notify_problem(settings, "block", "Jumia block signal: bot stopped and locked",
                       f"Reason: {summary.reason}\nLocked until: {time.ctime(until)}\n"
                       "Nothing is fetched while locked. Auto mode has stopped; restart it after the lock "
                       "ends (python -m promoscout auto, or docker compose restart bot).", force=True)
    elif summary.state == "stopped (errors)":
        notify_problem(settings, "errors", "Batch stopped: too many errors",
                       f"Reason: {summary.reason}\nPages: {summary.pages}. Auto mode keeps retrying.")
    elif summary.state in ("done",) and summary.pages > 0:
        notify_recovered(settings, f"Last batch: {summary.pages} pages, {summary.new_qualifying} new 80%+ finds.")


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    summary = await _one_run(args, settings, args.resume)
    print(f"\nRun {summary.run_id}: {summary.state} - {summary.reason}")
    print(f"Pages {summary.pages}, requests {summary.requests}, new qualifying {summary.new_qualifying}")
    print(f"Status file: {settings.status_path}\nRequest log: {settings.log_path}")
    return 0 if summary.state in ("done",) else (2 if summary.state == "refused" else 1)


PUSHBACK_OUTCOMES = ("pushback", "server_error")
FAILED_STATES = ("stopped (error)", "stopped (network)", "stopped (errors)")


def _midnight_after(now: float) -> float:
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 5, 0, 0, -1))


def _next_hour_mark(t: float, minute: int) -> float:
    """First HH:<minute>:00 at or after t."""
    lt = time.localtime(t)
    mark = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, minute, 0, 0, 0, -1))
    return mark if mark >= t else mark + 3600


def rest_after(settings: Settings, failures: int = 0, now: float | None = None) -> tuple[float, str]:
    """How long auto mode rests after the last batch, and why.

    The rest pays back the load of the work actually done:
      - real work: rest = batch duration x auto_rest_factor, between min and max cooldown
      - the server pushed back (429/503/5xx) in that batch: rest the max cooldown
      - the batch did no work because it failed (network, robots.txt unreachable): retry soon,
        auto_retry_minutes, doubling on each failure in a row (up to the max cooldown)
      - the batch found nothing to fetch: rest the min cooldown
    """
    now = now or time.time()
    store = Store(settings.db_path)
    try:
        lock = store.lock_until()
        if lock > now:
            return lock, "locked after a block signal"
        lo, hi = settings.auto_min_cooldown_minutes * 60, settings.auto_max_cooldown_minutes * 60
        last = store.db.execute("SELECT id, started_at, ended_at, state, pages_fetched FROM runs "
                                "WHERE ended_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        if last is None:
            return now, "first batch"
        worked = max(0.0, last["ended_at"] - last["started_at"])
        pushback = store.db.execute(
            "SELECT COUNT(*) FROM requests WHERE run_id=? AND outcome IN (?, ?)",
            (last["id"], *PUSHBACK_OUTCOMES)).fetchone()[0]
        if pushback:
            rest, reason = hi, f"rest {hi / 60:.0f} min: server pushed back {pushback}x in the last batch"
        elif not last["pages_fetched"] and last["state"] in FAILED_STATES:
            rest = min(hi, settings.auto_retry_minutes * 60 * (2 ** max(0, failures - 1)))
            reason = f"retry in {rest / 60:.0f} min: last batch failed ({last['state']})"
        elif not last["pages_fetched"]:
            rest, reason = lo, f"rest {lo / 60:.0f} min: nothing was due"
        else:
            rest = min(hi, max(lo, worked * settings.auto_rest_factor))
            reason = f"rest {rest / 60:.0f} min after a {worked / 60:.0f} min batch ({last['pages_fetched']} pages)"
        start = last["ended_at"] + rest
        if settings.auto_align_to_hour:
            aligned = _next_hour_mark(max(start, now), settings.auto_align_minute)
            start, reason = aligned, reason + f"; aligned to :{settings.auto_align_minute:02d} past the hour"
        if store.runs_today() >= settings.auto_max_runs_per_day:
            start = max(start, _midnight_after(now))
            reason = f"auto_max_runs_per_day ({settings.auto_max_runs_per_day}) reached"
        return max(start, now), reason
    finally:
        store.close()


def next_batch_at(settings: Settings, now: float | None = None, failures: int = 0) -> tuple[float, str]:
    return rest_after(settings, failures, now)


def _write_cooldown_status(settings: Settings, until: float, reason: str, batch: int) -> None:
    data = {"state": "cooldown", "reason": reason, "doing": f"next batch at {time.ctime(until)}",
            "next_batch_at": until, "batches_done": batch, "updated_at": time.time()}
    try:
        old = json.loads(settings.status_path.read_text(encoding="utf-8"))
        old.update(data)
        data = old
    except (OSError, ValueError):
        pass
    settings.status_path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")


async def _auto(args: argparse.Namespace, settings: Settings) -> int:
    try:
        return await _auto_loop(args, settings)
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as exc:  # unexpected crash: tell the recipients, then fail loudly
        import traceback
        await asyncio.to_thread(
            notify_problem, settings, "crash", f"Auto mode crashed: {type(exc).__name__}",
            traceback.format_exc()[-3000:], True)
        raise


async def _auto_loop(args: argparse.Namespace, settings: Settings) -> int:
    """Batch, rest, batch... The first batch starts at once (only a lock or the daily cap delays it).
    Stops for good on a block signal."""
    batch = 0
    failures = 0
    resume = args.resume
    run_settings = settings.model_copy(update={"max_runs_per_day": settings.auto_max_runs_per_day})
    while True:
        start, why = next_batch_at(settings, failures=failures)
        if why.startswith("locked"):
            print(f"[auto] bot is locked until {time.ctime(start)} after a block signal; not starting.")
            await asyncio.to_thread(
                notify_problem, settings, "locked", "Auto mode cannot start: bot is locked",
                f"Locked until {time.ctime(start)} after a Jumia block signal.")
            return 1
        if batch == 0 and not why.startswith("auto_max_runs_per_day"):
            start, why = time.time(), "starting"  # you started auto mode: work now
        wait = start - time.time()
        if wait > 0:
            print(f"[auto] next batch at {time.ctime(start)} ({why})")
            _write_cooldown_status(settings, start, why, batch)
            while time.time() < start:
                await asyncio.sleep(min(30, max(1, start - time.time())))
        summary = await _one_run(args, run_settings, resume)
        batch += 1
        print(f"[auto] batch {batch}: run {summary.run_id} {summary.state} - {summary.reason}; "
              f"pages {summary.pages}, new 80%+ {summary.new_qualifying}")
        if summary.state == "stopped (block)":
            print("[auto] block signal: stopping auto mode. Check `python -m promoscout check` before restarting.")
            return 1
        failures = failures + 1 if (summary.pages == 0 and summary.state in FAILED_STATES) else 0
        if failures == 3:
            await asyncio.to_thread(
                notify_problem, settings, "unreachable", "Jumia unreachable: 3 batches failed in a row",
                f"Last error: {summary.reason}\nThe bot keeps retrying (2, 4, 8 ... up to "
                f"{settings.auto_max_cooldown_minutes:g} min apart). No action needed if it is a short outage.")
        resume = summary.state == "stopped (network)"  # continue an interrupted batch
        if settings.auto_max_batches and batch >= settings.auto_max_batches:
            print(f"[auto] {batch} batches done")
            return 0


def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    store = Store(settings.db_path)
    rows = [r for r in store.qualifying(settings.max_price_ngn, settings.max_old_price_ngn) if r["review_status"] == "approved"]
    rows.sort(key=review_sort_key)
    cols = ["sku", "title", "discount_pct", "price", "old_price", "currency", "url", "flags", "campaign_hint",
            "rating_avg", "rating_count", "seller_score", "seller_quality", "stock_text", "first_seen", "last_seen"]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = dict(r)
            r["flags"] = "|".join(r["flags"])
            for k in ("first_seen", "last_seen"):
                r[k] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r[k])) if r[k] else ""
            w.writerow(r)
    store.close()
    print(f"Exported {len(rows)} approved products to {args.out}")
    return 0


def cmd_check(args: argparse.Namespace, settings: Settings) -> int:
    """PRD 9 success metrics + compliance audit of the logged requests."""
    from .robots import Robots
    from .settings import RPM_CEILING, ROBOTS_URL

    if not settings.db_path.exists():
        print("No database yet: run `python -m promoscout run` first.")
        return 1
    store = Store(settings.db_path)
    db = store.db
    ok = True
    lock = store.lock_until()
    print("== Lock")
    if lock > time.time():
        print(f"LOCKED until {time.ctime(lock)}: {store.get_kv('lock_reason')}")
    else:
        print("not locked")
    print(f"runs today: {store.runs_today()} (manual run cap {settings.max_runs_per_day}, "
          f"auto cap {settings.auto_max_runs_per_day})")

    print("\n== Runs (latest 10)")
    for r in db.execute("SELECT id, started_at, ended_at, state, reason, pages_fetched, requests, bytes, "
                        "new_qualifying FROM runs ORDER BY id DESC LIMIT 10"):
        dur = (r["ended_at"] or time.time()) - r["started_at"]
        print(f"#{r['id']} {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['started_at']))} "
              f"{r['state']:<18} pages {r['pages_fetched']:>4} req {r['requests']:>4} "
              f"{(r['bytes'] or 0) / 1e6:6.1f} MB  {dur / 60:5.1f} min  new80+ {r['new_qualifying']:>3}  {r['reason']}")

    print("\n== Compliance (PRD 9)")
    robots_txt = store.get_kv("robots_txt")
    if robots_txt:
        robots = Robots(robots_txt)
        bad = [u for (u,) in db.execute("SELECT DISTINCT url FROM requests")
               if u != ROBOTS_URL and not robots.allowed(u)]
        print(f"requests to disallowed URLs: {len(bad)}" + (f"  e.g. {bad[:3]}" if bad else ""))
        ok &= not bad
    ts_by_run: dict[int, list[float]] = {}
    for run_id, ts in db.execute("SELECT run_id, ts FROM requests ORDER BY ts"):
        ts_by_run.setdefault(run_id, []).append(ts)
    worst = 0
    for ts in ts_by_run.values():
        j = 0
        for i, t in enumerate(ts):
            while ts[j] <= t - 60:
                j += 1
            worst = max(worst, i - j + 1)
    print(f"max requests in any 60 s window: {worst} (setting {settings.max_rpm}, ceiling {RPM_CEILING})")
    ok &= worst <= RPM_CEILING
    blocks = db.execute("SELECT COUNT(*) FROM requests WHERE outcome='block'").fetchone()[0]
    stopped = db.execute("SELECT COUNT(*) FROM runs WHERE state='stopped (block)'").fetchone()[0]
    print(f"block signals seen: {blocks}; runs stopped by kill switch: {stopped} (every block must stop a run)")
    ok &= stopped >= min(blocks, 1) if blocks else True
    retries = db.execute("SELECT COUNT(*) FROM requests WHERE attempt > 1").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    print(f"retries: {retries} of {total} requests ({100 * retries / total if total else 0:.1f}%)")

    print("\n== Results")
    shown = store.qualifying(settings.max_price_ngn, settings.max_old_price_ngn)
    low = min((r["discount_pct"] for r in shown), default=None)
    print(f"qualifying products shown: {len(shown)} (lowest discount {low}; must be >= 79.5)")
    print(f"price filter: price < {settings.max_price_ngn:,.0f} NGN or old price < {settings.max_old_price_ngn:,.0f} NGN")
    ok &= low is None or low >= 79.5
    for st_, n in db.execute("SELECT review_status, COUNT(*) FROM products WHERE qualifies=1 GROUP BY 1"):
        print(f"  {st_}: {n}")
    hot = [r for r in shown if (r["discount_pct"] or 0) >= settings.alert_min_discount]
    print(f"hot finds ({settings.alert_min_discount:g}%+): {len(hot)}")
    for r in sorted(hot, key=review_sort_key)[:10]:
        print(f"  HOT {r['discount_pct']:.1f}%  {r['review_status']:<8} {(r['title'] or r['sku'])[:50]}  {r['url']}")
    top = [r["review_status"] for r in sorted(store.qualifying(settings.max_price_ngn, settings.max_old_price_ngn), key=review_sort_key)[:20]]
    reviewed = [s for s in top if s in ("approved", "rejected")]
    if reviewed:
        print(f"reviewer precision (top 20): {reviewed.count('approved')}/{len(reviewed)} approved")

    print("\n== Yield per page type (new 80%+ per 100 pages, all runs)")
    pages: dict[str, float] = {}
    hits: dict[str, float] = {}
    for (stats,) in db.execute("SELECT stats FROM runs WHERE stats IS NOT NULL"):
        d = json.loads(stats or "{}")
        for k, n in (d.get("pages_by_type") or {}).items():
            pages[k] = pages.get(k, 0) + n
            hits[k] = hits.get(k, 0) + (d.get("hits_by_type") or {}).get(k, 0)
    for k in sorted(pages):
        print(f"  {k:<9} pages {pages[k]:>6.0f}  hits {hits[k]:>4.0f}  yield {100 * hits[k] / pages[k]:.1f}/100")
    pq = db.execute("SELECT COUNT(*), SUM(CASE WHEN next_check_at > ? THEN 1 ELSE 0 END) FROM pages",
                    (time.time(),)).fetchone()
    print(f"\npages remembered: {pq[0]} (not yet due: {pq[1] or 0})")
    cp = store.get_kv("checkpoint")
    print(f"URLs waiting for the next run: {len(json.loads(cp)['entries']) if cp else 0}")
    print("\nOVERALL:", "OK" if ok else "PROBLEM - see above")
    store.close()
    return 0 if ok else 1


def cmd_test_email(args: argparse.Namespace, settings: Settings) -> int:
    if not (settings.smtp_user and settings.smtp_password):
        print("Set PROMOSCOUT_SMTP_USER and PROMOSCOUT_SMTP_PASSWORD (Gmail app password) in .env")
        return 1
    if not configured(settings):
        print("Note: PROMOSCOUT_EMAIL_ENABLED is not true; batches will not email until it is.")
    store = Store(settings.db_path)
    try:
        last = store.db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        msg = build_report(settings, last, store, test=True)
    finally:
        store.close()
    try:
        send(settings, msg)
    except Exception as exc:  # noqa: BLE001
        print(f"Email failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"Test email sent to {msg['To']}")
    return 0


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.status_path.exists():
        print("No status file yet.")
        return 1
    print(json.dumps(json.loads(settings.status_path.read_text(encoding="utf-8")), indent=1))
    return 0


def _sigterm(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="promoscout", description="Jumia deep-discount scout (80%+).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start a crawl run")
    r.add_argument("--seed", help="seed URL on www.jumia.com.ng (F1)")
    r.add_argument("--no-sitemaps", action="store_true", help="do not seed from the robots.txt sitemaps")
    r.add_argument("--resume", action="store_true", help="resume from the last checkpoint (F11)")
    r.add_argument("--quiet", action="store_true", help="no live terminal panel")
    e = sub.add_parser("export", help="export approved products to CSV (F13)")
    e.add_argument("--out", default="approved.csv")
    a = sub.add_parser("auto", help="run batches unattended with a cooldown in between")
    a.add_argument("--seed", help="seed URL on www.jumia.com.ng (F1)")
    a.add_argument("--no-sitemaps", action="store_true")
    a.add_argument("--resume", action="store_true", help="start by resuming the last checkpoint")
    a.add_argument("--quiet", action="store_true", help="no live terminal panel")
    sub.add_parser("status", help="print the last status.json")
    sub.add_parser("test-email", help="send a test email with the current best finds")
    sub.add_parser("check", help="is it working? runs, compliance audit, results, yield per page type")
    args = ap.parse_args(argv)
    # `docker stop` / systemd send SIGTERM: treat it like Ctrl+C so the checkpoint is saved
    signal.signal(signal.SIGTERM, _sigterm)
    settings = Settings()
    settings.ensure_dirs()
    if args.cmd == "run":
        configure_logging(settings, to_console=False)
        try:
            return asyncio.run(_run(args, settings))
        except KeyboardInterrupt:
            print("\nStopped. Checkpoint saved; continue with: python -m promoscout run --resume")
            return 130
    if args.cmd == "auto":
        configure_logging(settings, to_console=False)
        try:
            return asyncio.run(_auto(args, settings))
        except KeyboardInterrupt:
            print("\nAuto mode stopped. Restart with: python -m promoscout auto --resume")
            return 130
    if args.cmd == "export":
        return cmd_export(args, settings)
    if args.cmd == "check":
        return cmd_check(args, settings)
    if args.cmd == "test-email":
        return cmd_test_email(args, settings)
    return cmd_status(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
