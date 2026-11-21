"""SQLite store: pages (memory across runs, 6.1), products (2.3), runs, request audit log, lock."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    page_type TEXT,
    title TEXT,
    first_fetched REAL,
    last_fetched REAL,
    content_hash TEXT,
    best_discount REAL,
    etag TEXT,
    last_modified TEXT,
    sitemap_lastmod TEXT,
    last_outcome TEXT,
    next_check_at REAL
);
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    qualifies INTEGER NOT NULL DEFAULT 0,
    discount_pct REAL,
    url TEXT,
    last_seen REAL,
    -- the fields below are only filled for qualifying products (2.3)
    title TEXT,
    price REAL,
    old_price REAL,
    currency TEXT,
    source_page TEXT,
    first_seen REAL,
    first_seen_run INTEGER,
    flags TEXT,
    campaign_hint TEXT,
    rating_avg REAL,
    rating_count INTEGER,
    seller_score INTEGER,
    seller_quality TEXT,
    stock_text TEXT,
    review_status TEXT DEFAULT 'pending',
    reviewed_at REAL,
    reviewed_price REAL,
    reviewed_old_price REAL,
    card_seen_at REAL,
    product_page_checked_at REAL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL,
    ended_at REAL,
    state TEXT,
    reason TEXT,
    pages_fetched INTEGER DEFAULT 0,
    requests INTEGER DEFAULT 0,
    bytes INTEGER DEFAULT 0,
    new_qualifying INTEGER DEFAULT 0,
    stats TEXT
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    ts REAL,
    url TEXT,
    page_type TEXT,
    status INTEGER,
    outcome TEXT,
    robots TEXT,
    bytes INTEGER,
    latency_s REAL,
    attempt INTEGER
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_products_q ON products(qualifies, discount_pct);
CREATE INDEX IF NOT EXISTS idx_requests_run ON requests(run_id);
"""

QUALIFYING_COLS = (
    "title", "price", "old_price", "currency", "source_page", "flags", "campaign_hint",
    "rating_avg", "rating_count", "seller_score", "seller_quality", "stock_text",
)


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ kv / lock
    def get_kv(self, key: str) -> str | None:
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_kv(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value))

    def lock_until(self) -> float:
        v = self.get_kv("lock_until")
        return float(v) if v else 0.0

    def lock(self, hours: float, reason: str) -> None:
        self.set_kv("lock_until", str(time.time() + hours * 3600))
        self.set_kv("lock_reason", reason)

    # ------------------------------------------------------------------ runs
    def runs_today(self) -> int:
        lt = time.localtime()
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        return self.db.execute("SELECT COUNT(*) FROM runs WHERE started_at >= ?", (midnight,)).fetchone()[0]

    def start_run(self) -> int:
        cur = self.db.execute("INSERT INTO runs(started_at, state) VALUES(?, 'running')", (time.time(),))
        return int(cur.lastrowid)

    def end_run(self, run_id: int, state: str, reason: str, **stats) -> None:
        self.db.execute(
            "UPDATE runs SET ended_at=?, state=?, reason=?, pages_fetched=?, requests=?, bytes=?, new_qualifying=?, stats=? WHERE id=?",
            (time.time(), state, reason, stats.get("pages", 0), stats.get("requests", 0), stats.get("bytes", 0),
             stats.get("new_qualifying", 0), json.dumps(stats.get("extra", {})), run_id),
        )

    def last_completed_run_id(self, before: int) -> int | None:
        r = self.db.execute("SELECT MAX(id) FROM runs WHERE id < ?", (before,)).fetchone()
        return r[0]

    # ------------------------------------------------------------------ audit
    def log_request(self, run_id: int, url: str, page_type: str, status, outcome: str, robots: str,
                    nbytes: int, latency: float, attempt: int) -> None:
        self.db.execute(
            "INSERT INTO requests(run_id, ts, url, page_type, status, outcome, robots, bytes, latency_s, attempt) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, time.time(), url, page_type, status, outcome, robots, nbytes, latency, attempt),
        )

    # ------------------------------------------------------------------ pages
    def get_page(self, url: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM pages WHERE url=?", (url,)).fetchone()

    def upsert_page(self, url: str, **fields) -> None:
        now = time.time()
        existing = self.get_page(url)
        if existing is None:
            fields.setdefault("first_fetched", now)
            cols = ["url"] + list(fields)
            self.db.execute(f"INSERT INTO pages({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                            [url, *fields.values()])
        elif fields:
            sets = ",".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE pages SET {sets} WHERE url=?", [*fields.values(), url])

    # ------------------------------------------------------------------ products
    def get_product(self, sku: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()

    def upsert_product(self, *, sku: str, qualifies: bool, discount_pct, url, run_id: int, **q) -> str:
        """F9: dedupe by SKU, update price and last_seen. Returns 'new', 'updated' or 'minimal'."""
        now = time.time()
        row = self.get_product(sku)
        if not qualifies:
            # Below 80% (or unprovable): minimal form only, never shown (1.2)
            if row is None:
                self.db.execute(
                    "INSERT INTO products(sku, qualifies, discount_pct, url, last_seen, card_seen_at) VALUES(?,?,?,?,?,?)",
                    (sku, 0, discount_pct, url, now, q.get("card_seen_at")))
            else:
                self.db.execute(
                    "UPDATE products SET qualifies=0, discount_pct=?, url=COALESCE(?, url), last_seen=?, "
                    "card_seen_at=COALESCE(?, card_seen_at) WHERE sku=?",
                    (discount_pct, url, now, q.get("card_seen_at"), sku))
            return "minimal"
        values = {k: q.get(k) for k in QUALIFYING_COLS}
        values["flags"] = json.dumps(sorted(set(values["flags"] or [])))
        if row is None or not row["first_seen"]:
            first_seen_run = run_id
            status = "new"
        else:
            first_seen_run = row["first_seen_run"]
            status = "updated"
        review_status = row["review_status"] if row is not None and row["review_status"] else "pending"
        # 6.1: a rejected product is shown again only if its price or old price changed
        if row is not None and review_status == "rejected" and (
            values["price"] != row["reviewed_price"] or values["old_price"] != row["reviewed_old_price"]
        ):
            review_status = "pending"
        cols = {
            **values, "qualifies": 1, "discount_pct": discount_pct, "url": url, "last_seen": now,
            "first_seen": (row["first_seen"] if row is not None and row["first_seen"] else now),
            "first_seen_run": first_seen_run, "review_status": review_status,
        }
        for k in ("card_seen_at", "product_page_checked_at"):
            if q.get(k):
                cols[k] = q[k]
        if row is None:
            names = ["sku", *cols]
            self.db.execute(f"INSERT INTO products({','.join(names)}) VALUES({','.join('?' * len(names))})",
                            [sku, *cols.values()])
        else:
            # keep earlier review hints when this sighting (e.g. a card) lacks them
            for k in ("rating_avg", "rating_count", "seller_score", "seller_quality", "stock_text", "campaign_hint"):
                if cols.get(k) is None:
                    cols[k] = row[k]
            sets = ",".join(f"{k}=?" for k in cols)
            self.db.execute(f"UPDATE products SET {sets} WHERE sku=?", [*cols.values(), sku])
        return status

    def mark_gone(self, sku: str) -> None:
        self.db.execute("UPDATE products SET qualifies=0 WHERE sku=?", (sku,))

    def set_review(self, sku: str, status: str) -> None:
        assert status in ("approved", "rejected", "unsure", "pending")
        self.db.execute(
            "UPDATE products SET review_status=?, reviewed_at=?, reviewed_price=price, reviewed_old_price=old_price WHERE sku=?",
            (status, time.time(), sku))

    def qualifying(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM products WHERE qualifies=1").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["flags"] = json.loads(d["flags"] or "[]")
            d["last_seen_ts"] = d["last_seen"]
            out.append(d)
        return out
