"""Command line: run a crawl, show status, export approved products (F1, F13)."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time

import structlog

from .crawler import Crawler
from .fetcher import Fetcher
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


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    store = Store(settings.db_path)
    try:
        async with Fetcher(settings) as fetcher:
            crawler = Crawler(settings, store, fetcher, live=not args.quiet)
            summary = await crawler.run(seed=args.seed, use_sitemaps=not args.no_sitemaps, resume=args.resume)
    finally:
        store.close()
    print(f"\nRun {summary.run_id}: {summary.state} - {summary.reason}")
    print(f"Pages {summary.pages}, requests {summary.requests}, new qualifying {summary.new_qualifying}")
    print(f"Status file: {settings.status_path}\nRequest log: {settings.log_path}")
    return 0 if summary.state in ("done",) else (2 if summary.state == "refused" else 1)


def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    store = Store(settings.db_path)
    rows = [r for r in store.qualifying() if r["review_status"] == "approved"]
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
    print(f"runs today: {store.runs_today()} / {settings.max_runs_per_day}")

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
    acc = db.execute("SELECT COUNT(*), MIN(discount_pct) FROM products WHERE qualifies=1").fetchone()
    print(f"qualifying products: {acc[0]} (lowest shown discount {acc[1]}; must be >= 79.5)")
    ok &= acc[1] is None or acc[1] >= 79.5
    for st_, n in db.execute("SELECT review_status, COUNT(*) FROM products WHERE qualifies=1 GROUP BY 1"):
        print(f"  {st_}: {n}")
    top = [r["review_status"] for r in sorted(store.qualifying(), key=review_sort_key)[:20]]
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


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.status_path.exists():
        print("No status file yet.")
        return 1
    print(json.dumps(json.loads(settings.status_path.read_text(encoding="utf-8")), indent=1))
    return 0


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
    sub.add_parser("status", help="print the last status.json")
    sub.add_parser("check", help="is it working? runs, compliance audit, results, yield per page type")
    args = ap.parse_args(argv)
    settings = Settings()
    settings.ensure_dirs()
    if args.cmd == "run":
        configure_logging(settings, to_console=False)
        try:
            return asyncio.run(_run(args, settings))
        except KeyboardInterrupt:
            print("\nStopped. Checkpoint saved; continue with: python -m promoscout run --resume")
            return 130
    if args.cmd == "export":
        return cmd_export(args, settings)
    if args.cmd == "check":
        return cmd_check(args, settings)
    return cmd_status(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
