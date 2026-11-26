"""The New Pairs engine against what four reviewers found (one test class per finding, offline, on the engine
tests' FakeFeeds and curve-quoting market): the 5-minute fading window, held new pairs with New Pairs off,
break-even net of costs, a double buy with both strategies, RugCheck and HTTP never holding the exits, what
exit_ms times, a curve nobody trades, RugCheck's copy of the links, curve_gone for a brand-new account, the
job hand-off, the honeypot test on small orders, and the tick's speed."""

import datetime
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

import test_newpairs_engine as E
from test_newpairs_engine import (Clock, CurveMarket, FakeFeeds, RUNNER, SCALP, create, mb, mb_until, meta, np_cfg,
                                  reading, rug_report)

SOL = mb.SOL


class Base(unittest.TestCase):
    """The engine tests' helpers (setUp, night, turn, read, launch, ...), without running their tests again."""
    setUp = E.Engine.setUp
    night = E.Engine.night
    turn = E.Engine.turn
    woken = E.Engine.woken
    read = E.Engine.read
    launch = E.Engine.launch
    gain_rsol = E.Engine.gain_rsol
    lines = E.Engine.lines

    def tick(self, n, sec=1.0, left=400):
        """One 1 s step: the clock, the poller's good round (the curves read, unchanged ones too), watch()."""
        self.clock.advance(sec)
        n.now = self.clock.now()
        self.feeds.poller.ok += 1
        n.np_touch()
        n.watch(left)


class FadeWindow(Base):
    def fade_run(self, n):
        """310 s of small buys read every 0.5 s (600+ readings in 5 minutes), then only small sells.
        -> (coin, snapshots, the second the coin was sold or None)."""
        self.launch(n, "uzim", RUNNER, social=(True, True, True))
        coin = n.wl.get("uzim")
        box = {"rsol": coin.rsol}
        snaps = []

        def step(d):
            self.slot += 1
            box["rsol"] += d
            self.turn(n, reading("uzim", box["rsol"], self.slot), sec=0.5)
            snaps.append((n.epoch(), coin.buys, coin.sells, coin.rsol))

        for _ in range(620):                                            # 310 s of small buys
            step(0.0023)
        self.assertIn("uzim", n.state["positions"])
        self.assertLessEqual(len(n.np_win["uzim"]), 305)               # a snapshot a second, bounded by time
        base = [x for x in snaps if x[0] <= n.epoch() - 300][-1]
        info = n.np_now_info("uzim", coin)
        self.assertAlmostEqual(info["buys"], coin.buys - base[1], delta=2)
        self.assertLess(info["buys"], coin.buys - 20)                   # not since the launch
        self.assertAlmostEqual(info["sol_in"], coin.rsol - base[3], delta=0.01)
        for k in range(400):                                            # then the buyers leave: only small sells
            step(-0.001)
            if "uzim" not in n.state["positions"]:
                return coin, snaps, (k + 1) * 0.5
        return coin, snaps, None

    def test_fading_reads_the_last_5_minutes_with_a_reading_every_half_second(self):
        """feeds.py reads a held coin every 0.5 s: 600 readings in 5 minutes. The window must still be the last
        5 minutes (a 400-entry cap kept the launch as its baseline: buys since the launch, never fading).
        Without costs the coin is in profit when its buyers leave: fading sells it, and the trade is a win."""
        self.m.fee_pct = 0.0
        n = self.night(np_cfg(curve_fee_pct=0.0, latency_slip_pct=0.0, priority_fee_sol=0.0, network_fee_sol=0.0))
        coin, snaps, sold_at = self.fade_run(n)
        self.assertIsNotNone(sold_at)
        t = n.state["night"]["trades"][0]
        self.assertTrue(t["why"].startswith("fading: buys"), t["why"])
        self.assertTrue(165 <= sold_at <= 200, sold_at)                # buys under 45 % of the last 5 min, a refresh
        self.assertGreater(t["pnl"], 0)
        self.assertLessEqual(t["steps"], 1)                             # without costs the +7 % step may come first

    def test_fading_never_sells_at_a_loss_after_the_costs(self):
        """The same coin with the real costs (fee 1.25 % each way, the latency haircut, the fees): its gain
        when the buyers leave does not pay for selling it, so fading keeps it (a "profit" exit that books a
        loss was the defect); the stops and the time stops still decide it."""
        n = self.night()
        coin, snaps, sold_at = self.fade_run(n)
        fades = [t for t in n.state["night"]["trades"] if t["why"].startswith("fading")]
        for t in fades:
            self.assertGreaterEqual(t["pnl"], 0, t)
        if "uzim" in n.state["positions"]:
            pos = n.state["positions"]["uzim"]
            gain = (pos["last"] / pos["entry_price"] - 1) * 100
            self.assertGreater(n.np_even_gain(pos, coin, n.sol_usd()), gain)

    def test_the_window_survives_a_restart(self):
        n = self.night()
        self.launch(n, "uzim", RUNNER, social=(True, True, True))
        coin = n.wl.get("uzim")
        rsol = coin.rsol
        for _ in range(800):                                            # 400 s: the baseline has moved on
            rsol += 0.001
            self.slot += 1
            self.turn(n, reading("uzim", rsol, self.slot), sec=0.5)
        self.assertIn("uzim", n.state["positions"])
        n.save()
        saved = mb.read_json(os.path.join(self.dir, "state.json"), {})["positions"]["uzim"]["np_win"]
        self.assertLessEqual(len(saved), 35)                            # every 10 s: small in state.json
        n2 = mb.Night(self.dir, np_cfg(), self.m, self.clock.now(), self.out.append)
        n2.clock_fn = self.clock.now
        n2.begin("2026-11-25")
        n2.attach(FakeFeeds(self.clock, self.m))
        self.addCleanup(n2.np_close)
        a, b = n.np_now_info("uzim", coin), n2.np_now_info("uzim", n2.wl.get("uzim"))
        self.assertAlmostEqual(a["buys"], b["buys"], delta=20)          # within 10 s of readings, not since launch
        self.assertLess(b["buys"], n2.wl.get("uzim").buys - 150)
        self.assertIsNone(mb.np_win_load([[1, 2, 3]]))                  # a broken one is left out
        self.assertIsNone(mb.np_win_load([[5, 0, 0, 1.0], [4, 0, 0, 1.0]]))


class Orphans(Base):
    """A night is two jobs; the second reloads the book. With New Pairs off there (a broken module, or
    config.json without "new_pairs"), its held coins were never priced or sold, not even at 06:55."""

    def bought(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        self.assertIn("aaa", n.state["positions"])
        n.save()
        return n

    def again(self, c=None):
        n2 = mb.Night(self.dir, c or np_cfg(), self.m, self.clock.now(), self.out.append)
        n2.clock_fn = self.clock.now
        n2.begin("2026-11-25")
        n2.attach(None, "off")
        self.addCleanup(n2.np_close)
        return n2

    def check(self, n2, plain=False):
        self.assertIsNone(n2.wl)
        self.clock.advance(1)
        n2.now = self.clock.now()
        n2.watch(400)
        self.assertIn("aaa", n2.state["positions"])                     # priced, inside its stops: held
        self.assertTrue(any(" run down without New Pairs" in x for x in self.lines(n2, "RISK")))
        self.m.set_curve("aaa", *self.m.curve["aaa"])
        vsol = E.vsol_for(n2.state["positions"]["aaa"]["entry_price"] / 100.0 * 0.9)
        self.m.set_curve("aaa", vsol, E.K / vsol)                       # -10 %: the stop
        self.clock.advance(1)
        n2.now = self.clock.now()
        n2.watch(400)
        t = n2.state["night"]["trades"][-1]
        self.assertTrue(t["why"].startswith("stop -"), t["why"])
        self.assertEqual(n2.state["positions"], {})

    def test_config_without_new_pairs_runs_the_held_coin_down(self):
        self.bought()
        self.check(self.again(np_cfg(strategies=["migrated"])))

    def test_a_broken_newpairs_py_runs_the_held_coin_down(self):
        self.bought()
        real = mb.newpairs
        try:
            mb.newpairs = None
            self.check(self.again())
        finally:
            mb.newpairs = real

    def test_a_broken_plan_py_runs_it_down_by_the_plain_rules(self):
        self.bought()
        real = mb.holdplan
        try:
            mb.holdplan = None
            self.check(self.again())
        finally:
            mb.holdplan = real

    def test_the_end_of_the_night_sells_it_and_closes_the_night_early(self):
        """run() from 06:50 with newpairs.py missing: the coin goes at 06:55 and the night ends there."""
        self.bought()
        start = datetime.datetime(2026, 11, 26, 5, 50)                  # 06:50 in Bratislava
        clock = Clock(start)
        real = mb.newpairs
        out = []
        try:
            mb.newpairs = None
            night = mb.run(self.dir, np_cfg(), market=self.m, now_fn=clock.now, sleep=clock.advance, out=out.append,
                           live=E.Rec())
        finally:
            mb.newpairs = real
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        self.assertEqual(night, "2026-11-25")
        self.assertEqual(st["positions"], {})
        why = st["last_night"]["trades"][-1]["why"]
        # held for hours at 06:50: its time stop (on its plain rules) or the end of the night sells it at once
        self.assertTrue(why == "end of night" or " min, only " in why, why)
        self.assertTrue(any("end of night · $" in x for x in out))
        self.assertLess(clock.now(), datetime.datetime(2026, 11, 26, 5, 56))


class BreakEven(Base):
    def test_the_stop_after_the_first_step_is_break_even_after_the_costs(self):
        n = self.night()
        self.launch(n, "uzim", RUNNER, social=(True, True, True))
        self.read(n, "uzim", self.gain_rsol(n, "uzim", 8))
        pos = n.state["positions"]["uzim"]
        be = pos["plan"]["stop_pct"]
        self.assertTrue(3.5 < be < 5.0, be)                             # not +1 % of the price
        self.assertEqual(n.panel_doc()["positions"][0]["plan"]["stop_pct"], be)
        for g in (7.5, 6.0, 5.0):                                       # a slow way down: held over the stop
            self.read(n, "uzim", self.gain_rsol(n, "uzim", g))
            self.assertIn("uzim", n.state["positions"])
        self.read(n, "uzim", self.gain_rsol(n, "uzim", be - 0.05))
        t = n.state["night"]["trades"][0]
        self.assertTrue(t["why"].startswith("break-even stop"), t["why"])
        self.assertGreater(t["pnl"], -0.02)                             # +1 % of the price lost 2.2 % of the trade

    def test_a_break_even_exit_after_a_step_is_no_loss_in_a_row(self):
        """Five runners sold their first step and then gapped down past break-even: no 5-loss pause."""
        n = self.night()
        for i in range(5):
            mint = "be%d" % i
            self.assertIsNotNone(self.launch(n, mint, RUNNER, social=(True, True, True), sym="BE%d" % i))
            self.read(n, mint, self.gain_rsol(n, mint, 8))
            self.read(n, mint, self.gain_rsol(n, mint, 0.9))
            self.assertNotIn(mint, n.state["positions"])
        self.assertEqual(n.state["night"]["streak"], 0)
        self.assertEqual(self.lines(n, "RISK"), [])
        self.assertEqual(n.state["night"]["cooldown_until"], 0)


class DoubleBuy(Base):
    def test_a_coin_the_migrated_strategy_holds_is_never_bought_again(self):
        n = self.night(np_cfg(strategies=["new_pairs", "migrated"]))
        self.m.reports["aaa"] = rug_report("aaa")
        self.turn(n, create("aaa", "AAA"))
        for rsol in SCALP:                                              # every rule but the socials (not read yet)
            self.read(n, "aaa", rsol)
        self.assertEqual(n.state["positions"], {})
        px = n.wl.get("aaa").price * 100.0
        n.state["positions"]["aaa"] = {"mint": "aaa", "symbol": "AAA", "name": "AAA", "raw": 10 ** 12, "decimals": 6,
                                       "tokens_ui": 1e6, "cost": 10.0, "entry_price": px, "opened": n.ts(), "peak": px,
                                       "last": px, "tp1_done": False, "sold_usd": 0.0, "reason": "migrated scan"}
        n.state["balance"] -= 10.0
        self.turn(n, meta("aaa", False, True, False), sec=0.3)          # the links land: the rules all hold now
        self.read(n, "aaa", 4.8)
        pos = n.state["positions"]["aaa"]
        self.assertEqual((pos.get("strategy"), pos["reason"], pos["last"]), (None, "migrated scan", px))
        self.assertAlmostEqual(n.state["balance"], 90.0)
        self.assertEqual(n.wl.get("aaa").status, "rejected: held already (migrated)")
        self.assertFalse(any(x.startswith("ENTER") for x in self.lines(n, "EXECUTOR")))


class NeverBlocked(Base):
    def test_a_slow_rugcheck_never_holds_the_loop_nor_a_held_coins_stop(self):
        n = self.night()
        self.launch(n, "held", SCALP)
        self.assertIn("held", n.state["positions"])
        self.m.rug_gate = threading.Event()                             # RugCheck answers nothing for now
        self.addCleanup(self.m.rug_gate.set)
        for mint in ("s1", "s2", "s3"):
            self.m.reports[mint] = rug_report(mint)
            self.turn(n, create(mint, mint.upper()), sec=0.1)
            self.turn(n, meta(mint, False, True, False), sec=0.1)
            for rsol in SCALP[:-1]:
                self.read(n, mint, rsol, sec=0.1)
        self.slot += 10
        stop = reading("held", self.gain_rsol(n, "held", -9), self.slot)
        batch = [reading(m, SCALP[-1], self.slot + 1 + i) for i, m in enumerate(("s1", "s2", "s3"))] + [stop]
        self.clock.advance(0.5)
        n.now = self.clock.now()
        self.feeds.push(*batch)
        t0 = time.monotonic()
        n.np_events(0.25, 400)
        self.assertLess(time.monotonic() - t0, 0.2)                     # 3 coins x 0.6 s of waiting before
        self.assertEqual(set(n.np_fired), {"s1", "s2", "s3"})          # fired; they buy when RugCheck answers
        t = n.state["night"]["trades"][0]
        self.assertEqual((t["symbol"], t["exit_ms"]), ("HELD", 0))
        self.m.rug_gate.set()
        for mint in ("s1", "s2", "s3"):
            self.assertTrue(self.woken(mint))
        self.turn(n, sec=0.2)
        self.assertEqual(sorted(m for m in n.state["positions"]), ["s1", "s2", "s3"])

    def test_the_sol_price_is_read_on_a_worker_thread(self):
        """Jupiter's price, then DexScreener's, up to 4 s each, ran on the 1 s step: the exits waited."""
        gate, calls = threading.Event(), []
        self.addCleanup(gate.set)

        def sol_price():
            calls.append(threading.current_thread().name)
            if len(calls) > 1:
                gate.wait(10)
            return 100.0 if len(calls) == 1 else 150.0
        self.m.sol_price = sol_price
        n = self.night()
        self.tick(n)
        self.assertEqual((n.sol_usd(), len(calls)), (100.0, 1))         # none yet: read at once, on this thread
        t0 = time.monotonic()
        for _ in range(35):                                             # past sol_price_sec (30 s): asked again
            self.tick(n)
        self.assertLess(time.monotonic() - t0, 1.0)                     # the step never waited for it
        self.assertEqual((n.sol_usd(), len(calls)), (100.0, 2))         # the last price stands meanwhile
        self.assertTrue(calls[1].startswith("solprice"))
        gate.set()
        self.assertTrue(mb_until(lambda: n.sol_job is None or n.sol_job.done()))
        self.tick(n)
        self.assertEqual(n.sol_usd(), 150.0)

    def test_a_quote_on_the_loop_times_out_near_the_measured_latency(self):
        got = []

        def fetch(url, timeout=20):
            got.append((url.split("?")[0], timeout))
            return {"outAmount": "1000", "priceImpactPct": "0.01"}
        m = mb.Market(fetch=fetch, cfg=np_cfg())
        m.quote(SOL, "aaa", 1e8)
        self.assertEqual(got, [("https://lite-api.jup.ag/swap/v1/quote", 1.5)])
        self.assertEqual(mb.Market(fetch=fetch, cfg=np_cfg(quote_timeout_s=0.8)).quote_timeout, 0.8)
        self.assertEqual(mb.load_config()["quote_timeout_s"], 1.5)


class ExitTiming(Base):
    def test_a_time_stop_is_not_a_reaction_to_a_reading(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        rsol = self.gain_rsol(n, "aaa", -3)
        for k in range(6 * 60):                                         # a trade a minute: never a quiet curve
            if k % 60 == 0:
                rsol += 0.001
                self.read(n, "aaa", rsol, sec=0.0)
            self.tick(n)
            if not n.state["positions"]:
                break
        t = n.state["night"]["trades"][0]
        self.assertTrue(t["why"].startswith("5 min, only -"), t["why"])
        self.assertNotIn("exit_ms", t)
        self.assertNotIn("after the reading", [x for x in self.lines(n, "EXECUTOR") if x.startswith("EXIT")][0])
        self.assertNotIn("exit", n.state["night"].get("np_speed", {}))

    def test_a_retried_sale_is_timed_from_the_reading_that_wanted_it_out(self):
        n = self.night(np_cfg(np_validate_pct=-1))                      # no curve maths: the sale waits for Jupiter
        self.launch(n, "aaa", SCALP)
        real = self.m.quote

        def failing(i, o, amount, slippage_bps=300):
            if o == SOL and i != SOL:
                self.m.quote_why = "Jupiter HTTP 429"
                return None
            return real(i, o, amount, slippage_bps)
        self.m.quote = failing
        self.read(n, "aaa", self.gain_rsol(n, "aaa", -11))              # the stop: the sale fails
        self.assertIn("aaa", n.state["positions"])
        self.m.quote = real
        for _ in range(6):                                              # tried again 5 s later, on the 1 s step
            self.tick(n)
        t = n.state["night"]["trades"][0]
        self.assertTrue(t["why"].startswith("stop -11%"), t["why"])
        self.assertGreaterEqual(t["exit_ms"], 5000)
        self.assertIn("out · 5.0 s after the reading", [x for x in self.lines(n, "EXECUTOR") if x.startswith("EXIT")][0])

    def test_a_migrate_event_is_timed_from_when_the_feed_received_it(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        n.np_event({"kind": "migrate", "txType": "migrate", "mint": "aaa", "pool": "pump-amm", "_rx": n.epoch() - 2.0},
                   400)
        t = n.state["night"]["trades"][0]
        self.assertEqual((t["why"], t["exit_ms"]), ("migrating: take it", 2000))


class DeadCurve(Base):
    def test_a_curve_nobody_trades_is_sold_after_np_quiet_min(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        self.read(n, "aaa", self.gain_rsol(n, "aaa", 3))                # +3 %: over the time stop's +2 %
        for _ in range(170):                                            # the poller reads it unchanged
            self.tick(n)
        self.assertIn("aaa", n.state["positions"])
        for _ in range(15):
            self.tick(n)
        t = n.state["night"]["trades"][0]
        self.assertEqual(t["why"], "curve quiet for 3 min (no trade)")
        self.assertNotIn("exit_ms", t)

    def test_a_coin_short_of_its_first_step_goes_after_twice_its_time_stop(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        rsol = self.gain_rsol(n, "aaa", 3)
        for k in range(11 * 60):                                        # +3 %, a small trade every 30 s
            if k % 30 == 0:
                rsol += 0.0005 if k % 60 else -0.0005
                self.read(n, "aaa", rsol, sec=0.0)
            self.tick(n)
            if not n.state["positions"]:
                break
            if k == 6 * 60:
                self.assertIn("aaa", n.state["positions"])              # past its 5 min time stop: +3 % spares it
        t = n.state["night"]["trades"][0]
        self.assertEqual(t["why"], "10 min, no +7% yet (+3%)")

    def test_a_full_book_of_quiet_coins_frees_its_slots(self):
        n = self.night(np_cfg(max_open=2))
        for i in range(2):
            m = "q%d" % i
            self.launch(n, m, SCALP, sym="Q%d" % i)
            self.read(n, m, self.gain_rsol(n, m, 3))
        self.assertEqual(len(n.state["positions"]), 2)
        for _ in range(200):
            self.tick(n)
        self.assertEqual(n.state["positions"], {})
        self.assertIsNotNone(self.launch(n, "nxt", SCALP))              # a slot again


class MetaFallback(Base):
    """The metadata host answered 403: RugCheck's copy of the links (fileMeta), which a 1 s old coin often
    does not have yet. A miss is unknown, asked again, never "no social link" for good."""

    def failed_meta(self, n, mint):
        self.turn(n, create(mint, mint.upper()))
        self.turn(n, meta(mint, ok=False), sec=0.3)
        self.assertEqual(self.m.rug_calls, [mint])

    def test_a_rugcheck_miss_is_asked_again_and_the_coin_bought(self):
        n = self.night()
        self.failed_meta(n, "ipfs")                                     # RugCheck: nothing for this coin yet
        self.turn(n, sec=0.3)
        coin = n.wl.get("ipfs")
        self.assertEqual((coin.status, coin.meta), ("watching", None))
        self.m.reports["ipfs"] = rug_report("ipfs", file_meta={"twitter": "https://x.com/ipfs",
                                                                "telegram": "https://t.me/ipfs"})
        for _ in range(6):                                              # asked again np_rug_retry_s later
            self.tick(n)
        self.turn(n, sec=0.1)
        self.assertEqual(self.m.rug_calls, ["ipfs", "ipfs"])
        self.assertEqual(coin.meta["x"], "https://x.com/ipfs")
        for rsol in RUNNER:
            self.read(n, "ipfs", rsol)
        self.assertIn("ipfs", n.state["positions"])

    def test_an_empty_filemeta_is_asked_again(self):
        n = self.night()
        self.m.reports["early"] = rug_report("early")                   # a report, its fileMeta not filled yet
        self.failed_meta(n, "early")
        self.turn(n, sec=0.3)
        self.assertEqual(n.wl.get("early").status, "watching")
        self.m.reports["early"] = rug_report("early", file_meta={"name": "early", "website": "https://early.fun"})
        for _ in range(6):
            self.tick(n)
        self.turn(n, sec=0.1)
        self.assertEqual(n.wl.get("early").meta["web"], "https://early.fun")

    def test_metadata_rugcheck_read_without_a_link_is_no_social_link(self):
        n = self.night()
        self.m.reports["bare"] = rug_report("bare", file_meta={"name": "bare", "symbol": "BARE", "image": "x"})
        self.failed_meta(n, "bare")
        self.turn(n, sec=0.3)
        self.read(n, "bare", 1.0)
        self.assertEqual(n.wl.get("bare").status, "rejected: no social link")

    def test_the_asks_slow_down_and_stop_when_the_coin_is_too_old(self):
        n = self.night()
        self.failed_meta(n, "dud")
        for _ in range(200):
            self.tick(n)
            self.turn(n, sec=0.0)
        self.assertLessEqual(len(self.m.rug_calls), 7)                  # 5, 10, 20, 40, 60, 60 s apart
        self.assertGreaterEqual(len(self.m.rug_calls), 5)
        self.assertNotIn("dud", n.np_meta_rug)                          # 180 s: too old to be bought


class CurveGone(Base):
    def test_a_brand_new_account_a_node_has_not_seen_is_not_dropped(self):
        n = self.night()
        self.m.reports["young"] = rug_report("young")
        self.turn(n, create("young", "YOUNG"))
        self.turn(n, {"kind": "curve_gone", "mint": "young", "key": "curve-young", "src": "poll"}, sec=1.5)
        self.assertIsNotNone(n.wl.get("young"))
        self.assertIn("young", self.feeds.watching)
        self.turn(n, meta("young"), sec=0.3)
        for rsol in RUNNER:
            self.read(n, "young", rsol)
        self.assertIn("young", n.state["positions"])

    def test_a_curve_that_had_readings_and_went_null_is_dropped(self):
        n = self.night()
        self.turn(n, create("gone", "GONE"))
        self.read(n, "gone", 0.5)
        self.turn(n, {"kind": "curve_gone", "mint": "gone", "key": "curve-gone", "src": "poll"})
        self.assertIsNone(n.wl.get("gone"))
        self.assertNotIn("gone", self.feeds.watching)


class JobFeeds(FakeFeeds):
    """Script times in seconds after the run's start."""

    def __init__(self, clock, market, start):
        FakeFeeds.__init__(self, clock, market)
        self.start_at = start

    def _due(self):
        while self.script and (self.start_at + datetime.timedelta(seconds=self.script[0][0])) <= self.clock.now():
            self.q.append(self.script.pop(0)[1])

    def get(self, timeout=0.25):
        self.gets.append(timeout)
        self._due()
        if not self.q and timeout and timeout > 0:
            time.sleep(0.001)
            nxt = (self.start_at + datetime.timedelta(seconds=self.script[0][0]) - self.clock.now()).total_seconds() \
                if self.script else timeout
            self.clock.advance(max(0.0, min(timeout, nxt)))
            self._due()
        return self._out(self.q.popleft()) if self.q else None


class HandOff(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def script(self, feeds, at, mint, sym, slot):
        feeds.at(at, create(mint, sym))
        feeds.at(at + 0.3, meta(mint))
        for k, rsol in enumerate(RUNNER):
            feeds.at(at + 1 + 0.7 * k, reading(mint, rsol, slot + k))

    def test_a_job_the_next_one_takes_over_buys_no_new_pair_near_its_end_and_sells_what_it_holds(self):
        start = datetime.datetime(2026, 11, 26, 2, 0)                   # 03:00 in Bratislava, 4 h of the night left
        clock = Clock(start)
        m = CurveMarket()
        feeds = JobFeeds(clock, m, start)
        for mint in ("aaa", "bbb"):
            m.reports[mint] = rug_report(mint)
        self.script(feeds, 2, "aaa", "AAA", 1000)                       # bought in the job's first seconds
        for k in range(1, 13):                                          # a trade every 30 s: held, inside its stops
            feeds.at(10 + 30 * k, reading("aaa", 6.9 if k % 2 else 7.0, 2000 + k))
        self.script(feeds, 200, "bbb", "BBB", 3000)                     # fires 3.5 min before the job's end
        out = []
        mb.run(self.dir, np_cfg(), minutes=7, market=m, now_fn=clock.now, sleep=clock.advance, out=out.append,
               live=E.Rec(), feeds=feeds)
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        n = st["night"]
        self.assertEqual(st["positions"], {})                           # nothing left blind for the hand-off
        self.assertEqual([t["why"] for t in n["trades"]], ["the job ends · the next job takes over"])
        texts = [r["text"] for r in n["decisions"]]
        self.assertIn("no entry: last 5 min of this job: no new entries (the next job takes over)", texts)
        self.assertFalse(any(x.startswith("ENTER BBB") for x in texts))
        sold = [r for r in n["decisions"] if r["text"].startswith("EXIT AAA")][0]
        self.assertEqual(sold["t"][:16], "2026-11-26T02:06")             # in the job's last minute

    def test_a_test_run_and_the_nights_last_job_are_not_cut(self):
        start = datetime.datetime(2026, 11, 26, 5, 40)                  # 06:40: this job ends with the night
        clock = Clock(start)
        m = CurveMarket()
        feeds = JobFeeds(clock, m, start)
        m.reports["aaa"] = rug_report("aaa")
        self.script(feeds, 2, "aaa", "AAA", 1000)
        mb.run(self.dir, np_cfg(), minutes=30, market=m, now_fn=clock.now, sleep=clock.advance, out=lambda *a: None,
               live=E.Rec(), feeds=feeds)
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        self.assertTrue(any(r["text"].startswith("ENTER AAA") for r in st["last_night"]["decisions"]))


class Honeypot(Base):
    def buy_at(self, sol_usd, action, mult):
        self.m.sol = sol_usd
        n = self.night()
        n.learn_decide = lambda labels, symbol: ({"action": action, "mult": mult, "why": "test"}, None)
        self.launch(n, "aaa", SCALP)
        return n

    def test_a_small_order_at_a_real_sol_price_is_no_honeypot(self):
        """A $5 explore at SOL $250 pays $0.50 of fixed fees a round trip (10 %): the quotes alone lose 3 %."""
        for sol_usd, action, mult in ((250.0, "explore", 0.5), (250.0, "buy", 0.75), (150.0, "explore", 0.5)):
            self.setUp()
            self.m.sol = sol_usd
            n = self.buy_at(sol_usd, action, mult)
            self.assertIn("aaa", n.state["positions"], (sol_usd, action, self.lines(n, "RUG CHECK")))
            self.assertEqual(n.wl.get("aaa").status, "entered")

    def test_the_min_order_at_a_real_sol_price_is_no_honeypot(self):
        self.m.sol = 250.0
        n = self.night(np_cfg(start_balance=30.0))                      # 10 % of $30: the $3 floor
        self.launch(n, "aaa", SCALP)
        self.assertAlmostEqual(n.state["positions"]["aaa"]["cost"], 3.0, places=2)

    def test_a_real_honeypot_is_still_rejected(self):
        self.m.sol = 250.0
        self.m.tax["hp"] = 0.2                                          # a fifth of any sale gone
        n = self.night()
        self.launch(n, "hp", SCALP)
        self.assertEqual(n.wl.get("hp").status, "rejected: sell simulation")
        self.assertTrue(any("would lose 2" in x and "on the quotes" in x for x in self.lines(n, "RUG CHECK")))


class TickSpeed(Base):
    def test_the_tick_leaves_the_speed_out_while_only_its_counters_move(self):
        n = self.night()
        self.launch(n, "uzim", RUNNER, social=(True, True, True))
        for i in range(3):
            self.turn(n, create("w%d" % i, "W%d" % i), sec=0.2)
        self.tick(n)
        memo, live = {}, E.Rec()
        n.publish({}, live, 1, memo, force=True)                        # the retained panel
        sizes = []
        for k in range(4):                                              # readings, creates: the counters move
            self.read(n, "w0", 0.2 + 0.1 * k, sec=0.0)
            self.turn(n, create("x%d" % k, "X%d" % k), sec=0.0)
            self.tick(n)
            t = mb.tick_msg(n.state, n.cfg, {}, n.ts(), 2 + k, memo, n.speed, watch=n.np_rows())
            sizes.append(len(json.dumps(t)))
            self.assertNotIn("speed", t)
        self.feeds.pump_error = "closed 1006"                           # PumpPortal goes down: that is news
        self.tick(n)
        t = mb.tick_msg(n.state, n.cfg, {}, n.ts(), 9, memo, n.speed, watch=n.np_rows())
        self.assertIn("speed", t)
        self.assertFalse(t["speed"]["feeds"][0]["connected"])


class Config(unittest.TestCase):
    def test_config_json_and_prompt_say_the_new_rules(self):
        c = mb.load_config()
        self.assertNotIn("np_rug_wait_s", c)                            # the loop never waits for RugCheck
        for k, v in (("np_quiet_min", 3.0), ("time_stop_late_x", 2.0), ("job_no_entries_min", 5), ("job_flat_min", 1),
                     ("quote_timeout_s", 1.5)):
            self.assertEqual(c[k], v)
            self.assertEqual(mb.DEFAULTS[k], v)
        with open(mb.PROMPT, encoding="utf-8") as fh:
            p = fh.read()
        for text in ("Out when the curve has had no trade for %g minutes" % c["np_quiet_min"],
                     "after twice that time, out unless the first step was reached",
                     "the last %d minutes of a job" % c["job_no_entries_min"],
                     "sold in its last minute", "on the two quotes alone",
                     "a trade that sold its first step is not one",
                     "leaves the whole trade without a loss after its fees"):
            self.assertIn(text, p)


if __name__ == "__main__":
    unittest.main()
