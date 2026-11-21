"""Discount rule (PRD 1.2, 5.1) and review ranking (5.2)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from .settings import APPROVE_GATE, CHECK_PRICE_ABOVE, MISMATCH_POINTS

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_naira(text: str | float | int | None) -> float | None:
    """'₦ 17,330' -> 17330.0 ; '17330.00' -> 17330.0 ; 'From ₦ 59,000' -> 59000.0."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = _NUM_RE.search(str(text))
    if not m:
        return None
    return float(m.group(0).replace(",", ""))


def parse_percent(text: str | None) -> float | None:
    """'-14%' / '14%' / '84' -> 14.0 / 14.0 / 84.0 (badge and JSON cross-check value)."""
    if text is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", str(text))
    return float(m.group(0)) if m else None


def compute_discount(price: float | None, old_price: float | None) -> float | None:
    """pct = (old - price) / old * 100, rounded to 1 decimal. None when it can't be proven."""
    if price is None or old_price is None or old_price <= 0 or price < 0:
        return None
    pct = (Decimal(str(old_price)) - Decimal(str(price))) / Decimal(str(old_price)) * 100
    return float(pct.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def badge_round(pct: float) -> int:
    """Jumia's badge rounds half-up to a whole percent (81.9 -> 82, 79.5 -> 80)."""
    return int(Decimal(str(pct)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass
class Verdict:
    approved: bool
    discount_pct: float | None
    flags: list[str] = field(default_factory=list)


def apply_rule(
    price: float | None,
    old_price: float | None,
    *,
    cross_checks: list[float | None] | None = None,
    price_from: bool = False,
) -> Verdict:
    """Discount rule (1.2). Gate on the badge-rounded value, rank on the 1-decimal value."""
    flags: list[str] = ["old_price_unverified"]  # always (5.1)
    if price_from:
        flags.append("price_from")
    pct = compute_discount(price, old_price)
    if pct is None:
        return Verdict(False, None, flags)
    for check in cross_checks or []:
        if check is not None and abs(check - pct) > MISMATCH_POINTS:
            flags.append("discount_mismatch")  # use the computed value regardless
            break
    if pct < 0:
        return Verdict(False, pct, flags)
    approved = badge_round(pct) >= APPROVE_GATE
    if approved and pct > CHECK_PRICE_ABOVE:
        flags.append("check_price")
    return Verdict(approved, pct, flags)


def review_sort_key(p: dict) -> tuple:
    """5.2: discount desc; ties: New first, campaign hint present, most recently seen."""
    return (
        -(p.get("discount_pct") or 0),
        0 if p.get("is_new") else 1,
        0 if p.get("campaign_hint") else 1,
        -(p.get("last_seen_ts") or 0),
    )


def in_price_range(price: float | None, old_price: float | None, max_price: float, max_old_price: float) -> bool:
    """Operator price filter: price < max_price OR old price < max_old_price (strictly less)."""
    return (price is not None and price < max_price) or (old_price is not None and old_price < max_old_price)
