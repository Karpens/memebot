"""memebot/bot.py tests, offline: a simulated market (prices, quotes, RugCheck reports) and a
pinned clock. The night 23:00 to 07:00 is Bratislava time (UTC+1 in winter)."""

import datetime
import io
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

from _load import load

os.environ["MEMEBOT_LIVE"] = "0"                        # the tests never reach the public brokers
os.environ["MEMEBOT_FEEDS"] = "0"                       # ... nor PumpPortal or the Solana RPC
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
    """The defaults, with the migrated strategy these tests are about (New Pairs, the default strategy
    since, has its own tests in test_newpairs_engine.py)."""
    c = mb.load_config(None)
    c["strategies"] = ["migrated"]
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

    def test_a_coin_can_be_bought_again_after_its_wait(self):
        self.cfg["reenter_min"] = 20
        self.coin("aaa")
        n = self.night(T0)
        n.tick(400)
        n.close_part("aaa", 1.0, "test", 100.0)
        n.save()
        n = self.night(T0 + datetime.timedelta(minutes=10))
        n.tick(400)
        self.assertNotIn("aaa", n.state["positions"])                              # still waiting
        n = self.night(T0 + datetime.timedelta(minutes=21))
        n.tick(400)
        self.assertIn("aaa", n.state["positions"])                                 # bought again

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
        self.assertEqual(n2.state["rug_cache"], {})                                # a day-old report is of no use

    def test_a_stale_rugcheck_report_is_never_trusted(self):
        """A scan asks RugCheck about 8 coins at most: any other coin is judged on a cached report only
        while it is under 10 minutes old (an hour-old clean one would miss a mint authority opened since)."""
        self.cfg.update(max_open=20, max_trades_per_night=99, position_pct=4.0, min_order_usd=1.0)
        for i in range(8):
            self.coin("busy%d" % i, vol5=90000 + i)
        self.coin("old")
        self.m.reports["old"] = report("old", mint_auth="X")                       # what RugCheck says now
        n = self.night(T0)
        n.state["rug_cache"]["old"] = {"at": n.epoch() - 3600, "report": mb.trim_report(report("old"))}
        sc = mb.Scanner(self.m, self.cfg, threaded=False)
        found = sc.fetch(n.scan_job())
        self.assertNotIn("old", found["reports"])                                  # the 8 busiest were asked
        n.scan(self.m.prices([]), 400, found)
        self.assertNotIn("old", n.state["positions"])
        self.assertNotIn("old", n.state["night"]["entered"])                       # not judged: no ban either
        n.now = T0 + datetime.timedelta(seconds=5)
        n.scan(self.m.prices([]), 400, sc.fetch(n.scan_job()))                     # the next scan asks about it
        self.assertNotIn("old", n.state["positions"])
        self.assertIn("old", n.state["night"]["entered"])
        self.assertIn("mint authority", [r["text"] for r in n.state["night"]["decisions"] if r["coin"] == "OLD"][-1])

    def test_buys_keep_room_in_the_jupiter_minute_for_the_open_coins_sales(self):
        asked = []
        self.m.can_quote = lambda k: asked.append(k) or True
        for c in ("a1", "a2"):
            self.coin(c)
        self.night(T0).tick(400)
        self.assertEqual(asked, [3, 4])                                            # the 2nd buy keeps one for the 1st coin

    def test_a_stop_is_sold_at_once_when_the_buys_took_the_jupiter_minute(self):
        web = FakeWeb()
        m = mb.Market(fetch=web, cfg=self.cfg, clock=web.clock)
        m.sol = (web.t, {"usd": 150.0})
        n = mb.Night(self.dir, self.cfg, m, T0, self.out.append)
        n.begin("2026-11-25")
        n.state["positions"]["meme"] = {"mint": "meme", "symbol": "MEME", "name": "", "raw": 10 ** 9, "decimals": 6,
                                        "tokens_ui": 1000.0, "cost": 10.0, "entry_price": 0.01, "opened": n.ts(),
                                        "peak": 0.01, "last": 0.01, "tp1_done": False, "sold_usd": 0.0, "reason": ""}
        while m.jup.take():                                                        # a burst of buys: 30 of 30
            pass
        n.manage(m.live_prices(["meme"]), 400)                                     # -79 %: under the stop
        self.assertEqual(n.state["positions"], {})
        self.assertIn("stop", n.state["night"]["trades"][0]["why"])
        self.assertEqual([r for r in n.state["night"]["decisions"] if r["action"] == "stuck"], [])

    def test_the_cash_running_out_is_said_once_and_the_balance_never_goes_under_0(self):
        self.cfg.update(position_pct=100.0, min_order_usd=1.0, max_open=9, start_balance=10.123456789)
        self.coin("aaa")
        n = self.night(T0)                                                         # all of it goes into one buy
        n.tick(400)
        self.assertIn("aaa", n.state["positions"])
        self.assertGreaterEqual(n.state["balance"], 0.0)
        for k in range(5):                                                         # five more scans, 5 s apart
            self.coin("b%d" % k)
            n.now = T0 + datetime.timedelta(seconds=5 * (k + 1))
            n.tick(400)
        said = [r["text"] for r in n.state["night"]["decisions"] if r["agent"] == "SIZER" and r["action"] == "skip"]
        self.assertEqual(said, ["only $0.00 left to size"])


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

    def test_around_the_clock_the_day_rolls_over_at_midnight_without_a_sale(self):
        """session 00:00 → 00:00 (24/7): a coin held at 23:58 is not sold for the day's end, the run goes on
        past midnight, the day closes into nights and the next day starts with the coin still held."""
        c = cfg(time_stop_min=999, session_start="00:00", session_end="00:00")
        self.assertTrue(mb.always_on(c))
        self.assertFalse(mb.always_on(cfg()))
        start = datetime.datetime(2026, 11, 25, 22, 50)                           # 23:50 in Bratislava
        self.assertEqual(mb.session_of(start, c)[0], "2026-11-25")
        now, sleep = self.clock(start)
        night = mb.run(self.dir, c, minutes=15, market=self.market(start), now_fn=now, sleep=sleep,
                       out=lambda *a: None)
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        self.assertEqual(night, "2026-11-26")                                       # it ran on into the next day
        self.assertEqual(st["nights"][-1]["id"], "2026-11-25")                      # the day before is closed
        self.assertEqual(st["night"]["id"], "2026-11-26")
        self.assertEqual(len(st["positions"]), 1)                                   # held over midnight
        self.assertFalse(any(t["why"] == "end of night" for n in (st.get("last_night") or {},) for t in n.get("trades", [])))
        self.assertEqual(mb.panel(st, c, {}, "x")["session"], "24/7")

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

    def test_a_test_run_sells_everything_even_when_a_sale_fails_at_first(self):
        """A coin left open would keep the test night open, and the next real night would take it over."""
        start = datetime.datetime(2026, 11, 25, 12, 0)
        m = self.market(start)
        now, step = self.clock(start)
        end = start + datetime.timedelta(minutes=3)
        later = []

        def sleep(sec):
            step(sec)
            if now() == start + datetime.timedelta(seconds=30):
                m.no_sell.add("aaa")                                               # bought; now no route for a while
            if now() > end:
                later.append(now())
                if len(later) == 2:
                    m.no_sell.discard("aaa")
        night = mb.run(self.dir, cfg(), minutes=3, anytime=True, market=m, now_fn=now, sleep=sleep, out=lambda *a: None)
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        self.assertEqual((night, st["night"], st["positions"]), ("2026-11-25-test", None, {}))
        self.assertEqual(st["last_night"]["trades"][0]["why"], "end of test")
        self.assertEqual(later, [end + datetime.timedelta(seconds=5), end + datetime.timedelta(seconds=10)])
        self.assertIn("stuck", [r["action"] for r in st["last_night"]["decisions"]])

    def test_a_step_that_fails_every_second_is_saved_every_5_seconds(self):
        class Broken(FakeMarket):
            def prices(self, mints):
                raise ValueError("a bad answer")
        now, sleep = self.clock(T0)
        writes = []
        real = mb.write_json

        def counted(path, doc):
            if path.endswith("state.json"):
                writes.append(now())
            real(path, doc)
        with mock.patch.object(mb, "write_json", counted):
            mb.run(self.dir, cfg(), minutes=1, market=Broken(), now_fn=now, sleep=sleep, out=lambda *a: None)
        self.assertTrue(12 <= len(writes) <= 14, len(writes))                     # not 60
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        errors = [r["text"] for r in st["night"]["decisions"] if r["action"] == "error"]
        self.assertEqual(errors, ["error in a step (ValueError: a bad answer) · going on"])


def until(cond, timeout=5.0):
    """Waits for cond() to be true (a thread's work), at most timeout seconds."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeBroker(object):
    """A tiny MQTT 3.1.1 broker on 127.0.0.1 for the tests: answers CONNECT with CONNACK `code`,
    PINGREQ with PINGRESP, records every CONNECT and PUBLISH, and with die_after closes a
    connection after that many PUBLISH packets. silent: a hung broker that takes every byte and
    answers no PINGREQ."""

    def __init__(self, code=0, die_after=None, silent=False):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.code, self.die_after, self.silent = code, die_after, silent
        self.connects, self.publishes, self.pings, self.bye = [], [], 0, 0
        threading.Thread(target=self.accept, daemon=True).start()

    def close(self):
        self.srv.close()

    def accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    @staticmethod
    def read(c, n):
        buf = b""
        while len(buf) < n:
            d = c.recv(n - len(buf))
            if not d:
                raise EOFError
            buf += d
        return buf

    def packet(self, c):
        first = self.read(c, 1)[0]
        length, mult = 0, 1
        while True:
            b = self.read(c, 1)[0]
            length += (b & 0x7F) * mult
            mult *= 128
            if not b & 0x80:
                break
        return first, self.read(c, length)

    def serve(self, c):
        sent = 0
        try:
            while True:
                first, body = self.packet(c)
                kind = first >> 4
                if kind == 1:                                       # CONNECT
                    pl = struct.unpack("!H", body[:2])[0]
                    level, flags = body[2 + pl], body[3 + pl]
                    keep = struct.unpack("!H", body[4 + pl:6 + pl])[0]
                    cl = struct.unpack("!H", body[6 + pl:8 + pl])[0]
                    self.connects.append({"proto": body[2:2 + pl].decode(), "level": level, "flags": flags,
                                          "keepalive": keep, "client": body[8 + pl:8 + pl + cl].decode()})
                    c.sendall(bytes([0x20, 2, 0, self.code]))
                    if self.code:
                        return
                elif kind == 3:                                     # PUBLISH, QoS 0: no packet id
                    tl = struct.unpack("!H", body[:2])[0]
                    self.publishes.append((body[2:2 + tl].decode(), json.loads(body[2 + tl:].decode()), bool(first & 1),
                                           first & 0x06))
                    sent += 1
                    if self.die_after and sent >= self.die_after:
                        return
                elif kind == 12:                                    # PINGREQ
                    self.pings += 1
                    if not self.silent:
                        c.sendall(b"\xd0\x00")
                elif kind == 14:                                    # DISCONNECT
                    self.bye += 1
                    return
        except (EOFError, OSError):
            pass
        finally:
            c.close()

    def topics(self, sub):
        return [p for p in self.publishes if p[0].endswith("/" + sub)]


def live_cfg(*ports, **kw):
    return cfg(live_topic="memebot/test", live_brokers=[{"host": "127.0.0.1", "port": p, "tls": False,
                                                         "ws": "ws://127.0.0.1:%d/mqtt" % p} for p in ports], **kw)


class LiveChannel(unittest.TestCase):
    """The MQTT publisher against an in-process fake broker: offline, no packages."""

    def test_the_mqtt_bytes(self):
        for n, want in ((0, "00"), (127, "7f"), (128, "8001"), (16383, "ff7f"), (16384, "808001"),
                        (2097151, "ffff7f"), (2097152, "80808001"), (268435455, "ffffff7f")):
            self.assertEqual(mb.mqtt_length(n).hex(), want)
        self.assertEqual(mb.mqtt_connect("memebot1"), b"\x10\x14\x00\x04MQTT\x04\x02\x00\x3c\x00\x08memebot1")
        self.assertEqual(mb.mqtt_publish("a/b", b"{}", retain=True), b"\x31\x07\x00\x03a/b{}")
        self.assertEqual(mb.mqtt_publish("a/b", b"x" * 200)[:3], b"\x30\xcd\x01")   # 2 + 3 + 200 = 205

    def test_the_panel_is_retained_and_the_ticks_are_not(self):
        fb = FakeBroker()
        self.addCleanup(fb.close)
        live = mb.Live(live_cfg(fb.port))
        live.panel({"kind": "panel", "n": 1})
        self.assertTrue(until(lambda: fb.topics("panel")))
        for k in range(3):
            live.tick({"kind": "tick", "seq": k + 1})
            self.assertTrue(until(lambda: len(fb.topics("tick")) == k + 1))
        live.stop()
        c = fb.connects[0]
        self.assertEqual((c["proto"], c["level"], c["flags"], c["keepalive"]), ("MQTT", 4, 0x02, 60))
        self.assertTrue(c["client"].startswith("memebot-") and len(c["client"]) <= 23)
        self.assertEqual(fb.topics("panel")[0][1:], ({"kind": "panel", "n": 1}, True, 0))           # retained, QoS 0
        self.assertEqual([(p[0], p[1]["seq"], p[2], p[3]) for p in fb.topics("tick")],
                         [("memebot/test/tick", k, False, 0) for k in (1, 2, 3)])
        st = live.stats()["127.0.0.1"]
        self.assertEqual((st["sent"], st["errors"]), (4, 0))
        self.assertTrue(until(lambda: fb.bye == 1))                                   # a clean DISCONNECT

    def test_only_the_latest_message_of_a_topic_waits(self):
        b = mb.Broker("127.0.0.1", free_port(), tls=False)                            # nothing listens there
        self.addCleanup(b.stop)
        for k in range(50):
            b.publish("t/tick", str(k).encode())
        b.publish("t/panel", b"p", retain=True)
        self.assertEqual(b.slots, {"t/tick": (b"49", False), "t/panel": (b"p", True)})

    def test_a_broker_that_refuses_or_dies_never_raises(self):
        refusing = FakeBroker(code=5)                                                  # CONNACK: not authorized
        dying = FakeBroker(die_after=1)
        self.addCleanup(refusing.close)
        self.addCleanup(dying.close)
        nobody = free_port()
        live = mb.Live(cfg(live_topic="memebot/test", live_brokers=[
            {"host": "127.0.0.1", "port": nobody, "tls": False}, {"host": "127.0.0.1", "port": refusing.port, "tls": False},
            {"host": "127.0.0.1", "port": dying.port, "tls": False}]))
        t = time.monotonic()
        for k in range(20):
            self.assertTrue(live.tick({"seq": k}))
        live.panel({"x": 1})
        self.assertLess(time.monotonic() - t, 0.1)                                     # publishing never waits
        self.assertFalse(live.send("tick", {"bad": float("nan")}))                     # invalid JSON is dropped
        self.assertTrue(until(lambda: live.stats()["127.0.0.1:%d" % nobody]["errors"] >= 1))
        self.assertTrue(until(lambda: live.stats()["127.0.0.1:%d" % refusing.port]["errors"] >= 1))
        self.assertIn("refused", live.stats()["127.0.0.1:%d" % nobody]["last_error"].lower())
        self.assertIn("code 5", live.stats()["127.0.0.1:%d" % refusing.port]["last_error"])
        live.tick({"seq": 99})
        self.assertTrue(until(lambda: len(dying.connects) >= 2, timeout=6))           # it reconnects after 1 s
        self.assertGreaterEqual(live.stats()["127.0.0.1:%d" % dying.port]["errors"], 1)
        self.assertTrue(until(lambda: len(dying.topics("panel")) >= 2))               # the retained panel again
        live.stop()

    def test_it_pings_when_idle(self):
        fb = FakeBroker()
        self.addCleanup(fb.close)
        b = mb.Broker("127.0.0.1", fb.port, tls=False, ping_after=0.2)
        self.addCleanup(b.stop)
        self.assertTrue(until(lambda: fb.pings >= 2, timeout=5))
        self.assertTrue(b.stats["connected"])
        self.assertEqual(b.stats["errors"], 0)                                          # each PINGRESP was read

    def test_a_broker_that_stops_answering_is_dropped_while_ticks_flow(self):
        """QoS 0 publishes get no answer: only the pings show a hung broker, so they go out with
        ticks flowing too (a tick a second used to hold every ping back, and stats said connected)."""
        fb = FakeBroker(silent=True)
        self.addCleanup(fb.close)
        b = mb.Broker("127.0.0.1", fb.port, tls=False, ping_after=0.3)
        self.addCleanup(b.stop)
        end = time.monotonic() + 5
        while time.monotonic() < end and len(fb.connects) < 2:
            b.publish("t/tick", b"{}")
            time.sleep(0.05)
        self.assertGreaterEqual(len(fb.connects), 2)                                    # dropped and made again
        self.assertGreaterEqual(fb.pings, 1)
        self.assertIn("no PINGRESP", b.stats["last_error"])
        self.assertGreater(len(fb.topics("tick")), 3)


class LiveNight(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.m = FakeMarket()
        self.m.px["aaa"] = 0.001
        self.m.pairs_by_mint["aaa"] = pair("aaa", sym="AAA", age_min=90)
        self.m.reports["aaa"] = report("aaa")
        self.m.fresh.append("aaa")

    def test_every_log_row_has_its_line_number(self):
        n = mb.Night(self.dir, cfg(), self.m, T0, lambda *a: None)
        n.begin("2026-11-25")
        n.tick(400)
        rows = n.state["night"]["decisions"]
        self.assertEqual([r["n"] for r in rows], list(range(1, len(rows) + 1)))
        self.assertEqual(rows[-1]["n"], n.state["night"]["lines"])

    def test_the_tick_message(self):
        n = mb.Night(self.dir, cfg(), self.m, T0, lambda *a: None)
        n.begin("2026-11-25")
        n.tick(400)
        prices = self.m.prices(["aaa"])
        memo = {}
        t1 = mb.tick_msg(n.state, n.cfg, prices, n.ts(), 1, memo)
        self.assertEqual(set(t1), {"v", "kind", "seq", "updated", "equity", "balance", "live", "night", "positions",
                                   "log", "trades_n", "trades", "flags", "curve_last", "speed", "learner", "watch"})
        self.assertEqual(t1["watch"], [])                                              # New Pairs' rows: none here
        self.assertEqual((t1["v"], t1["kind"], t1["seq"], t1["live"], t1["trades_n"], t1["trades"]), (1, "tick", 1, True, 0, []))
        pos = t1["positions"][0]
        self.assertEqual((pos["mint"], pos["symbol"], "hist" in pos), ("aaa", "AAA", False))
        p = mb.panel(n.state, n.cfg, prices, n.ts())
        self.assertEqual(t1["night"], p["night"])
        self.assertEqual({k: v for k, v in p["positions"][0].items() if k != "hist"}, pos)
        self.assertEqual([r["n"] for r in t1["log"]], [r["n"] for r in n.state["night"]["decisions"]])
        self.assertEqual((t1["speed"]["tick_sec"], t1["curve_last"]), (1, n.state["night"]["curve"][-1]))
        self.assertEqual((t1["learner"]["samples"], t1["learner"]), (0, n.state["learner_summary"]))   # from the start
        self.assertLess(len(json.dumps(t1, separators=(",", ":"))), 3000)
        t2 = mb.tick_msg(n.state, n.cfg, prices, n.ts(), 2, memo)                     # nothing new: no rows, no trades
        self.assertEqual((t2["log"], "trades" in t2), ([], False))
        last = memo["n"]
        n.now = T0 + datetime.timedelta(minutes=1)
        n.close_part("aaa", 1.0, "test", 100.0)
        t3 = mb.tick_msg(n.state, n.cfg, prices, n.ts(), 3, memo)
        self.assertEqual([(r["n"], r["agent"]) for r in t3["log"]], [(last + 1, "EXECUTOR"), (last + 2, "LEARNER")])
        self.assertEqual((t3["trades_n"], t3["trades"][0]["why"], t3["positions"]), (1, "test", []))
        for k in range(60):                                                            # a burst: 40 rows at most
            n.log("LOGGER", None, "x", "row %d" % k)
        t4 = mb.tick_msg(n.state, n.cfg, prices, n.ts(), 4, memo)
        self.assertEqual([r["n"] for r in t4["log"]], list(range(last + 23, last + 63)))
        n2 = mb.Night(self.dir, cfg(), self.m, T0 + datetime.timedelta(days=1), lambda *a: None)
        n2.begin("2026-11-26")                                                         # a new night starts over
        t5 = mb.tick_msg(n2.state, n2.cfg, {}, n2.ts(), 5, memo)
        self.assertEqual([r["n"] for r in t5["log"]], [1])

    def test_a_tick_leaves_out_what_the_last_panel_says(self):
        """The flags, the LEARNER's summary and the speed ride a tick only while they differ from the
        last retained panel: a viewer gets that panel on connect, so a full book stays near 2 KB a tick."""
        n = mb.Night(self.dir, cfg(), self.m, T0, lambda *a: None)
        n.begin("2026-11-25")
        n.tick(400)
        sent = []

        class Rec(object):
            def panel(self, doc):
                sent.append(("panel", doc))

            def tick(self, doc):
                sent.append(("tick", doc))

            def stats(self):
                return {}
        memo, prices = {}, self.m.prices(["aaa"])
        n.publish(prices, Rec(), 1, memo, force=True)
        self.assertEqual([k for k, _ in sent], ["panel", "tick"])
        t1 = sent[-1][1]
        self.assertEqual({"flags", "learner", "speed"} & set(t1), set())
        self.assertEqual((t1["positions"][0]["mint"], t1["trades_n"]), ("aaa", 0))
        n.now = T0 + datetime.timedelta(seconds=1)
        n.state["night"]["flags"].append({"coin": "NEW", "why": "trending", "t": n.ts()})
        n.speed["watch_ms"] = 12                                                       # timings come with the panels
        n.publish(prices, Rec(), 2, memo)
        kind, t2 = sent[-1]
        self.assertEqual((kind, t2["flags"][-1]["coin"], "learner" in t2, "speed" in t2), ("tick", "NEW", False, False))
        n.now = T0 + datetime.timedelta(seconds=2)
        n.publish(prices, Rec(), 3, memo)
        self.assertEqual(sent[-1][1]["flags"][-1]["coin"], "NEW")                     # until a panel has it
        n.now = T0 + datetime.timedelta(seconds=16)
        n.publish(prices, Rec(), 4, memo)
        self.assertEqual([k for k, _ in sent[-2:]], ["panel", "tick"])
        self.assertNotIn("flags", sent[-1][1])
        self.assertIn("flags", mb.tick_msg(n.state, n.cfg, prices, n.ts(), 5, {}))      # no panel yet: all of it

    def test_the_panel_says_where_the_stream_is_and_how_fast(self):
        n = mb.Night(self.dir, cfg(), self.m, T0, lambda *a: None)
        n.begin("2026-11-25")
        n.tick(400)
        p = mb.read_json(os.path.join(self.dir, "panel.json"), {})
        self.assertEqual(p["stream"], {"topic": "memebot/00ade2c3a60b7da8", "enabled": False,     # MEMEBOT_LIVE=0
                                       "ws": ["wss://broker.hivemq.com:8884/mqtt", "wss://broker.emqx.io:8084/mqtt"]})
        sp = p["speed"]
        self.assertEqual((sp["watch_sec"], sp["tick_sec"], sp["scan_sec"]), (1, 1, 5))
        self.assertEqual(sp["feeds"], ["geckoterminal", "dexscreener", "rugcheck", "jupiter"])
        self.assertIn("live 1s", sp["feed"])
        self.assertEqual((p["learner"]["samples"], p["learner"]["min_samples"], p["live_stats"]), (0, 8, {}))
        self.assertIn("LEARNER", p["agents"])
        pos = p["positions"][0]
        self.assertEqual((pos["mint"], pos["hist"]), ("aaa", [pos["entry_price"]]))
        n.state["learner_summary"] = {"samples": 3}
        self.assertEqual(mb.panel(n.state, n.cfg, {}, n.ts())["learner"], {"samples": 3})

    def test_a_night_publishes_its_panel_and_ticks(self):
        fb = FakeBroker()
        self.addCleanup(fb.close)
        start = datetime.datetime(2026, 11, 25, 12, 0)
        t = [start]
        self.m.pairs_by_mint["aaa"] = pair("aaa", sym="AAA", age_min=90, now=start)
        c = live_cfg(fb.port)

        def sleep(sec):
            time.sleep(0.002)                   # a breath, so the broker's thread sends now and then
            t[0] += datetime.timedelta(seconds=sec)
        night = mb.run(self.dir, c, minutes=1, anytime=True, market=self.m, now_fn=lambda: t[0], sleep=sleep,
                       out=lambda *a: None, live=mb.Live(c))
        self.assertEqual(night, "2026-11-25-test")
        self.assertTrue(until(lambda: fb.bye == 1))
        panels, ticks = fb.topics("panel"), fb.topics("tick")
        self.assertTrue(all(p[2] for p in panels) and not any(p[2] for p in ticks))
        first = panels[0][1]
        self.assertEqual((first["stream"]["topic"], first["stream"]["ws"], first["stream"]["enabled"]),
                         ("memebot/test", ["ws://127.0.0.1:%d/mqtt" % fb.port], True))
        self.assertEqual(panels[-1][1]["trades"][-1]["why"], "end of test")            # the last word is the panel
        self.assertGreaterEqual(len(panels), 3)                                        # start, buy, every 15 s, end
        seqs = [p[1]["seq"] for p in ticks]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertGreaterEqual(len(ticks), 2)
        held = [p[1] for p in ticks if p[1]["positions"]]
        self.assertTrue(held and held[0]["positions"][0]["mint"] == "aaa" and "hist" not in held[0]["positions"][0])
        rows = [r["n"] for p in ticks for r in p[1]["log"]]
        self.assertEqual(rows, sorted(set(rows)))                                      # no row twice
        self.assertEqual(panels[-1][1]["live_stats"]["127.0.0.1"]["errors"], 0)

    def test_a_night_with_a_dead_broker_runs_as_usual(self):
        c = live_cfg(free_port())
        start = datetime.datetime(2026, 11, 25, 12, 0)
        t = [start]
        self.m.pairs_by_mint["aaa"] = pair("aaa", sym="AAA", age_min=90, now=start)
        live = mb.Live(c)
        night = mb.run(self.dir, c, minutes=1, anytime=True, market=self.m, now_fn=lambda: t[0],
                       sleep=lambda s: t.__setitem__(0, t[0] + datetime.timedelta(seconds=s)), out=lambda *a: None,
                       live=live)
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        self.assertEqual((night, st["nights"][-1]["trades"]), ("2026-11-25-test", 1))
        p = mb.read_json(os.path.join(self.dir, "panel.json"), {})
        self.assertFalse(p["live_stats"]["127.0.0.1"]["connected"])


class SlowMarket(FakeMarket):
    """A FakeMarket whose pairs() takes `slow` seconds from its slow_from-th call, and which notes
    the threads each call ran on."""

    def __init__(self, slow=0.0, slow_from=2):
        FakeMarket.__init__(self)
        self.slow, self.slow_from = slow, slow_from
        self.calls = 0
        self.threads = {"pairs": set(), "rug_report": set(), "quote": set(), "prices": set()}

    def pairs(self, mints):
        self.threads["pairs"].add(threading.current_thread().name)
        self.calls += 1
        if self.calls >= self.slow_from and self.slow:
            time.sleep(self.slow)
        return FakeMarket.pairs(self, mints)

    def rug_report(self, mint):
        self.threads["rug_report"].add(threading.current_thread().name)
        return FakeMarket.rug_report(self, mint)

    def quote(self, *a, **kw):
        self.threads["quote"].add(threading.current_thread().name)
        return FakeMarket.quote(self, *a, **kw)

    def prices(self, mints):
        self.threads["prices"].add(threading.current_thread().name)
        return FakeMarket.prices(self, mints)


class Speed(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def coin(self, m, mint, now=T0):
        m.px[mint] = 0.001
        m.pairs_by_mint[mint] = pair(mint, sym=mint.upper(), age_min=90, now=now)
        m.reports[mint] = report(mint)
        m.fresh.append(mint)

    def test_the_scanner_thread_fetches_and_the_main_thread_trades(self):
        m = SlowMarket(slow=0.3, slow_from=1)                              # still fetching when watch() looks
        self.coin(m, "aaa")
        n = mb.Night(self.dir, cfg(), m, T0, lambda *a: None)
        n.begin("2026-11-25")
        sc = mb.Scanner(m, n.cfg)
        sc.first = False                                                    # this test wants the thread from the start
        n.watch(400, sc)
        self.assertTrue(until(lambda: sc.done.qsize() == 1))
        self.assertEqual(n.state["positions"], {})                          # fetched, not applied: the book is untouched
        self.assertEqual(n.state["night"]["scans"], 0)
        n.now = T0 + datetime.timedelta(seconds=1)
        n.watch(400, sc)                                                    # the next second applies it
        self.assertIn("aaa", n.state["positions"])
        main = threading.current_thread().name
        self.assertNotIn(main, m.threads["pairs"] | m.threads["rug_report"])
        self.assertEqual(m.threads["quote"] | m.threads["prices"], {main})
        sc.stop()

    def test_the_first_scan_is_waited_for_a_moment_not_for_ever(self):
        m = SlowMarket()                                                    # a quick first scan: the first second buys
        self.coin(m, "aaa")
        n = mb.Night(self.dir, cfg(), m, T0, lambda *a: None)
        n.begin("2026-11-25")
        sc = mb.Scanner(m, n.cfg)
        n.watch(400, sc)
        self.assertIn("aaa", n.state["positions"])
        self.assertNotIn(threading.current_thread().name, m.threads["pairs"])   # on the scanner thread all the same
        sc.stop()
        shutil.rmtree(self.dir)
        m = SlowMarket(slow=1.0, slow_from=1)                               # DexScreener hanging at the start
        self.coin(m, "aaa")
        n = mb.Night(self.dir, cfg(), m, T0, lambda *a: None)
        n.begin("2026-11-25")
        sc = mb.Scanner(m, n.cfg)
        sc.first_wait = 0.2
        t = time.monotonic()
        n.watch(400, sc)
        self.assertLess(time.monotonic() - t, 0.8)                          # the second goes on
        self.assertEqual(n.state["positions"], {})
        self.assertTrue(until(lambda: sc.done.qsize() == 1))
        n.now = T0 + datetime.timedelta(seconds=1)
        n.watch(400, sc)                                                    # applied when it lands
        self.assertIn("aaa", n.state["positions"])
        sc.stop()

    def test_the_loop_keeps_its_second_while_a_scan_is_slow(self):
        m = SlowMarket(slow=1.0)
        self.coin(m, "aaa", now=mb.utc_now())
        m.fresh.append("zzz")                                               # no pair: in every scan, never bought
        seen = []

        class Clock(object):
            def panel(self, doc):
                pass

            def tick(self, doc):
                seen.append((time.monotonic(), doc["seq"]))

            def stats(self):
                return {}

            def stop(self):
                pass
        c = cfg(watch_sec=0.2, scan_every_sec=0.2)
        mb.run(self.dir, c, minutes=2.4 / 60, anytime=True, market=m, out=lambda *a: None, live=Clock())
        gaps = [b[0] - a[0] for a, b in zip(seen, seen[1:])]
        self.assertGreaterEqual(len(seen), 9)                                # 2.4 s at 0.2 s, with 1 s scans going on
        self.assertLess(max(gaps), 0.35)
        self.assertGreaterEqual(m.calls, 2)                                  # a slow scan was in flight meanwhile

    def test_one_night_object_lives_the_whole_run(self):
        made = []
        base = mb.Night

        class Counted(base):
            def __init__(self, *a, **kw):
                made.append(1)
                base.__init__(self, *a, **kw)
        m = FakeMarket()
        start = datetime.datetime(2026, 11, 25, 12, 0)
        self.coin(m, "aaa", now=start)
        t = [start]
        writes = []
        real = mb.write_json

        def counted(path, doc):
            if path.endswith("state.json"):
                writes.append(t[0])
            real(path, doc)
        with mock.patch.object(mb, "Night", Counted), mock.patch.object(mb, "write_json", counted):
            mb.run(self.dir, cfg(), minutes=1, anytime=True, market=m, now_fn=lambda: t[0],
                   sleep=lambda s: t.__setitem__(0, t[0] + datetime.timedelta(seconds=s)), out=lambda *a: None)
        self.assertEqual(len(made), 1)
        # 60 seconds: every 5 s (12), the buy came in the first second's save, and the end
        self.assertTrue(12 <= len(writes) <= 14, len(writes))
        self.assertEqual(writes[0], start)
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        self.assertEqual(st["nights"][-1]["trades"], 1)

    def test_a_trade_is_saved_at_once(self):
        m = FakeMarket()
        start = datetime.datetime(2026, 11, 25, 12, 0)
        self.coin(m, "aaa", now=start)
        t = [start]
        writes = []
        real = mb.write_json

        def counted(path, doc):
            if path.endswith("state.json"):
                writes.append((t[0], len(doc.get("positions") or {})))
            real(path, doc)

        def sleep(s):
            t[0] += datetime.timedelta(seconds=s)
            if t[0] == start + datetime.timedelta(seconds=7):
                m.px["aaa"] = 0.0005                                         # -50 %: the stop at second 7
        with mock.patch.object(mb, "write_json", counted):
            mb.run(self.dir, cfg(), minutes=0.2, anytime=True, market=m, now_fn=lambda: t[0], sleep=sleep,
                   out=lambda *a: None)
        at = [w[0] - start for w in writes]
        self.assertIn(datetime.timedelta(seconds=7), at)                     # the exit, between the 5 s saves
        self.assertEqual(writes[at.index(datetime.timedelta(seconds=7))][1], 0)

    def test_a_stuck_sale_is_tried_every_5_seconds_not_every_second(self):
        m = SlowMarket()
        self.coin(m, "aaa")
        n = mb.Night(self.dir, cfg(), m, T0, lambda *a: None)
        n.begin("2026-11-25")
        n.tick(400)
        m.no_sell.add("aaa")
        m.px["aaa"] = 0.0005
        sc = mb.Scanner(m, n.cfg, threaded=False)
        tries = []
        real = m.quote
        m.quote = lambda *a, **kw: tries.append(n.now) or real(*a, **kw)
        for k in range(1, 21):
            n.now = T0 + datetime.timedelta(seconds=k)
            n.watch(400, sc)
        self.assertEqual(len(tries), 4)                                      # seconds 1, 6, 11, 16
        stuck = [d for d in n.state["night"]["decisions"] if d["action"] == "stuck"]
        self.assertEqual(len(stuck), 1)

    def test_a_full_jupiter_minute_postpones_a_buy_and_rejects_nothing(self):
        m = FakeMarket()
        self.coin(m, "aaa")
        m.can_quote = lambda k: False
        n = mb.Night(self.dir, cfg(), m, T0, lambda *a: None)
        n.begin("2026-11-25")
        n.tick(400)
        self.assertEqual((n.state["positions"], n.state["night"]["entered"]), ({}, []))
        m.can_quote = lambda k: True
        n.now = T0 + datetime.timedelta(seconds=5)
        n.tick(400)
        self.assertIn("aaa", n.state["positions"])

    def test_the_shorter_pause(self):
        mine = mb.load_config()
        self.assertEqual((mine["loss_streak"], mine["cooldown_min"], mine["watch_sec"], mine["scan_every_sec"]), (5, 2, 1, 5))
        with open(mb.PROMPT, encoding="utf-8") as fh:
            self.assertIn("a 2 minute pause after 5 losses in a row", fh.read())
        m = FakeMarket()
        c = cfg(loss_streak=5, cooldown_min=2, max_open=9, position_pct=10.0)
        for k in range(5):
            self.coin(m, "c%d" % k)
        n = mb.Night(self.dir, c, m, T0, lambda *a: None)
        n.begin("2026-11-25")
        n.tick(400)
        self.assertEqual(len(n.state["positions"]), 5)
        for k in range(5):
            m.px["c%d" % k] = 0.0006                                         # -40 %: five stops in a row
        n.now = T0 + datetime.timedelta(minutes=1)
        n.tick(400, scan=False)
        self.assertEqual((len(n.state["night"]["trades"]), n.state["night"]["streak"]), (5, 5))
        self.assertIn("5 losses in a row · 2 min pause", [d["text"] for d in n.state["night"]["decisions"]])
        self.coin(m, "nxt")
        n.now = T0 + datetime.timedelta(minutes=2, seconds=50)
        n.tick(400)
        self.assertNotIn("nxt", n.state["positions"])                        # still in the pause
        n.now = T0 + datetime.timedelta(minutes=3, seconds=1)
        n.tick(400)
        self.assertIn("nxt", n.state["positions"])                           # 2 minutes later it buys again

    def test_one_night_object_still_moves_to_the_next_night(self):
        m = FakeMarket()
        n = mb.Night(self.dir, cfg(), m, T0, lambda *a: None)
        n.begin("2026-11-25")
        n.log("LOGGER", None, "x", "one")
        n.now = T0 + datetime.timedelta(days=1)
        n.begin("2026-11-26")
        self.assertEqual((n.state["nights"][-1]["id"], n.state["night"]["id"]), ("2026-11-25", "2026-11-26"))
        self.assertEqual([r["n"] for r in n.state["night"]["decisions"]], [1])

    def test_the_prompt_says_what_config_json_does(self):
        """prompt.txt (New Pairs since the strategy changed) says the numbers config.json trades by."""
        c = mb.load_config()
        with open(mb.PROMPT, encoding="utf-8") as fh:
            p = fh.read()
        tiers = {t["name"]: t for t in c["hold_tiers"]}

        def says(t):
            """A ladder in the prompt's words: "34% at +10%, 50% at +20%, the rest at +60%"."""
            steps = ["%.0f%% at +%g%%" % (frac * 100, gain) for gain, frac in t["ladder"][:-1]]
            steps.append("the rest at +%g%%" % t["ladder"][-1][0])
            return (", " if len(steps) > 2 else " and ").join(steps)

        self.assertEqual(c["strategies"], ["new_pairs"])
        for text in ("starting balance of $%g." % c["start_balance"],
                     "Every new coin is watched for %d seconds (%d at once" % (c["np_watch_s"], c["np_max_watch"]),
                     "every %g seconds while anything is held, every second otherwise" % c["curve_poll_sec"],
                     "within %d seconds of the launch" % c["np_max_age_s"],
                     "Never before %d seconds after the launch" % c["np_min_age_s"], "at least %g SOL came in" % c["np_min_sol"],
                     "at least %d readings were buys and %.0f%% of the moves" % (c["np_min_buys"], c["np_min_buy_share"] * 100),
                     "at least %g%% above the launch price" % c["np_min_rise_pct"],
                     "market cap is at most %g SOL" % c["np_max_mcap_sol"],
                     "the creator bought at most %g%% of the supply" % c["np_max_dev_pct"],
                     "no single sell of %g SOL or more" % c["np_dump_sol"],
                     "%d or more other launches in the last %d minutes" % (c["np_serial_max"] + 1, c["np_serial_window_min"]),
                     "a RugCheck report within %g seconds" % c["np_rug_timeout_s"],
                     "no single holder above %g%%" % c["np_max_holder_pct"],
                     "the top 10 holders at most %g%%" % c["np_max_top10_pct"],
                     "may lose at most %g%%" % c["max_roundtrip_loss_pct"],
                     "%g%% of the balance per trade, never less than $%g" % (c["position_pct"], c["min_order_usd"]),
                     "between %gx and %gx" % (c["learn_mult_min"], c["learn_mult_max"]),
                     "up to %d open positions and %d trades a night" % (c["max_open"], c["max_trades_per_night"]),
                     "a %g minute pause after %d losses in a row" % (c["cooldown_min"], c["loss_streak"]),
                     "no new entries in the last %d minutes" % c["no_entries_last_min"],
                     "stop for the night at %g%%" % c["night_stop_pct"],
                     "the new order could lose another %g%% in one dump" % c["dump_loss_pct"],
                     "lost %g%% or more on average" % -c["learn_skip_pct"], "every %dth such skip" % c["learn_explore_every"],
                     "reads them again every %d seconds" % c["news_refresh_sec"],
                     "take the whole profit at +%g%% (scalp, NEWS 0-2)" % tiers["scalp"]["ladder"][0][0],
                     "swing (NEWS %d-4) %s;" % (tiers["swing"]["min_score"], says(tiers["swing"])),
                     "runner (%d-6) %s;" % (tiers["runner"]["min_score"], says(tiers["runner"])),
                     "moon (%d+) %s." % (tiers["moon"]["min_score"], says(tiers["moon"])),
                     "Stop loss at %g%% until the first step" % c["stop_pct"],
                     "the stop moves to break-even (+%g%%)" % c["breakeven_pct"],
                     "(%g%%, %g%% and %g%% under it for swing, runner and moon)" % tuple(
                         tiers[k]["trail_pct"] for k in ("swing", "runner", "moon")),
                     "Out after %g minutes unless up %g%% (%g, %g and %g minutes for swing, runner and moon)" % (
                         (tiers["scalp"]["time_stop_min"], c["time_stop_below_pct"]) +
                         tuple(tiers[k]["time_stop_min"] for k in ("swing", "runner", "moon"))),
                     "one sell of %g SOL or more after the entry" % c["np_dump_sol"],
                     "buys under %.0f%% of the last %d minutes" % (c["fade_buy_ratio"] * 100, c["fade_window_s"] / 60),
                     "pump.fun's fee is about %g%% each way" % c["curve_fee_pct"],
                     "the %g%% latency haircut each way" % c["latency_slip_pct"],
                     # entry quality (newpairs.py): RugCheck must see the holders, the SOL must come from enough buys
                     "no insiders, and at least %d holders seen, a holder being a wallet that holds the coin" %
                     c["np_min_holders"],
                     "RugCheck is asked again every %g seconds while the trigger holds" % c["np_rug_recheck_s"],
                     "at most %g SOL in for each holder" % c["np_max_sol_per_holder"],
                     # the SHADOW trades (bot.py np_shadow_*)
                     "Coins it skips, and those RISK blocks, are followed as shadow trades, up to %d at once" %
                     c["shadow_max_open"],
                     "teaches it at %s the weight of a real trade" % {0.5: "half", 1.0: "full"}[c["shadow_weight"]],
                     "Run around the clock, 24/7" if mb.always_on(c) else
                     "everything sold by 06:%02d" % (60 - c["flat_before_end_min"])):
            self.assertIn(text, p)
        # the first step moved from +7 % to +10 % for the costs (tests/test_levers.py has the arithmetic)
        for t, ladder in (("scalp", [[10, 1]]), ("swing", [[10, 0.5], [20, 1]]),
                          ("runner", [[10, 0.34], [20, 0.5], [60, 1]]),
                          ("moon", [[10, 0.25], [20, 0.34], [60, 0.5], [100, 1]])):
            self.assertEqual(tiers[t]["ladder"], ladder)
        self.assertEqual((c["tp1_sell_frac"], c["watch_sec"]), (1.0, 1))                # "the whole profit"; not tick_sec
        # the words that hold only for these settings: the one-buy cap off, shadows on, RISK's blocks shadowed too
        self.assertEqual((c["np_max_buy_sol"], c["shadow_on"], c["shadow_on_risk"]), (0.0, True, True))
        self.assertIn("One buy reading's size is not capped on its own", p)

    def test_tick_sec_is_read_as_watch_sec(self):
        path = os.path.join(self.dir, "c.json")
        with open(path, "w") as fh:
            json.dump({"tick_sec": 7}, fh)
        self.assertEqual(mb.watch_sec(mb.load_config(path)), 7)
        self.assertEqual(mb.watch_sec(mb.load_config(None)), 1)


class FakeWeb(object):
    """The real Market against canned answers: Jupiter's price v3 and quote, DexScreener's tokens and
    lists, GeckoTerminal's pools, with a clock the test moves."""

    def __init__(self):
        self.t = 1000.0
        self.calls = []
        self.jup_down = False
        self.quote_fail = None                  # None, "timeout", 400 (no route) or 429
        self.dex_down = False

    def clock(self):
        return self.t

    def __call__(self, url, timeout=20):
        self.calls.append((self.t, url))
        if "lite-api.jup.ag/price" in url:
            if self.jup_down:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b""))
            ids = url.split("ids=")[1].split(",")
            return {m: {"usdPrice": 150.0 if m == SOL else 0.002, "decimals": 9 if m == SOL else 6} for m in ids}
        if "lite-api.jup.ag/swap" in url:
            if self.quote_fail == "timeout":
                raise socket.timeout("timed out")
            if self.quote_fail:
                raise urllib.error.HTTPError(url, self.quote_fail, "no", {}, io.BytesIO(b""))
            return {"outAmount": "1000000", "priceImpactPct": "0.001"}
        if "dexscreener.com/tokens/v1/solana/" in url:
            if self.dex_down:
                raise socket.timeout("timed out")
            mints = url.rsplit("/", 1)[1].split(",")
            return [{"baseToken": {"address": m}, "quoteToken": {"symbol": "SOL"}, "priceUsd": "0.0021",
                     "liquidity": {"usd": 9000}} for m in mints]
        if "geckoterminal.com" in url:
            return {"data": [{"relationships": {"base_token": {"data": {"id": "solana_gt%d" % len(self.calls)}}}}]}
        return []

    def jupiter(self):
        return [t for t, u in self.calls if "lite-api.jup.ag" in u]

    def quotes(self):
        return [t for t, u in self.calls if "jup.ag/swap" in u]

    def dex(self):
        return [t for t, u in self.calls if "dexscreener.com/tokens" in u]


class Prices(unittest.TestCase):
    def test_jupiter_held_between_reads_then_dexscreener_and_a_10_s_backoff(self):
        web = FakeWeb()
        m = mb.Market(fetch=web, cfg=cfg(), clock=web.clock)
        p = m.live_prices(["meme"])
        self.assertEqual((p[SOL]["usd"], p["meme"]["usd"], m.source), (150.0, 0.002, "jupiter"))
        web.t += 1
        p = m.live_prices(["meme"])        # Jupiter rests 3 s: its last price stands, no CDN copy of DexScreener's
        self.assertEqual((p[SOL]["usd"], p["meme"]["usd"], m.source), (150.0, 0.002, "jupiter"))
        self.assertEqual(web.dex(), [])
        web.t += 2
        web.jup_down = True                                                   # a 429 at second 3
        p = m.live_prices(["meme"])
        self.assertEqual((p[SOL]["usd"], p["meme"]["usd"]), (150.0, 0.002))   # second 0's price, 3 s old
        self.assertIn("HTTP 429", m.errors[-1])
        web.jup_down = False
        n = len(web.jupiter())
        seen = []
        for k in range(9):                                                    # seconds 4-12: Jupiter rests 10 s
            web.t += 1
            seen.append(m.live_prices(["meme"])["meme"]["usd"])
        self.assertEqual(len(web.jupiter()), n)
        self.assertEqual(seen, [0.002] * 6 + [0.0021] * 3)                    # held while under 10 s old, then DexScreener
        self.assertEqual(m.source, "dexscreener")
        web.t += 1
        self.assertEqual((m.live_prices(["meme"])["meme"]["usd"], m.source), (0.002, "jupiter"))

    def test_a_rising_coin_is_not_pulled_back_by_a_stale_dexscreener_copy(self):
        """DexScreener's CDN serves the same priceUsd for 30 s, Jupiter's 5 s: the watch must not
        swing between them (the stale second read as a fall from the peak: a false trailing stop)."""
        cache, t = {}, [0.0]

        def fetch(url, timeout=20):
            def cached(ttl, make):
                if url not in cache or t[0] - cache[url][0] >= ttl:
                    cache[url] = (t[0], make())
                return cache[url][1]
            px = 0.001 * 1.005 ** t[0]                                        # +0.5 % a second, never down
            if "jup.ag/price" in url:
                return cached(5, lambda: {m: {"usdPrice": 150.0 if m == SOL else px} for m in url.split("ids=")[1].split(",")})
            if "jup.ag/swap" in url:
                return {"outAmount": "1000000", "priceImpactPct": "0.001"}
            if "dexscreener.com/tokens/v1/solana/" in url:
                return cached(30, lambda: [{"baseToken": {"address": "meme"}, "priceUsd": str(px), "liquidity": {"usd": 9e4}}])
            return []
        m = mb.Market(fetch=fetch, cfg=cfg(), clock=lambda: t[0])
        seen = []
        for k in range(120):
            t[0] = float(k)
            seen.append(m.live_prices(["meme"])["meme"]["usd"])
        self.assertEqual(seen, sorted(seen))                                  # never a step down
        self.assertEqual(m.source, "jupiter")

    def test_a_sale_is_never_held_back_by_the_bots_own_minute(self):
        web = FakeWeb()
        m = mb.Market(fetch=web, cfg=cfg(), clock=web.clock)
        while m.jup.take():                                                   # buys and prices took the whole minute
            pass
        self.assertIsNone(m.quote(SOL, "meme", 1e8))                          # a buy waits
        self.assertEqual(m.quote_why, "Jupiter minute full")
        self.assertIsNotNone(m.quote("meme", SOL, 1e6))                       # a sale goes, and counts
        self.assertEqual(len(m.jup.calls), 31)
        self.assertFalse(m.can_quote(1))

    def test_a_failing_jupiter_quote_rests_and_fails_at_once_meanwhile(self):
        web = FakeWeb()
        m = mb.Market(fetch=web, cfg=cfg(), clock=web.clock)
        web.quote_fail = 400                                                  # no route: Jupiter itself is fine
        self.assertIsNone(m.quote("meme", SOL, 1e6))
        self.assertEqual(m.quote_why, "no Jupiter route")
        web.quote_fail = "timeout"
        self.assertIsNone(m.quote("meme", SOL, 1e6))                          # 6 s lost once ...
        self.assertEqual(m.quote_why, "Jupiter timed out")
        tries = len(web.quotes())
        for k in range(9):                                                    # ... then 10 s without a call
            web.t += 1
            self.assertIsNone(m.quote("meme", SOL, 1e6))
            self.assertEqual(m.quote_why, "Jupiter resting after a failure")
        self.assertEqual(len(web.quotes()), tries)
        web.t += 1
        self.assertIsNone(m.quote("meme", SOL, 1e6))                          # tried again: still down, 20 s now
        self.assertEqual(len(web.quotes()), tries + 1)
        web.t += 19
        self.assertIsNone(m.quote("meme", SOL, 1e6))
        self.assertEqual(len(web.quotes()), tries + 1)
        web.quote_fail = None
        web.t += 1
        self.assertIsNotNone(m.quote("meme", SOL, 1e6))
        web.quote_fail = 429
        web.t += 1
        self.assertIsNone(m.quote("meme", SOL, 1e6))
        self.assertEqual((m.quote_why, m.quote_pause.until), ("Jupiter HTTP 429", web.t + 10))   # a success starts over

    def test_a_hanging_dexscreener_is_asked_once_in_5_seconds(self):
        web = FakeWeb()
        web.jup_down = web.dex_down = True
        m = mb.Market(fetch=web, cfg=cfg(), clock=web.clock)
        m.sol = (web.t, {"usd": 150.0})
        for k in range(10):
            web.t += 1
            m.live_prices(["meme"])
        self.assertLessEqual(len(web.dex()), 2)                               # not 10 timeouts of 4 s

    def test_jupiter_stays_inside_its_free_minute_and_quotes_come_first(self):
        web = FakeWeb()
        c = cfg()
        m = mb.Market(fetch=web, cfg=c, clock=web.clock)
        quotes = 0
        for k in range(600):                                                  # ten minutes of seconds
            web.t = 1000.0 + k
            m.live_prices(["meme"])
            if k % 7 == 0:
                quotes += m.quote(SOL, "meme", 1e8) is not None
        jt = web.jupiter()
        worst = max(sum(1 for t in jt if s <= t < s + 60) for s in jt)
        self.assertLessEqual(worst, c["jup_per_min"])
        self.assertEqual(quotes, len(range(0, 600, 7)))                       # no quote went without
        price_t = [t for t, u in web.calls if "jup.ag/price" in u]
        self.assertGreaterEqual(min(b - a for a, b in zip(price_t, price_t[1:])), c["jup_price_sec"])
        burst = [m.quote(SOL, "meme", 1e8) is not None for k in range(40)]   # a burst over the limit
        self.assertIn(False, burst)
        self.assertIn("free limit", m.errors[-1])
        self.assertFalse(m.can_quote(2))
        web.t += 60
        self.assertTrue(m.can_quote(2))

    def test_dexscreener_sol_when_jupiter_never_answered(self):
        web = FakeWeb()
        web.jup_down = True
        real = web.__call__

        def fetch(url, timeout=20):
            if url.endswith("/" + SOL):
                return [{"baseToken": {"address": SOL}, "quoteToken": {"symbol": "USDC"}, "priceUsd": "149.5",
                         "liquidity": {"usd": 9e6}},
                        {"baseToken": {"address": SOL}, "quoteToken": {"symbol": "USDT"}, "priceUsd": "140",
                         "liquidity": {"usd": 1e5}}]
            return real(url, timeout)
        m = mb.Market(fetch=fetch, cfg=cfg(), clock=web.clock)
        self.assertEqual(m.live_prices([])[SOL]["usd"], 149.5)

    def test_gecko_terminal_once_in_20_seconds_and_its_pools_kept_between(self):
        web = FakeWeb()
        c = cfg()
        m = mb.Market(fetch=web, cfg=c, clock=web.clock)
        sc = mb.Scanner(m, c, threaded=False)
        rounds = []
        for k in range(24):                                                   # two minutes of 5 s scans
            web.t = 1000.0 + 5 * k
            sc.start({"now": web.t, "skip": frozenset(), "rug_fresh": frozenset()})
            found = sc.poll()
            rounds.append(found["gt"])
            self.assertTrue(any(x.startswith("gt") for x in found["mints"]))  # the pools stay in every scan
        gt = [t for t, u in web.calls if "geckoterminal" in u]
        self.assertEqual(rounds.count(True), 6)
        for s in gt:
            self.assertLessEqual(sum(1 for t in gt if s <= t < s + 60), 10)   # CoinGecko's keyless ~10 a minute


class Learner(unittest.TestCase):
    """The LEARNER wired into the night: what the closed trades taught it changes the later buys,
    and memory.json carries it over a restart."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.m = FakeMarket()
        # many small positions, no pause, no night stop: only the LEARNER can say no
        self.cfg = cfg(max_open=20, max_trades_per_night=999, position_pct=10.0, min_order_usd=1.0, loss_streak=99,
                       night_stop_pct=-99.0, reenter_min=20)
        self.t = T0
        self.path = os.path.join(self.dir, "memory.json")

    def coin(self, mint):
        self.m.px[mint] = 0.001
        self.m.pairs_by_mint[mint] = pair(mint, sym=mint.upper(), age_min=90)    # the same facts: the same labels
        self.m.reports[mint] = report(mint)
        self.m.fresh.append(mint)

    def night(self):
        n = mb.Night(self.dir, self.cfg, self.m, self.t, lambda *a: None)
        n.begin("2026-11-25")
        return n

    def tick(self, n, sec=1, scan=True):
        """sec on (30: the scanner reads GeckoTerminal's new pools once in 20 s), then a tick."""
        self.t += datetime.timedelta(seconds=sec)
        n.now = self.t
        n.tick(400, scan=scan)

    def history(self, n, win, k=8):
        """k closed trades of one kind of coin: a -40 % stop each, or +60 % (half sold) and the trail."""
        coins = ["%s%d" % ("w" if win else "l", i) for i in range(k)]
        for c in coins:
            self.coin(c)
        self.tick(n, 30)
        self.assertEqual(set(coins) - set(n.state["positions"]), set())
        for px in ((0.0016, 0.0012) if win else (0.0006,)):
            for c in coins:
                self.m.px[c] = px
            self.tick(n, scan=False)
        self.assertEqual(set(coins) & set(n.state["positions"]), set())
        self.m.fresh = [c for c in self.m.fresh if c not in coins]              # gone from the lists

    def rows(self, n, action=None):
        return [r for r in n.state["night"]["decisions"] if r["agent"] == "LEARNER" and action in (None, r["action"])]

    def test_a_losing_pattern_skips_a_look_alike_and_looks_at_it_again_later(self):
        n = self.night()
        self.history(n, win=False)
        self.assertEqual(mb.learner.load(self.path)["samples"], 8)                  # saved at every close
        learned = self.rows(n, "learn")
        self.assertEqual(len(learned), 8)
        self.assertRegex(learned[-1]["text"], r"^learned · \S+ now avg -\d+\.\d% over 8$")
        t = n.state["night"]["trades"][-1]
        self.assertEqual(len(t["learn"]), 10)                                       # state.json keeps the labels,
        self.assertNotIn("learn", mb.panel(n.state, n.cfg, {}, n.ts())["trades"][-1])   # the page gets none
        self.coin("nxt")
        self.tick(n, 30)
        self.assertNotIn("nxt", n.state["positions"])
        skip = self.rows(n, "skip")
        self.assertEqual([r["coin"] for r in skip], ["NXT"])
        self.assertRegex(skip[0]["text"], r"^skip · past trades like this lost avg \d+\.\d% · \S+ -\d+\.\d% over 8, ")
        self.assertNotIn("nxt", n.state["night"]["entered"])                       # no ban for the night
        self.assertEqual((mb.learner.load(self.path)["skipped"], n.state["learner_summary"]["skipped"]), (1, 1))
        self.tick(n, 60)                                                            # inside its 20 minutes: left alone
        self.assertEqual(len(self.rows(n, "skip")), 1)
        self.tick(n, 20 * 60)                                                       # then looked at again
        self.assertEqual(len(self.rows(n, "skip")), 2)
        self.assertNotIn("nxt", n.state["positions"])

    def test_every_fifth_skip_is_bought_at_half_size(self):
        n = self.night()
        self.history(n, win=False)
        bal = n.state["balance"]                                                    # all sold: the equity
        look = ["s%d" % i for i in range(5)]
        for c in look:
            self.coin(c)
        self.tick(n, 30)
        bought = [c for c in look if c in n.state["positions"]]
        self.assertEqual(len(bought), 1)
        self.assertEqual([r["action"] for r in self.rows(n) if r["action"] != "learn"], ["skip"] * 4 + ["explore"])
        self.assertIn("explore · 1 in 5 skips is bought at half size · ", self.rows(n, "explore")[0]["text"])
        pos = n.state["positions"][bought[0]]
        self.assertAlmostEqual(pos["cost"], bal * 0.10 * 0.5, places=3)            # half of 10 % of the equity
        self.assertIn("x0.50 LEARNER", [r for r in n.state["night"]["decisions"] if r["action"] == "size"][-1]["text"])
        self.assertEqual(len(pos["learn"]), 10)
        mem = mb.learner.load(self.path)
        self.assertEqual((mem["skipped"], mem["explored"], mem["skip_run"]), (4, 1, 0))

    def test_an_explore_that_is_not_bought_leaves_the_1_in_5_to_the_next_coin(self):
        n = self.night()
        self.history(n, win=False)
        for c in ["s%d" % i for i in range(4)]:
            self.coin(c)
        self.tick(n, 30)                                                            # four skips
        self.coin("hp")
        self.m.no_sell.add("hp")                                                    # the 5th: its sell simulation fails
        self.tick(n, 30)
        self.assertNotIn("hp", n.state["positions"])
        self.assertEqual([r["action"] for r in self.rows(n) if r["action"] != "learn"], ["skip"] * 4 + ["explore"])
        mem = mb.learner.load(self.path)
        self.assertEqual((mem["skipped"], mem["explored"], mem["skip_run"]), (4, 0, 4))  # no explore was made
        self.assertEqual(n.state["learner_summary"]["explored"], 0)
        self.coin("ok")
        self.tick(n, 30)
        self.assertIn("ok", n.state["positions"])                                   # the next look-alike is the explore
        mem = mb.learner.load(self.path)
        self.assertEqual((mem["skipped"], mem["explored"], mem["skip_run"]), (4, 1, 0))

    def test_a_winning_pattern_sizes_up_at_most_1_5x(self):
        n = self.night()
        self.history(n, win=True)
        self.assertGreater(min(t["pnl_pct"] for t in n.state["night"]["trades"]), 25)
        bal = n.state["balance"]
        self.coin("nxt")
        self.tick(n, 30)
        pos = n.state["positions"]["nxt"]
        self.assertAlmostEqual(pos["cost"], bal * 0.10 * 1.5, places=3)            # x1.5, the cap (1 + score / 20 is more)
        self.assertAlmostEqual(pos["tokens_ui"] * 0.001, (pos["cost"] - 0.05) * 0.99 * 0.99, places=3)  # quoted at that size
        self.assertRegex(self.rows(n, "buy")[-1]["text"], r"^buy · score \+\d+\.\d% · size x1\.50 · ")
        self.assertEqual(self.rows(n, "skip"), [])

    def test_the_memory_survives_a_restart(self):
        n = self.night()
        self.history(n, win=False)
        n2 = self.night()                                                           # the next job: a new Night
        self.assertEqual((n2.mem["samples"], n2.state["learner_summary"]["samples"]), (8, 8))
        n2.save()
        self.assertEqual(mb.read_json(os.path.join(self.dir, "panel.json"), {})["learner"]["samples"], 8)
        self.coin("nxt")
        self.tick(n2, 30)
        self.assertNotIn("nxt", n2.state["positions"])
        self.assertEqual(len(self.rows(n2, "skip")), 1)

    def test_a_lost_memory_is_rebuilt_from_the_trades_in_state_json(self):
        n = self.night()
        self.history(n, win=False)
        os.remove(self.path)
        n2 = self.night()
        self.assertEqual((n2.mem["samples"], n2.mem["labels"]["liq:50k+"]["n"]), (8, 8))    # from the trades' labels
        self.assertTrue(os.path.exists(self.path))
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        for t in st["night"]["trades"]:
            t.pop("learn")                                                          # trades from before the LEARNER
        mb.write_json(os.path.join(self.dir, "state.json"), st)
        os.remove(self.path)
        self.assertEqual(list(self.night().mem["labels"]), ["hour:23-01"])          # only the hour they opened

    def test_without_learner_py_it_trades_as_before_and_says_so_once(self):
        with mock.patch.object(mb, "learner", None), mock.patch.object(mb, "LEARNER_OFF", "SyntaxError: invalid syntax"):
            n = self.night()
            self.history(n, win=False)
            self.coin("nxt")
            self.tick(n, 30)
            n.begin("2026-11-25")
        self.assertIn("nxt", n.state["positions"])                                  # bought at full size
        self.assertAlmostEqual(n.state["positions"]["nxt"]["cost"], n.state["balance"] / 0.9 * 0.1, places=2)
        self.assertEqual([r["action"] for r in self.rows(n)], ["off"])
        self.assertEqual(self.rows(n)[0]["text"], "off · learner.py did not load (SyntaxError: invalid syntax) · trading without it")
        self.assertFalse(os.path.exists(self.path))
        self.assertIsNone(mb.panel(n.state, n.cfg, {}, n.ts())["learner"])

    def test_a_learner_that_raises_is_switched_off_and_the_buys_go_on(self):
        n = self.night()
        self.coin("aaa")
        with mock.patch.object(mb.learner, "decide", side_effect=ValueError("boom")):
            self.tick(n)
            self.coin("bbb")
            self.tick(n, 30)
        self.assertTrue({"aaa", "bbb"} <= set(n.state["positions"]))
        self.assertEqual([r["text"] for r in self.rows(n)], ["off · decide() failed: ValueError: boom · trading without it"])
        self.assertIsNone(n.state.get("learner_summary"))

    def test_a_broken_learner_py_is_reported_not_raised(self):
        bad = os.path.join(self.dir, "learner.py")
        with open(bad, "w") as fh:
            fh.write("def features(:\n")
        mod, why = mb.load_learner(bad)
        self.assertEqual((mod, why[:12]), (None, "SyntaxError:"))
        self.assertIn("FileNotFoundError", mb.load_learner(os.path.join(self.dir, "nothing.py"))[1])
        with open(bad, "w") as fh:
            fh.write("def features(*a):\n    return []\n")                          # half a learner
        self.assertEqual(mb.load_learner(bad), (None, "MemeError: no decide()"))
        self.assertIsNotNone(mb.learner)                                            # the real one loads
        self.assertIsNone(mb.LEARNER_OFF)


if __name__ == "__main__":
    unittest.main()
