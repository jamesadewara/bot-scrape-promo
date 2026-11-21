"""Email notification over SMTP (Gmail: smtp.gmail.com:465 with an app password).

One email per batch that produced new hot finds (discount >= alert_min_discount), with the
review page link, those hot finds, and the best unreviewed finds. Never raises into the bot.
"""
from __future__ import annotations

import html
import smtplib
import ssl
import time
from email.message import EmailMessage

import structlog

from .scoring import review_sort_key
from .settings import Settings
from .store import Store

log = structlog.get_logger("promoscout.notify")


def recipients(settings: Settings) -> list[str]:
    """PROMOSCOUT_EMAIL_TO: one or more addresses separated by commas or semicolons."""
    raw = settings.email_to or settings.smtp_user
    seen: list[str] = []
    for a in raw.replace(";", ",").split(","):
        a = a.strip()
        if a and a.lower() not in (x.lower() for x in seen):
            seen.append(a)
    return seen


def _money(v) -> str:
    return f"₦ {v:,.0f}" if v is not None else "-"


def build_report(settings: Settings, run_id: int | None, store: Store, test: bool = False) -> EmailMessage | None:
    """The email for this run, or None when there is nothing to send."""
    started = None
    if run_id is not None:
        row = store.db.execute("SELECT started_at FROM runs WHERE id=?", (run_id,)).fetchone()
        started = row["started_at"] if row else None
    shown = store.qualifying(settings.max_price_ngn, settings.max_old_price_ngn)
    hot_new = [r for r in shown if started is not None and r.get("alerted_at") and r["alerted_at"] >= started
               and (r["discount_pct"] or 0) >= settings.alert_min_discount]
    new_any = [r for r in shown if run_id is not None and r.get("first_seen_run") == run_id]
    trigger = settings.email_trigger
    if not test:
        if trigger == "hot" and not hot_new:
            return None
        if trigger == "new" and not new_any:
            return None
    best = sorted([r for r in shown if (r["review_status"] or "pending") in ("pending", "unsure")],
                  key=review_sort_key)[: settings.email_top_n]
    hot_new.sort(key=review_sort_key)

    if test:
        subject = "PromoScout: test email"
    elif hot_new:
        subject = f"PromoScout: {len(hot_new)} hot find(s), best {hot_new[0]['discount_pct']:.1f}% off"
    elif new_any:
        top = max(r["discount_pct"] for r in new_any)
        subject = f"PromoScout: {len(new_any)} new 80%+ find(s), best {top:.1f}% off"
    else:
        subject = "PromoScout: batch report"

    def text_rows(rows):
        return "\n".join(
            f"  {r['discount_pct']:.1f}% off  {_money(r['price'])} (was {_money(r['old_price'])})  "
            f"{(r['title'] or r['sku'])[:70]}\n    {r['url']}" + ("  [check price]" if "check_price" in r["flags"] else "")
            for r in rows) or "  (none)"

    def html_rows(rows):
        if not rows:
            return "<p>(none)</p>"
        out = ["<table cellpadding='4' style='border-collapse:collapse'>",
               "<tr><th align='left'>Discount</th><th align='left'>Price</th><th align='left'>Was</th>"
               "<th align='left'>Product</th></tr>"]
        for r in rows:
            warn = " ⚠️ check price" if "check_price" in r["flags"] else ""
            out.append(
                f"<tr><td><b>{r['discount_pct']:.1f}%</b></td><td>{_money(r['price'])}</td>"
                f"<td>{_money(r['old_price'])}</td><td><a href='{html.escape(r['url'] or '')}'>"
                f"{html.escape((r['title'] or r['sku'])[:90])}</a>{warn}</td></tr>")
        out.append("</table>")
        return "\n".join(out)

    review = settings.review_url
    when = time.strftime("%Y-%m-%d %H:%M")
    text = (
        f"PromoScout report {when}\n\nReview page: {review}\n\n"
        f"New hot finds ({settings.alert_min_discount:g}%+):\n{text_rows(hot_new)}\n\n"
        f"Best unreviewed finds (top {settings.email_top_n}):\n{text_rows(best)}\n\n"
        "Old prices are the seller's reference price: verify on the live Jumia page.\n"
    )
    body = (
        f"<p>PromoScout report {when}</p>"
        f"<p><a href='{html.escape(review)}'><b>Open the review page</b></a> ({html.escape(review)})</p>"
        f"<h3>New hot finds ({settings.alert_min_discount:g}%+)</h3>{html_rows(hot_new)}"
        f"<h3>Best unreviewed finds (top {settings.email_top_n})</h3>{html_rows(best)}"
        "<p style='color:#666'>Old prices are the seller's reference price: verify on the live Jumia page.</p>"
    )
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email_from or settings.smtp_user
    msg["To"] = ", ".join(recipients(settings))
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    return msg


def send(settings: Settings, msg: EmailMessage) -> None:
    ctx = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ctx, timeout=30) as s:
            s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)


def configured(settings: Settings) -> bool:
    return bool(settings.email_enabled and settings.smtp_user and settings.smtp_password)


def notify_run(settings: Settings, run_id: int | None) -> bool:
    """Send the batch email if enabled and there is something to report. Returns True if sent."""
    if not configured(settings) or run_id is None:
        return False
    store = Store(settings.db_path)
    try:
        msg = build_report(settings, run_id, store)
        if msg is None:
            return False
        send(settings, msg)
        log.info("email_sent", to=msg["To"], subject=msg["Subject"])
        return True
    except Exception as exc:  # noqa: BLE001 - email must never stop the bot
        log.error("email_failed", error=f"{type(exc).__name__}: {exc}")
        print(f"[email] failed: {type(exc).__name__}: {exc}")
        return False
    finally:
        store.close()


PROBLEM_KEY = "problem_email:"


def notify_problem(settings: Settings, kind: str, subject: str, details: str, force: bool = False) -> bool:
    """Email every recipient about a problem (block, Jumia unreachable, auto mode stopped ...).

    At most one email per `kind` every problem_email_every_hours, so a long outage does not spam.
    Records that a problem is open, so a "back to normal" email can follow. Never raises.
    """
    if not configured(settings):
        return False
    store = Store(settings.db_path)
    try:
        last = float(store.get_kv(PROBLEM_KEY + kind) or 0)
        if not force and time.time() - last < settings.problem_email_every_hours * 3600:
            return False
        msg = EmailMessage()
        msg["Subject"] = f"PromoScout problem: {subject}"
        msg["From"] = settings.email_from or settings.smtp_user
        msg["To"] = ", ".join(recipients(settings))
        msg.set_content(
            f"{subject}\n\nTime: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n{details}\n\n"
            f"Review page: {settings.review_url}\n"
            "Check: python -m promoscout check  (server: docker compose exec bot python -m promoscout check)\n")
        send(settings, msg)
        store.set_kv(PROBLEM_KEY + kind, str(time.time()))
        store.set_kv("problem_open", kind)
        log.warning("problem_email_sent", kind=kind, subject=subject)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("problem_email_failed", kind=kind, error=f"{type(exc).__name__}: {exc}")
        print(f"[email] problem email failed: {type(exc).__name__}: {exc}")
        return False
    finally:
        store.close()


def notify_recovered(settings: Settings, details: str) -> bool:
    """After a problem email, send one "back to normal" email when a batch works again."""
    if not configured(settings):
        return False
    store = Store(settings.db_path)
    try:
        open_kind = store.get_kv("problem_open")
        if not open_kind:
            return False
        store.set_kv("problem_open", "")
    finally:
        store.close()
    msg = EmailMessage()
    msg["Subject"] = "PromoScout: working again"
    msg["From"] = settings.email_from or settings.smtp_user
    msg["To"] = ", ".join(recipients(settings))
    msg.set_content(f"The bot is working again after: {open_kind}\n\n{details}\n\nReview page: {settings.review_url}\n")
    try:
        send(settings, msg)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("recovered_email_failed", error=f"{type(exc).__name__}: {exc}")
        return False
