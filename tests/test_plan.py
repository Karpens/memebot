"""memebot/plan.py tests, offline and pure: the NEWS score and its parts, the tiers, every tier's
ladder with its exact fractions, the hard stop, break-even and the trailing stop after the first
step, the time stop per tier before it, upgrades and downgrades on a NEWS refresh, fading, and
garbage input that must never raise."""

import copy
import json
import unittest

from _load import load

P = load("memebot", "plan")

# hold-spec.md's tiers as the user first set them (the first step at +7 %): the ladder, stop, replan and
# fading tests below walk these numbers. plan.DEFAULT_TIERS (and config.json) moved the first step to
# +10 % for the costs; test_default_tiers_are_the_spec and tests/test_levers.py check those.
SPEC_TIERS = [
    {"name": "scalp", "min_score": 0, "ladder": [[7.0, 1.0]], "trail_pct": 0.0, "time_stop_min": 5.0},
    {"name": "swing", "min_score": 3, "ladder": [[7.0, 0.5], [20.0, 1.0]], "trail_pct": 6.0, "time_stop_min": 10.0},
    {"name": "runner", "min_score": 5, "ladder": [[7.0, 0.34], [20.0, 0.5], [60.0, 1.0]], "trail_pct": 10.0,
     "time_stop_min": 15.0},
    {"name": "moon", "min_score": 7, "ladder": [[7.0, 0.25], [20.0, 0.34], [60.0, 0.5], [100.0, 1.0]],
     "trail_pct": 15.0, "time_stop_min": 20.0},
]
# the values of config.json + hold-spec.md
CFG = {"stop_pct": -7.0, "breakeven_pct": 1.0, "time_stop_below_pct": 2.0, "fade_buy_ratio": 0.45,
       "hold_tiers": SPEC_TIERS}


def garbage():
    return [None, float("nan"), float("inf"), float("-inf"), "", "abc", [], {}, object(), -1, -1e9, 10 ** 30,
            10 ** 400, True, False, ("x",), {"n": 3}, [[None]], [["a", "b"]]]


def plan_for(tier, cfg=CFG):
    score = {"scalp": 0, "swing": 3, "runner": 5, "moon": 7}[tier]
    plan = P.new_plan(score, ["test"], P.tiers_from_cfg(cfg), cfg)
    assert plan["tier"] == tier
    return plan


def walk(plan, gains, cfg=CFG, held=1.0):
    """Feeds price readings as the bot would; returns the sales [(gain, frac, kind)] and what is
    left of 1.0 coin."""
    left, sales, peak = 1.0, [], 0.0
    for g in gains:
        peak = max(peak, g)
        P.track(plan, g, cfg)
        d = P.decide_exit(plan, g, peak, held, cfg)
        if d:
            sales.append((g, d["frac"], d["kind"]))
            left *= 1 - d["frac"]
            P.mark_step(plan, d, cfg)
            if d["final"]:
                break
    return sales, round(left, 6)


class TestTiers(unittest.TestCase):
    def test_default_tiers_are_the_spec(self):
        """hold-spec.md's tiers with the first step at +10 % instead of +7 % (the costs: tests/test_levers.py)."""
        by = {t["name"]: t for t in P.DEFAULT_TIERS}
        self.assertEqual([t["name"] for t in P.DEFAULT_TIERS], ["scalp", "swing", "runner", "moon"])
        self.assertEqual(by["scalp"]["ladder"], [[10, 1.0]])
        self.assertEqual(by["swing"]["ladder"], [[10, 0.5], [20, 1.0]])
        self.assertEqual(by["runner"]["ladder"], [[10, 0.34], [20, 0.5], [60, 1.0]])
        self.assertEqual(by["moon"]["ladder"], [[10, 0.25], [20, 0.34], [60, 0.5], [100, 1.0]])
        for new, spec in zip(P.DEFAULT_TIERS, SPEC_TIERS):            # only the first step's gain moved
            self.assertEqual(dict(new, ladder=new["ladder"][1:]), dict(spec, ladder=spec["ladder"][1:]))
            self.assertEqual((new["ladder"][0][1], spec["ladder"][0][0]), (spec["ladder"][0][1], 7))
        self.assertEqual([by[n]["trail_pct"] for n in ("swing", "runner", "moon")], [6, 10, 15])
        self.assertEqual([by[n]["time_stop_min"] for n in ("scalp", "swing", "runner", "moon")], [5, 10, 15, 20])
        self.assertEqual([by[n]["min_score"] for n in ("scalp", "swing", "runner", "moon")], [0, 3, 5, 7])

    def test_pick_tier_boundaries(self):
        want = {0: "scalp", 2: "scalp", 2.5: "scalp", 3: "swing", 4: "swing", 4.5: "swing", 5: "runner",
                6: "runner", 6.5: "runner", 7: "moon", 10: "moon", 99: "moon", -3: "scalp"}
        for score, name in want.items():
            self.assertEqual(P.pick_tier(score, P.DEFAULT_TIERS)["name"], name, score)
            self.assertEqual(P.pick_tier(score)["name"], name, score)

    def test_pick_tier_any_order_skips_broken_tiers(self):
        tiers = list(reversed(P.DEFAULT_TIERS)) + [None, {"name": "x"}, {"name": "", "ladder": [[5, 1]]},
                                                    {"name": "bad", "min_score": 4, "ladder": "junk"}]
        self.assertEqual(P.pick_tier(4, tiers)["name"], "swing")
        self.assertEqual(P.pick_tier(8, tiers)["name"], "moon")
        self.assertEqual(P.pick_tier(4, [None, "x"])["name"], "swing")         # nothing usable: defaults
        only_high = [{"name": "hi", "min_score": 5, "ladder": [[10, 1]]}, {"name": "mid", "min_score": 2,
                                                                          "ladder": [[7, 1]]}]
        self.assertEqual(P.pick_tier(0, only_high)["name"], "mid")             # under every min: the lowest

    def test_pick_tier_returns_a_fresh_copy(self):
        t = P.pick_tier(5)
        t["ladder"][0][1] = 0.9
        t["ladder"].append([500, 1])
        self.assertEqual(P.pick_tier(5)["ladder"], [[10, 0.34], [20, 0.5], [60, 1.0]])
        self.assertEqual(P.DEFAULT_TIERS[2]["ladder"], [[10, 0.34], [20, 0.5], [60, 1.0]])

    def test_ladder_is_cleaned(self):
        t = P.pick_tier(0, [{"name": "t", "min_score": 0, "ladder": [[20, 1.5], None, [7, 0.5], ["x", 1],
                                                                       [-3, 0.5], [9, 0], [12]]}])
        self.assertEqual(t["ladder"], [[7.0, 0.5], [20.0, 1.0]])

    def test_tiers_from_cfg(self):
        self.assertEqual([t["name"] for t in P.tiers_from_cfg(CFG)], ["scalp", "swing", "runner", "moon"])
        self.assertEqual([t["name"] for t in P.tiers_from_cfg(None)], ["scalp", "swing", "runner", "moon"])
        self.assertEqual([t["name"] for t in P.tiers_from_cfg({})], ["scalp", "swing", "runner", "moon"])
        # no hold_tiers: the old exit keys (bot.DEFAULTS) as one tier, so old configs exit as before
        old = {"tp1_pct": 50.0, "tp1_sell_frac": 0.5, "trail_pct": 20.0, "time_stop_min": 45, "hold_tiers": "junk"}
        base = P.tiers_from_cfg(old)
        self.assertEqual(len(base), 1)
        self.assertEqual(base[0], {"name": "base", "min_score": 0.0, "ladder": [[50.0, 0.5]], "trail_pct": 20.0,
                                   "time_stop_min": 45.0})
        self.assertEqual(P.new_plan(9, [], cfg=old)["tier"], "base")

    def test_ladder_text(self):
        self.assertEqual(P.ladder_text(P.DEFAULT_TIERS[2]["ladder"]), "+10 → +20 → +60")
        self.assertEqual(P.ladder_text(SPEC_TIERS[2]["ladder"]), "+7 → +20 → +60")
        self.assertEqual(P.ladder_text([[7.5, 1]]), "+7.5")
        self.assertEqual(P.ladder_text(None), "-")


class TestScore(unittest.TestCase):
    def score(self, **info):
        return P.news_score(info, CFG)

    def test_each_part(self):
        cases = [({"twitter": "https://x.com/meme"}, 1, ["X"]),
                 ({"x": "https://twitter.com/meme"}, 1, ["X"]),
                 ({"telegram": "https://t.me/meme"}, 1, ["Telegram"]),
                 ({"website": "https://meme.io"}, 1, ["web"]),
                 ({"web": True}, 1, ["web"]),
                 ({"boosts": 1}, 1, ["boosts 1"]),
                 ({"boosts": 49}, 1, ["boosts 49"]),
                 ({"boosts": 50}, 2, ["boosts 50"]),
                 ({"trending": True}, 2, ["trending"]),
                 ({"spike": 2.99}, 0, []),
                 ({"spike": 3.0}, 1, ["volume 3.0x"]),
                 ({"buy_ratio": 0.64}, 0, []),
                 ({"buy_ratio": 0.65}, 1, ["buys 65%"]),
                 ({"buys": 13, "sells": 7}, 1, ["buys 65%"]),
                 ({"buys": 3, "sells": 0}, 0, []),                  # 3 trades: unknown, not 100 %
                 ({"holder_signal": True}, 1, ["holders"])]
        for info, pts, parts in cases:
            self.assertEqual(P.news_score(info, CFG), (pts, parts), info)

    def test_twitter_status_counts_half(self):
        self.assertEqual(self.score(twitter="https://x.com/elonmusk/status/123"), (0.5, ["X post"]))
        self.assertEqual(self.score(twitter="https://x.com/search?q=meme"), (0.5, ["X post"]))
        self.assertEqual(self.score(twitter="https://x.com/meme", twitter_kind="status"), (0.5, ["X post"]))
        self.assertEqual(self.score(twitter="https://x.com/a/status/1", twitter_kind="profile"), (1, ["X"]))
        self.assertEqual(self.score(twitter="https://x.com/i/communities/77"), (1, ["X"]))
        self.assertEqual(self.score(twitter_kind="profile"), (0, []))             # a kind without a link
        self.assertEqual(P.twitter_kind("https://x.com/a/status/1"), "status")
        self.assertEqual(P.twitter_kind("@meme"), "profile")
        self.assertIsNone(P.twitter_kind(""))
        self.assertIsNone(P.twitter_kind(None))

    def test_empty_metadata_socials_do_not_count(self):
        for v in ("", " ", "null", "None", "undefined", "n/a", 0, False, None, float("nan"), 7):
            self.assertEqual(self.score(twitter=v, telegram=v, website=v), (0, []), v)

    def test_flags_must_be_true(self):
        self.assertEqual(self.score(trending="false", holder_signal="no"), (0, []))
        self.assertEqual(self.score(trending=1, holder_signal=float("nan")), (2, ["trending"]))

    def test_dexscreener_full_house_is_ten(self):
        s, parts = self.score(twitter="https://x.com/m", telegram="t.me/m", website="m.io", boosts=80,
                              trending=True, spike=5.0, buy_ratio=0.8, holder_signal=True)
        self.assertEqual(s, 10)
        self.assertEqual(parts, ["X", "Telegram", "web", "boosts 80", "trending", "volume 5.0x", "buys 80%",
                                 "holders"])

    def test_spec_example(self):
        s, parts = self.score(twitter="https://x.com/m", telegram="t.me/m", trending=True, buy_ratio=0.7)
        self.assertEqual((s, parts), (5, ["X", "Telegram", "trending", "buys 70%"]))
        self.assertEqual(P.plan_text(P.new_plan(s, parts, cfg=CFG)),
                         "hold plan · 5/10 (X, Telegram, trending, buys 70%) · runner: +7 → +20 → +60")

    def test_curve_momentum_for_new_pairs(self):
        self.assertEqual(self.score(sol_in=5.9, buys=10, sells=4), (0, []))     # 71 % buys: the entry minimum
        self.assertEqual(self.score(sol_in=6.0), (1, ["inflow 6.0 SOL"]))
        self.assertEqual(self.score(sol_in=12.0), (2, ["inflow 12.0 SOL"]))
        self.assertEqual(self.score(sol_in=3, buys=8, sells=2), (1, ["buys 80%"]))
        self.assertEqual(self.score(sol_in=3, buys=79, sells=21), (0, []))
        self.assertEqual(self.score(sol_in=3, buy_ratio=0.9), (1, ["buys 90%"]))
        # DexScreener's spike does not count for a curve coin; its own 65 % buy bar does not either
        self.assertEqual(self.score(sol_in=3, spike=9.0, buy_ratio=0.7), (0, []))
        # socials + strong inflow + buys: a runner at entry
        s, parts = self.score(twitter="https://x.com/m", telegram="t.me/m", website="m.io", sol_in=14.2, buys=20,
                              sells=3)
        self.assertEqual((s, parts), (6, ["X", "Telegram", "web", "inflow 14.2 SOL", "buys 87%"]))

    def test_curve_can_overflow_and_is_capped(self):
        s, parts = self.score(twitter="https://x.com/m", telegram="t.me/m", website="m.io", boosts=60,
                              trending=True, sol_in=30, buys=30, sells=1, holder_signal=True)
        self.assertEqual(len(parts), 8)
        self.assertEqual(s, 10)                    # 11 points, capped

    def test_config_thresholds(self):
        cfg = dict(CFG, news_spike_x=2.0, news_buy_ratio=0.5, news_boosts_big=10, news_inflow_sol=2,
                   news_inflow_big_sol=4, news_curve_buy_ratio=0.6, news_min_trades=1)
        self.assertEqual(P.news_score({"spike": 2.0, "buy_ratio": 0.5, "boosts": 10}, cfg)[0], 4)
        self.assertEqual(P.news_score({"sol_in": 4, "buys": 2, "sells": 1}, cfg)[0], 3)

    def test_info_from_facts(self):
        fx = {"mint": "m1", "x": "https://x.com/m", "telegram": "", "web": True, "boosts": 2, "spike": 3.5,
              "buys5": 70, "sells5": 30, "buy_ratio": 0.7}
        info = P.info_from_facts(fx, {"m1", "m2"}, holder_signal=True)
        self.assertEqual(P.news_score(info, CFG), (8, ["X", "web", "boosts 2", "trending", "volume 3.5x", "buys 70%",
                                                       "holders"]))
        quiet = dict(fx, buys5=0, sells5=0, buy_ratio=0.0)                     # no trades: unknown, not 0 %
        self.assertIsNone(P.buy_share(P.info_from_facts(quiet)))
        self.assertEqual(P.info_from_facts({"mint": "m1", "buy_ratio": 0.8})["buy_ratio"], 0.8)
        self.assertFalse(P.info_from_facts(fx, None)["trending"])
        self.assertTrue(P.info_from_facts(fx, True)["trending"])
        self.assertEqual(P.info_from_facts(None), {})
        # trending must be a collection of mints: text would match substrings ("" is in any text)
        self.assertFalse(P.info_from_facts({"mint": ""}, "m1m2")["trending"])
        self.assertFalse(P.info_from_facts({"mint": "m1"}, "m1m2")["trending"])
        self.assertFalse(P.info_from_facts({"mint": -1}, b"abc")["trending"])   # bytes: ValueError before
        self.assertFalse(P.info_from_facts({"mint": None}, {None})["trending"])
        self.assertTrue(P.info_from_facts({"mint": "m1"}, ["m0", "m1"])["trending"])
        self.assertTrue(P.info_from_facts({"mint": "m1"}, {"m1": 3})["trending"])
        # holder_signal counts as news_score counts it: True or a positive number
        self.assertTrue(P.info_from_facts(fx, (), 1)["holder_signal"])
        self.assertFalse(P.info_from_facts(fx, (), "holders +5%")["holder_signal"])

    def test_news_bucket(self):
        want = {0: "news:0-2", 2.5: "news:0-2", 3: "news:3-4", 4.5: "news:3-4", 5: "news:5-6", 6.5: "news:5-6",
                7: "news:7+", 10: "news:7+", None: "news:0-2", "x": "news:0-2"}
        for s, lab in want.items():
            self.assertEqual(P.news_bucket(s), lab, s)

    def test_garbage_never_raises(self):
        for g in garbage():
            s, parts = P.news_score(g, CFG)
            self.assertEqual((s, parts), (0, []), g)
            info = {k: g for k in ("twitter", "telegram", "website", "twitter_kind", "boosts", "trending", "spike",
                                   "buy_ratio", "holder_signal", "sol_in", "buys", "sells")}
            s, parts = P.news_score(info, g)
            self.assertTrue(0 <= s <= 10, g)
            self.assertIsInstance(parts, list)
            P.buy_share(info, g)
            P.info_from_facts(info, g, g)
        bad = {"boosts": float("nan"), "spike": float("inf"), "sol_in": -5, "buys": -3, "sells": "a", "twitter": 5,
               "trending": "false", "holder_signal": float("nan"), "buy_ratio": 7}
        self.assertEqual(P.news_score(bad, CFG), (0, []))


class TestLadders(unittest.TestCase):
    def test_scalp_sells_everything_at_7(self):
        self.assertEqual(walk(plan_for("scalp"), [3, 6.9, 7.0]), ([(7.0, 1.0, "take")], 0.0))

    def test_swing(self):
        sales, left = walk(plan_for("swing"), [3, 7, 12, 19.9, 20])
        self.assertEqual(sales, [(7, 0.5, "take"), (20, 1.0, "take")])
        self.assertEqual(left, 0.0)

    def test_runner(self):
        plan = plan_for("runner")
        left = 1.0
        for g, frac, rest in ((7, 0.34, 0.66), (20, 0.5, 0.33), (60, 1.0, 0.0)):
            self.assertIsNone(P.decide_exit(plan, g - 0.1, g - 0.1, 1, CFG))
            d = P.decide_exit(plan, g, g, 1, CFG)
            self.assertEqual((d["frac"], d["kind"], d["final"]), (frac, "take", frac == 1.0))
            left *= 1 - d["frac"]
            self.assertAlmostEqual(left, rest, places=9)
            P.mark_step(plan, d, CFG)
        self.assertEqual(plan["step"], 3)
        self.assertEqual(plan["sold"], 3)

    def test_moon(self):
        sales, left = walk(plan_for("moon"), [5, 7, 15, 20, 45, 60, 90, 100])
        self.assertEqual(sales, [(7, 0.25, "take"), (20, 0.34, "take"), (60, 0.5, "take"), (100, 1.0, "take")])
        self.assertEqual(left, 0.0)

    def test_a_jump_sells_the_passed_steps_at_once(self):
        plan = plan_for("runner")
        d = P.decide_exit(plan, 25, 25, 1, CFG)
        self.assertEqual((d["frac"], d["step"], d["steps"], d["gain"], d["final"]), (0.67, 2, 2, 20, False))
        self.assertEqual(P.take_text(d), "TP +20% · sold 67% (2 steps) · stop to break-even")
        P.mark_step(plan, d, CFG)
        self.assertEqual((plan["step"], plan["sold"]), (2, 2))
        d = P.decide_exit(plan, 70, 70, 1, CFG)
        self.assertEqual((d["frac"], d["final"]), (1.0, True))
        self.assertEqual(P.decide_exit(plan_for("moon"), 150, 150, 1, CFG)["frac"], 1.0)

    def test_take_texts(self):
        plan = plan_for("runner")
        d = P.decide_exit(plan, 7.2, 7.2, 1, CFG)
        self.assertEqual(d["why"], "TP +7%")
        self.assertEqual(P.take_text(d), "TP +7% · sold 34% · stop to break-even")
        P.mark_step(plan, d, CFG)
        d = P.decide_exit(plan, 21, 21, 1, CFG)
        self.assertEqual(P.take_text(d), "TP +20% · sold 50%")
        self.assertEqual(P.decide_exit(plan_for("scalp"), 8, 8, 1, CFG)["after"], "")    # nothing left to stop
        self.assertEqual(P.take_text(None), "")

    def test_a_ladder_whose_last_step_keeps_a_bag(self):
        cfg = dict(CFG, hold_tiers=[{"name": "bag", "min_score": 0, "ladder": [[7, 0.5]], "trail_pct": 0,
                                     "time_stop_min": 5}])
        plan = P.new_plan(0, [], cfg=cfg)
        sales, left = walk(plan, [7, 30, 2, 1], cfg)          # no trail: only break-even sells the bag
        self.assertEqual(sales, [(7, 0.5, "take"), (1, 1.0, "breakeven")])

    def test_decide_exit_is_pure(self):
        plan = plan_for("moon")
        P.mark_step(plan, 1, CFG)
        before = copy.deepcopy(plan)
        for g in (-50, -7, 0, 1, 7, 15, 25, 70, 200):
            P.decide_exit(plan, g, 300, 999, CFG)
        self.assertEqual(plan, before)


class TestStops(unittest.TestCase):
    def test_hard_stop_before_step_one_in_every_tier(self):
        for tier in ("scalp", "swing", "runner", "moon"):
            plan = plan_for(tier)
            self.assertIsNone(P.decide_exit(plan, -6.9, 3, 1, CFG), tier)
            d = P.decide_exit(plan, -7.0, 3, 1, CFG)
            self.assertEqual((d["kind"], d["frac"], d["final"], d["why"], d["step"]), ("stop", 1.0, True, "stop -7%",
                                                                                         None), tier)
        self.assertEqual(P.decide_exit(plan_for("swing"), -4, 0, 1, dict(CFG, stop_pct=-4))["kind"], "stop")
        self.assertEqual(plan_for("runner")["stop_pct"], -7.0)

    def test_break_even_after_step_one(self):
        plan = plan_for("swing")
        self.assertIsNone(P.decide_exit(plan, 1.0, 6, 1, CFG))          # before step 1: +1 % is no exit
        P.mark_step(plan, P.decide_exit(plan, 7, 7, 1, CFG), CFG)
        self.assertEqual(plan["stop_pct"], 1.0)                         # trail 6 % under +7 is +0.58: break-even wins
        self.assertIsNone(P.decide_exit(plan, 1.1, 7, 1, CFG))
        d = P.decide_exit(plan, 1.0, 7, 1, CFG)
        self.assertEqual((d["kind"], d["frac"], d["final"]), ("breakeven", 1.0, True))
        self.assertEqual(d["why"], "break-even stop (peak +7%)")
        # the hard stop no longer applies: a crash sells at "break-even" (the floor), never waits for -7
        self.assertEqual(P.decide_exit(plan, -12, 7, 1, CFG)["kind"], "breakeven")
        self.assertEqual(P.decide_exit(plan, 2.9, 7, 1, dict(CFG, breakeven_pct=3))["kind"], "breakeven")
        self.assertEqual(P.decide_exit(plan, 0.5, 7, 1, dict(CFG, breakeven_pct=0.5))["kind"], "trail")   # +0.58

    def test_trailing_only_after_step_one(self):
        plan = plan_for("runner")
        self.assertIsNone(P.decide_exit(plan, 0.0, 6.9, 1, CFG))         # peak +6.9 then 0: no trail before step 1
        P.mark_step(plan, P.decide_exit(plan, 7, 7, 1, CFG), CFG)
        # peak +30, trail 10 %: the stop is 1.30 x 0.90 = +17 %
        self.assertIsNone(P.decide_exit(plan, 17.05, 30, 1, CFG))
        d = P.decide_exit(plan, 17.0, 30, 1, CFG)
        self.assertEqual((d["kind"], d["frac"], d["final"]), ("trail", 1.0, True))
        self.assertEqual(d["why"], "trailing stop 10% off the top (peak +30%)")
        self.assertEqual(P.stop_now(plan, CFG, 30), 17.0)

    def test_trailing_is_on_price_not_on_gain(self):
        plan = plan_for("runner")
        P.mark_step(plan, 1, CFG)
        P.mark_step(plan, 2, CFG)                                       # +7 and +20 sold; next +60
        self.assertIsNone(P.decide_exit(plan, 39, 50, 1, CFG))           # gain-based would stop at +40
        self.assertIsNone(P.decide_exit(plan, 35.1, 50, 1, CFG))
        self.assertEqual(P.decide_exit(plan, 35, 50, 1, CFG)["kind"], "trail")   # 1.5 x 0.9 = 1.35

    def test_the_plan_peak_counts_too(self):
        plan = plan_for("moon")
        P.mark_step(plan, 2, CFG)                                       # +7 and +20 sold; next +60
        P.track(plan, 50, CFG)
        self.assertEqual(plan["peak_gain"], 50)
        self.assertEqual(plan["stop_pct"], 27.5)                        # 1.5 x 0.85
        self.assertEqual(P.decide_exit(plan, 27, 0, 1, CFG)["kind"], "trail")     # caller's peak is stale
        P.track(plan, 40, CFG)
        self.assertEqual(plan["peak_gain"], 50)

    def test_a_sold_step_is_a_peak_floor(self):
        plan = plan_for("runner")
        P.mark_step(plan, 2, CFG)                                       # sold at +20, peak never tracked
        self.assertEqual(P.stop_now(plan, CFG), 8.0)                    # 1.20 x 0.90
        self.assertIsNone(P.decide_exit(plan, 8.1, 0, 1, CFG))
        self.assertEqual(P.decide_exit(plan, 8.0, 0, 1, CFG)["kind"], "trail")

    def test_walk_through_a_runner_that_turns(self):
        sales, left = walk(plan_for("runner"), [2, 7.5, 12, 26, 31, 22, 18, 17.5])     # stop 1.31 x 0.9: +17.9
        self.assertEqual(sales, [(7.5, 0.34, "take"), (26, 0.5, "take"), (17.5, 1.0, "trail")])

    def test_stop_now(self):
        plan = plan_for("runner")
        self.assertEqual(P.stop_now(plan, CFG), -7.0)
        P.mark_step(plan, 1, CFG)
        self.assertEqual(plan["stop_pct"], 1.0)
        self.assertEqual(P.stop_now(plan, CFG, 30), 17.0)
        self.assertEqual(P.stop_now(None, CFG), -7.0)


class TestTimeStop(unittest.TestCase):
    def test_per_tier_before_step_one(self):
        for tier, minutes in (("scalp", 5), ("swing", 10), ("runner", 15), ("moon", 20)):
            plan = plan_for(tier)
            self.assertIsNone(P.decide_exit(plan, 1.9, 1.9, minutes - 0.01, CFG), tier)
            d = P.decide_exit(plan, 1.9, 4, minutes, CFG)
            self.assertEqual((d["kind"], d["frac"], d["final"]), ("time", 1.0, True), tier)
            self.assertEqual(d["why"], "%d min, only +2%%" % minutes)
            self.assertIsNone(P.decide_exit(plan, 2.0, 4, minutes * 9, CFG), tier)   # +2 % keeps it
        self.assertEqual(P.decide_exit(plan_for("swing"), -3, 0, 10, CFG)["why"], "10 min, only -3%")

    def test_not_after_step_one(self):
        plan = plan_for("swing")
        P.mark_step(plan, P.decide_exit(plan, 7, 7, 1, CFG), CFG)
        self.assertIsNone(P.decide_exit(plan, 1.5, 7, 1000, CFG))

    def test_zero_means_no_time_stop(self):
        cfg = dict(CFG, hold_tiers=[{"name": "t", "min_score": 0, "ladder": [[7, 1]], "time_stop_min": 0}])
        self.assertIsNone(P.decide_exit(P.new_plan(0, [], cfg=cfg), 0, 0, 10 ** 6, cfg))


class TestReplan(unittest.TestCase):
    def test_same_tier_updates_the_score_only(self):
        plan = P.new_plan(3, ["X", "Telegram", "web"], cfg=CFG)
        plan2, note = P.replan(plan, 4, ["X", "Telegram", "web", "boosts 3"], 5, CFG)
        self.assertIs(plan2, plan)
        self.assertIsNone(note)
        self.assertEqual((plan["tier"], plan["score"], plan["entry_score"]), ("swing", 4, 3))
        self.assertEqual(plan["parts"][-1], "boosts 3")

    def test_upgrade_after_step_one_keeps_the_sold_step(self):
        plan = P.new_plan(3, ["X", "Telegram", "web"], cfg=CFG)
        P.mark_step(plan, P.decide_exit(plan, 8, 8, 1, CFG), CFG)          # swing sold 50 % at +7
        plan, note = P.replan(plan, 5, ["X", "Telegram", "web", "trending"], 9, CFG)
        self.assertEqual(note, "upgrade swing → runner (trending)")
        self.assertEqual((plan["tier"], plan["step"], plan["trail_pct"], plan["time_stop_min"]), ("runner", 1, 10, 15))
        self.assertEqual(plan["ladder"], [[7, 0.34], [20, 0.5], [60, 1.0]])
        self.assertIsNone(P.decide_exit(plan, 9, 9, 1, CFG))                # +7 is not sold again
        d = P.decide_exit(plan, 20, 20, 1, CFG)
        self.assertEqual((d["frac"], d["final"]), (0.5, False))
        P.mark_step(plan, d, CFG)
        self.assertEqual(P.decide_exit(plan, 60, 60, 1, CFG)["frac"], 1.0)
        self.assertEqual(P.labels(plan), ["tier:swing", "news:3-4"])       # the labels stay those of the entry
        self.assertEqual((plan["entry_tier"], plan["entry_score"]), ("swing", 3))

    def test_upgrade_before_step_one(self):
        plan = P.new_plan(1, ["X"], cfg=CFG)
        plan, note = P.replan(plan, 7.5, ["X", "Telegram", "web", "boosts 60", "trending", "buys 70%"], 4, CFG)
        self.assertEqual(note, "upgrade scalp → moon (Telegram, web, boosts 60, trending, buys 70%)")
        self.assertEqual((plan["tier"], plan["step"], plan["time_stop_min"]), ("moon", 0, 20))
        self.assertEqual(P.decide_exit(plan, 7, 7, 1, CFG)["frac"], 0.25)
        self.assertEqual(P.decide_exit(plan, 1, 4, 12, CFG), None)          # moon waits 20 min, not scalp's 5

    def test_upgrade_by_a_part_that_moved(self):
        plan = P.new_plan(4, ["X", "Telegram", "web", "boosts 12"], cfg=CFG)
        plan, note = P.replan(plan, 5, ["X", "Telegram", "web", "boosts 62"], 3, CFG)
        self.assertEqual(note, "upgrade swing → runner (boosts 62)")

    def test_downgrade_past_the_next_step_sells_now(self):
        plan = P.new_plan(5, ["X", "Telegram", "boosts 60"], cfg=CFG)
        P.mark_step(plan, P.decide_exit(plan, 7, 7, 1, CFG), CFG)          # runner sold 34 % at +7
        plan, note = P.replan(plan, 3, ["X", "Telegram", "buys 70%"], 25, CFG)
        self.assertEqual(note, "downgrade runner → swing (boosts gone) · +25% is past +20%: sell now")
        self.assertEqual((plan["tier"], plan["step"], plan["trail_pct"]), ("swing", 1, 6))
        d = P.decide_exit(plan, 25, 25, 1, CFG)
        self.assertEqual((d["kind"], d["frac"], d["final"]), ("take", 1.0, True))

    def test_downgrade_not_yet_past(self):
        plan = P.new_plan(5, ["X", "Telegram", "trending"], cfg=CFG)
        P.mark_step(plan, 1, CFG)
        plan, note = P.replan(plan, 3, ["X", "Telegram", "web"], 10, CFG)
        self.assertEqual(note, "downgrade runner → swing (no longer trending)")
        self.assertIsNone(P.decide_exit(plan, 10, 10, 1, CFG))
        self.assertEqual(P.decide_exit(plan, 20, 20, 1, CFG)["frac"], 1.0)

    def test_downgrade_past_a_whole_ladder(self):
        plan = P.new_plan(7, ["a"], cfg=CFG)
        P.mark_step(plan, 2, CFG)                                           # moon sold at +7 and +20
        plan, note = P.replan(plan, 3, ["b"], 30, CFG)
        self.assertTrue(note.endswith("+30% is past +20%: sell now"), note)
        self.assertEqual(plan["step"], 1)                                   # swing's +20 sells the rest
        self.assertEqual(P.decide_exit(plan, 30, 30, 1, CFG)["frac"], 1.0)
        plan = P.new_plan(7, ["a"], cfg=CFG)
        P.mark_step(plan, 2, CFG)
        plan, note = P.replan(plan, 3, ["a"], 15, CFG)
        self.assertNotIn("sell now", note)
        self.assertEqual(plan["stop_pct"], 12.8)                            # swing's 6 % trail under the +20 peak
        self.assertIsNone(P.decide_exit(plan, 15, 20, 1, CFG))
        self.assertEqual(P.decide_exit(plan, 12.8, 20, 1, CFG)["kind"], "trail")

    def test_downgrade_to_scalp_keeps_break_even(self):
        plan = P.new_plan(5, ["X", "Telegram", "trending"], cfg=CFG)
        P.mark_step(plan, 1, CFG)                                           # sold 34 % at +7
        plan, note = P.replan(plan, 1, ["X"], 3, CFG)
        self.assertEqual((plan["tier"], plan["step"]), ("scalp", 0))
        self.assertNotIn("sell now", note)
        self.assertEqual(plan["stop_pct"], 1.0)                             # a winner never turns into a loser
        self.assertIsNone(P.decide_exit(plan, 3, 7, 1000, CFG))             # no time stop any more
        self.assertEqual(P.decide_exit(plan, -3, 7, 1, CFG)["kind"], "breakeven")
        self.assertEqual(P.decide_exit(plan, 7, 7, 1, CFG)["frac"], 1.0)

    def test_unknown_score_keeps_the_plan(self):
        plan = P.new_plan(5, ["X"], cfg=CFG)
        before = copy.deepcopy(plan)
        for g in (None, float("nan"), "abc", [], {}):
            self.assertEqual(P.replan(plan, g, ["Y"], 3, CFG), (plan, None))
        self.assertEqual(plan, before)

    def test_no_plan_yet(self):
        plan, note = P.replan(None, 5, ["X"], 3, CFG)
        self.assertIsNone(note)
        self.assertEqual((plan["tier"], plan["step"]), ("runner", 0))
        self.assertEqual(P.replan("junk", None, None, None, None)[0]["tier"], "scalp")

    def test_old_config_never_changes_tier(self):
        old = {"tp1_pct": 50.0, "tp1_sell_frac": 0.5, "trail_pct": 20.0, "time_stop_min": 45}
        plan = P.new_plan(0, [], cfg=old)
        self.assertEqual(P.replan(plan, 10, ["X"], 5, old)[1], None)

    def test_a_tier_gone_from_the_config(self):
        plan = P.new_plan(2, ["X"], cfg=CFG)
        plan["tier"] = "retired"
        self.assertTrue(P.replan(plan, 7, ["X", "trending"], 1, CFG)[1].startswith("upgrade retired → moon"))


class TestFading(unittest.TestCase):
    ENTRY = {"twitter": "https://x.com/m", "telegram": "t.me/m", "trending": True, "buys": 14, "sells": 4}

    def test_fading(self):
        now = {"twitter": "https://x.com/m", "telegram": "t.me/m", "buys": 8, "sells": 12}     # 40 %, no trend
        self.assertEqual(P.news_score(self.ENTRY, CFG)[0], 5)
        self.assertEqual(P.news_score(now, CFG)[0], 2)
        self.assertTrue(P.fading(self.ENTRY, now, CFG))
        d = P.fade_exit(self.ENTRY, now, 5.0, CFG)
        self.assertEqual((d["kind"], d["frac"], d["final"]), ("fade", 1.0, True))
        self.assertEqual(d["why"], "fading: buys 40%, no longer trending → out")
        self.assertTrue(P.fading(5, now, CFG))                              # the plan's entry_score will do
        self.assertEqual(P.fade_exit(5, now, 5, CFG)["why"], "fading: buys 40% → out")

    def test_only_in_profit(self):
        now = {"buys": 8, "sells": 12}
        self.assertIsNone(P.fade_exit(self.ENTRY, now, -2.0, CFG))
        self.assertIsNone(P.fade_exit(self.ENTRY, now, 0.0, CFG))
        self.assertIsNotNone(P.fade_exit(self.ENTRY, now, 0.1, CFG))
        self.assertIsNone(P.fade_exit(self.ENTRY, now, 3.0, dict(CFG, fade_min_gain_pct=3.0)))

    def test_needs_both_few_buys_and_a_score_drop(self):
        tw = {"twitter": "https://x.com/m", "telegram": "t.me/m"}
        self.assertFalse(P.fading(self.ENTRY, dict(tw, buys=9, sells=11), CFG))               # 45 %: not under
        self.assertTrue(P.fading(self.ENTRY, dict(tw, buys=8.9, sells=11.1), CFG))
        self.assertFalse(P.fading(self.ENTRY, dict(tw, trending=True, buys=8, sells=12), CFG))  # fell by 1
        self.assertFalse(P.fading(self.ENTRY, dict(tw, buys=1, sells=2), CFG))                # too few trades
        self.assertFalse(P.fading(self.ENTRY, dict(tw, buy_ratio=0.4, sells=99), dict(CFG, fade_buy_ratio=0.3)))
        self.assertTrue(P.fading(self.ENTRY, dict(tw, buy_ratio=0.4), CFG))

    def test_garbage(self):
        for g in garbage():
            self.assertFalse(P.fading(g, g, g))
            self.assertIsNone(P.fade_exit(g, g, g, g))
            P.fading(self.ENTRY, {"buys": g, "sells": g, "buy_ratio": g}, CFG)


class TestGarbage(unittest.TestCase):
    def test_decide_exit(self):
        plan = plan_for("runner")
        for g in garbage():
            self.assertIsNone(P.decide_exit(g, 10, 10, 1, CFG))
            P.decide_exit(plan, 3, g, g, g)
            P.decide_exit(plan, g, 30, 99, CFG)
        for g in (None, float("nan"), float("inf"), "abc", [], 10 ** 400):
            self.assertIsNone(P.decide_exit(plan, g, 10, 99, CFG), g)
        self.assertIsNone(P.decide_exit(plan, 1, 1, float("nan"), CFG))     # no time, no time stop
        self.assertIsNone(P.decide_exit(plan, 1, 1, -50, CFG))
        self.assertIsNone(P.decide_exit(plan, 3, float("nan"), 1, CFG))     # no peak: the gain is the peak

    def test_a_broken_plan(self):
        plan = {"tier": 5, "ladder": "junk", "step": float("nan"), "trail_pct": "a", "time_stop_min": -4,
                "peak_gain": float("inf"), "done_gain": [], "score": {}, "parts": [1, None, "X"]}
        for g in (-10, 0, 10, 100):
            P.decide_exit(plan, g, g, 99, CFG)
        self.assertEqual(P.decide_exit(plan, -10, 0, 99, CFG)["kind"], "stop")
        P.track(plan, 5, CFG)
        P.mark_step(plan, None, CFG)
        P.replan(plan, 6, ["X"], 5, CFG)
        self.assertEqual(plan["tier"], "runner")
        P.plan_text(plan)
        P.labels(plan)
        P.panel_plan(plan)
        json.dumps(plan, allow_nan=False)

    def test_new_plan(self):
        for g in garbage():
            plan = P.new_plan(g, g, g, g)
            self.assertIn(plan["tier"], ("scalp", "swing", "runner", "moon"))
            self.assertTrue(0 <= plan["score"] <= 10)
            json.dumps(plan, allow_nan=False)
        self.assertEqual(P.new_plan(float("nan"), None)["tier"], "scalp")
        self.assertEqual(P.new_plan(-4, None)["score"], 0)
        self.assertEqual(P.new_plan(25, None)["score"], 10)

    def test_mark_step_and_track(self):
        self.assertIsNone(P.mark_step(None))
        self.assertIsNone(P.track(None, 5))
        plan = plan_for("runner")
        P.mark_step(plan, {"step": None, "kind": "stop"}, CFG)              # a stop marks nothing
        self.assertEqual(plan["step"], 0)
        P.mark_step(plan, "x", CFG)                                         # garbage: one step
        self.assertEqual(plan["step"], 1)
        P.mark_step(plan, 0, CFG)                                           # never goes back
        self.assertEqual(plan["step"], 1)
        P.mark_step(plan, 99, CFG)                                          # never past the ladder
        self.assertEqual((plan["step"], plan["sold"], plan["done_gain"]), (3, 3, 60))
        before = copy.deepcopy(plan)
        for g in garbage():
            if not isinstance(g, (int, float)) or g != g or abs(g) == float("inf") or g is True or g is False:
                P.track(plan, g, CFG)
        self.assertEqual(plan, before)

    def test_labels_are_the_entry_pair(self):
        for score, want in ((0, ["tier:scalp", "news:0-2"]), (3, ["tier:swing", "news:3-4"]),
                            (5.5, ["tier:runner", "news:5-6"]), (9, ["tier:moon", "news:7+"])):
            plan = P.new_plan(score, [], cfg=CFG)
            self.assertEqual(P.labels(plan), want)
            for later in (0, 10, 4, 6):                                     # replans never change them
                P.replan(plan, later, ["X"], 3, CFG)
                self.assertEqual(P.labels(plan), want, (score, later))
        old = {"tier": "swing", "score": 4}                                 # a plan from before entry_tier
        self.assertEqual(P.labels(old), ["tier:swing", "news:3-4"])
        self.assertEqual(P.labels({"tier": float("nan"), "entry_tier": ["x"], "score": 9}), ["tier:?", "news:7+"])

    def test_take_text_garbage(self):
        for g in garbage():
            self.assertIsInstance(P.take_text({"frac": g, "steps": g, "why": g, "after": g}), str)
        self.assertEqual(P.take_text({"frac": 0.5, "why": 7, "after": True}), "TP · sold 50%")

    def test_panel_tier_is_text(self):
        plan = plan_for("runner")
        for g in garbage():
            plan["tier"] = g
            doc = P.panel_plan(plan)
            json.dumps(doc, allow_nan=False)
            self.assertIsInstance(doc["tier"], str)
        plan["tier"] = "runner"
        self.assertEqual(P.panel_plan(plan)["tier"], "runner")

    def test_texts(self):
        self.assertEqual(P.plan_text(None), "hold plan · none")
        self.assertEqual(P.plan_text(P.new_plan(0, [], cfg=CFG)), "hold plan · 0/10 (no news) · scalp: +7")
        self.assertEqual(P.plan_text(P.new_plan(4.5, ["X post", "Telegram", "web", "boosts 3"], cfg=CFG)),
                         "hold plan · 4.5/10 (X post, Telegram, web, boosts 3) · swing: +7 → +20")
        self.assertEqual(P.labels(None), [])
        self.assertIsNone(P.panel_plan(None))

    def test_panel_and_json(self):
        plan = plan_for("moon")
        walk(plan, [7, 12, 25, 30])
        doc = P.panel_plan(plan)
        self.assertEqual(sorted(doc), ["ladder", "parts", "score", "step", "stop_pct", "tier", "trail_pct"])
        self.assertEqual((doc["tier"], doc["step"], doc["trail_pct"]), ("moon", 2, 15))
        self.assertEqual(doc["stop_pct"], 10.5)                             # 1.30 x 0.85
        json.dumps(plan, allow_nan=False)
        self.assertEqual(json.loads(json.dumps(plan)), plan)


if __name__ == "__main__":
    unittest.main()
