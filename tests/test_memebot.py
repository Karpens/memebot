"""memebot/bot.py tests, offline: a simulated market (prices, quotes, RugCheck reports) and a
pinned clock. The night 23:00 to 07:00 is Bratislava time (UTC+1 in winter)."""

import datetime
import os
import shutil
import tempfile
import unittest

from _load import load

mb = load("memebot", "bot")
SOL = mb.SOL
T0 = datetime.datetime(2026, 11, 25, 22, 30)            # 23:30 in Bratislava


def pair(mint, sym="MEME", liq=60000.0, vol5=30000.0, vol1h=60000.0, buys=80, sells=20, age_min=30.0, dex="pumpswap",
         price=0.001, now=T0):
    created = (now - datetime.datetime(1970, 1, 1)).total_seconds() * 1000 - age_min * 60000
    return {"chainId": "solana", "dexId": dex, "pairAddress": "pool-" + mint, "url": "https://dexscreener.com/solana/" + mint,
            "baseToken": {"address": mint, "name": sym + " coin", "symbol": sym},
            "liquidity": {"usd": liq}, "volume": {"m5": vol5, "h1": vol1h}, "txns": {"m5": {"buys": buys, "sells": sells}},
            "priceUsd": str(price), "marketCap": 500000, "pairCreatedAt": created}


def report(mint, holders=600, mint_auth=None, freeze=None, top=(4.0, 3.0, 2.0), lp=100.0, risks=(), creator=0,
           insiders=0):
    th = [{"address": "pool-acct-" + mint, "owner": "pool-" + mint, "pct": 30.0, "insider": False}]
    th += [{"address": "h%d" % i, "owner": "o%d" % i, "pct": p, "insider": False} for i, p in enumerate(top)]
    return {"mint": mint, "rugged": False, "mintAuthority": mint_auth, "freezeAuthority": freeze,
            "token": {"supply": 1e15, "decimals": 6, "mintAuthority": mint_auth, "freezeAuthority": freeze},
            "creatorBalance": creator, "totalHolders": holders, "graphInsidersDetected": insiders,
            "markets": [{"pubkey": "pool-" + mint, "liquidityA": "pool-acct-" + mint, "liquidityB": "pool-sol-" + mint,
                         "liquidityAAccount": {"owner": "pool-" + mint, "amount": 1},
                         "liquidityBAccount": {"owner": "pool-" + mint, "amount": 1}, "lp": {"lpLockedPct": lp}}],
            "topHolders": th, "risks": list(risks), "knownAccounts": {}}


class FakeMarket(object):
    """Prices in USD per whole token, SOL at $100. A quote takes a 1 % pool fee each way."""

    def __init__(self):
        self.errors = []
        self.px = {SOL: 100.0}
        self.pairs_by_mint = {}
        self.reports = {}
        self.no_sell = set()
        self.sell_haircut = {}
        self.fresh = []

    def new_pools(self, pages=2, now=None):
        return list(self.fresh)

    def promoted(self):
        return []

    def trending(self, now=None):
        return set()

    def pairs(self, mints):
        return {m: self.pairs_by_mint[m] for m in mints if m in self.pairs_by_mint}

    def rug_report(self, mint):
        return self.reports.get(mint)

    def quote(self, input_mint, output_mint, amount, slippage_bps=300):
        if input_mint == SOL:
            usd = amount / 1e9 * self.px[SOL]
            return {"outAmount": str(int(usd / self.px[output_mint] * 1e6 * 0.99)), "priceImpactPct": "0.004"}
        if input_mint in self.no_sell:
            return None
        usd = amount / 1e6 * self.px[input_mint] * 0.99 * (1 - self.sell_haircut.get(input_mint, 0))
        return {"outAmount": str(int(usd / self.px[SOL] * 1e9)), "priceImpactPct": "0.01"}

    def prices(self, mints):
        return {m: {"usd": self.px[m]} for m in [SOL] + list(mints) if m in self.px}


def cfg(**kw):
    c = mb.load_config(None)
    c.update(kw)
    return c


class Rules(unittest.TestCase):
    def test_the_night_is_23_to_7_in_bratislava(self):
        c = cfg()
        self.assertEqual(mb.session_of(T0, c)[:2], ("2026-11-25", 30.0))
        night, into, left = mb.session_of(datetime.datetime(2026, 11, 26, 5, 58), c)     # 06:58
        self.assertEqual((night, round(left)), ("2026-11-25", 2))
        self.assertIsNone(mb.session_of(datetime.datetime(2026, 11, 26, 11, 0), c))      # noon
        self.assertIsNone(mb.session_of(datetime.datetime(2026, 11, 25, 21, 59), c))     # 22:59

    def test_the_universe(self):
        c, now = cfg(), (T0 - datetime.datetime(1970, 1, 1)).total_seconds() * 1000
        self.assertIsNone(mb.universe_ok(mb.pair_facts(pair("a"), now), c))
        self.assertIn("liquidity", mb.universe_ok(mb.pair_facts(pair("a", liq=39000), now), c))
        self.assertIn("5m volume", mb.universe_ok(mb.pair_facts(pair("a", vol5=20000), now), c))
        self.assertIn("older than", mb.universe_ok(mb.pair_facts(pair("a", age_min=7 * 60), now), c))
        self.assertIn("not in the list", mb.universe_ok(mb.pair_facts(pair("a", dex="orca"), now), c))

    def test_the_scanner_needs_buyers_and_a_spike_or_holder_growth(self):
        c, now = cfg(), (T0 - datetime.datetime(1970, 1, 1)).total_seconds() * 1000
        spike = mb.pair_facts(pair("a", vol5=30000, vol1h=60000, age_min=90), now)        # 1h avg 5000: 6x
        self.assertEqual(spike["spike"], 6.0)
        self.assertTrue(mb.scanner_signals(spike, [], c, now / 1000)[0])
        sellers = mb.pair_facts(pair("a", vol5=30000, vol1h=60000, age_min=90, buys=50, sells=50), now)
        self.assertFalse(mb.scanner_signals(sellers, [], c, now / 1000)[0])
        calm = mb.pair_facts(pair("a", vol5=30000, vol1h=300000, age_min=90), now)
        t = now / 1000
        self.assertFalse(mb.scanner_signals(calm, [[t - 700, 400], [t, 420]], c, t)[0])
        ok, sig = mb.scanner_signals(calm, [[t - 700, 400], [t, 480]], c, t)              # +20 % in 10+ min
        self.assertTrue(ok)
        self.assertIn("holders +20%", sig)

    def test_news_trending_and_boosts_count_and_the_socials_are_written(self):
        c, now = cfg(), (T0 - datetime.datetime(1970, 1, 1)).total_seconds() * 1000
        p = pair("tr", vol5=30000, vol1h=300000, age_min=90)                         # no spike, no holder growth
        p["info"] = {"socials": [{"type": "twitter", "url": "https://x.com/trcoin"}, {"type": "telegram", "url": "t.me/x"}]}
        fx = mb.pair_facts(p, now)
        self.assertFalse(mb.scanner_signals(fx, [], c, now / 1000)[0])
        ok, sig = mb.scanner_signals(fx, [], c, now / 1000, trending={"tr"})
        self.assertEqual((ok, sig), (True, ["trending"]))
        p["boosts"] = {"active": 3}
        self.assertIn("boosted", mb.scanner_signals(mb.pair_facts(p, now), [], c, now / 1000)[1])
        self.assertEqual(mb.news_text(mb.pair_facts(p, now), {"tr"}),
                         "promo: X @trcoin · Telegram · 3 boosts · trending")
        self.assertIn("anonymous", mb.news_text(mb.pair_facts(pair("an"), now)))

    def test_the_rug_check(self):
        c = cfg()
        ok, bad, n = mb.rug_check(report("a"), c)
        self.assertEqual((ok, bad, n), (True, [], 600))                 # the pool's 30 % is not a holder
        self.assertIn("mint authority open", mb.rug_check(report("a", mint_auth="X"), c)[1])
        self.assertIn("freeze authority open", mb.rug_check(report("a", freeze="X"), c)[1])
        self.assertIn("top 10 hold 40%", mb.rug_check(report("a", top=(20, 20)), c)[1])
        self.assertIn("LP locked only 50%", mb.rug_check(report("a", lp=50), c)[1])
        self.assertIn("only 200 holders (needs 300)", mb.rug_check(report("a", holders=200), c)[1])
        self.assertIn("dev holds 6.0%", mb.rug_check(report("a", creator=6e13), c)[1])
        self.assertIn("2 insiders", mb.rug_check(report("a", insiders=2), c)[1])
        self.assertIn("risk: Bundled launch", mb.rug_check(report("a", risks=[{"name": "Bundled launch", "level": "warn"}]), c)[1])
        self.assertFalse(mb.rug_check(None, c)[0])


class Night(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.m = FakeMarket()
        self.out = []
        self.cfg = cfg()

    def coin(self, mint, price=0.001, **kw):
        self.m.px[mint] = price
        self.m.pairs_by_mint[mint] = pair(mint, sym=mint.upper(), price=price, vol1h=kw.pop("vol1h", 60000),
                                          age_min=kw.pop("age_min", 90), **kw)
        self.m.reports[mint] = report(mint)
        self.m.fresh.append(mint)

    def night(self, now):
        n = mb.Night(self.dir, self.cfg, self.m, now, self.out.append)
        n.begin("2026-11-25")
        return n

    def test_it_buys_a_flagged_clean_coin_with_a_quarter_of_the_balance(self):
        self.coin("aaa")
        n = self.night(T0)
        n.tick(400)
        pos = n.state["positions"]["aaa"]
        self.assertAlmostEqual(pos["cost"], 17.5, places=2)                        # 25 % of $70, fee included
        self.assertAlmostEqual(n.state["balance"], 52.5, places=2)
        acts = [(d["agent"], d["action"]) for d in n.state["night"]["decisions"]]
        self.assertIn(("SCANNER", "flag"), acts)
        self.assertIn(("SIZER", "size"), acts)
        self.assertIn(("EXECUTOR", "enter"), acts)
        self.assertIn(("RUG CHECK", "pass"), acts)
        p = mb.read_json(os.path.join(self.dir, "panel.json"), {})
        self.assertEqual((p["live"], len(p["positions"]), p["agents"][0]), (True, 1, "SCANNER"))
        self.assertIn("Meme Bot", p["prompt"])
        self.assertEqual(p["positions"][0]["reason"], "buys 80% · volume 6.0x")
        self.assertEqual((p["positions"][0]["size_pct"], p["night"]["filtered"], p["flags"][0]["coin"]), (25.0, 0, "AAA"))
        self.assertEqual(len(p["curve"]), 1)
        self.assertEqual(p["night"]["last_rug"], {"coin": "AAA", "pass": True})

    def test_a_coin_that_cant_be_sold_is_never_bought(self):
        self.coin("hp")
        self.m.no_sell.add("hp")
        n = self.night(T0)
        n.tick(400)
        self.assertEqual(n.state["positions"], {})
        self.assertIn("honeypot", n.state["night"]["decisions"][-1]["text"])
        self.coin("tax")
        self.m.sell_haircut["tax"] = 0.15                                          # a 15 % sell tax
        n = self.night(T0 + datetime.timedelta(minutes=2))
        n.tick(400)
        self.assertEqual(n.state["positions"], {})
        self.assertIn("would lose", n.state["night"]["decisions"][-1]["text"])

    def test_a_rug_is_rejected_once_and_not_checked_again(self):
        self.coin("rug")
        self.m.reports["rug"] = report("rug", mint_auth="X")
        n = self.night(T0)
        n.tick(400)
        self.assertEqual(n.state["positions"], {})
        self.assertIn("rug", n.state["night"]["entered"])
        rows = [d for d in n.state["night"]["decisions"] if d["agent"] == "RUG CHECK"]
        self.assertIn("mint authority", rows[-1]["text"])

    def test_exits_take_half_then_trail_then_stop(self):
        self.coin("aaa")
        n = self.night(T0)
        n.tick(400)
        cost = n.state["positions"]["aaa"]["cost"]
        self.m.px["aaa"] = 0.0016                                                  # +60 %: half sold
        n = self.night(T0 + datetime.timedelta(minutes=5))
        n.tick(400, scan=False)
        pos = n.state["positions"]["aaa"]
        self.assertTrue(pos["tp1_done"])
        self.m.px["aaa"] = 0.0020                                                  # a new peak
        self.night(T0 + datetime.timedelta(minutes=6)).tick(400, scan=False)
        self.m.px["aaa"] = 0.0015                                                  # 25 % below it: the rest goes
        n = self.night(T0 + datetime.timedelta(minutes=7))
        n.tick(400, scan=False)
        self.assertEqual(n.state["positions"], {})
        t = n.state["night"]["trades"][0]
        self.assertGreater(t["pnl"], 0)
        self.assertIn("trailing", t["why"])
        self.assertAlmostEqual(t["proceeds"] - cost, t["pnl"], places=1)

    def test_a_stop_loss_and_the_loss_streak_pause(self):
        self.cfg["loss_streak"] = 1
        self.coin("aaa")
        n = self.night(T0)
        n.tick(400)
        self.m.px["aaa"] = 0.0007                                                  # -30 %
        n = self.night(T0 + datetime.timedelta(minutes=3))
        n.tick(400, scan=False)
        t = n.state["night"]["trades"][0]
        self.assertIn("stop", t["why"])
        self.assertLess(t["pnl"], 0)
        self.coin("bbb")
        n = self.night(T0 + datetime.timedelta(minutes=4))
        n.tick(400)
        self.assertNotIn("bbb", n.state["positions"])                              # paused after the loss
        self.assertIn("pause", [d["text"] for d in n.state["night"]["decisions"] if d["agent"] == "RISK"][-1])
        for k in range(3):                                                         # the same reason: written once
            self.night(T0 + datetime.timedelta(minutes=5, seconds=20 * k)).tick(400)
        n = self.night(T0 + datetime.timedelta(minutes=6))
        self.assertEqual(sum(1 for d in n.state["night"]["decisions"] if d["agent"] == "RISK" and "pause after" in d["text"]), 1)

    def test_time_stop_and_the_end_of_the_night(self):
        self.coin("aaa")
        self.night(T0).tick(400)
        self.m.px["aaa"] = 0.00105                                                 # +5 % after 46 minutes
        n = self.night(T0 + datetime.timedelta(minutes=46))
        n.tick(400, scan=False)
        self.assertIn("46 min, only", n.state["night"]["trades"][0]["why"])
        self.coin("bbb")
        self.night(T0 + datetime.timedelta(minutes=50)).tick(400)
        n = self.night(T0 + datetime.timedelta(minutes=60))
        n.tick(4)                                                                  # 4 minutes left
        self.assertEqual(n.state["positions"], {})
        self.assertEqual(n.state["night"]["trades"][-1]["why"], "end of night")

    def test_the_risk_limits(self):
        for m in ("a1", "a2", "a3", "a4"):
            self.coin(m)
        n = self.night(T0)
        n.tick(400)
        self.assertEqual(len(n.state["positions"]), 3)                             # max_open
        self.assertIn("3 positions open", n.state["night"]["decisions"][-1]["text"])
        self.cfg["max_open"] = 9
        n = self.night(T0 + datetime.timedelta(minutes=2))
        n.tick(20)                                                                 # last 30 minutes
        self.assertNotIn("a4", n.state["positions"])
        self.assertIn("last 30 min", n.state["night"]["decisions"][-1]["text"])

    def test_a_new_night_closes_the_last_one(self):
        self.coin("aaa")
        n = self.night(T0)
        n.tick(400)
        n.close_part("aaa", 1.0, "test", 100.0)
        n.save()
        n2 = mb.Night(self.dir, self.cfg, self.m, T0 + datetime.timedelta(days=1), self.out.append)
        n2.begin("2026-11-26")
        self.assertEqual(n2.state["nights"][-1]["id"], "2026-11-25")
        self.assertEqual(n2.state["nights"][-1]["trades"], 1)
        self.assertEqual(n2.state["night"]["id"], "2026-11-26")
        self.assertIn("Meme Bot", mb.report_text(n2.state))


class Run(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def clock(self, start):
        t = [start]
        return (lambda: t[0]), (lambda sec: t.__setitem__(0, t[0] + datetime.timedelta(seconds=sec)))

    def test_outside_the_night_it_does_nothing(self):
        now, sleep = self.clock(datetime.datetime(2026, 11, 25, 12, 0))
        out = []
        self.assertIsNone(mb.run(self.dir, cfg(), market=FakeMarket(), now_fn=now, sleep=sleep, out=out.append))
        self.assertIn("Mimo noci", out[0])

    def market(self, start):
        m = FakeMarket()
        m.px["aaa"] = 0.001
        m.pairs_by_mint["aaa"] = pair("aaa", age_min=90, now=start)
        m.reports["aaa"] = report("aaa")
        m.fresh.append("aaa")
        return m

    def test_a_night_runs_to_its_end_and_a_test_run_sells_at_its_end(self):
        start = datetime.datetime(2026, 11, 26, 5, 0)                             # 06:00, an hour left
        now, sleep = self.clock(start)
        night = mb.run(self.dir, cfg(time_stop_min=999), market=self.market(start), now_fn=now, sleep=sleep,
                       out=lambda *a: None)
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        self.assertEqual((night, st["night"], st["nights"][-1]["id"]), ("2026-11-25", None, "2026-11-25"))
        self.assertEqual((st["positions"], st["nights"][-1]["trades"]), ({}, 1))
        self.assertEqual(st["last_night"]["trades"][0]["why"], "end of night")      # sold before 07:00
        shutil.rmtree(self.dir)
        start = datetime.datetime(2026, 11, 25, 12, 0)
        m = self.market(start)
        now, sleep = self.clock(start)
        night = mb.run(self.dir, cfg(), minutes=3, anytime=True, market=m, now_fn=now, sleep=sleep,
                       out=lambda *a: None)
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        self.assertEqual(night, "2026-11-25-test")
        self.assertEqual((st["positions"], st["nights"][-1]["trades"]), ({}, 1))


if __name__ == "__main__":
    unittest.main()
