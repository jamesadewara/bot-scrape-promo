"""Review screen (F10, F13, F17; PRD 3 steps 4-5). Run: streamlit run review_app.py

A ranked table, highest discount first. The reviewer clicks the live Jumia link to verify,
sets Approve / Reject / Unsure in the Decision column and saves. Reviewed products leave
the queue; products new since the last run are marked New.
"""
from __future__ import annotations

import csv
import importlib
import io
import json
import os
import time
from urllib.parse import quote_plus

import pandas as pd
import streamlit as st

import promoscout.scoring
import promoscout.settings

# A long-running review page keeps imported modules in memory; reload the settings and scoring code
# on every rerun so a bot update (new settings) never breaks an open page.
importlib.reload(promoscout.settings)
importlib.reload(promoscout.scoring)

from promoscout.scoring import review_sort_key  # noqa: E402
from promoscout.settings import Settings  # noqa: E402
from promoscout.store import Store  # noqa: E402

settings = Settings()
st.set_page_config(page_title="PromoScout review", layout="wide")


def require_password() -> None:
    """Simple password gate (PROMOSCOUT_REVIEW_PASSWORD). Stops the page until the right password is in."""
    import hmac

    expected = settings.review_password
    if not expected:
        if os.environ.get("PROMOSCOUT_REVIEW_PUBLIC") == "true":
            st.error("This review page is published on a public IP but has no password. "
                     "Set PROMOSCOUT_REVIEW_PASSWORD in .env, then: docker compose up -d")
            st.stop()
        return
    if st.session_state.get("authed"):
        return
    st.title("PromoScout review")
    pw = st.text_input("Password", type="password")
    if pw:
        if hmac.compare_digest(pw.encode(), expected.encode()):
            st.session_state["authed"] = True
            st.rerun()
        time.sleep(2)  # slow down guessing
        st.error("Wrong password")
    st.stop()


require_password()


@st.cache_resource
def get_store() -> Store:
    settings.ensure_dirs()
    return Store(settings.db_path)


store = get_store()
st.title("PromoScout review")


# ------------------------------------------------------------------ HOT finds banner (refreshes every 30 s)
@st.fragment(run_every="30s")
def hot_banner() -> None:
    hot = [r for r in store.qualifying(settings.max_price_ngn, settings.max_old_price_ngn)
           if (r["discount_pct"] or 0) >= settings.alert_min_discount
           and (r["review_status"] or "pending") in ("pending", "unsure")]
    hot.sort(key=review_sort_key)
    if not hot:
        st.caption(f"No unreviewed hot finds ({settings.alert_min_discount:g}%+) · checked "
                   f"{time.strftime('%H:%M:%S')}")
        return
    lines = []
    for r in hot[:15]:
        found = time.strftime("%d %b %H:%M", time.localtime(r["alerted_at"] or r["first_seen"] or 0))
        price = f"₦ {r['price']:,.0f}" if r["price"] is not None else "-"
        old = f"₦ {r['old_price']:,.0f}" if r["old_price"] is not None else "-"
        flag = " ⚠️ check price" if "check_price" in r["flags"] else ""
        lines.append(f"- **{r['discount_pct']:.1f}% off** · {price} (was {old}) · "
                     f"[{(r['title'] or r['sku'])[:70]}]({r['url']}) · found {found}{flag}")
    st.error(f"🔥 **HOT: {len(hot)} unreviewed find(s) at {settings.alert_min_discount:g}% or more** "
             f"· updated {time.strftime('%H:%M:%S')}\n\n" + "\n".join(lines))


hot_banner()

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
# auto-hide finds the reviewer does not need (below keep-min after N hours, or not seen for long)
auto_hidden = store.auto_hide(settings.review_hide_after_hours, settings.review_keep_min_discount,
                              settings.review_stale_hours)
latest_run = store.db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
rows = store.qualifying(settings.max_price_ngn, settings.max_old_price_ngn)
for r in rows:
    r["is_new"] = latest_run is not None and r.get("first_seen_run") == latest_run

s1, s2 = st.columns([3, 1])
search = s1.text_input("Search", placeholder="e.g. earbuds oraimo, charger type-c, iphone case, SKU ...",
                       help="Every word must appear in the title, category, campaign or SKU (any order, any case)")
bf_choice = s2.selectbox("Black Friday", ["All products", "Black Friday only", "Not Black Friday"],
                         help="Black Friday = Jumia's 'Black Friday deal' badge or BLF / JBF campaign tags")

f1, f2, f3 = st.columns([1, 2, 1])
show = f1.selectbox("Show", ["To review", "Unsure", "Approved", "Rejected", "Hidden", "All"])
all_flags = sorted({f for r in rows for f in r["flags"]})
flag_filter = f2.multiselect("Must have flags", all_flags)
only_new = f3.checkbox("New only")

GADGET_CATS = {c.lower() for c in settings.gadget_categories}


def top_category(r) -> str:
    return (r.get("category") or "Unknown").split(" > ")[0]


def is_gadget(r) -> bool:
    """Gadget if any level of its category path is a gadget category (Phones & Tablets, Electronics ...)."""
    cat = r.get("category")
    if not cat:
        return False
    return any(level.strip().lower() in GADGET_CATS for level in cat.split(" > "))


def is_black_friday(r) -> bool:
    """Jumia's own campaign marks: the 'Black Friday deal' badge or BLF / BLF_* / JBF tags."""
    hint = (r.get("campaign_hint") or "").lower()
    if "black friday" in hint:
        return True
    tags = {t.strip() for part in hint.split(";") for t in part.split("|")}
    return any(t == "blf" or t.startswith("blf_") or t == "jbf" for t in tags)


def matches_search(r, query: str) -> bool:
    words = query.lower().split()
    if not words:
        return True
    haystack = " ".join(str(r.get(k) or "") for k in ("title", "category", "campaign_hint", "sku")).lower()
    return all(w in haystack for w in words)


g1, g2 = st.columns([1, 3])
gadgets_only = g1.toggle("Gadgets only", value=False,
                         help="Phones & Tablets, Electronics, Computing, Gaming and their sub-categories")
mystery_only = g1.toggle("Mystery boxes only", value=False,
                         help=f"Mystery / surprise / blind boxes (kept from {settings.hunt_min_discount:g}% off)")
all_cats = sorted({top_category(r) for r in rows})
cat_filter = g2.multiselect("Categories", all_cats)

status_map = {"To review": ("pending",), "Unsure": ("unsure",), "Approved": ("approved",),
              "Rejected": ("rejected",), "Hidden": ("hidden",),
              "All": ("pending", "unsure", "approved", "rejected", "hidden")}
n_hidden = sum(1 for r in rows if r["review_status"] == "hidden")
st.caption(f"Auto-hidden: {n_hidden} (under {settings.review_keep_min_discount:g}% after "
           f"{settings.review_hide_after_hours:g} h, or not seen for {settings.review_stale_hours:g} h; "
           "they return if their price changes)" + (f" · {auto_hidden} hidden just now" if auto_hidden else ""))
view = [r for r in rows if (r["review_status"] or "pending") in status_map[show]
        and all(f in r["flags"] for f in flag_filter) and (r["is_new"] or not only_new)
        and (is_gadget(r) or not gadgets_only) and (top_category(r) in cat_filter or not cat_filter)
        and matches_search(r, search)
        and ("mystery_box" in r["flags"] or not mystery_only)
        and (bf_choice == "All products" or is_black_friday(r) == (bf_choice == "Black Friday only"))]
view.sort(key=review_sort_key)  # 5.2: discount desc, then New, campaign hint, most recently seen
n_bf = sum(1 for r in view if is_black_friday(r))
st.write(f"{len(view)} products ({n_bf} Black Friday) · ranked by discount, highest first · "
         "open the live link to verify · "
         f"price filter: price < ₦{settings.max_price_ngn:,.0f} or old price < ₦{settings.max_old_price_ngn:,.0f}")

LABEL = {"pending": "", "approved": "Approve", "rejected": "Reject", "unsure": "Unsure", "hidden": ""}
REV = {"Approve": "approved", "Reject": "rejected", "Unsure": "unsure"}
df = pd.DataFrame([{
    "Rank": i + 1,
    "Discount %": r["discount_pct"],
    "New": bool(r["is_new"]),
    "Black Friday": is_black_friday(r),
    "Title": r["title"] or r["sku"],
    "Category": r.get("category") or "(seen before categories were saved)",
    "Live link": r["url"],
    # compare elsewhere: opened by you in the browser; the bot never contacts these sites
    "Google": "https://www.google.com/search?tbm=shop&q=" + quote_plus((r["title"] or r["sku"])[:120]),
    "Konga": "https://www.konga.com/search?search=" + quote_plus((r["title"] or r["sku"])[:120]),
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
        df, hide_index=True, width="stretch", key=f"table-{show}",
        disabled=[c for c in df.columns if c != "Decision"],
        column_config={
            "Live link": st.column_config.LinkColumn("Live link", display_text="open on Jumia"),
            "Google": st.column_config.LinkColumn("Google", display_text="compare"),
            "Konga": st.column_config.LinkColumn("Konga", display_text="compare"),
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
