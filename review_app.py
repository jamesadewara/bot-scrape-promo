"""Review screen (F10, F13, F17; PRD 3 steps 4-5). Run: streamlit run review_app.py

A ranked table, highest discount first. The reviewer clicks the live Jumia link to verify,
sets Approve / Reject / Unsure in the Decision column and saves. Reviewed products leave
the queue; products new since the last run are marked New.
"""
from __future__ import annotations

import csv
import io
import json
import time

import pandas as pd
import streamlit as st

from promoscout.scoring import review_sort_key
from promoscout.settings import Settings
from promoscout.store import Store

settings = Settings()
st.set_page_config(page_title="PromoScout review", layout="wide")


@st.cache_resource
def get_store() -> Store:
    settings.ensure_dirs()
    return Store(settings.db_path)


store = get_store()
st.title("PromoScout review")

# ------------------------------------------------------------------ live status (F17, reads status.json)
status = None
if settings.status_path.exists():
    try:
        status = json.loads(settings.status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = None
running = bool(status and status.get("state") not in ("done",) and not str(status.get("state", "")).startswith("stopped"))
with st.expander("Run status", expanded=running):
    if status:
        p = status.get("predictions", {})
        c = st.columns(6)
        c[0].metric("State", status.get("state", "-"))
        c[1].metric("Pages", f"{status.get('pages_done', 0)} / {status.get('max_links', 0)}")
        c[2].metric("Data", f"{status.get('bytes_total', 0) / 1e6:.1f} MB")
        c[3].metric("New 80%+", status.get("new_qualifying", 0))
        c[4].metric("Best", f"{status['best_discount']:.0f}%" if status.get("best_discount") else "-")
        c[5].metric("Hit rate /100 pages", f"{status.get('hit_rate_per_100', 0):.1f}")
        tl = p.get("time_left_s", [0, 0])
        fm = p.get("finds_more", [0, 0])
        st.caption(
            f"Now: {status.get('doing', '-')} · queue {status.get('queue', 0)} · "
            f"rate {status.get('current_rpm', 0):.0f} rpm (set {status.get('set_rpm')}) · "
            f"median {status.get('median_latency') or 0:.1f} s · retries {status.get('retries', 0)} · "
            f"errors {status.get('errors', 0)}"
        )
        st.caption(
            f"Time left ~{tl[0] / 60:.0f}-{tl[1] / 60:.0f} min" + (" (estimating)" if p.get("estimating") else "")
            + f" · likely end: {p.get('likely_end', '-')} · predicted finds +{fm[0]:.0f}-{fm[1]:.0f}"
            + (f" · {p['note']}" if p.get("note") else "")
            + (f" · reason: {status['reason']}" if status.get("reason") else "")
            + f" · skipped (recently checked) {status.get('skipped_recent', 0)}"
            + f" · updated {time.strftime('%H:%M:%S', time.localtime(status.get('updated_at', 0)))}"
        )
        hr = status.get("hit_rate_by_type") or {}
        if hr:
            st.caption("Hit rate per page type: " + " · ".join(f"{k} {v:.1f}/100" for k, v in hr.items()))
    else:
        st.write("No run status yet.")
    if st.button("Refresh status"):
        st.rerun()

# ------------------------------------------------------------------ queue
latest_run = store.db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
rows = store.qualifying()
for r in rows:
    r["is_new"] = latest_run is not None and r.get("first_seen_run") == latest_run

f1, f2, f3 = st.columns([1, 2, 1])
show = f1.selectbox("Show", ["To review", "Unsure", "Approved", "Rejected", "All"])
all_flags = sorted({f for r in rows for f in r["flags"]})
flag_filter = f2.multiselect("Must have flags", all_flags)
only_new = f3.checkbox("New only")

status_map = {"To review": ("pending",), "Unsure": ("unsure",), "Approved": ("approved",),
              "Rejected": ("rejected",), "All": ("pending", "unsure", "approved", "rejected")}
view = [r for r in rows if (r["review_status"] or "pending") in status_map[show]
        and all(f in r["flags"] for f in flag_filter) and (r["is_new"] or not only_new)]
view.sort(key=review_sort_key)  # 5.2: discount desc, then New, campaign hint, most recently seen
st.write(f"{len(view)} products · ranked by discount, highest first · open the live link to verify")

LABEL = {"pending": "", "approved": "Approve", "rejected": "Reject", "unsure": "Unsure"}
REV = {"Approve": "approved", "Reject": "rejected", "Unsure": "unsure"}
df = pd.DataFrame([{
    "Rank": i + 1,
    "Discount %": r["discount_pct"],
    "New": bool(r["is_new"]),
    "Title": r["title"] or r["sku"],
    "Live link": r["url"],
    "Price ₦": r["price"],
    "Old price ₦": r["old_price"],
    "Flags": ", ".join(r["flags"]),
    "Rating": r["rating_avg"],
    "Ratings": r["rating_count"],
    "Seller %": r["seller_score"],
    "Quality": r["seller_quality"],
    "Stock": r["stock_text"],
    "Campaign": r["campaign_hint"],
    "Last seen": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_seen"])) if r["last_seen"] else "",
    "SKU": r["sku"],
    "Decision": LABEL.get(r["review_status"] or "pending", ""),
} for i, r in enumerate(view)])

if not df.empty:
    edited = st.data_editor(
        df, hide_index=True, use_container_width=True, key=f"table-{show}",
        disabled=[c for c in df.columns if c != "Decision"],
        column_config={
            "Live link": st.column_config.LinkColumn("Live link", display_text="open on Jumia"),
            "Discount %": st.column_config.NumberColumn(format="%.1f"),
            "Price ₦": st.column_config.NumberColumn(format="%,.0f"),
            "Old price ₦": st.column_config.NumberColumn(format="%,.0f"),
            "Decision": st.column_config.SelectboxColumn("Decision", options=["", "Approve", "Reject", "Unsure"]),
        },
    )
    if st.button("Save decisions", type="primary"):
        n = 0
        for _, row in edited.iterrows():
            new = REV.get(row["Decision"])
            old = df.loc[df["SKU"] == row["SKU"], "Decision"].iloc[0]
            if new and row["Decision"] != old:
                store.set_review(row["SKU"], new)
                n += 1
        st.success(f"Saved {n} decisions")
        st.rerun()

# ------------------------------------------------------------------ export (F13)
approved = sorted([r for r in rows if r["review_status"] == "approved"], key=review_sort_key)
if approved:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["sku", "title", "discount_pct", "price", "old_price", "url", "flags"])
    for r in approved:
        w.writerow([r["sku"], r["title"], r["discount_pct"], r["price"], r["old_price"], r["url"],
                    "|".join(r["flags"])])
    st.download_button(f"Export {len(approved)} approved to CSV", buf.getvalue(), "approved.csv", "text/csv")
