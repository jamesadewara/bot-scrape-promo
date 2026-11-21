"""Frontier (F7, F8, F11) and status predictions (7.1, F17, F18)."""
from promoscout.frontier import TIER_NEW, TIER_OTHER, TIER_STALE_QUALIFYING, Entry, Frontier, link_priority
from promoscout.status import RunStatus

B = "https://www.jumia.com.ng"


def e(path, pr, tier=TIER_NEW, section="x"):
    return Entry(url=B + path, depth=1, priority=pr, tier=tier, kind="listing", section=section)


def test_best_first_and_tiers():
    f = Frontier()
    f.push(e("/a/", 10, TIER_OTHER))
    f.push(e("/b/", 50, TIER_STALE_QUALIFYING))
    f.push(e("/c/", 5, TIER_NEW))
    f.push(e("/d/", 90, TIER_NEW))
    assert [f.pop().url[-3:] for _ in range(4)] == ["/d/", "/c/", "/b/", "/a/"]
    assert f.pop() is None


def test_dedupe_and_visited():
    f = Frontier()
    assert f.push(e("/a/", 10))
    assert not f.push(e("/a/", 5))          # worse duplicate ignored
    assert f.push(e("/a/", 20))             # better priority replaces
    assert f.pop().priority == 20 and f.pop() is None
    f.mark_visited(B + "/a/", "ok")
    assert not f.push(e("/a/", 99))


def test_prune_section():
    f = Frontier()
    f.push(e("/a/", 10, section="fans"))
    f.push(e("/b/", 5, section="tablets"))
    f.prune("fans")
    assert f.pop().url == B + "/b/" and f.pop() is None
    assert not f.push(e("/c/", 50, section="fans"))


def test_checkpoint_roundtrip():
    f = Frontier()
    f.push(e("/a/", 10))
    f.mark_visited(B + "/z/", "ok")
    f.prune("old")
    g = Frontier.from_json(f.to_json())
    assert len(g) == 1 and B + "/z/" in g.visited and "old" in g.pruned_sections


def test_link_priority_prefers_campaign_and_hot_parents():
    assert link_priority(B + "/mlp-black-friday/", None, False) > link_priority(B + "/tablets/", None, False)
    assert link_priority(B + "/tablets/", 90, False) > link_priority(B + "/tablets/", 10, False)
    assert link_priority(B + "/tablets/", None, True) > link_priority(B + "/tablets/", None, False)
    assert link_priority(B + "/tablets/?page=9", None, False) < link_priority(B + "/tablets/", None, False)


def test_predictions_estimating_range_then_point():
    s = RunStatus(max_links=300, max_runtime_s=1800, set_rpm=30, target_candidates=30)
    s.queue = 200
    s.queue_kinds = {"listing": 150, "product": 50}
    for i in range(5):
        s.add_bytes(200_000)
        s.page_done("listing", 200_000, 1 if i == 0 else 0)
    p = s.predictions()
    assert p["estimating"] and p["time_left_s"][0] < p["time_left_s"][1]
    for _ in range(20):
        s.add_bytes(100_000)
        s.page_done("product", 100_000, 0)
    p = s.predictions()
    assert not p["estimating"] and p["time_left_s"][0] == p["time_left_s"][1]
    assert p["data_left_bytes"][0] > 0 and p["likely_end"] in ("page cap", "time cap", "queue empty",
                                                                "target reached")
    s.estimate_note = "slowed: responses 3 times normal"
    p = s.predictions()
    assert p["time_left_s"][0] < p["time_left_s"][1] and p["note"]   # slowdown widens the estimate
    assert "PromoScoutBot" in s.panel().title
