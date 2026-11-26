"""The levers that decide whether a paper trade can pay after its costs, on the engine's own cost model
(memebot/bot.py: Night.buy, sell_quote, close_part, np_even_gain, np_plan_cfg; curve_buy_tokens and
curve_sell_sol) with today's config.json: the order size against the fixed fees and the curve's price impact,
the first step of every hold tier, the stop, the RISK caps and the LEARNER's skip line.

The scenario is the morning of the first New Pairs trades on GitHub Actions (2026-11-26): SOL at $71.14
(Jupiter's price v3 that morning), a pump.fun curve with 35 SOL of virtual reserves at the entry (the live
entries had 33.5-37.6), a $10 order. At that SOL price the engine priced those trades to the cent
(test_the_engine_priced_the_first_live_trades). Paper money: none of this says the bot will make money, only
what a trade costs and where the break-even is."""

import copy
import json
import math
import shutil
import tempfile
import types
import unittest

from _load import load

import test_newpairs_engine as E        # its harness: FakeFeeds and CurveMarket (quotes by the curve maths)

mb, plan, SOL, K = E.mb, E.plan, E.SOL, E.K
learner = load("memebot", "learner")

SOL_USD = 71.14                         # Jupiter's SOL price on the morning of the first live trades
VSOL = 35.0                             # a curve's virtual SOL at the entry
CONFIG = mb.load_config()               # today's config.json on the code's defaults
# the first live trades (meme-data state.json): cost, proceeds, the exit reading's price over the entry
# price, the market cap in SOL at the entry
LIVE = {"Drone": (10.0, 8.80, 2.65657945e-06 / 2.89468886e-06, 40.8),
        "ZEROCAST": (9.82, 8.23, 2.52691721e-06 / 2.89107607e-06, 39.7),
        "SWARM": (9.65, 8.39, 2.80571244e-06 / 3.09460721e-06, 42.5),
        "BST": (9.88, 8.96, 2.40038822e-06 / 2.53912081e-06, 34.9),
        "HOGZ": (9.66, 9.84, 3.45314642e-06 / 3.19595579e-06, 43.9)}
LIVE_PNL = [-16.2, 1.9, -9.3, -13.1, -25.3, -26.1, -29.1, -12.0]      # their pnl_pct, Drone last
# every coin that fired the trigger that morning (the live log), PRE-FLOOR: those triggers fired before config.json's
# 45 s minimum age (np_min_age_s) was on. SOL in, buy readings, the holders RugCheck's report saw at the fire (its
# totalHolders: empty accounts counted, "0 holders": not indexed yet), the result in % of the 10 bought (None: the
# LEARNER skipped it), the age at the entry and, for the 4 dumps, the age of the dump sale and its SOL
MORNING = [("Drone", 3.8, 7, 0, -12), ("BST", 2.5, 13, 8, -9), ("ZEROCAST", 4.6, 6, 11, -16), ("HOGZ", 7.6, 6, 0, 2),
           ("SWARM", 6.1, 6, 0, -13), ("STOREB", 3.3, 6, 0, -25), ("KIDSTOCK", 3.3, 6, 3, -26),
           ("TANOSHIMI", 3.3, 6, 0, -29), ("YARL", 2.9, 9, 9, -14), ("MEME", 4.3, 6, 4, -28),
           ("SALECOIN", 9.4, 6, 0, None), ("OFTANIA", 4.1, 6, 0, None), ("Oro", 3.0, 7, 7, None),
           ("BACKER", 6.8, 8, 13, None), ("TIPPD", 7.1, 7, 7, None), ("TICKZ", 7.8, 21, 30, None),
           ("AISLE", 3.3, 9, 7, None), ("NODIVO", 10.6, 9, 5, None), ("MARU", 3.6, 6, 10, None),
           ("SPRAYZ", 7.8, 18, 31, None), ("VP64", 6.6, 7, 10, None)]
ENTRY_AGE = {"Drone": 13, "BST": 141, "ZEROCAST": 10, "HOGZ": 14, "SWARM": 18, "STOREB": 25, "KIDSTOCK": 20,
             "TANOSHIMI": 23, "YARL": 45, "MEME": 134}
DUMPS = {"STOREB": (25 + 3, 2.9), "KIDSTOCK": (20 + 18, 3.9), "TANOSHIMI": (23 + 3, 4.4), "MEME": (134 + 6, 3.1)}


class Book(object):
    """One Night on a curve market (CurveMarket's idea): buys and sells go through the engine's own code."""

    def __init__(self, cfg=None, sol_usd=SOL_USD):
        self.dir = tempfile.mkdtemp()
        self.m = E.CurveMarket()
        self.m.sol = sol_usd
        self.cfg = cfg or CONFIG
        self.n = mb.Night(self.dir, self.cfg, self.m, E.T0, out=lambda *a: None)
        self.n.begin("2026-11-26")

    def close(self):
        self.n.np_close()
        shutil.rmtree(self.dir, True)

    def fee_sol(self):
        return mb.f(self.cfg.get("network_fee_sol")) + mb.f(self.cfg.get("priority_fee_sol"))

    def buy(self, order, vsol=VSOL, mint="coin"):
        """A New Pair's buy as np_fire() makes it: `order` dollars with the order's fixed fee inside."""
        self.m.set_curve(mint, vsol, K / vsol)
        pos = self.n.buy(mint, {"symbol": mint.upper(), "name": mint}, order - self.fee_sol() * self.m.sol,
                         self.m.sol, 6, fee_sol=self.fee_sol())
        pos["strategy"] = "new_pairs"
        self.n.state["positions"][mint] = pos
        self.n.state["balance"] -= pos["cost"]
        return pos

    def move(self, pos, gain_pct, mint="coin"):
        """The curve where its price is gain_pct over the position's fill price (plan.py's gain) -> the coin."""
        spot = pos["entry_price"] * (1 + gain_pct / 100.0) / self.m.sol
        vsol = math.sqrt(spot * K)
        self.m.set_curve(mint, vsol, K / vsol)
        return types.SimpleNamespace(vsol=vsol, vtok=K / vsol, price=vsol / (K / vsol))

    def spot_gain(self, pos, vsol=VSOL):
        """The gain the curve's price at the buy shows against the fill price (negative: the buy's costs)."""
        return ((vsol / (K / vsol)) * self.m.sol / pos["entry_price"] - 1) * 100

    def sale_pct(self, pos, gain_pct):
        """The whole trade's P&L % of its cost if everything is sold at gain_pct."""
        self.move(pos, gain_pct)
        return (self.n.sell_quote(pos, pos["raw"], self.m.sol) - pos["cost"]) / pos["cost"] * 100


def costs(order, sol_usd=SOL_USD, vsol=VSOL):
    """The cost table's row for one order size: the round trip at an unchanged price, the break-even gain, and
    the whole trade's P&L % sold at +7, +10, -7 and -8 % (the gains from the fill price, as the plan counts)."""
    b = Book(sol_usd=sol_usd)
    try:
        pos = b.buy(order, vsol)
        row = {"rt": b.sale_pct(pos, b.spot_gain(pos, vsol)), "spot": b.spot_gain(pos, vsol),
               "fixed": 2 * b.fee_sol() * sol_usd / order * 100}
        lo, hi = -20.0, 40.0
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if b.sale_pct(pos, mid) >= 0 else (mid, hi)
        row["be"] = hi
        for g in (7, 10, -7, -8):
            row[g] = b.sale_pct(pos, g)
        return row
    finally:
        b.close()


def sk(x):
    """A number as config.json's Slovak notes write it: "-6,5"."""
    return ("%+.1f" % x).replace(".", ",").replace("+", "" if x < 0 else "+")


class Costs(unittest.TestCase):
    def test_the_engine_priced_the_first_live_trades(self):
        """At SOL $71.14 the engine's maths books the live stops and the time stop to the cent (so 0.001 SOL an
        order is $0.07, not the $0.15-0.25 the plan was drawn on). HOGZ's take filled 1.9 % of the order under
        its +8 % reading: the price fell back between the reading and the quote."""
        for sym, (cost, proceeds, ratio, mcap) in LIVE.items():
            b = Book()
            try:
                pos = b.buy(cost, math.sqrt(mcap * 30 * 1.073))
                b.move(pos, (ratio - 1) * 100)
                model = b.n.sell_quote(pos, pos["raw"], SOL_USD)
            finally:
                b.close()
            if sym == "HOGZ":
                self.assertAlmostEqual((model - proceeds) / cost * 100, 1.9, delta=0.15)
            else:
                self.assertAlmostEqual(model, proceeds, delta=0.0101, msg=sym)

    def test_a_bigger_order_is_not_cheaper_at_this_sol_price(self):
        """The fixed fee (0.002 SOL a round trip) falls with the order, the curve's price impact rises with it:
        together they are lowest near 0.2 SOL, and $10 to $20 are within 0.15 % of each other. At SOL $150 the
        fixed fee weighs twice as much and $20 is a point cheaper than $10 (the _cost note: position_pct 20 then)."""
        rows = {o: costs(o) for o in (5, 10, 15, 20, 30)}
        for o in (10, 15, 20):
            self.assertTrue(-6.75 < rows[o]["rt"] < -6.35, (o, rows[o]["rt"]))
        self.assertLess(max(rows[o]["rt"] for o in (10, 15, 20)) - min(rows[o]["rt"] for o in (10, 15, 20)), 0.15)
        self.assertLess(rows[5]["rt"], rows[10]["rt"] - 0.9)                 # $5: the fixed fee is 2.8 % of it
        self.assertLess(rows[30]["rt"], rows[15]["rt"] - 0.5)                # $30: the impact is 2.2 %
        self.assertGreater(costs(20, sol_usd=150.0)["rt"], costs(10, sol_usd=150.0)["rt"] + 1.0)

    def test_today_s_order_size(self):
        """10 % of the $100 start is $10 (0.14 SOL): within 0.15 % of the cheapest round trip of any size from $5
        to $40. The smallest order's fixed fee is under 1.5 % of it each way. max_open x position_pct leaves cash."""
        c = CONFIG
        order = c["start_balance"] * c["position_pct"] / 100.0
        self.assertEqual((c["position_pct"], order, c["min_order_usd"], c["max_open"]), (10.0, 10.0, 5.0, 8))
        best = max(costs(o)["rt"] for o in range(5, 41, 5))
        self.assertGreater(costs(order)["rt"], best - 0.15)
        self.assertLess((c["network_fee_sol"] + c["priority_fee_sol"]) * SOL_USD / c["min_order_usd"] * 100, 1.5)
        self.assertLessEqual(c["max_open"] * c["position_pct"], 100)
        self.assertEqual(c["min_order_usd"] / (c["position_pct"] / 100.0), 50.0)      # the book it stops buying under

    def test_the_first_step_of_every_tier_pays_after_its_costs(self):
        """Sold whole at its first step, a $10 trade books at least +5 % (+5.6 %), 5 points over its break-even
        (+4.2 %); at +7 % it booked +2.7 %, +0.8 % with HOGZ's fill. At SOL $150 the step still books +4 %."""
        row, dear = costs(10), costs(10, sol_usd=150.0)
        self.assertTrue(3.8 < row["be"] < 4.5, row["be"])
        self.assertLess(row[7], 3.0)
        for t in CONFIG["hold_tiers"]:
            g1 = t["ladder"][0][0]
            self.assertEqual(g1, 10.0, t["name"])
            self.assertGreater(row[10], 5.0)
            self.assertGreater(g1 - row["be"], 5.0)
            self.assertGreater(row[10] - 1.9, 3.5)                           # with a fill like HOGZ's
            self.assertGreater(dear[10], 4.0)

    def test_a_partial_first_step_leaves_the_rest_room_under_it(self):
        """After its first step the rest's break-even stop is where its sale leaves the whole trade at 0 after every
        cost (np_plan_cfg, at least +1 %): with +10 % steps that is +1 / +2 / +3 % for swing / runner / moon (50 /
        34 / 25 % sold), and the trade sold there is not a loss. With the old +7 % step the same stops sat at +2.4 /
        +3.5 / +4 %: a pullback of 3 to 4.6 points ended the trade at 0. (Where the rest really goes out is
        test_where_each_tiers_rest_goes_out: swing's trailing stop binds before its break-even.)"""
        for tiers, first, most in ((CONFIG["hold_tiers"], 10.0, 3.2), (E.SPEC_TIERS, 7.0, 4.2)):
            cfg = dict(CONFIG, hold_tiers=tiers)
            for score, name, frac in ((3, "swing", 0.5), (5, "runner", 0.34), (7, "moon", 0.25)):
                b = Book(cfg)
                try:
                    pos = b.buy(10.0)
                    p = plan.new_plan(score, [], plan.tiers_from_cfg(cfg), cfg)
                    pos["plan"] = p
                    coin = b.move(pos, first)
                    d = plan.decide_exit(p, first, first, 1, cfg)
                    self.assertEqual((p["tier"], d["frac"], d["final"]), (name, frac, False))
                    self.assertTrue(b.n.close_part("coin", d["frac"], d["why"], SOL_USD, take=d))
                    plan.mark_step(p, d, cfg)
                    be = b.n.np_plan_cfg(pos, p, coin, SOL_USD)["breakeven_pct"]
                    self.assertTrue(1.0 <= be <= most, (name, first, be))
                    if first == 10.0:
                        self.assertGreaterEqual(first - be, 6.8, name)
                    else:
                        self.assertLessEqual(first - be, 4.7, name)
                    b.move(pos, be)
                    self.assertTrue(b.n.close_part("coin", 1.0, "break-even", SOL_USD))
                    self.assertGreaterEqual(b.n.state["night"]["trades"][-1]["pnl"], -0.01, (name, first))
                finally:
                    b.close()

    def test_where_each_tiers_rest_goes_out(self):
        """The review's correction: after the +10 % step, swing's 6 % trailing stop sits at +3.4 % (1.10 x 0.94),
        above its +1 % break-even, so swing's rest goes out there (the trade +1.9 %), not at +1 %; runner's 10 % and
        moon's 15 % trails sit under their break-evens (+2 and +3 %), which hold the rest. 6.6 to 8 points under the
        step on a $10 order; a $5 order's break-evens sit higher (+3.7 / +5.2 / +5.8 %), the plan.py docstring and
        config.json's _cost say the same."""
        want = {"swing": (3.4, "trailing stop 6% off the top (peak +10%)"), "runner": (2.0, "break-even stop (peak +10%)"),
                "moon": (3.0, "break-even stop (peak +10%)")}
        for order, highs in ((10.0, None), (5.0, {"swing": 3.65, "runner": 5.2, "moon": 5.8})):
            for score, name in ((3, "swing"), (5, "runner"), (7, "moon")):
                b = Book()
                try:
                    pos = b.buy(order)
                    p = plan.new_plan(score, [], plan.tiers_from_cfg(CONFIG), CONFIG)
                    pos["plan"] = p
                    coin = b.move(pos, 10.0)
                    d = plan.decide_exit(p, 10.0, 10.0, 1, CONFIG)
                    self.assertTrue(b.n.close_part("coin", d["frac"], d["why"], SOL_USD, take=d))
                    plan.mark_step(p, d, CONFIG)
                    pc = b.n.np_plan_cfg(pos, p, coin, SOL_USD)
                    g = 10.0
                    while True:                                         # the price falls back from its +10 % top
                        g = round(g - 0.05, 2)
                        e = plan.decide_exit(p, g, 10.0, 2, pc)
                        if e:
                            break
                    if highs is None:
                        self.assertAlmostEqual(g, want[name][0], delta=0.06, msg=name)
                        self.assertEqual(e["why"], want[name][1])
                        self.assertTrue(6.5 <= 10.0 - g <= 8.05, (name, g))
                    else:
                        self.assertAlmostEqual(g, highs[name], delta=0.1, msg=(name, order))
                finally:
                    b.close()
        with open(mb.CONFIG, encoding="utf-8") as fh:
            note = json.load(fh)["_cost"]
        self.assertIn("klzavý stop 6 % pod vrcholom (+3,4 %", note)
        with open(plan.__file__, encoding="utf-8") as fh:
            self.assertIn("swing's rest is held by its 6 % trailing stop, +3.4 %", fh.read())

    def test_the_stop(self):
        """-7 % from the fill books about -10.8 % on $10, -8 % (Drone) -11.8 %. Right after the buy the position
        shows about -2.6 % (pump.fun's fee, the haircut and the impact are in the fill price), so a -5 % stop
        would sit 2.4 points of price under the price the coin was bought at: one 0.5 s reading moved the live
        stops 1 to 6 points past -7."""
        row = costs(10)
        self.assertTrue(-11.3 < row[-7] < -10.3, row[-7])
        self.assertTrue(-12.3 < row[-8] < -11.3, row[-8])
        self.assertTrue(-2.8 < row["spot"] < -2.3, row["spot"])
        self.assertEqual(CONFIG["stop_pct"], -7.0)


class Risk(unittest.TestCase):
    def test_the_day_stop(self):
        """-30 % a day is where buying stops (before, -50 % let two bad days of a 24/7 run take 75 % of the book).
        It is checked before an entry, so an entry must also leave room for one more dump of every open position
        and of itself (dump_loss_pct 30: the live dumps took -25 to -29 %): with no position open the last entry
        comes at about -27.8 %."""
        c = CONFIG
        stops = sum(LIVE_PNL[i] for i in (0, 3, 7)) / 3.0
        self.assertAlmostEqual(stops, -13.77, places=2)
        self.assertEqual((c["night_stop_pct"], c["dump_loss_pct"]), (-30.0, 30.0))
        b = Book()
        try:
            b.n.state["balance"] = 72.2                                 # a $7.22 order could lose $2.17: -29.97 %
            self.assertIsNone(b.n.why_no_entries(400, {}))
            b.n.state["balance"] = 72.1
            self.assertEqual(b.n.why_no_entries(400, {}),
                             "night at -28%: 0 open and one more order could take it past -30% in one dump wave")
            b.n.state["balance"] = 70.0
            self.assertEqual(b.n.why_no_entries(400, {}), "night at -30%: stopped for the night")
        finally:
            b.close()

    def test_the_smallest_order(self):
        """Under a $50 book 10 % is under the $5 minimum: the SIZER buys nothing (np_fire says so)."""
        c = CONFIG
        self.assertLess(49.99 * c["position_pct"] / 100.0, c["min_order_usd"])
        self.assertGreaterEqual(50.0 * c["position_pct"] / 100.0, c["min_order_usd"])


class Learner(unittest.TestCase):
    LABELS = ["hour:day", "news:0-2", "tier:scalp", "np:age:30-90s"]

    def decide(self, pnls):
        mem = learner.fresh()
        for p in pnls:
            learner.update(mem, self.LABELS, p)
        return learner.decide(copy.deepcopy(mem), self.LABELS, CONFIG)

    def test_the_skip_line_against_the_costs(self):
        """A coin bought at random loses the round trip (-6.5 % at SOL $71, $10-20 orders). Shrunk by learn_k 5
        flat trades, 8 of them score -4.0 %: learn_skip_pct -4 split that by a rounding (-3.98 % at $15) and -3.5
        skips it with room. A kind that reaches the first step half of the time (-2.6 % a trade) is bought at a
        smaller size, one that always reaches it at a bigger one; 7 trades are no data yet."""
        c = CONFIG
        self.assertEqual((c["learn_skip_pct"], c["learn_k"], c["learn_min_n"]), (-3.5, 5, 8))
        row = costs(10)
        for order in (10, 15, 20):
            rt = costs(order)["rt"]
            shrunk = rt * c["learn_min_n"] / (c["learn_min_n"] + c["learn_k"])
            self.assertTrue(-4.15 < shrunk < -3.9, (order, shrunk))
            self.assertLessEqual(shrunk, c["learn_skip_pct"] - 0.3)
            self.assertEqual(self.decide([rt] * 8)["action"], "skip", order)
            d = self.decide([rt] * 7)
            self.assertEqual((d["action"], d["mult"]), ("buy", 1.0))
        self.assertEqual(self.decide([row[-7]] * 8)["action"], "skip")          # the stop, every time
        self.assertEqual(self.decide(LIVE_PNL)["action"], "skip")               # the first live morning
        half = self.decide([row[10], row[-7]] * 4)
        self.assertEqual(half["action"], "buy")
        self.assertTrue(0.85 < half["mult"] < 1.0, half)
        self.assertGreater(self.decide([row[10]] * 8)["mult"], 1.1)
        three = self.decide([row[10]] * 3 + [row[-7]] * 5)                      # 3 of 8 reach +10 %: -4.7 % a trade
        self.assertEqual(three["action"], "buy")
        self.assertLess(three["mult"], 0.9)
        self.assertEqual(self.decide([row[10]] * 2 + [row[-7]] * 6)["action"], "skip")


class Engine(unittest.TestCase):
    """Today's tiers, sizes and stops in the New Pairs engine (test_newpairs_engine's harness: a create, its
    metadata, its curve readings, RugCheck; the trigger rules of that harness)."""
    setUp, night, turn, read, launch, lines = (E.Engine.setUp, E.Engine.night, E.Engine.turn, E.Engine.read,
                                               E.Engine.launch, E.Engine.lines)

    def gain_rsol(self, n, mint, gain_pct):
        """The real SOL at which the coin's curve price is gain_pct over its fill price, at this test's SOL price."""
        return E.vsol_for(n.state["positions"][mint]["entry_price"] / self.m.sol * (1 + gain_pct / 100.0)) - 30.0

    def cfg(self):
        self.m.sol = SOL_USD
        keep = ("hold_tiers", "position_pct", "min_order_usd", "max_open", "night_stop_pct", "stop_pct", "tp1_pct",
                "breakeven_pct", "learn_skip_pct", "learn_k", "learn_min_n", "loss_streak", "cooldown_min")
        return E.np_cfg(**{k: copy.deepcopy(CONFIG[k]) for k in keep})

    def test_a_scalp_rides_past_7_and_takes_everything_at_10(self):
        n = self.night(self.cfg())
        self.launch(n, "aaa", E.SCALP)
        pos = n.state["positions"]["aaa"]
        self.assertAlmostEqual(pos["cost"], 10.0, places=2)
        self.assertEqual(pos["plan"]["tier"], "scalp")
        self.read(n, "aaa", self.gain_rsol(n, "aaa", 8.5))
        self.assertIn("aaa", n.state["positions"])                      # +8.5 %: no longer a take
        self.read(n, "aaa", self.gain_rsol(n, "aaa", 10.2))
        t = n.state["night"]["trades"][0]
        self.assertEqual((t["why"], t["steps"]), ("TP +10%", 1))
        self.assertTrue(5.0 < t["pnl_pct"] < 6.5, t["pnl_pct"])

    def test_a_full_book_bought_near_the_day_stop_cannot_take_the_day_far_past_it(self):
        # the review's case: at -28 % the bot bought 8 positions ($57) and one dump wave (3.1 SOL sells, -23 % each)
        # ended the day at -41 % with night_stop_pct -30: the stop was checked before each entry, on marks
        n = self.night(self.cfg())
        n.state["night"]["start_equity"] = 100.0
        n.state["balance"] = 72.0
        for i in range(8):
            self.launch(n, "c%d" % i, E.SCALP)
        self.assertEqual(n.state["positions"], {})
        self.assertTrue(any("could take it past -30% in one dump wave" in t for t in self.lines(n, "RISK")))
        # the same wave from a day at -15 %: it buys fewer positions, and the wave leaves the day above -30 %
        n = self.night(self.cfg())
        n.state["night"]["start_equity"] = 100.0
        n.state["balance"] = 85.0
        for i in range(8):
            self.launch(n, "d%d" % i, E.SCALP)
        held = list(n.state["positions"])
        self.assertTrue(3 <= len(held) <= 5, len(held))
        for m in held:
            self.read(n, m, max(0.0, n.wl.get(m).rsol - 3.1))
        self.assertEqual(n.state["positions"], {})
        self.assertGreater((n.state["balance"] / 100.0 - 1) * 100, -30.0)

    def test_a_half_size_explore_raised_to_the_minimum_says_so(self):
        # the review's case: the live book $87.14, an explore at 0.5x: 10 % x 0.5 is $4.36, bought at the $5 minimum,
        # and the SIZER line said "x0.50 LEARNER = $5.00", arithmetic that does not hold
        n = self.night(self.cfg())
        n.state["balance"] = 87.14
        n.learn_decide = lambda labels, sym: ({"action": "explore", "mult": 0.5, "score": -11.4, "why": "x"}, None)
        self.launch(n, "aaa", E.SCALP)
        line = [t for t in self.lines(n, "SIZER") if "LEARNER" in t][0]
        self.assertEqual(line, "10% of $87.14 x0.50 LEARNER = $4.36, raised to the $5.00 minimum: $5.00")
        self.assertAlmostEqual(n.state["positions"]["aaa"]["cost"], 5.0, places=2)
        with open(mb.PROMPT, encoding="utf-8") as fh:
            self.assertIn("a half-size buy under a $100 book is raised to the $5 minimum", fh.read())

    def test_a_runner_sells_a_third_at_10_and_its_rest_stops_at_0_for_the_trade(self):
        n = self.night(self.cfg())
        self.launch(n, "uzim", E.RUNNER, social=(True, True, True))
        raw0 = n.state["positions"]["uzim"]["raw"]
        self.read(n, "uzim", self.gain_rsol(n, "uzim", 10.5))
        pos = n.state["positions"]["uzim"]
        self.assertEqual((pos["plan"]["tier"], pos["raw"]), ("runner", raw0 - int(raw0 * 0.34)))
        self.assertRegex([x for x in self.lines(n, "EXECUTOR") if x.startswith("TP")][0],
                         r"^TP \+10% · sold 34% for \$\d+\.\d\d · stop to break-even")
        be = n.np_plan_cfg(pos, pos["plan"], n.wl.get("uzim"), SOL_USD)["breakeven_pct"]
        self.assertTrue(1.0 <= be <= 3.0, be)
        self.read(n, "uzim", self.gain_rsol(n, "uzim", be + 1.0))
        self.assertIn("uzim", n.state["positions"])
        be = n.np_plan_cfg(pos, pos["plan"], n.wl.get("uzim"), SOL_USD)["breakeven_pct"]    # the curve's impact moved it
        self.read(n, "uzim", self.gain_rsol(n, "uzim", be - 0.01))
        t = n.state["night"]["trades"][0]
        self.assertEqual((t["why"], t["steps"]), ("break-even stop (peak +10%)", 1))
        self.assertGreaterEqual(t["pnl"], -0.01)
        self.assertEqual(n.state["night"]["streak"], 0)


class Words(unittest.TestCase):
    def test_config_json_and_plan_py_have_the_same_tiers(self):
        self.assertEqual(json.loads(json.dumps(CONFIG["hold_tiers"])), json.loads(json.dumps(plan.DEFAULT_TIERS)))
        self.assertEqual(CONFIG["tp1_pct"], CONFIG["hold_tiers"][0]["ladder"][0][0])   # the fallback tier: the scalp

    def test_the_prompt_and_the_cost_note_say_the_engine_s_numbers(self):
        with open(mb.PROMPT, encoding="utf-8") as fh:
            p = fh.read()
        with open(mb.CONFIG, encoding="utf-8") as fh:
            note = json.load(fh)["_cost"]                               # load_config() leaves the notes out
        row = costs(10)
        self.assertAlmostEqual(row["rt"], -6.5, delta=0.1)
        self.assertAlmostEqual(row["be"], 4.0, delta=0.3)
        for text in ("a round trip at an unchanged price costs about 6.5%", "breaks even at about +4%",
                     "+10%% nets about +%.1f%%" % row[10], "the -7%% stop costs about %.0f%%" % row[-7],
                     "+%.1f%% (+1.9%% on the first live take)" % row[7], "never less than $%g" % CONFIG["min_order_usd"],
                     "lost %g%% or more on average" % -CONFIG["learn_skip_pct"]):
            self.assertIn(text, p)
        for order in (10, 15, 20, 30):
            r = costs(order)
            self.assertIn("$%d: %s %%, %s %%, %s %%, %s %%" % (order, sk(r["rt"]), sk(r[7]), sk(r[10]), sk(r[-7])),
                          note)
        r = costs(5)
        self.assertIn("$5 (0,07 SOL): tam a späť %s %%, +7 %% → %s %%, +10 %% → %s %%, stop -7 %% → %s %%" % (
            sk(r["rt"]), sk(r[7]), sk(r[10]), sk(r[-7])), note)
        for text in ("night_stop_pct -30 %", "min_order_usd 5", "position_pct zostáva 10", "learn_skip_pct -3,5"):
            self.assertIn(text, note)

    def test_the_entry_note_says_what_the_rules_do_to_the_live_morning(self):
        """config.json's _entry note and the prompt count the morning of 2026-11-26 by newpairs.py's own rules with
        today's config, and say plainly that those triggers fired before the 45 s age floor: under the floor, 3 of the
        4 dumps came before 45 s (np_dump_sol rejects them there), so the new rules' own evidence is MEME and the waits
        of BST and YARL; the morning's holder counts (RugCheck's totalHolders then: empty accounts counted) are an
        upper bound of the wallets."""
        npm = mb.newpairs
        bt = load("memebot", "backtest")
        with open(mb.CONFIG, encoding="utf-8") as fh:
            note = json.load(fh)["_entry"]
        with open(mb.PROMPT, encoding="utf-8") as fh:
            p = fh.read()
        sk2 = lambda x: ("%g" % x).replace(".", ",")                            # noqa: E731 - "0,35"
        floor = CONFIG["np_min_age_s"]
        self.assertEqual(floor, 45)
        verdict = {}
        for sym, sol, buys, holders, pnl in MORNING:
            c = npm.Coin(mint=sym.lower(), symbol=sym, rsol0=0.0, rsol=sol, buys=buys)
            rep = {"mint": c.mint, "risks": [], "topHolders": [], "totalHolders": holders}
            ok, why, _ = npm.rug_check_np(rep, c, CONFIG)
            verdict[sym] = "pass" if ok else "fail" if ok is False else (
                "conc" if why[0].startswith("waiting: %d holders for" % holders) else "holders")
        bought = [s_ for s_, _, _, _, pnl in MORNING if pnl is not None]
        pnl = {s_: v for s_, _, _, _, v in MORNING}
        holders = {s_: h for s_, _, _, h, _ in MORNING}
        self.assertEqual((len(MORNING), len(bought), len(DUMPS)), (21, 10, 4))
        # the age floor: 7 of the 10 entries fired before it; 3 of the 4 dumps sold before it (the dump rule rejects)
        pre = [s_ for s_ in bought if ENTRY_AGE[s_] < floor]
        early_dumps = sorted(s_ for s_, (at, sol) in DUMPS.items() if at < floor and sol >= CONFIG["np_dump_sol"])
        self.assertEqual((len(pre), early_dumps), (7, ["KIDSTOCK", "STOREB", "TANOSHIMI"]))
        self.assertIn("7 z 10 pred vekovou hranicou np_min_age_s 45 s", note)
        self.assertIn("3 z tých 4 dumpov (STOREB, TANOSHIMI, KIDSTOCK) prišli pred 45 s, kde ich už zamietne np_dump_sol",
                      note)
        # what is left as the new rules' own live evidence: the entries at or past the floor, all of them waits
        late = sorted((s_ for s_ in bought if ENTRY_AGE[s_] >= floor), key=lambda x: ENTRY_AGE[x])
        self.assertEqual(late, ["YARL", "MEME", "BST"])
        self.assertTrue(all(verdict[s_] == "holders" for s_ in late))
        self.assertIn("MEME (%d s, %d držitelia, dump %d %%) a čakanie BST (%d držiteľov, %d %%) a YARL (%d, %d %%)" % (
            ENTRY_AGE["MEME"], holders["MEME"], pnl["MEME"], holders["BST"], pnl["BST"], holders["YARL"], pnl["YARL"]),
            note)
        # all 21 by today's rules on the counts RugCheck saw then
        passed = [s_ for s_, _, _, _, _ in MORNING if verdict[s_] == "pass"]
        conc = [s_ for s_, _, _, _, _ in MORNING if verdict[s_] == "conc"]
        wait = [s_ for s_, _, _, _, _ in MORNING if verdict[s_] == "holders"]
        self.assertEqual(passed, ["ZEROCAST", "TICKZ", "MARU", "SPRAYZ"])
        self.assertTrue(set(DUMPS) <= set(wait) and "HOGZ" in wait)
        per = {s_: sol / holders[s_] for s_, sol, _, _, _ in MORNING if s_ in conc}
        self.assertIn("by %d prešli hneď (ZEROCAST %d %%, MARU, TICKZ, SPRAYZ), %d by čakali pre koncentráciu (%s SOL na "
                      "držiteľa) a %d na držiteľov (%d z %d vstupov: všetky %d dumpy aj HOGZ" % (
                          len(passed), pnl["ZEROCAST"], len(conc),
                          ", ".join("%s %s" % (s_, sk2(round(per[s_], 2))) for s_ in conc), len(wait),
                          len([s_ for s_ in wait if s_ in bought]), len(bought), len(DUMPS)), note)
        thin = [s_ for s_ in bought if holders[s_] <= 4]
        self.assertTrue(set(DUMPS) <= set(thin))
        self.assertIn("%d z %d vstupov prešlo RugCheckom na správe, ktorá videla 0 až 4 držiteľov" % (len(thin), len(bought)),
                      note)
        self.assertIn("%d of the first %d live entries passed on 0-4 holders, and all %d dumps were among them; 6 of "
                      "those 7 came at 13-25 seconds, before today's 45 second minimum age, which by itself keeps out 3 "
                      "of the 4 dumps" % (len(thin), len(bought), len(DUMPS)), p)
        self.assertEqual(sorted(ENTRY_AGE[s_] for s_ in thin)[:6], [13, 14, 18, 20, 23, 25])
        self.assertIn("the fourth, MEME, came at %d seconds with %d holders" % (ENTRY_AGE["MEME"], holders["MEME"]), p)
        g = bt.DEFAULT_GRID
        self.assertIn("np_min_holders %s, np_max_sol_per_holder %s, np_max_buy_sol %s" % tuple(
            " / ".join(sk2(v) for v in g[k]) for k in ("np_min_holders", "np_max_sol_per_holder", "np_max_buy_sol")),
            note)


if __name__ == "__main__":
    unittest.main()
