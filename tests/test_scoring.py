"""Discount rule (1.2, 5.1) and ranking (5.2)."""
from promoscout.scoring import apply_rule, compute_discount, parse_naira, review_sort_key


def test_parse_naira():
    assert parse_naira("₦ 17,330") == 17330
    assert parse_naira("17330.00") == 17330
    assert parse_naira("From ₦ 59,000") == 59000
    assert parse_naira(None) is None
    assert parse_naira("") is None


def test_compute_discount_rounding():
    assert compute_discount(17330, 20060) == 13.6
    assert compute_discount(69984, 385920) == 81.9
    assert compute_discount(100, None) is None
    assert compute_discount(None, 100) is None
    assert compute_discount(100, 0) is None


def test_below_80_not_approved():
    v = apply_rule(17330, 20060, cross_checks=[14, 14])
    assert not v.approved and v.discount_pct == 13.6
    assert "discount_mismatch" not in v.flags


def test_gate_uses_badge_rounding():
    assert apply_rule(2050, 10000).approved          # 79.5 -> 80
    assert not apply_rule(2060, 10000).approved      # 79.4 -> 79
    v = apply_rule(69984, 385920, cross_checks=[82])
    assert v.approved and v.discount_pct == 81.9     # rank on 81.9


def test_above_99_check_price():
    v = apply_rule(0, 25000)
    assert v.approved and v.discount_pct == 100 and "check_price" in v.flags
    assert "check_price" not in apply_rule(5000, 50000).flags  # 90%


def test_no_old_price_not_approved():
    v = apply_rule(12000, None)
    assert not v.approved and v.discount_pct is None


def test_mismatch_flag_uses_computed():
    v = apply_rule(1000, 10000, cross_checks=[80])
    assert v.approved and v.discount_pct == 90 and "discount_mismatch" in v.flags


def test_old_price_unverified_always():
    for args in ((1, 10), (9, 10), (1, None)):
        assert "old_price_unverified" in apply_rule(*args).flags


def test_ranking_order():
    rows = [
        {"sku": "a", "discount_pct": 85, "is_new": False, "campaign_hint": None, "last_seen_ts": 5},
        {"sku": "b", "discount_pct": 90, "is_new": False, "campaign_hint": None, "last_seen_ts": 1},
        {"sku": "c", "discount_pct": 85, "is_new": True, "campaign_hint": None, "last_seen_ts": 1},
        {"sku": "d", "discount_pct": 85, "is_new": False, "campaign_hint": "BLF", "last_seen_ts": 1},
        {"sku": "e", "discount_pct": 85, "is_new": False, "campaign_hint": "BLF", "last_seen_ts": 9},
    ]
    assert [r["sku"] for r in sorted(rows, key=review_sort_key)] == ["b", "c", "e", "d", "a"]


def test_price_filter_or_rule():
    from promoscout.scoring import in_price_range
    assert in_price_range(15_000, 2_000_000, 20_000, 1_000_000)        # price under 20k
    assert in_price_range(50_000, 400_000, 20_000, 1_000_000)          # old price under 1M
    assert not in_price_range(102_999, 1_200_000, 20_000, 1_000_000)   # neither
    assert not in_price_range(20_000, 1_000_000, 20_000, 1_000_000)    # limits are strict "<"
    assert not in_price_range(None, None, 20_000, 1_000_000)
