"""memebot/learner.py tests, offline and pure: the buckets, the shrunk score and its min_n gate,
the skip and explore cycle, the size multiplier, garbage input and the memory file."""

import datetime
import json
import os
import random
import shutil
import tempfile
import unittest

from _load import load

lr = load("memebot", "learner")

# what bot.pair_facts() returns for a fresh, thin pumpswap coin with an X account
FX = {"mint": "m1", "symbol": "MEME", "liquidity": 12000.0, "vol5": 3000.0, "buy_ratio": 0.6, "age_min": 12.0,
      "dex": "pumpswap", "x": "https://x.com/meme", "telegram": "", "web": False, "boosts": 0, "spike": 2.4,
      "mcap": 90000.0}


def garbage():
    """Broken inputs, new each call: update() and decide() repair a dict they are given in place."""
    return [None, float("nan"), float("inf"), float("-inf"), "", "abc", [], {}, object(), -1, 10 ** 30, 10 ** 400,
            True, ("x",), {"n": 3}]


def taught(*runs):
    """A memory that learned: runs of (label, trades, pnl % each)."""
    mem = lr.fresh()
    for label, n, pnl in runs:
        for _ in range(n):
            lr.update(mem, [label], pnl)
    return mem


class Features(unittest.TestCase):
    def test_a_candidate_gets_one_label_per_family_and_one_per_signal(self):
        got = lr.features(FX, 450, 0.4, ["trending", "volume 2.3x", "holders +7%"], 23)
        self.assertEqual(got, ["liq:<20k", "vol5:<5k", "buys:<65", "age:<30m", "holders:300-1k", "dex:pumpswap",
                               "social:x", "sig:trending", "sig:volume", "sig:holders", "slip:<1", "hour:23-01"])

    def test_the_bucket_edges(self):
        def one(key, v):
            return lr.features({key: v}, None, None, None, None)

        self.assertEqual([one("liquidity", v)[0] for v in (8000, 19999.99, 20000, 49999, 50000, 2e6)],
                         ["liq:<20k", "liq:<20k", "liq:20-50k", "liq:20-50k", "liq:50k+", "liq:50k+"])
        self.assertEqual([one("vol5", v)[0] for v in (2000, 5000, 19999, 20000)],
                         ["vol5:<5k", "vol5:5-20k", "vol5:5-20k", "vol5:20k+"])
        self.assertEqual([one("buy_ratio", v)[0] for v in (0.0, 0.5499, 0.65, 0.7999, 0.8, 1.0, 72)],
                         ["buys:<65", "buys:<65", "buys:65-80", "buys:65-80", "buys:80+", "buys:80+", "buys:65-80"])
        self.assertEqual([one("age_min", v)[0] for v in (0, 29.9, 30, 119, 120, 700)],
                         ["age:<30m", "age:<30m", "age:30m-2h", "age:30m-2h", "age:2h+", "age:2h+"])
        holders = [lr.features(None, v, None, None, None) for v in (0, -5, 100, 299, 300, 999, 1000)]
        self.assertEqual(holders, [[], [], ["holders:<300"], ["holders:<300"], ["holders:300-1k"],
                                   ["holders:300-1k"], ["holders:1k+"]])          # 0 holders: no count, no label
        self.assertEqual([lr.features(None, None, v, None, None) for v in (-0.1, 0.99, 1, 1.99, 2, 9)],
                         [["slip:<1"], ["slip:<1"], ["slip:1-2"], ["slip:1-2"], ["slip:2+"], ["slip:2+"]])

    def test_the_hours_in_two_hour_blocks_and_the_day_apart(self):
        def h(x):
            return lr.features(None, None, None, None, x)

        self.assertEqual([h(x)[0] for x in (23, 0, 1, 2, 3, 4, 5, 6, 7, 12, 22)],
                         ["hour:23-01"] * 2 + ["hour:01-03"] * 2 + ["hour:03-05"] * 2 + ["hour:05-07"] * 2
                         + ["hour:day"] * 3)
        self.assertEqual((h(24), h(-1), h(2.9), h("3")), (["hour:23-01"], ["hour:23-01"], ["hour:01-03"],
                                                         ["hour:03-05"]))
        self.assertEqual(h(datetime.datetime(2026, 11, 26, 4, 30)), ["hour:03-05"])
        self.assertEqual((h(None), h("noon"), h(float("nan"))), ([], [], []))

    def test_the_dex_and_the_socials(self):
        def dex(d):
            return lr.features({"dex": d}, None, None, None, None)

        self.assertEqual([dex(d) for d in ("pumpfun", "PumpSwap", "raydium", "meteora", "meteoradbc", "orca")],
                         [["dex:pumpfun"], ["dex:pumpswap"], ["dex:raydium"], ["dex:meteora"], ["dex:meteora"],
                          ["dex:other"]])
        self.assertEqual((dex(None), dex(""), dex(7)), ([], [], []))

        def social(**kw):
            return lr.features(kw, None, None, None, None)

        self.assertEqual(social(x="https://x.com/c", telegram="t.me/c", web=True), ["social:x"])
        self.assertEqual(social(x="", telegram="t.me/c", web=False), ["social:no-x"])
        self.assertEqual(social(x="", telegram="", web=True), ["social:no-x"])
        self.assertEqual(social(x="  ", telegram=None, web=False), ["social:none"])
        self.assertEqual(social(x=float("nan")), ["social:none"])                  # NaN is not a link
        self.assertEqual(social(), [])                                             # nothing known: no label

    def test_signals_from_a_list_or_a_reason_text(self):
        def sig(s):
            return lr.features(None, None, None, s, None)

        self.assertEqual(sig(["volume 6.0x", "boosted", "trending", "trending", "buys 80%", "smart money", 5]),
                         ["sig:trending", "sig:boosted", "sig:volume"])               # fixed order, each once
        self.assertEqual(sig("buys 80% · volume 6.0x · holders +20%"), ["sig:volume", "sig:holders"])
        self.assertEqual(sig({"Trending"}), ["sig:trending"])
        self.assertEqual((sig(None), sig(42), sig([])), ([], [], []))

    def test_garbage_never_raises_and_only_known_labels_come_out(self):
        keys = ("liquidity", "vol5", "buy_ratio", "age_min", "dex", "x", "telegram", "web")
        for g in garbage():
            for out in (lr.features(g, g, g, g, g), lr.features({k: g for k in keys}, g, g, g, g),
                        lr.features(dict(FX, liquidity=g), g, 0.5, g, 1)):
                self.assertTrue(set(out) <= lr.KNOWN, out)
        self.assertEqual(lr.features(None, None, None, None, None), [])
        self.assertEqual(lr.features(None, None, None, None, 23), ["hour:23-01"])        # the backfill case

    def test_the_labels_never_change(self):
        # memory.json keeps these for months: renaming one silently forgets what it learned
        self.assertEqual(lr.KNOWN, frozenset([
            "liq:<20k", "liq:20-50k", "liq:50k+", "vol5:<5k", "vol5:5-20k", "vol5:20k+",
            "buys:<65", "buys:65-80", "buys:80+", "age:<30m", "age:30m-2h", "age:2h+",
            "holders:<300", "holders:300-1k", "holders:1k+", "slip:<1", "slip:1-2", "slip:2+",
            "dex:pumpfun", "dex:pumpswap", "dex:raydium", "dex:meteora", "dex:other",
            "sig:trending", "sig:boosted", "sig:volume", "sig:holders",
            "social:x", "social:no-x", "social:none",
            "hour:23-01", "hour:01-03", "hour:03-05", "hour:05-07", "hour:day"]))
        self.assertTrue(all(len(lab) <= 16 and lab.count(":") == 1 for lab in lr.KNOWN))


class Learning(unittest.TestCase):
    def test_update_keeps_n_sum_sumsq_and_wins(self):
        mem = lr.fresh()
        lr.update(mem, ["liq:<20k", "liq:<20k", "bogus", "liq:1m", 5, None], -12.5)
        lr.update(mem, ["liq:<20k"], 20)
        self.assertEqual(mem["labels"], {"liq:<20k": {"n": 2, "sum": 7.5, "sumsq": 556.25, "wins": 1}})
        self.assertEqual(mem["samples"], 2)
        lr.update(mem, "hour:day", 3)                                           # one label as a text
        self.assertEqual(mem["labels"]["hour:day"]["n"], 1)

    def test_nothing_to_learn_is_a_no_op(self):
        mem = lr.fresh()
        for pnl in (None, float("nan"), float("inf"), "abc", True, [], 10 ** 400):
            lr.update(mem, ["liq:<20k"], pnl)
        lr.update(mem, [], -10)
        lr.update(mem, ["nope:x"], -10)
        lr.update(mem, None, -10)
        self.assertEqual(mem, lr.fresh())
        for g in garbage():
            lr.update(g, ["liq:<20k"], -10)                                     # not a memory: never raises

    def test_one_moon_shot_is_capped(self):
        mem = taught(("age:<30m", 1, 900), ("age:<30m", 1, -250))
        self.assertEqual(mem["labels"]["age:<30m"]["sum"], 0)                   # +100 and -100

    def test_a_broken_memory_is_repaired_in_place(self):
        mem = {"labels": [1, 2], "samples": "x", "skip_run": float("nan")}
        lr.update(mem, ["vol5:<5k"], -4)
        self.assertEqual((mem["samples"], mem["skip_run"], mem["v"], mem["labels"]["vol5:<5k"]["n"]), (1, 0, 1, 1))
        mem["labels"]["vol5:<5k"] = {"n": "?", "sum": None}                       # broken stats start over
        lr.update(mem, ["vol5:<5k"], 6)
        self.assertEqual(mem["labels"]["vol5:<5k"], {"n": 1, "sum": 6, "sumsq": 36, "wins": 1})

    def test_old_trades_fade_but_the_mean_stays(self):
        mem = taught(("hour:day", 400, -5))
        st = mem["labels"]["hour:day"]
        self.assertAlmostEqual(st["n"], 300)
        self.assertAlmostEqual(st["sum"] / st["n"], -5.0)
        lr.update(mem, ["hour:day"], 95)
        st = mem["labels"]["hour:day"]
        self.assertAlmostEqual(st["n"], 300)
        self.assertAlmostEqual(st["sum"] / st["n"], (-5 * 300 + 95) / 301.0, places=3)

    def test_the_memory_is_bounded(self):
        rnd, mem = random.Random(7), lr.fresh()
        for i in range(3000):
            fx = {"liquidity": rnd.uniform(0, 1e6), "vol5": rnd.uniform(0, 1e5), "buy_ratio": rnd.random(),
                  "age_min": rnd.uniform(0, 900), "dex": rnd.choice(["pumpswap", "orca", "x%d" % i]),
                  "x": rnd.choice(["", "x.com/a"]), "telegram": "", "web": rnd.random() < 0.5}
            labels = lr.features(fx, rnd.randint(0, 5000), rnd.uniform(0, 5), ["trending", "volume 9x"],
                                 rnd.randint(0, 23)) + ["coin:%d" % i, "liq:%d" % i, "sig:%d" % i]
            lr.update(mem, labels, rnd.uniform(-30, 30))
        self.assertTrue(set(mem["labels"]) <= lr.KNOWN)
        self.assertLessEqual(len(json.dumps(mem)), 6000)
        self.assertEqual(mem["samples"], 3000)


class Score(unittest.TestCase):
    def test_the_shrunk_mean_sum_over_n_plus_k(self):
        mem = taught(("liq:<20k", 8, -10))
        s, why = lr.score(mem, ["liq:<20k"])
        self.assertAlmostEqual(s, -80 / 18.0)                                   # -4.44, not -10
        self.assertEqual(why, "liq:<20k -10.0% over 8")
        mem = taught(("liq:<20k", 8, -10), ("age:<30m", 10, 5))
        self.assertAlmostEqual(lr.score(mem, ["liq:<20k", "age:<30m"])[0], (-80 / 18.0 + 50 / 20.0) / 2)
        self.assertAlmostEqual(lr.score(mem, ["liq:<20k", "age:<30m"], k=0)[0], (-10 + 5) / 2.0)
        self.assertAlmostEqual(lr.score(taught(("liq:<20k", 40, -10)), ["liq:<20k"])[0], -8.0)
        self.assertAlmostEqual(lr.score(mem, ["liq:<20k", "liq:<20k", "bogus"])[0], -80 / 18.0)   # once each

    def test_the_min_n_gate(self):
        mem = taught(("liq:<20k", 7, -10))
        self.assertEqual(lr.score(mem, ["liq:<20k"]), (0.0, ""))
        self.assertAlmostEqual(lr.score(mem, ["liq:<20k"], min_n=7)[0], -70 / 17.0)
        lr.update(mem, ["liq:<20k"], -10)
        self.assertAlmostEqual(lr.score(mem, ["liq:<20k"])[0], -80 / 18.0)
        mem = taught(("liq:<20k", 8, -10), ("age:<30m", 3, -50))                  # age has only 3: left out
        self.assertAlmostEqual(lr.score(mem, ["liq:<20k", "age:<30m"])[0], -80 / 18.0)
        self.assertEqual(lr.score(mem, ["age:<30m", "vol5:<5k"]), (0.0, ""))

    def test_why_names_the_two_worst_of_a_bad_score_and_the_two_best_of_a_good_one(self):
        mem = taught(("liq:<20k", 8, -20), ("age:<30m", 8, -10), ("sig:trending", 8, 5))
        s, why = lr.score(mem, ["sig:trending", "age:<30m", "liq:<20k"])
        self.assertLess(s, 0)
        self.assertEqual(why, "liq:<20k -20.0% over 8, age:<30m -10.0% over 8")
        mem = taught(("liq:<20k", 8, 20), ("age:<30m", 8, 10), ("sig:trending", 8, -5))
        s, why = lr.score(mem, ["sig:trending", "age:<30m", "liq:<20k"])
        self.assertGreater(s, 0)
        self.assertEqual(why, "liq:<20k +20.0% over 8, age:<30m +10.0% over 8")

    def test_garbage_scores_zero_and_never_raises(self):
        mem = taught(("liq:<20k", 8, -10))
        for g in garbage():
            self.assertEqual(lr.score(g, ["liq:<20k"]), (0.0, ""))
            self.assertIsInstance(lr.score(mem, g)[0], float)
            self.assertIsInstance(lr.score(mem, ["liq:<20k"], k=g, min_n=g)[0], float)   # defaults
        self.assertAlmostEqual(lr.score(mem, ["liq:<20k"], k=-5, min_n=0)[0], -10.0)     # k >= 0, min_n >= 1
        mem["labels"]["age:<30m"] = {"n": float("nan"), "sum": 5}
        mem["labels"]["vol5:<5k"] = "broken"
        self.assertAlmostEqual(lr.score(mem, ["liq:<20k", "age:<30m", "vol5:<5k"])[0], -80 / 18.0)


class Decide(unittest.TestCase):
    def test_no_data_buys_at_full_size(self):
        d = lr.decide(lr.fresh(), lr.features(FX, 450, 0.4, ["trending"], 23), {})
        self.assertEqual(d, {"action": "buy", "mult": 1.0, "score": 0.0, "why": "no bucket has 8 trades yet"})
        for g in garbage():
            self.assertEqual(lr.decide(g, ["liq:<20k"], g)["action"], "buy")      # never raises

    def test_the_size_multiplier_and_its_clamp(self):
        def buy(pnl, **cfg):
            return lr.decide(taught(("liq:<20k", 10, pnl)), ["liq:<20k"], cfg)

        d = buy(8)                                                              # 80 / 20 = +4: x1.2
        self.assertEqual((d["action"], d["mult"], d["score"]), ("buy", 1.2, 4.0))
        self.assertEqual(d["why"], "score +4.0% · size x1.20 · liq:<20k +8.0% over 10")
        self.assertEqual(buy(100)["mult"], 1.5)                                  # +50: clamped
        self.assertEqual((buy(-10)["action"], buy(-10)["mult"]), ("buy", 0.75))   # -5 is over -6: smaller
        self.assertEqual(buy(-10, learn_mult_min=0.8)["mult"], 0.8)
        self.assertEqual(buy(8, learn_mult_max=1.1)["mult"], 1.1)
        self.assertEqual(buy(100, learn_mult_min=1.5, learn_mult_max=0.5)["mult"], 1.5)    # swapped: sorted
        self.assertEqual(buy(-10, learn_mult_min=-3)["mult"], 0.75)
        self.assertEqual(buy(-10, learn_skip_pct=-4)["action"], "skip")

    def test_skip_and_every_fifth_skip_explores_at_half_size(self):
        mem = taught(("liq:<20k", 10, -20))                                    # -200 / 20 = -10
        ds = [lr.decide(mem, ["liq:<20k"], {}) for _ in range(12)]
        self.assertEqual([d["action"] for d in ds], ["skip"] * 4 + ["explore"] + ["skip"] * 4 + ["explore"] + ["skip"] * 2)
        self.assertEqual((mem["skipped"], mem["explored"], mem["skip_run"]), (10, 2, 2))
        self.assertEqual(ds[0], {"action": "skip", "mult": 0.0, "score": -10.0,
                                 "why": "past trades like this lost avg 10.0% · liq:<20k -20.0% over 10"})
        self.assertEqual((ds[4]["mult"], ds[4]["score"]), (0.5, -10.0))
        self.assertEqual(ds[4]["why"], "1 in 5 skips is bought at half size · past trades like this lost avg 10.0%"
                                       " · liq:<20k -20.0% over 10")

    def test_buys_in_between_do_not_reset_the_skip_count(self):
        mem = taught(("liq:<20k", 10, -20), ("liq:50k+", 10, 10))
        acts = []
        for _ in range(5):
            acts.append(lr.decide(mem, ["liq:<20k"], {})["action"])
            acts.append(lr.decide(mem, ["liq:50k+"], {})["action"])
        self.assertEqual(acts, ["skip", "buy"] * 4 + ["explore", "buy"])

    def test_the_skip_threshold_and_the_explore_setting(self):
        mem = taught(("liq:<20k", 10, -12))                                    # exactly -6: skipped
        self.assertEqual(lr.decide(mem, ["liq:<20k"], {})["action"], "skip")
        never = [lr.decide(mem, ["liq:<20k"], {"learn_explore_every": 0})["action"] for _ in range(12)]
        self.assertEqual(set(never), {"skip"})
        self.assertEqual(lr.decide(mem, ["liq:<20k"], {"learn_explore_every": 1})["action"], "explore")
        self.assertEqual(lr.decide(mem, ["liq:<20k"], {"learn_skip_pct": float("nan"), "learn_explore_every": "x",
                                                       "learn_k": None, "learn_min_n": "8"})["action"], "skip")
        mem = lr.fresh()                                                       # no data: a threshold of 0 never skips
        self.assertEqual(lr.decide(mem, ["liq:<20k"], {"learn_skip_pct": 0})["action"], "buy")

    def test_the_gate_comes_from_the_config_and_the_summary_shows_it(self):
        mem = taught(("liq:<20k", 3, -30))
        d = lr.decide(mem, ["liq:<20k"], {"learn_min_n": 3, "learn_k": 2})
        self.assertEqual((d["action"], d["score"]), ("skip", -18.0))           # -90 / (3 + 2)
        self.assertEqual((mem["k"], mem["min_n"]), (2, 3))
        s = lr.summary(mem)
        self.assertEqual((s["min_samples"], s["best"]), (3, [{"label": "liq:<20k", "n": 3, "avg": -30.0}]))
        self.assertEqual(lr.summary(mem, min_n=8)["best"], [])


class Summary(unittest.TestCase):
    def test_a_fresh_memory(self):
        self.assertEqual(lr.summary(lr.fresh()), {"samples": 0, "skipped": 0, "explored": 0, "min_samples": 8,
                                                  "best": [], "worst": []})
        for g in garbage():
            self.assertEqual(lr.summary(g)["best"], [])

    def test_best_and_worst_three_by_shrunk_mean(self):
        mem = taught(("liq:50k+", 8, 12), ("vol5:20k+", 8, 6), ("buys:80+", 8, 3), ("age:2h+", 8, 0),
                     ("dex:raydium", 8, -2), ("social:none", 8, -8), ("age:<30m", 8, -15),
                     ("liq:<20k", 7, -50))                                       # 7 trades: not shown
        s = lr.summary(mem)
        self.assertEqual(s["samples"], 63)
        self.assertEqual(s["best"], [{"label": "liq:50k+", "n": 8, "avg": 12.0}, {"label": "vol5:20k+", "n": 8, "avg": 6.0},
                                     {"label": "buys:80+", "n": 8, "avg": 3.0}])
        self.assertEqual(s["worst"], [{"label": "age:<30m", "n": 8, "avg": -15.0},
                                      {"label": "social:none", "n": 8, "avg": -8.0},
                                      {"label": "dex:raydium", "n": 8, "avg": -2.0}])

    def test_the_ranking_is_by_shrunk_mean_not_the_plain_mean(self):
        mem = taught(("age:<30m", 8, -20), ("liq:<20k", 40, -12), ("vol5:20k+", 8, 1), ("buys:80+", 8, 2))
        worst = lr.summary(mem)["worst"]
        self.assertEqual([w["label"] for w in worst], ["liq:<20k", "age:<30m"])  # -9.6 is worse than -8.9
        self.assertEqual(worst[0], {"label": "liq:<20k", "n": 40, "avg": -12.0})

    def test_best_and_worst_never_show_the_same_label(self):
        mem = taught(("age:<30m", 8, -3), ("liq:<20k", 8, 4))
        s = lr.summary(mem)
        self.assertEqual(([b["label"] for b in s["best"]], [w["label"] for w in s["worst"]]), (["liq:<20k"], ["age:<30m"]))
        s = lr.summary(taught(("age:<30m", 9, -3)))
        self.assertEqual((len(s["best"]), s["worst"]), (1, []))
        five = ["liq:<20k", "age:<30m", "vol5:<5k", "buys:<65", "hour:day"]
        s = lr.summary(taught(*[(lab, 8, i) for i, lab in enumerate(five)]))
        self.assertEqual((len(s["best"]), len(s["worst"])), (3, 2))
        self.assertFalse({b["label"] for b in s["best"]} & {w["label"] for w in s["worst"]})

    def test_the_counts(self):
        mem = taught(("liq:<20k", 10, -20))
        for _ in range(6):
            lr.decide(mem, ["liq:<20k"], {})
        s = lr.summary(mem)
        self.assertEqual((s["samples"], s["skipped"], s["explored"]), (10, 5, 1))


class Learned(unittest.TestCase):
    def test_the_close_line_names_the_strongest_label(self):
        mem = taught(("liq:<20k", 8, -10), ("age:<30m", 2, 30))                  # -4.4 against +5.0
        self.assertEqual(lr.learned(mem, ["liq:<20k", "age:<30m", "bogus"]), "age:<30m now avg +30.0% over 2")
        mem = taught(("liq:<20k", 20, -10), ("age:<30m", 2, 30))                 # -6.7 against +5.0
        self.assertEqual(lr.learned(mem, ["liq:<20k", "age:<30m"]), "liq:<20k now avg -10.0% over 20")
        self.assertEqual((lr.learned(lr.fresh(), ["liq:<20k"]), lr.learned(None, None), lr.learned(mem, 5)), ("", "", ""))


class Files(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "memory.json")

    def write(self, text):
        with open(self.path, "wb") as fh:
            fh.write(text if isinstance(text, bytes) else text.encode("utf-8"))

    def test_a_missing_or_unreadable_file_is_a_fresh_memory(self):
        want = {"v": 1, "labels": {}, "samples": 0, "skipped": 0, "explored": 0, "skip_run": 0}
        self.assertEqual(lr.load(self.path), want)
        for text in ("not json{", "[1, 2]", "null", '{"v": 2, "samples": 9}', b"\xff\xfe{}", ""):
            self.write(text)
            self.assertEqual(lr.load(self.path), want, text)
        self.assertEqual((lr.load(None), lr.load(3), lr.load(self.dir)), (want, want, want))
        a, b = lr.fresh(), lr.fresh()
        a["labels"]["liq:<20k"] = {}
        self.assertEqual(b["labels"], {})                                        # a new dict each time

    def test_save_and_load_round_trip(self):
        mem = taught(("liq:<20k", 10, -20), ("age:<30m", 9, 3.3333333), ("hour:day", 400, -1.5))
        for _ in range(7):
            lr.decide(mem, ["liq:<20k"], {"learn_k": 12})
        self.assertTrue(lr.save(self.path, mem))
        back = lr.load(self.path)
        self.assertEqual(back, mem)
        self.assertEqual(lr.summary(back), lr.summary(mem))
        self.assertEqual(lr.score(back, ["liq:<20k", "age:<30m"], k=12), lr.score(mem, ["liq:<20k", "age:<30m"], k=12))
        self.assertEqual(os.listdir(self.dir), ["memory.json"])                 # no temp file left
        lr.update(back, ["age:<30m"], 10)
        self.assertTrue(lr.save(self.path, back))                              # over the old file
        self.assertEqual(lr.load(self.path)["labels"]["age:<30m"]["n"], 10)

    def test_the_file_is_strict_json_with_only_sound_known_labels(self):
        mem = taught(("liq:<20k", 8, -10))
        mem["labels"]["age:<30m"] = {"n": 3, "sum": float("nan")}
        mem["labels"]["liq:tiny"] = {"n": 3, "sum": -3}
        mem["labels"]["vol5:<5k"] = {"n": 2, "sum": -4, "sumsq": -1, "wins": 9}
        mem["extra"] = object()
        self.assertTrue(lr.save(self.path, mem))
        with open(self.path, encoding="utf-8") as fh:
            doc = json.loads(fh.read(), parse_constant=lambda c: self.fail("%s in the file" % c))
        self.assertEqual(sorted(doc["labels"]), ["liq:<20k", "vol5:<5k"])
        self.assertEqual(doc["labels"]["vol5:<5k"], {"n": 2, "sum": -4, "sumsq": 0, "wins": 2})
        self.assertNotIn("extra", doc)

    def test_load_repairs_a_hand_edited_file(self):
        self.write(json.dumps({"v": 1, "samples": -3, "skipped": "7", "explored": None, "skip_run": 2.0,
                               "labels": {"liq:<20k": {"n": 8, "sum": -80, "sumsq": 800, "wins": 0},
                                          "liq:bogus": {"n": 8, "sum": -80}, "age:<30m": {"n": "x"},
                                          "vol5:<5k": [1], "hour:day": {"n": 0, "sum": 0}},
                               "k": -4, "min_n": "z"}))
        mem = lr.load(self.path)
        self.assertEqual(mem, {"v": 1, "samples": 0, "skipped": 7, "explored": 0, "skip_run": 2, "k": 0, "min_n": 8,
                               "labels": {"liq:<20k": {"n": 8, "sum": -80, "sumsq": 800, "wins": 0}}})
        self.write('{"labels": {"liq:<20k": {"n": 1, "sum": 2}}}')               # no "v": still a memory
        self.assertEqual(lr.load(self.path)["labels"]["liq:<20k"]["n"], 1)

    def test_save_never_raises(self):
        mem = taught(("liq:<20k", 8, -10))
        sub = os.path.join(self.dir, "new", "memory.json")
        self.assertTrue(lr.save(sub, mem))                                     # makes the folder
        self.assertEqual(lr.load(sub), mem)
        blocked = os.path.join(self.dir, "blocked.json")
        os.mkdir(blocked)
        self.assertFalse(lr.save(blocked, mem))                                # a folder is in the way
        self.assertEqual(sorted(os.listdir(self.dir)), ["blocked.json", "new"])  # its temp file is gone
        self.assertTrue(lr.save(self.path, mem))
        for g in (None, 3, [], "x"):
            self.assertFalse(lr.save(self.path, g))
        self.assertEqual(lr.load(self.path), mem)                              # a bad call left the file alone
        self.assertFalse(lr.save(None, mem))
        self.assertFalse(lr.save("", mem))


class Backfill(unittest.TestCase):
    def test_old_trades_teach_their_hour(self):
        mem = lr.fresh()
        trades = [{"opened": "2026-11-25T22:30:00Z", "pnl_pct": -10.0},          # 23:30 in Bratislava
                  {"opened": "2026-11-26T01:10:00Z", "pnl_pct": 4.0},           # 02:10
                  {"opened": "2026-07-01T01:10:00Z", "pnl_pct": 1.0},           # 03:10 in summer time
                  {"opened": "2026-11-26T12:00:00Z", "pnl_pct": 2.0},           # a day test run
                  {"opened": "garbage", "pnl_pct": 3}, {"opened": "2026-11-25T23:00:00Z"}, "junk", None,
                  {"opened": "2026-11-25T23:00:00Z", "pnl_pct": -8, "learn": ["liq:<20k", "age:<30m"]}]
        self.assertEqual(lr.backfill(mem, trades), 5)
        self.assertEqual(sorted(mem["labels"]), ["age:<30m", "hour:01-03", "hour:03-05", "hour:23-01", "hour:day",
                                                 "liq:<20k"])
        self.assertEqual(mem["labels"]["hour:23-01"]["sum"], -10)
        self.assertEqual(lr.backfill(mem, trades, tz="Mars/Olympus"), 1)        # an unknown zone: only "learn"
        self.assertEqual((lr.backfill(None, trades), lr.backfill(mem, None), lr.backfill(mem, "x")), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
