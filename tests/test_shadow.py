"""SHADOW TRADES (memebot/bot.py np_shadow_*, memebot/learner.py weights): a New Pair whose trigger fired and that
passed RugCheck, but was not bought (the LEARNER skipped it; with shadow_on_risk, RISK said no entry), is followed
as if it had been bought: priced like a real entry, run by the very exit code of a held coin on the same readings in
the same loop turn, outside the book, and when it closes it teaches the LEARNER with shadow_weight. Offline, on
tests/test_newpairs_engine.py's FakeFeeds and CurveMarket (pump.fun's maths as Jupiter, SOL at $100). Paper on paper:
these tests say the code does what it says, never that a shadow's P&L would have been earned."""

import json
import os
import shutil
import tempfile
import unittest

from _load import load
import test_newpairs_engine as E

os.environ["MEMEBOT_LIVE"] = "0"
os.environ["MEMEBOT_FEEDS"] = "0"
mb = E.mb
lr = mb.learner
recorder = mb.recorder


def cfg(**kw):
    """The engine tests' settings (+7 % first step, SPEC_TIERS), without the entry-quality rules that are not what these
    tests are about (concentration, RugCheck's holder count: tests/test_newpairs*.py have them)."""
    c = E.np_cfg(np_max_sol_per_holder=0.0, np_max_buy_sol=0.0, np_min_holders=0)
    c.update(kw)
    return c


TG = ["np:social:tg"]                   # the SCALP coin's Telegram link: taught bad, the LEARNER skips it


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.clock = E.Clock()
        self.m = E.CurveMarket()
        self.feeds = E.FakeFeeds(self.clock, self.m)
        self.out = []
        self.slot = 1000

    def night(self, c=None, folder=None):
        n = mb.Night(folder or self.dir, c or cfg(), self.m, self.clock.now(), self.out.append)
        n.clock_fn = self.clock.now
        n.begin("2026-11-25")
        n.attach(self.feeds)
        self.addCleanup(n.np_close)
        return n

    def turn(self, n, *evs, sec=1.0, left=400):
        E.mb_until(lambda: all(j[0].done() for j in list(n.np_jobs.values())), 2.0)
        self.clock.advance(sec)
        n.now = self.clock.now()
        self.feeds.push(*evs)
        n.np_events(0.25, left)

    def read(self, n, mint, rsol, sec=1.0, left=400, **kw):
        self.slot += 1
        self.turn(n, E.reading(mint, rsol, self.slot, **kw), sec=sec, left=left)

    def launch(self, n, mint, path=E.SCALP, social=(False, True, False), report=None, **kw):
        """A create, its links, its readings until it is bought or shadowed (RugCheck has a clean report)."""
        self.m.reports[mint] = report or E.rug_report(mint)
        x, tg, web = social
        self.turn(n, E.create(mint, mint.upper(), **kw))
        self.turn(n, E.meta(mint, x, tg, web), sec=0.3)
        for rsol in path:
            self.read(n, mint, rsol)
            if mint in n.state["positions"] or mint in n.shadows:
                return

    def rsol(self, pos, gain_pct):
        """The real SOL at which the curve's price is gain_pct over the position's entry price."""
        return E.vsol_for(pos["entry_price"] / self.m.sol * (1 + gain_pct / 100.0)) - 30.0

    @staticmethod
    def skip_tg(n):
        for _ in range(10):                             # 10 coins with a Telegram link lost 15 % each
            lr.update(n.mem, TG, -15.0)

    @staticmethod
    def lines(n, agent=None, action=None):
        return [r["text"] for r in n.state["night"]["decisions"]
                if agent in (None, r["agent"]) and action in (None, r["action"])]

    def tick(self, n, sec):
        self.clock.advance(sec)
        n.now = self.clock.now()
        n.watch(400)


class TheShadow(Base):
    def test_a_learner_skip_becomes_a_shadow_that_exits_by_the_plan_and_teaches_with_its_weight(self):
        n = self.night()
        self.skip_tg(n)
        self.launch(n, "lll")
        self.assertEqual(n.state["positions"], {})
        self.assertEqual(n.wl.get("lll").status, "rejected: learner: skip")
        sh = n.shadows["lll"]
        self.assertEqual((sh["strategy"], sh["shadow"], sh["shadow_why"], sh["plan"]["tier"], sh["fill"]),
                         ("new_pairs", "learner", "learner skip", "scalp", "curve"))
        self.assertEqual([q for q in self.m.quotes if "lll" in q], [])  # never a Jupiter quote for a shadow
        self.assertAlmostEqual(sh["cost"], 10.0, places=2)              # what the SIZER would size: 10 % of $100
        self.assertIn("np:social:tg", sh["learn"])
        self.assertEqual(n.np_shadow_wl.get("lll").status, "entered")   # its own copy of the coin, entered there
        opened = [t for t in self.lines(n, "LEARNER", "shadow") if t.startswith("SHADOW LLL · not bought (learner skip)")]
        self.assertEqual(len(opened), 1, self.lines(n, "LEARNER"))
        self.assertIn("same exits, outside the book", opened[0])
        n_before = n.mem["labels"]["np:social:tg"]["n"]
        sum_before = n.mem["labels"]["np:social:tg"]["sum"]
        self.read(n, "lll", self.rsol(sh, 7.3))                       # the scalp's +7 %: the plan sells it all
        self.assertEqual(n.shadows, {})
        row = n.state["shadow"]["closed"][-1]
        self.assertEqual((row["status"], row["exit"], row["kind"], row["steps"], row["weight"]),
                         ("closed", "TP +7%", "learner", 1, 0.5))
        self.assertGreater(row["pnl_pct"], 0)
        st = n.mem["labels"]["np:social:tg"]
        self.assertAlmostEqual(st["n"], n_before + 0.5)                 # half a trade
        self.assertAlmostEqual(st["sum"], sum_before + 0.5 * row["pnl_pct"], places=3)
        self.assertEqual((n.mem["shadows"], n.mem["samples"]), (1, 10))   # the page's "closed trades" stay real ones
        self.assertEqual(lr.load(n.mem_path)["shadows"], 1)            # memory.json saved
        said = [t for t in self.lines(n, "LEARNER", "shadow") if t.startswith("SHADOW LLL would have made")]
        self.assertEqual(len(said), 1)
        self.assertRegex(said[0], r"^SHADOW LLL would have made \+\d+\.\d% \(TP \+7%\) · learned x0\.5 · np:")
        # the book never saw it
        self.assertEqual((n.state["balance"], n.state["night"]["trades"], n.state["night"]["streak"]), (100.0, [], 0))
        self.assertFalse([t for t in self.lines(n, "EXECUTOR")])

    def test_a_shadow_is_priced_and_exits_exactly_as_the_real_trade_on_the_same_readings(self):
        # the same coin and the same readings, bought for real in one run and skipped by the LEARNER in the other:
        # every exit rule (the stop, the ladder, the dump, the migration, the time stop) books the same P&L %
        def after(n, mint, book, kind):
            pos = book[mint]
            if kind == "stop":
                self.read(n, mint, self.rsol(pos, -6))
                self.read(n, mint, self.rsol(pos, -9))
            elif kind == "tp":
                self.read(n, mint, self.rsol(pos, 7.3))
            elif kind == "dump":
                self.read(n, mint, 4.9)
                self.read(n, mint, 1.8)                         # one reading 3.1 SOL lower
            elif kind == "migrate":
                self.read(n, mint, 4.5)
                self.turn(n, {"kind": "migrate", "txType": "migrate", "mint": mint, "pool": "pump-amm", "signature": "s"})
            elif kind == "time":
                self.read(n, mint, self.rsol(pos, -2))
                self.tick(n, 6)                                 # stale: a real coin's Jupiter price, a shadow's last one
                self.tick(n, 300)                               # the scalp's 5 minutes, under +2 %
            elif kind == "quiet":
                self.read(n, mint, self.rsol(pos, 1))
                for _ in range(4):
                    self.tick(n, 50)                            # no reading for 3 minutes: dead, sold

        for kind, why in (("stop", "stop -9%"), ("tp", "TP +7%"), ("dump", "dump: 3.1 SOL sold"),
                          ("migrate", "migrating: take it"), ("time", None), ("quiet", "no curve reading for 3 min")):
            with self.subTest(kind=kind):
                self.setUp()
                real = self.night()
                self.launch(real, "aaa")
                after(real, "aaa", real.state["positions"], kind)
                t = real.state["night"]["trades"][0]
                self.setUp()
                n = self.night()
                self.skip_tg(n)
                self.launch(n, "aaa")
                after(n, "aaa", n.shadows, kind)
                self.assertEqual(n.shadows, {})
                row = n.state["shadow"]["closed"][-1]
                if why is not None:
                    self.assertEqual((t["why"], row["exit"]), (why, why))
                else:
                    self.assertRegex(t["why"], r"^5 min, only -\d%$")
                    self.assertEqual(row["exit"], t["why"])
                self.assertEqual((row["pnl_pct"], row["cost"]), (t["pnl_pct"], t["cost"]))
                self.assertEqual(row["pnl"], t["pnl"])

    def test_a_shadow_is_priced_by_the_curve_maths_with_every_cost_never_by_jupiter(self):
        real = self.night()
        self.launch(real, "aaa")
        self.read(real, "aaa", self.rsol(real.state["positions"]["aaa"], -9))
        t = real.state["night"]["trades"][0]
        self.setUp()
        n = self.night()
        self.skip_tg(n)
        self.launch(n, "aaa")
        sh = n.shadows["aaa"]
        self.assertEqual(sh["fill"], "curve")
        self.assertEqual(self.m.quotes, [])                     # not one quote spent on it
        # pump.fun's fee and the impact by the curve maths, the 1 % haircut, the $0.10 order fee: the real fill's
        sol = (sh["cost"] - 0.1) / 100.0
        want = E.mb.curve_buy_tokens(30.0 + E.SCALP[-1], E.K / (30.0 + E.SCALP[-1]), sol, 1.25) * 0.99
        self.assertAlmostEqual(sh["tokens_ui"], want, delta=1e-3)
        self.read(n, "aaa", self.rsol(sh, -9))
        row = n.state["shadow"]["closed"][-1]
        self.assertEqual((row["exit"], row["fill"]), ("stop -9%", "curve"))
        self.assertAlmostEqual(row["pnl_pct"], t["pnl_pct"], delta=0.11)
        self.assertEqual(self.m.quotes, [])

    def test_the_book_risk_and_the_night_are_untouched(self):
        # one run with shadows, one without: everything but the shadow's own record and its log lines is the same,
        # though the shadow lost (no loss streak, no pause, no trade, no cash)
        drop = {}

        def run(on):
            self.setUp()
            n = self.night(cfg(shadow_on=on, loss_streak=1))
            self.skip_tg(n)
            self.launch(n, "lll")
            if on:
                drop["rsol"] = self.rsol(n.shadows["lll"], -9)
            self.read(n, "lll", drop["rsol"])                           # the same reading in both runs
            if on:
                self.assertEqual(n.state["shadow"]["closed"][-1]["exit"], "stop -9%")
            n.save()
            # everything but the log and the night's own shadow counts (night["shadow"]: the morning report's)
            night = {k: v for k, v in n.state["night"].items() if k not in ("decisions", "lines", "np_said", "shadow")}
            return n, night

        on, night_on = run(True)
        off, night_off = run(False)
        self.assertEqual(night_on, night_off)
        for k in ("balance", "positions", "nights", "holders"):
            self.assertEqual(on.state[k], off.state[k], k)
        self.assertEqual((night_on["streak"], night_on["cooldown_until"], night_on["trades"]), (0, 0, []))
        self.assertIsNone(on.why_no_entries(400, {}))
        self.assertEqual(on.equity({}), 100.0)
        self.assertEqual((on.mem["skipped"], on.mem["samples"]), (off.mem["skipped"], off.mem["samples"]))
        self.assertEqual(off.shadows, {})
        self.assertEqual(off.state["shadow"]["n"], 0)
        self.assertFalse([t for t in self.lines(off, "LEARNER", "shadow")])

    def test_the_cap(self):
        n = self.night(cfg(shadow_max_open=2))
        self.skip_tg(n)
        for mint in ("aaa", "bbb", "ccc"):
            self.launch(n, mint)
        self.assertEqual(sorted(n.shadows), ["aaa", "bbb"])
        self.assertIn("SHADOW CCC not followed: 2 shadows open (max 2)", self.lines(n, "LEARNER", "shadow"))
        # rec_max_keys less np_max_watch caps them too: a shadow keeps its curve polled beside a full watch list
        self.setUp()
        n = self.night(cfg(np_max_watch=40, rec_max_keys=41))
        self.skip_tg(n)
        self.launch(n, "aaa")
        self.launch(n, "bbb")
        self.assertEqual(list(n.shadows), ["aaa"])
        self.assertIn("SHADOW BBB not followed: 1 shadows open (max 1)", self.lines(n, "LEARNER", "shadow"))

    def test_the_explore_stays_every_fifth_skip_is_bought_for_real(self):
        n = self.night(cfg(learn_explore_every=5))
        self.skip_tg(n)
        for i in range(5):
            self.launch(n, "c%d" % i)
        self.assertEqual(list(n.state["positions"]), ["c4"])            # the 5th skip: bought at half size
        self.assertEqual(sorted(n.shadows), ["c0", "c1", "c2", "c3"])
        self.assertEqual((n.mem["skipped"], n.mem["explored"]), (4, 1))
        self.assertTrue(any(t.startswith("explore · 1 in 5 skips") for t in self.lines(n, "LEARNER")))

    def test_no_shadow_when_the_coin_failed_the_rug_check(self):
        bad = E.rug_report("bad", risks=[{"name": "Creator history of rugged tokens", "level": "warn"}])
        for c in (cfg(), cfg(max_open=0)):                      # the LEARNER's path and RISK's
            with self.subTest(max_open=c["max_open"]):
                self.setUp()
                n = self.night(c)
                self.skip_tg(n)
                self.launch(n, "bad", report=bad)
                self.read(n, "bad", 4.8)
                self.assertEqual((n.shadows, n.state["positions"]), ({}, {}))
                self.assertTrue(n.wl.get("bad").status.startswith("rejected: risk: Creator history"))
                self.assertFalse([t for t in self.lines(n, "LEARNER", "shadow")])
        # said plainly: a shadow asks Jupiter nothing, so no sell simulation judges it; a coin a real buy's simulation
        # would reject (a pump.fun curve buys back by its maths, so it is rare) is followed by the curve maths
        self.setUp()
        n = self.night(cfg(max_open=0))
        self.m.tax["hp"] = 0.5
        self.launch(n, "hp")
        self.assertEqual(n.shadows["hp"]["fill"], "curve")
        self.assertEqual(self.m.quotes, [])


class Risk(Base):
    def test_a_risk_blocked_coin_is_a_shadow_until_it_is_bought_for_real(self):
        n = self.night(cfg(max_open=0))
        self.launch(n, "aaa")
        self.assertIn("no entry: 0 positions open (max)", self.lines(n, "RISK"))
        self.assertEqual(n.wl.get("aaa").status, "watching")          # RISK's retry stands: it can still be bought
        sh = n.shadows["aaa"]
        self.assertEqual((sh["shadow"], sh["shadow_why"]), ("risk", "0 positions open (max)"))
        self.assertEqual(n.state["night"]["checked"], 0)                # quiet: no night counts for it
        self.read(n, "aaa", 4.4)                                        # RISK still says no: one shadow, not two
        self.assertEqual(len([t for t in self.lines(n, "LEARNER", "shadow") if "not bought" in t]), 1)
        n.cfg["max_open"] = 8
        self.read(n, "aaa", 4.8)                                        # the next reading buys it for real
        self.assertIn("aaa", n.state["positions"])
        self.assertEqual(n.shadows, {})
        self.assertIn("SHADOW AAA dropped: bought for real · not learned", self.lines(n, "LEARNER", "shadow"))
        self.assertNotIn("shadows", n.mem)
        self.assertEqual(self.feeds.watching.get("aaa"), "curve-aaa")
        self.assertEqual(self.feeds.holding[-1], True)

    def test_a_paused_night_and_its_closed_risk_shadow_teach_the_learner(self):
        n = self.night()
        n.state["night"]["cooldown_until"] = n.epoch() + 600            # after a losing streak
        self.launch(n, "aaa")
        self.assertEqual(n.shadows["aaa"]["shadow_why"], "pause after a losing streak")
        self.read(n, "aaa", self.rsol(n.shadows["aaa"], -9))
        stats = n.panel_doc()["shadow_stats"]
        self.assertEqual((stats["n"], stats["wins"], stats["risk"]["n"], stats["learner"]["n"], stats["open"]),
                         (1, 0, 1, 0, 0))
        self.assertEqual(n.mem["shadows"], 1)
        self.assertEqual(n.state["night"]["streak"], 0)                 # its loss is no loss of the night's

    def test_shadow_on_risk_off_and_the_jobs_end_make_none(self):
        n = self.night(cfg(max_open=0, shadow_on_risk=False))
        self.launch(n, "aaa")
        self.assertEqual(n.shadows, {})
        self.setUp()
        n = self.night(cfg(max_open=0))
        n.job_left = 3.0                                                # the last 5 minutes of a job: nothing is bought
        self.launch(n, "aaa")
        self.assertEqual(n.shadows, {})


class SameTurn(Base):
    def test_a_shadow_and_a_real_position_on_different_coins_in_the_same_turn(self):
        n = self.night()
        self.skip_tg(n)
        self.launch(n, "sss", social=(False, True, False))              # Telegram: skipped, a shadow
        self.launch(n, "rrr", social=(True, False, False))              # X: bought for real
        self.assertEqual((list(n.shadows), list(n.state["positions"])), (["sss"], ["rrr"]))
        bal = n.state["balance"]
        cost = n.state["positions"]["rrr"]["cost"]
        self.assertAlmostEqual(bal, 100.0 - cost, places=6)             # the real buy alone took cash
        sh, pos = n.shadows["sss"], n.state["positions"]["rrr"]
        self.slot += 2
        self.turn(n, E.reading("sss", self.rsol(sh, 7.3), self.slot - 1), E.reading("rrr", self.rsol(pos, -9), self.slot))
        self.assertEqual((n.shadows, n.state["positions"]), ({}, {}))
        t = n.state["night"]["trades"]
        self.assertEqual([(x["symbol"], x["why"]) for x in t], [("RRR", "stop -9%")])
        self.assertEqual(n.state["shadow"]["closed"][-1]["exit"], "TP +7%")
        self.assertAlmostEqual(n.state["balance"], bal + t[0]["proceeds"], places=2)      # the shadow's sale not in it
        self.assertEqual(n.state["night"]["streak"], 1)
        rows = n.state["night"]["decisions"]
        ex = [r["n"] for r in rows if r["text"].startswith("EXIT RRR")][0]
        sw = [r["n"] for r in rows if r["text"].startswith("SHADOW SSS would have made")][0]
        self.assertLess(ex, sw)                                         # the held coin's reading first
        self.assertEqual(rows[-1]["t"], rows[[r["n"] for r in rows].index(ex)]["t"])   # one turn


class Safety(Base):
    def test_a_shadow_error_switches_the_shadows_off_and_the_real_trade_goes_on(self):
        n = self.night()
        self.skip_tg(n)
        self.launch(n, "sss", social=(False, True, False))
        self.launch(n, "rrr", social=(True, False, False))
        sh, pos = n.shadows["sss"], n.state["positions"]["rrr"]

        def broken(*a):
            raise ZeroDivisionError("a bug in a shadow")
        n.np_shadow_sale = broken
        self.slot += 2
        self.turn(n, E.reading("sss", self.rsol(sh, -9), self.slot - 1), E.reading("rrr", self.rsol(pos, -9), self.slot))
        self.assertEqual([t["why"] for t in n.state["night"]["trades"]], ["stop -9%"])     # the real exit went through
        self.assertEqual((n.shadows, n.np_shadow_wl), ({}, None))
        off = [t for t in self.lines(n, "LEARNER", "error") if t.startswith("shadow trades off: np_shadow_curve: "
                                                                          "ZeroDivisionError: a bug in a shadow")]
        self.assertEqual(len(off), 1)
        self.assertIn("1 open dropped, not learned · the trading goes on", off[0])
        self.assertEqual(n.panel_doc()["shadow_stats"]["open"], 0)
        self.launch(n, "ttt")                                           # skipped, never a shadow again this run
        self.assertEqual((n.shadows, n.state["positions"]), ({}, {}))

    def test_a_broken_drop_never_stops_the_real_buy(self):
        n = self.night(cfg(max_open=0))
        self.launch(n, "aaa")
        self.assertIn("aaa", n.shadows)

        def broken(*a):
            raise KeyError("drop")
        n.np_shadow_drop = broken
        n.cfg["max_open"] = 8
        self.read(n, "aaa", 4.8)
        self.assertIn("aaa", n.state["positions"])                      # bought all the same
        self.assertIsNone(n.np_shadow_wl)


class Polling(Base):
    def keep_open(self, n, mint, until_s):
        """Readings every 30 s that stay between the stop and the first step, with the 1 s step, until until_s."""
        k = 0
        while (self.clock.now() - E.T0).total_seconds() < until_s:
            k += 1
            self.read(n, mint, self.rsol(n.shadows[mint], 0.5 if k % 2 else -0.5), sec=30)
            n.watch(400)

    def test_the_shadow_keeps_its_curve_polled_and_lets_it_go_when_it_closes(self):
        n = self.night(cfg(rec_on=False))
        self.skip_tg(n)
        self.launch(n, "lll")
        self.assertNotIn(True, self.feeds.holding)                      # the poll rate is the REAL book's alone
        self.keep_open(n, "lll", 200)
        self.assertIsNone(n.wl.get("lll"))                              # the watch list let it go at np_watch_s
        self.assertNotIn("lll", self.feeds.unwatched)
        self.assertIn("lll", self.feeds.watching)
        self.read(n, "lll", self.rsol(n.shadows["lll"], 7.3))           # its readings still decide its exits
        self.assertEqual(n.state["shadow"]["closed"][-1]["exit"], "TP +7%")
        self.assertIn("lll", self.feeds.unwatched)
        self.assertNotIn("lll", self.feeds.watching)
        self.assertEqual(self.feeds.holding[-1], False)
        self.assertNotIn("lll", n.np_win)

    def test_the_recorder_follows_a_shadow_and_its_lines_are_on_the_tape(self):
        n = self.night(cfg(rec_max_follow=0))                           # no room for any other follow-up
        self.assertIsNotNone(n.rec)
        self.skip_tg(n)
        self.launch(n, "lll")
        self.keep_open(n, "lll", 200)
        self.assertIsNone(n.wl.get("lll"))
        self.assertIn("lll", n.rec.follow)                              # followed past its room: the bot keeps it
        self.read(n, "lll", self.rsol(n.shadows["lll"], 7.3))
        self.tick(n, 1)
        self.assertNotIn("lll", n.rec.follow)                           # closed: the follow-up lets it go
        self.assertIn("lll", self.feeds.unwatched)
        self.assertTrue(n.rec.drain(5.0))
        rows = [x for _, p in recorder.tape_files(os.path.join(self.dir, "tapes")) for x in recorder.read_lines(p)
                if x.get("m") == "lll"]
        follow = [x for x in rows if x["k"] == "follow"]
        self.assertEqual([x["why"] for x in follow], ["shadow"])
        self.assertTrue([x for x in rows if x["k"] == "c" and x["t"] > follow[0]["t"]])     # its readings after
        sh = [x for x in rows if x["k"] == "shadow"]
        self.assertEqual([x["act"] for x in sh], ["open", "sell"])
        self.assertEqual((sh[0]["kind"], sh[0]["fill"], sh[1]["final"], sh[1]["why"]), ("learner", "curve", True, "TP +7%"))
        self.assertFalse([x for x in rows if x["k"] in ("buy", "sell")])    # never a live trade's line
        start = [x for _, p in recorder.tape_files(os.path.join(self.dir, "tapes")) for x in recorder.read_lines(p)
                 if x["k"] == "start"][0]
        self.assertEqual((start["cfg"]["shadow_on"], start["cfg"]["shadow_weight"]), (True, 0.5))
        replay = load("memebot", "replay")
        tape = replay.read_tape(self.dir)
        self.assertEqual(tape.summary()["unknown_kinds"], {})
        self.assertEqual(tape.live_trades(), [])


class RecorderKept(unittest.TestCase):
    def test_a_kept_coin_is_followed_first_never_dropped_while_kept_and_ranked_as_a_shadow(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)

        class WL(object):
            def __init__(self):
                self.coins, self.skipped = {}, []

            def get(self, mint):
                return self.coins.get(mint)

        clock = [E.Clock().epoch() + 60]
        kept = set()
        r = recorder.Recorder(os.path.join(d, "tapes"), {"rec_max_follow": 2, "rec_hb_s": 10}, wl=WL(),
                              clock=lambda: clock[0])
        self.addCleanup(r.close, 5)
        r.kept = lambda: list(kept)
        for i, m in enumerate(("f1", "f2", "sh", "dud")):
            r.event(dict(E.create(m), _rx=clock[0] - 30 + i))
            r.coins[m].fired = m != "dud"
        self.assertTrue(r.keep("f1") and r.keep("f2"))                 # two fired coins fill the follow-up
        kept.add("sh")
        self.assertTrue(r.keep("sh"))                                   # the bot keeps it: followed all the same
        self.assertEqual(sorted(r.follow), ["f2", "sh"])                # the oldest fired coin made room
        self.assertFalse(r.keep("dud"))                                 # far from a trigger: not followed
        self.assertEqual(r._rank(r.coins["sh"]), "shadow")                # under a real held coin, over the rest
        self.assertEqual(r._rank(r.coins["f2"]), "follow")
        clock[0] += 2000                                                # past every rec_follow_s
        r.tick(clock[0])
        self.assertEqual(list(r.follow), ["sh"])                        # kept: it stays
        kept.clear()                                                    # the shadow closed
        clock[0] += 1
        self.assertEqual(r.tick(clock[0]), ["sh"])                      # let go: the bot unwatches it
        r.kept = lambda: 1 / 0                                          # a broken hook: the follow-up as before
        self.assertEqual(r._kept(), frozenset())
        self.assertTrue(r.drain(5))
        follow = [x for _, p in recorder.tape_files(os.path.join(d, "tapes")) for x in recorder.read_lines(p)
                  if x["k"] == "follow" and x["m"] == "sh"]
        self.assertEqual([x["why"] for x in follow], ["shadow"])


class JupMarket(E.CurveMarket):
    """CurveMarket's curve maths, but every quote goes through the bot's own Market.quote (its Budget, its rest after a
    429 / 5xx / timeout: quote_pause) with a fetch that serves the curve maths and answers 429 while fail_until (the
    test clock) has not passed: a one-second Jupiter blip. (The adversarial review's proof, kept as a regression.)"""

    def __init__(self, clock):
        E.CurveMarket.__init__(self)
        self.fail_until = float("-inf")
        self.clock = clock
        self.real = mb.Market(fetch=self.fetch, cfg=mb.DEFAULTS, clock=clock.epoch)
        self.quote_pause = self.real.quote_pause

    def fetch(self, url, timeout=20):
        import urllib.error
        import urllib.parse
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if self.clock.epoch() <= self.fail_until:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        out = E.CurveMarket.quote(self, q["inputMint"], q["outputMint"], int(q["amount"]))
        if out is None:
            raise urllib.error.HTTPError(url, 400, "no route", {}, None)
        return out

    def quote(self, input_mint, output_mint, amount, slippage_bps=300):
        q = self.real.quote(input_mint, output_mint, amount, slippage_bps)
        self.quote_why = self.real.quote_why
        return q

    def can_quote(self, n=1):
        return self.real.can_quote(n)


class Jupiter(Base):
    """A shadow never asks Jupiter: its quotes would share the real book's Jupiter, and one that failed (a 429, a 5xx, a
    timeout) rests every quote 10-60 s (Market.quote_pause), so the next REAL entry would find "Jupiter resting after
    a failure" and be bought late (9 s and +22 % in the review's probe) or never. Priced by the curve maths instead."""

    def scenario(self, shadow_on):
        self.m = JupMarket(self.clock)
        self.feeds = E.FakeFeeds(self.clock, self.m)
        n = self.night(cfg(shadow_on=shadow_on))
        self.skip_tg(n)
        self.launch(n, "sss", social=(False, True, False))             # Telegram: the LEARNER skips it
        self.assertEqual(bool(n.shadows), shadow_on)
        drop = E.vsol_for(n.shadows["sss"]["entry_price"] / self.m.sol * 0.91) - 30.0 if shadow_on else 3.0
        self.m.reports["rrr"] = E.rug_report("rrr")                     # a real coin with an X link: not skipped
        self.turn(n, E.create("rrr", "RRR"))
        self.turn(n, E.meta("rrr", True, False, False), sec=0.3)
        for rsol in E.SCALP[:-1]:
            self.read(n, "rrr", rsol)
        self.assertNotIn("rrr", n.state["positions"])
        self.m.fail_until = self.clock.epoch() + 1.0                    # a one-second Jupiter blip, now
        self.read(n, "sss", drop)                                       # the shadow's -9 % stop sells inside it
        self.read(n, "rrr", E.SCALP[-1])                                # rrr's trigger reading
        return n

    def test_a_shadows_sale_inside_a_jupiter_blip_never_delays_the_next_real_entry(self):
        for on in (True, False):
            with self.subTest(shadow_on=on):
                self.setUp()
                n = self.scenario(on)
                self.assertEqual([t for t in self.lines(n, "EXECUTOR", "wait") if "resting" in t], [])
                self.assertIn("rrr", n.state["positions"])             # bought on its trigger reading
                self.assertFalse(self.m.quote_pause.on())              # no quote failed: Jupiter never rested
                if on:
                    self.assertEqual(n.shadows, {})                    # the shadow sold at its stop
                    self.assertEqual(n.state["shadow"]["closed"][-1]["fill"], "curve")

    def test_a_skipped_coin_spends_no_quote_and_the_next_coin_is_bought(self):
        """The regression review's case: Jupiter's next answer fails, the LEARNER skips a coin, a real coin fires right
        after: with shadows on, the skipped coin's shadow asks nothing, so the real buy finds Jupiter as it was."""
        self.m = JupMarket(self.clock)
        self.feeds = E.FakeFeeds(self.clock, self.m)
        n = self.night(cfg())
        self.skip_tg(n)
        calls = []
        orig = self.m.fetch
        self.m.real.fetch = lambda url, timeout=20: (calls.append(url), orig(url, timeout))[1]
        self.launch(n, "skipme", social=(False, True, False))
        self.assertIn("skipme", n.shadows)
        self.assertEqual([u for u in calls if "skipme" in u], [])
        self.launch(n, "buyme", social=(True, False, False))
        self.assertIn("buyme", n.state["positions"])


class PollRate(Base):
    """The poll rate is every watched coin's (feeds.set_holding), and the entry rules count buy READINGS (np_min_buys,
    the buy share): an open SHADOW must not change it, or a shadow would change which real coins are bought."""

    def test_only_the_real_book_sets_the_poll_rate(self):
        n = self.night()
        self.skip_tg(n)
        self.launch(n, "lll")
        self.assertIn("lll", n.shadows)
        self.assertNotIn(True, self.feeds.holding)                      # a shadow alone: the idle rate (1 s)
        self.launch(n, "rrr", social=(True, False, False))              # a real buy: the held rate (0.5 s)
        self.assertIn("rrr", n.state["positions"])
        self.assertEqual(self.feeds.holding[-1], True)
        self.read(n, "rrr", self.rsol(n.state["positions"]["rrr"], -9))     # sold; the shadow is still open
        self.assertEqual((list(n.state["positions"]), list(n.shadows)), ([], ["lll"]))
        self.assertEqual(self.feeds.holding[-1], False)


class ReportScope(Base):
    """report_text() is a NIGHT's report: its shadow line counts that night's shadows (state["night"]["shadow"]), and
    the ones of every night since state.json began only as such ("od začiatku")."""

    def test_the_morning_report_counts_the_nights_own_shadows(self):
        n = self.night()
        self.skip_tg(n)
        for m in ("aaa", "bbb", "ccc"):
            self.launch(n, m)
            self.read(n, m, self.rsol(n.shadows[m], -9))
        self.assertEqual((n.state["shadow"]["n"], n.state["night"]["shadow"]["n"]), (3, 3))
        rep = mb.report_text(n.state)
        self.assertIn("Tieňové obchody tejto noci", rep)
        self.assertIn("spolu 3, v zisku 0", rep)
        self.assertNotIn("Od začiatku", rep)                            # the same three: said once
        n.begin("2026-11-26")
        n.begin("2026-11-27")                                           # a night with no shadow at all
        rep = mb.report_text(n.state)
        self.assertIn("noc 2026-11-27", rep)
        self.assertNotIn("spolu 3", rep)                                # not this night's
        self.assertIn("Tieňové obchody tejto noci", rep)
        self.assertIn("žiadne. Od začiatku (všetky noci): 3, v zisku 0, priemer", rep)
        self.launch(n, "ddd")
        self.read(n, "ddd", self.rsol(n.shadows["ddd"], 7.3))
        rep = mb.report_text(n.state)
        self.assertIn("spolu 1, v zisku 1", rep)
        self.assertIn("Od začiatku (všetky noci): 4, v zisku 1", rep)
        self.assertEqual(mb.report_text({"night": {"id": "x", "start_equity": 100.0, "trades": []}}).count("Tieň"), 0)
        # a night's own count with the all-time one lost (a hand-edited state.json): said, never a crash
        rep = mb.report_text({"night": {"id": "x", "start_equity": 100.0, "trades": [],
                                        "shadow": {"n": 2, "wins": 1, "sum": 3.0}}, "shadow": "junk"})
        self.assertIn("spolu 2, v zisku 1, priemer +1.5 %", rep)
        self.assertNotIn("Od začiatku", rep)


class TapeShare(unittest.TestCase):
    """recorder.py SHARE: a full hour drops the followed coins' readings at 60 %, the watched at 80 %, an open SHADOW's
    at 90 %, a REAL held coin's at 100 %: 20 shadows read every 0.5 s must never take the room a real position's
    readings need (its replay would see its curve "not covered")."""

    def run_hour(self, hook):
        import types
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        clock = [E.Clock().epoch() + 60]
        held = types.SimpleNamespace(mint="real", status="entered", why="", t0=clock[0] - 50, curve="curve-real",
                                     sol_in=0.0, buys=0, age_s=50.0)

        class WL(object):
            coins, skipped = {"real": held}, []

            def get(self, mint):
                return self.coins.get(mint)

        kept = {"sh%d" % i for i in range(20)}
        r = recorder.Recorder(os.path.join(d, "tapes"), {"rec_max_mb_hour": 0.05, "rec_hb_s": 1e9, "rec_max_follow": 60},
                              wl=WL(), clock=lambda: clock[0])
        self.addCleanup(r.close, 5)
        if hook:
            r.kept = lambda: list(kept)
        for i, m in enumerate(sorted(kept)):
            r.event(dict(E.create(m), _rx=clock[0] - 40 + i * 0.1))
            r.coins[m].fired = True
            r.keep(m)
        cap = 0.05 * recorder.MB
        slot = [10]

        def read(m, rsol):
            slot[0] += 1
            clock[0] += 0.01
            r.event(dict(E.reading(m, rsol, slot[0]), t=clock[0]))

        k = 0
        while (r.handed.get(r._hkey, 0) or 0) < 0.81 * cap:            # the hour's tape at 81 %
            k += 1
            read("real", 1.0 + k * 0.01)
        shadow_kept = 0
        for j in range(400):                                           # the 20 shadows' readings
            n0 = r.total["capped"]
            read("sh%d" % (j % 20), 1.0 + j * 0.01)
            shadow_kept += r.total["capped"] == n0
        held_ok = 0
        for j in range(30):                                            # then the REAL held coin's next readings
            n0 = r.total["capped"]
            read("real", 3.0 + j * 0.01)
            held_ok += r.total["capped"] == n0
        return held_ok, shadow_kept

    def test_kept_shadows_never_crowd_out_a_real_held_coins_readings(self):
        held, shadows = self.run_hour(True)
        self.assertEqual(held, 30)                                     # every real reading on the tape
        self.assertGreater(shadows, 0)                                 # the shadows' share: up to 90 % of the hour
        self.assertLess(shadows, 400)
        held_off, shadows_off = self.run_hour(False)                   # no hook: they rank as follow-ups (60 %)
        self.assertEqual((held_off, shadows_off), (30, 0))


class Restart(Base):
    def test_a_restart_drops_open_shadows_says_so_and_keeps_the_counts(self):
        n = self.night()
        self.skip_tg(n)
        self.launch(n, "aaa")
        self.read(n, "aaa", self.rsol(n.shadows["aaa"], -9))           # one closed
        self.launch(n, "bbb")                                           # one open
        n.save()
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        self.assertEqual(st["shadow"]["open"], ["BBB"])
        n2 = self.night()
        self.assertEqual(n2.shadows, {})
        self.assertIn("SHADOW 1 open shadow trade dropped by the restart, not learned: BBB", self.lines(n2, "LEARNER"))
        self.assertEqual((n2.state["shadow"]["n"], n2.state["shadow"]["closed"][-1]["symbol"]), (1, "AAA"))
        n2.save()
        self.assertEqual(mb.read_json(os.path.join(self.dir, "state.json"), {})["shadow"]["open"], [])
        n3 = self.night()
        self.assertEqual(len([t for t in self.lines(n3, "LEARNER") if "dropped by the restart" in t]), 1)   # n2's line

    def test_a_broken_shadow_record_never_breaks_the_load(self):
        for junk in (None, [], "x", {"n": "x", "wins": float("nan"), "by": [], "closed": {"a": 1}, "open": "AAA"},
                     {"n": 3, "wins": 9, "sum": float("inf"), "closed": [1, {"symbol": ["x"], "pnl_pct": float("nan")}],
                      "open": [1, "B" * 99]}):
            with self.subTest(junk=repr(junk)[:40]):
                self.setUp()
                with open(os.path.join(self.dir, "state.json"), "w") as fh:
                    json.dump({"balance": 100.0, "positions": {}, "shadow": junk}, fh)
                n = self.night()
                doc = n.panel_doc()
                json.dumps(doc, allow_nan=False)
                self.assertLessEqual(doc["shadow_stats"]["wins"], max(1, doc["shadow_stats"]["n"]))
                n.save()


class Page(Base):
    def test_the_panel_lists_the_shadows_and_their_stats_and_the_report_says_them(self):
        n = self.night()
        self.skip_tg(n)
        self.launch(n, "aaa")
        self.read(n, "aaa", self.rsol(n.shadows["aaa"], 7.3))
        self.launch(n, "bbb")
        self.read(n, "bbb", self.rsol(n.shadows["bbb"], 2.0))
        p = n.panel_doc()
        self.assertEqual([(r["symbol"], r["status"]) for r in p["shadows"]], [("BBB", "open"), ("AAA", "closed")])
        cost = n.shadows["bbb"]["cost"]                                 # the mark: the price +2 %, the sale not taken
        self.assertAlmostEqual(p["shadows"][0]["pnl_pct"], round(((cost - 0.1) * 1.02 / cost - 1) * 100, 1), delta=0.11)
        self.assertTrue(set(p["shadows"][0]) >= {"symbol", "mint", "status", "kind", "why", "opened", "cost", "pnl_pct",
                                                 "tier", "score", "steps", "fill"})
        st = p["shadow_stats"]
        closed = p["shadows"][1]
        self.assertEqual((st["n"], st["wins"], st["win_rate"], st["mean_pnl_pct"], st["open"]),
                         (1, 1, 100.0, closed["pnl_pct"], 1))
        self.assertEqual((st["learner"]["n"], st["risk"]["n"]), (1, 0))
        self.assertEqual(p["trades"], [])
        self.assertNotIn("shadows", mb.tick_msg(n.state, n.cfg, {}, n.ts(), 1, {}, n.speed, watch=n.np_rows()))
        json.dumps(p, allow_nan=False)
        self.assertIn("Tieňové obchody", mb.report_text(n.state))
        self.assertIn("spolu 1, v zisku 1", mb.report_text(n.state))
        self.assertEqual(p["learner"]["shadows"], 1)


# ---------------------------------------------------------------- the LEARNER's weights and memory.json

# memebot/learner.py's memory loading before the weights (f16ee6e), verbatim: what an older bot does with the file
def old_repair(doc):
    def _count(x):
        v = lr._num(x)
        return int(v) if v is not None and v > 0 else 0

    def fresh():
        return {"v": 1, "labels": {}, "samples": 0, "skipped": 0, "explored": 0, "skip_run": 0}

    if not isinstance(doc, dict) or doc.get("v", 1) != 1:
        return fresh()
    mem = fresh()
    for key in ("samples", "skipped", "explored", "skip_run"):
        mem[key] = _count(doc.get(key))
    for lab, st in lr._table(doc).items():
        st = lr._clean_stat(st)
        if lab in lr.KNOWN and st:
            mem["labels"][lab] = {k: lr._tidy(v) for k, v in st.items()}
    if "k" in doc or "min_n" in doc:
        mem["k"], mem["min_n"] = lr._gate(doc.get("k"), doc.get("min_n"))
    return mem


class Weights(unittest.TestCase):
    def test_a_weighted_shadow_counts_by_its_weight(self):
        mem = lr.fresh()
        lr.update(mem, ["liq:<20k", "age:<30m"], -10.0, weight=0.5, shadow=True)
        self.assertEqual(mem["labels"]["liq:<20k"], {"n": 0.5, "sum": -5, "sumsq": 50, "wins": 0})
        self.assertEqual((mem["samples"], mem["shadows"]), (0, 1))
        lr.update(mem, ["liq:<20k"], 20.0, weight=0.5, shadow=True)
        self.assertEqual(mem["labels"]["liq:<20k"], {"n": 1, "sum": 5, "sumsq": 250, "wins": 0.5})
        lr.update(mem, ["liq:<20k"], 4.0)                              # a real trade: weight 1, as before
        self.assertEqual((mem["labels"]["liq:<20k"]["n"], mem["samples"], mem["shadows"]), (2, 1, 2))
        self.assertEqual(lr.summary(mem)["shadows"], 2)

    def test_weights_are_clamped_and_garbage_learns_nothing(self):
        for w, n in ((2.0, 1), (1.0, 1), (0.25, 0.25), (0, None), (-1, None), (float("nan"), None), ("x", None),
                     (True, None)):
            with self.subTest(w=w):
                mem = lr.fresh()
                lr.update(mem, ["liq:<20k"], -10.0, weight=w, shadow=True)
                got = (mem["labels"].get("liq:<20k") or {}).get("n")
                self.assertEqual(got, n)
                if n is None:
                    self.assertEqual(mem, lr.fresh())                  # not even a count

    def test_the_score_moves_by_half_a_trade(self):
        mem = lr.fresh()
        for _ in range(8):
            lr.update(mem, ["liq:<20k"], -10.0)
        s0 = lr.score(mem, ["liq:<20k"], k=10, min_n=8)[0]             # -80 / 18
        lr.update(mem, ["liq:<20k"], 30.0, weight=0.5, shadow=True)
        s1 = lr.score(mem, ["liq:<20k"], k=10, min_n=8)[0]             # (-80 + 15) / 18.5
        self.assertAlmostEqual(s0, -80 / 18.0)
        self.assertAlmostEqual(s1, -65 / 18.5)
        self.assertEqual(lr.learned(mem, ["liq:<20k"]), "liq:<20k now avg -7.6% over 8.5")

    def test_memory_json_stays_readable_by_the_code_before_weights(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "memory.json")
        mem = lr.fresh()
        for i in range(9):
            lr.update(mem, ["liq:<20k", "np:social:tg"], -10.0 + i)
        for i in range(3):
            lr.update(mem, ["np:social:tg", "tier:scalp"], 7.5 - i, weight=0.5, shadow=True)
        lr.decide(mem, ["liq:<20k"], {"learn_k": 5})
        self.assertTrue(lr.save(path, mem))
        with open(path, encoding="utf-8") as fh:
            doc = json.loads(fh.read(), parse_constant=lambda c: self.fail(c))
        self.assertEqual(doc["v"], 1)                                   # the old code reads "v": 1 only
        old = old_repair(doc)
        new = lr.load(path)
        self.assertEqual(new["shadows"], 3)
        self.assertEqual(old, {k: v for k, v in new.items() if k != "shadows"})     # every label and count, as learned
        self.assertEqual(old["labels"]["tier:scalp"]["n"], 1.5)
        self.assertEqual(lr.score(old, ["np:social:tg"], k=5, min_n=8), lr.score(new, ["np:social:tg"], k=5, min_n=8))
        # what an older bot saves back (no "shadows") loads here as a memory without shadows
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(old, fh)
        back = lr.load(path)
        self.assertNotIn("shadows", back)
        self.assertEqual(back["labels"], new["labels"])

    def test_an_old_memory_loads_and_learns_as_before(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "memory.json")
        old = {"v": 1, "samples": 12, "skipped": 4, "explored": 1, "skip_run": 2, "k": 5, "min_n": 8,
               "labels": {"hour:day": {"n": 10, "sum": -170, "sumsq": 4000, "wins": 2},
                          "news:0-2": {"n": 10, "sum": -170, "sumsq": 4000, "wins": 2}}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(old, fh)
        mem = lr.load(path)
        self.assertEqual(mem, old)                                      # no new key on a memory without shadows
        self.assertEqual(lr.summary(mem), {"samples": 12, "skipped": 4, "explored": 1, "min_samples": 8,
                                           "best": [{"label": "hour:day", "n": 10, "avg": -17.0}],
                                           "worst": [{"label": "news:0-2", "n": 10, "avg": -17.0}]})
        a, b = json.loads(json.dumps(mem)), json.loads(json.dumps(mem))
        lr.update(a, ["hour:day"], 5.0)
        lr.update(b, ["hour:day"], 5.0, weight=None, shadow=False)
        self.assertEqual(a, b)
        self.assertEqual(a["labels"]["hour:day"], {"n": 11, "sum": -165, "sumsq": 4025, "wins": 3})

    def test_the_bot_learns_a_shadow_only_from_a_learner_with_weights(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        n = mb.Night(d, cfg(), E.CurveMarket(), E.T0, lambda *a: None)
        pos = {"symbol": "X", "learn": ["liq:<20k"]}
        real = lr.WEIGHTS
        try:
            lr.WEIGHTS = False
            self.assertEqual(n.np_shadow_learn(pos, -5.0), ("not learned (learner.py takes no weights)", 0.0))
        finally:
            lr.WEIGHTS = real
        self.assertEqual(n.np_shadow_learn(dict(pos, learn=[]), -5.0), ("not learned (no labels)", 0.0))
        n.cfg["shadow_weight"] = 0
        self.assertEqual(n.np_shadow_learn(pos, -5.0)[1], 0.0)
        n.cfg["shadow_weight"] = 0.5
        text, w = n.np_shadow_learn(pos, -5.0)
        self.assertEqual((text, w, n.mem["labels"]["liq:<20k"]["n"]), ("learned x0.5", 0.5, 0.5))   # under one trade
        text, w = n.np_shadow_learn(pos, -7.0)
        self.assertEqual(text, "learned x0.5 · liq:<20k now avg -6.0% over 1")
        n.mem = None
        self.assertEqual(n.np_shadow_learn(pos, -5.0), ("not learned (LEARNER off)", 0.0))


if __name__ == "__main__":
    unittest.main()
