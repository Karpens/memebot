"""SNIPE mode (config.json np_snipe; newpairs.snipe, bot.py Night.np_snipe_fire): a watched pump.fun coin is bought on
its FIRST curve reading, within np_snipe_max_age_s of its create, once, when the cheap checks pass: no momentum trigger,
no minimum age, no RugCheck wait, no LEARNER skip (it still learns). RISK and the SIZER as for any entry; the fill a
Jupiter quote when Jupiter has a route, else the curve maths on that reading with every cost ("fill curve"); the stop
np_snipe_stop_pct (-10 %) until the first step, while a momentum trade keeps stop_pct (-7 %). Offline, on
tests/test_newpairs_engine.py's FakeFeeds and CurveMarket (pump.fun's maths as Jupiter, SOL at $100) and
tests/test_shadow.py's harness; the replay on tests/test_endtoend.py's synthetic stream. Paper on paper: these tests say
the code does what it says, never that a snipe makes money."""

import copy
import json
import os
import shutil
import tempfile
import threading
import unittest
import unittest.mock

import test_newpairs_engine as E
import test_shadow as S

os.environ["MEMEBOT_LIVE"] = "0"
os.environ["MEMEBOT_FEEDS"] = "0"
mb = E.mb
npm = mb.newpairs
lr = mb.learner
CONFIG = mb.load_config()                       # today's config.json on the code's defaults


def cfg(**kw):
    """The engine tests' settings with SNIPE on as config.json has it (its tiers, holders and the creator's buy up to
    70 %, the $5 minimum order, no social link needed, one other launch allowed, the -30 % night stop)."""
    c = S.cfg(np_snipe=True, np_snipe_max_age_s=10, np_snipe_stop_pct=-10.0, np_max_dev_pct=70.0,
              np_max_holder_pct=70.0, np_need_social=False, min_order_usd=5.0, np_serial_max=1,
              hold_tiers=copy.deepcopy(CONFIG["hold_tiers"]), tp1_pct=10.0, night_stop_pct=-30.0, dump_loss_pct=30.0)
    c.update(kw)
    return c


class Base(S.Base):
    def first(self, n, mint, rsol=0.8, after=1.0, left=400, complete=False, **kw):
        """A create (kw: its fields), then the coin's first curve reading `after` seconds later. -> the coin (None: not
        watched)."""
        self.turn(n, E.create(mint, mint.upper(), **kw), sec=0.5, left=left)
        self.read(n, mint, rsol, sec=after, left=left, complete=complete)
        return n.wl.get(mint)

    def to_gain(self, n, mint, gain_pct, sec=1.0):
        pos = n.state["positions"].get(mint) or n.shadows.get(mint)
        self.read(n, mint, self.rsol(pos, gain_pct), sec=sec)


class FirstReading(Base):
    def test_a_snipe_buys_on_the_first_reading_with_no_rugcheck_answer(self):
        n = self.night(cfg())
        self.m.rug_gate = threading.Event()             # RugCheck would never answer: it is not asked anyway
        self.addCleanup(self.m.rug_gate.set)
        self.turn(n, E.create("zap", "ZAP"), sec=0.5)
        self.assertEqual(n.state["positions"], {})      # never before the first reading
        self.turn(n, E.meta("zap", True, True, False), sec=0.3)
        self.assertEqual(n.state["positions"], {})      # the links are no reading
        self.read(n, "zap", 0.8, sec=0.9)
        pos, coin = n.state["positions"]["zap"], n.wl.get("zap")
        self.assertEqual(self.m.rug_calls, [])
        self.assertEqual((coin.status, pos["snipe"], pos["stop_pct"], pos["entry_fill"], pos["strategy"]),
                         ("entered", True, -10.0, "quote", "new_pairs"))
        self.assertTrue(pos["reason"].startswith("snipe · first reading 1.2 s after the create · 0.5 SOL in"), pos)
        self.assertTrue(pos["reason"].endswith(" · fill quote"))
        # filled at Jupiter's quote on that very reading (the curve maths: 0.0 % off), the SIZER's 10 %
        self.assertEqual(pos["curve_check"]["buy_pct"], 0.0)
        self.assertEqual((pos["size_pct"], pos["plan"]["stop_pct"]), (10.0, -10.0))
        enter = self.lines(n, "EXECUTOR", "enter")
        self.assertEqual(len(enter), 1)
        self.assertIn("· snipe · first in · 0.0 s after the first reading · 1 s after launch · fill quote · Jupiter 0.0 "
                      "s · curve maths 0.0% off Jupiter · stop -10%", enter[0])
        self.assertEqual(self.lines(n, "SCANNER", "flag"), [coin.why])
        self.assertEqual(n.state["night"]["flags"][-1]["why"], "snipe")
        # RugCheck for the tape only, once, 25 s after the launch: never waited on, the coin stays held
        self.tick(n, 10)
        self.assertEqual(self.m.rug_calls, [])
        self.tick(n, 15)
        self.tick(n, 1)
        self.assertTrue(E.mb_until(lambda: self.m.rug_calls == ["zap"], 2.0))
        self.assertIn("zap", n.state["positions"])
        self.assertEqual(n.np_snipe_rugs, {})                           # asked: once

    def test_never_on_a_coin_first_read_after_np_snipe_max_age_s(self):
        n = self.night(cfg())
        late = self.first(n, "late", 0.8, after=11.0)
        self.assertEqual(late.status, "rejected: snipe: first reading 11.0 s after the create (max 10 s)")
        edge = self.first(n, "edge", 0.8, after=10.0)               # inclusive
        self.assertEqual(edge.status, "entered")
        for rsol in E.RUNNER:                                        # a momentum run later: never bought
            self.read(n, "late", 1.0 + rsol)
        self.assertNotIn("late", n.state["positions"])
        self.assertEqual(self.m.rug_calls, [])

    def test_once_a_coin_not_bought_on_its_first_reading_is_never_bought_later(self):
        n = self.night(cfg())
        self.turn(n, E.create("busy", "BUSY"), sec=0.5)
        with unittest.mock.patch.object(n, "why_no_entries", return_value="pause after a losing streak"):
            self.read(n, "busy", 0.8, sec=1.0)
        coin = n.wl.get("busy")
        self.assertEqual(coin.status, "rejected: snipe not bought: pause after a losing streak")
        for rsol in E.RUNNER:
            self.read(n, "busy", 1.0 + rsol)
        self.assertNotIn("busy", n.state["positions"])
        # a turn that failed after the fire (an error, caught by the loop) leaves the coin unbought for good: the next
        # look (its links arrive, the curve unchanged) would fill that old reading at a later time
        self.turn(n, E.create("oops", "OOPS"), sec=0.5)
        with unittest.mock.patch.object(n, "np_snipe_buy", side_effect=RuntimeError("boom")):
            self.read(n, "oops", 0.8, sec=1.0)
        self.assertIn("error in a New Pairs event (RuntimeError: boom) · going on", self.lines(n, "LOGGER", "error"))
        self.turn(n, E.meta("oops", True, False, False), sec=0.3)
        self.assertEqual((n.wl.get("oops").status, "oops" in n.state["positions"]),
                         ("rejected: snipe: fired once, not bought", False))
        self.assertEqual(self.first(n, "after", 0.8).status, "entered")      # the loop went on
        self.assertEqual(npm.snipe(npm.Coin(mint="x", readings=2), cfg()), (False, "snipe: past its first reading"))
        self.assertEqual(npm.snipe(npm.Coin(mint="x"), cfg()), (None, "no curve reading yet"))


class CheapChecks(Base):
    def test_each_cheap_check_still_rejects(self):
        n = self.night(cfg())
        self.assertIsNone(self.first(n, "may", mayhem=True))         # mayhem mode: never watched
        self.assertNotIn("may", n.state["positions"])
        for m in ("s1", "s2", "s3"):                                 # a creator's third coin in 60 min (np_serial_max 1)
            self.turn(n, E.create(m, m.upper(), creator="dev-serial"), sec=0.5)
        self.read(n, "s3", 0.8)
        self.assertTrue(n.wl.get("s3").status.startswith("rejected: serial launcher: 2 more coins in 60 min"))
        heavy = self.first(n, "heavy", 60.8, dev_sol=60.0)           # the creator bought 71.5 % of the supply
        self.assertEqual(heavy.status, "rejected: dev bought 71.5% (max 70%)")
        big = self.first(n, "big", 50.8, dev_sol=50.0)               # 67.1 %: under 70 % (the old 8 % refused it)
        self.assertEqual((round(big.dev_pct, 1), big.status), (67.1, "entered"))
        done = self.first(n, "done", 0.8, complete=True)
        self.assertEqual(done.status, "rejected: curve complete")
        self.turn(n, E.create("odd", "ODD"), sec=0.5)
        ev = E.reading("odd", 0.8, 5000)                             # the account is not a pump.fun bonding curve
        ev["disc_ok"] = False
        self.turn(n, ev)
        self.assertEqual(n.wl.get("odd").status, "rejected: not a pump.fun bonding curve")
        dump = self.first(n, "dump", 0.5, dev_sol=3.0)               # 2.5 SOL sold before the first reading
        self.assertEqual(dump.status, "rejected: dump: 2.5 SOL sold")
        self.assertEqual(sorted(n.state["positions"]), ["big"])


class Fills(Base):
    def test_no_jupiter_route_fills_by_the_curve_maths_with_every_cost_and_the_loop_goes_on(self):
        n = self.night(cfg())
        self.m.no_route.add("nor")
        coin = self.first(n, "nor", 3.0, after=1.0)                  # the snipers' 2.7 SOL in its first second
        pos = n.state["positions"]["nor"]
        # the curve maths on the reading that fired (not the create's state), every cost of a real fill
        usd_in, fee_sol = 10.0 - 0.001 * 100.0, 0.001
        got = mb.curve_buy_tokens(coin.vsol, coin.vtok, usd_in / 100.0, 1.25)
        self.assertAlmostEqual(coin.vsol, 33.0, places=6)
        self.assertEqual(pos["raw"], int(got * 1e6 * 0.99))
        self.assertNotEqual(pos["raw"], int(mb.curve_buy_tokens(coin.vsol0, 1.073e9 * 30 / coin.vsol0, usd_in / 100.0,
                                                                1.25) * 1e6 * 0.99))
        self.assertEqual((pos["cost"], pos["fee_sol"]), (10.0, fee_sol))
        # no "fill" on the position (that is a sale's: np_curve_sale) and nothing to check the maths against
        self.assertEqual((pos["entry_fill"], pos.get("fill"), pos["curve_check"]), ("curve", None, None))
        self.assertTrue(pos["reason"].endswith(" · fill curve"))
        self.assertGreater(pos["impact_pct"], 0.0)
        enter = self.lines(n, "EXECUTOR", "enter")[0]
        self.assertIn("· snipe · first in · 0.0 s after the first reading · 1 s after launch · fill curve (Jupiter: no "
                      "Jupiter route · 0.0 s) · pump.fun's fee, the curve's impact, the 1% haircut and the fees · stop "
                      "-10%", enter)
        # the loop goes on: the next coin is bought too (by the curve maths at once: a coin is held, its exits first)
        nxt = self.first(n, "next", 0.6)
        self.assertEqual((nxt.status, n.state["positions"]["next"]["entry_fill"]), ("entered", "curve"))
        # still no route at the stop: sold by the same curve maths
        self.to_gain(n, "nor", -11.0)
        self.assertNotIn("nor", n.state["positions"])
        t = n.state["night"]["trades"][-1]
        self.assertEqual((t["mint"], t["why"], t["fill"], t["strategy"]), ("nor", "stop -11%", "curve", "new_pairs"))
        self.assertIn("by the curve maths (Jupiter: no Jupiter route; the entry was filled by it too)",
                      self.lines(n, "EXECUTOR", "exit")[-1])

    def test_a_full_jupiter_minute_fills_by_the_curve_without_asking(self):
        n = self.night(cfg())
        self.m.can_quote = lambda k: False
        self.first(n, "full", 0.8)
        pos = n.state["positions"]["full"]
        self.assertEqual((pos["entry_fill"], pos["fill_why"]), ("curve", "Jupiter not asked: its minute is full"))
        self.assertEqual([q for q in self.m.quotes if q[0] == mb.SOL], [])

    def test_a_failed_sell_simulation_rejects_the_snipe(self):
        n = self.night(cfg())
        self.m.tax["pot"] = 0.5                                      # the quotes lose half: a honeypot by Jupiter
        coin = self.first(n, "pot", 0.8)
        self.assertNotIn("pot", n.state["positions"])
        self.assertTrue(coin.status.startswith("rejected: snipe not bought: sell simulation: would lose"), coin.status)
        self.assertTrue(self.lines(n, "RUG CHECK", "reject")[-1].startswith("sell simulation: would lose"))


class Stops(Base):
    def test_a_sniped_trade_stops_at_minus_10_and_a_momentum_trade_keeps_minus_7(self):
        n = self.night(cfg())
        self.first(n, "snp", 3.0)
        self.to_gain(n, "snp", -8.0)
        self.assertIn("snp", n.state["positions"])                   # a momentum trade would be out at -7
        row = mb.positions_view(n.state, {})[0][0]
        self.assertEqual(row["plan"]["stop_pct"], -10.0)
        self.to_gain(n, "snp", -11.0)
        self.assertNotIn("snp", n.state["positions"])
        self.assertEqual(n.state["night"]["trades"][-1]["why"], "stop -11%")
        # the momentum strategy (np_snipe off): -7 %, as before
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        m = mb.Night(d, S.cfg(), self.m, self.clock.now(), self.out.append)
        m.clock_fn = self.clock.now
        m.begin("2026-11-25")
        m.attach(self.feeds)
        self.addCleanup(m.np_close)
        self.launch(m, "mom", E.SCALP)
        pos = m.state["positions"]["mom"]
        self.assertNotIn("stop_pct", pos)
        self.assertEqual(pos["plan"]["stop_pct"], -7.0)
        self.assertIs(m.np_pos_cfg(pos), m.cfg)
        self.to_gain(m, "mom", -8.0)
        self.assertEqual(m.state["night"]["trades"][-1]["why"], "stop -8%")

    def test_after_the_first_step_the_snipe_has_the_plans_break_even_and_trail(self):
        n = self.night(cfg())
        n.np_entry_plan = lambda coin, stop=None, real=n.np_entry_plan: (
            lambda p: (dict(p[0], tier="swing", ladder=[[10.0, 0.5], [20.0, 1.0]], trail_pct=6.0, time_stop_min=10.0,
                            entry_tier="swing"), p[1]))(real(coin, stop))
        self.first(n, "lad", 3.0)
        self.to_gain(n, "lad", 11.0)                                 # the first step: half sold at +10 %
        pos = n.state["positions"]["lad"]
        self.assertTrue(pos["tp1_done"])
        self.assertGreater(pos["plan"]["stop_pct"], 0.0)             # break-even after costs, not -10
        self.to_gain(n, "lad", -1.0)
        self.assertNotIn("lad", n.state["positions"])
        self.assertIn(n.state["night"]["trades"][-1]["why"].split(" (")[0], ("break-even stop", "trailing stop 6% off the top"))

    def test_a_broken_stop_on_a_position_falls_back_to_the_config(self):
        n = self.night(cfg())
        for bad in (None, "x", True, 5.0, 0.0, float("nan"), -150.0):
            self.assertIs(n.np_pos_cfg({"stop_pct": bad}), n.cfg, bad)
        self.assertEqual(n.np_pos_cfg({"stop_pct": -10.0})["stop_pct"], -10.0)
        for bad, want in ((None, -10.0), ("x", -10.0), (True, -10.0), (3.0, -10.0), (-12.5, -12.5), (-500.0, -99.0)):
            self.assertEqual(mb.snipe_stop({"np_snipe_stop_pct": bad}), want, bad)


class MomentumOff(Base):
    def test_the_momentum_entry_is_off_in_snipe_mode(self):
        n = self.night(cfg())
        with unittest.mock.patch.object(n, "np_fire", side_effect=AssertionError("momentum entry")), \
                unittest.mock.patch.object(n, "np_prefetch", side_effect=AssertionError("prefetch")):
            late = self.first(n, "mo", 0.8, after=12.0)             # not sniped: too late
            for rsol in E.RUNNER:                                    # then a run the momentum trigger fires on
                self.read(n, "mo", 1.0 + rsol)
        self.assertEqual((late.status, n.state["positions"], self.m.rug_calls),
                         ("rejected: snipe: first reading 12.0 s after the create (max 10 s)", {}, []))

    def test_with_np_snipe_off_the_first_reading_buys_nothing_and_momentum_buys_as_before(self):
        n = self.night(S.cfg(np_snipe=False))
        self.turn(n, E.create("mo", "MO"), sec=0.5)
        self.read(n, "mo", 0.8)
        self.assertEqual(n.state["positions"], {})
        self.launch(n, "uzim", E.RUNNER, social=(True, True, True))
        self.assertIn("uzim", n.state["positions"])
        pos = n.state["positions"]["uzim"]
        self.assertTrue(pos["reason"].startswith("momentum ·"))
        self.assertNotIn("snipe", pos)
        self.assertNotIn("entry_fill", pos)
        self.assertFalse(mb.snipe_on(n.cfg))
        self.assertFalse(mb.snipe_on(mb.load_config(None)))            # a config without the key: off
        self.assertTrue(mb.snipe_on(CONFIG))                            # config.json: on


class Learner(Base):
    def test_the_learner_never_skips_a_snipe_and_learns_from_every_closed_one(self):
        n = self.night(cfg())
        for _ in range(10):                                          # coins like it lost 15 % each: momentum would skip
            lr.update(n.mem, ["np:age:<30s"], -15.0)
        self.assertEqual(lr.decide(copy.deepcopy(n.mem), ["np:age:<30s"], n.cfg)["action"], "skip")
        self.first(n, "lrn", 3.0)
        self.first(n, "lrn2", 0.7)
        pos = n.state["positions"]["lrn"]
        self.assertIn("np:age:<30s", pos["learn"])
        self.assertEqual(pos["size_pct"], 10.0)                       # the SIZER's 10 %, no LEARNER size
        self.assertEqual([t for t in self.lines(n, "LEARNER") if t.startswith(("skip", "explore", "buy"))], [])
        self.assertEqual(len(self.lines(n, "LEARNER", "snipe")), 1)
        self.assertEqual((n.mem["skip_run"], n.mem["skipped"], n.mem["explored"]), (0, 0, 0))
        self.to_gain(n, "lrn", -11.0)
        # the closed snipe teaches the snipes' own memory (memory_snipe.json); memory.json keeps the momentum's 10
        self.assertEqual((n.mem["samples"], n.mem_snipe["samples"]), (10, 1))
        self.assertEqual((n.mem["labels"]["np:age:<30s"]["n"], n.mem_snipe["labels"]["np:age:<30s"]["n"]), (10, 1))
        saved = mb.read_json(os.path.join(self.dir, "memory_snipe.json"), {})
        self.assertEqual(saved.get("samples"), 1)
        want = lr.summary(n.mem_snipe, n.cfg.get("learn_k"), n.cfg.get("learn_min_n"))
        self.assertEqual(n.state["learner_summary"], dict(want, memory="snipe"))      # (no label has 8 trades yet)


class Risk(Base):
    def test_eight_open_then_no_more_and_the_blocked_snipe_is_a_risk_shadow_with_the_snipe_stop(self):
        n = self.night(cfg())
        for i in range(8):
            self.first(n, "c%d" % i, 0.8)
        self.assertEqual(len(n.state["positions"]), 8)
        ninth = self.first(n, "c8", 3.0)
        self.assertEqual(ninth.status, "rejected: snipe not bought: 8 positions open (max)")
        self.assertIn("no entry: 8 positions open (max)", self.lines(n, "RISK"))
        sh = n.shadows["c8"]
        self.assertEqual((sh["shadow"], sh["snipe"], sh["stop_pct"], sh["fill"]), ("risk", True, -10.0, "curve"))
        self.to_gain(n, "c8", -8.0)
        self.assertIn("c8", n.shadows)                                # its stop is the snipe's -10, not -7
        self.to_gain(n, "c8", -11.0)
        self.assertNotIn("c8", n.shadows)
        self.assertEqual(n.state["shadow"]["closed"][-1]["exit"], "stop -11%")
        self.assertEqual(len(n.state["positions"]), 8)

    def test_the_night_stop_and_its_guard_the_last_minutes_the_job_end_and_a_small_book(self):
        n = self.night(cfg())
        n.state["night"]["start_equity"] = 200.0                      # the night at -50 %
        self.assertEqual(self.first(n, "a", 0.8).status, "rejected: snipe not bought: night at -50%: stopped for the night")
        n.state["night"]["start_equity"] = 140.0                      # -28.6 %, but one dump wave would pass -30 %
        self.assertTrue(self.first(n, "b", 0.8).status.startswith("rejected: snipe not bought: night at -29%: 0 open "
                                                                  "and one more order could take it past -30%"))
        n.state["night"]["start_equity"] = 100.0
        c = self.first(n, "c", 0.8, left=10)                          # the session's last 10 minutes
        self.assertEqual(c.status, "rejected: snipe not bought: last 10 min: no new entries")
        n.job_left = 4.0                                              # a job's last 5 minutes
        self.assertTrue(self.first(n, "d", 0.8).status.startswith("rejected: snipe not bought: last 5 min of this job"))
        n.job_left = None
        self.assertNotIn("c", n.shadows)                              # nothing is bought there, RISK or not: no shadow
        self.assertNotIn("d", n.shadows)
        self.assertIn("a", n.shadows)                                 # the night stop: RISK's, shadowed
        n.state["balance"] = n.state["night"]["start_equity"] = 45.0  # under a $50 book: 10 % is under $5
        e = self.first(n, "e", 0.8)
        self.assertEqual(e.status, "rejected: snipe not bought: only $4.50 to size")
        self.assertIn("only $4.50 left to size", self.lines(n, "SIZER"))
        self.assertEqual(n.state["positions"], {})

    def test_the_trades_of_the_night_and_the_pause_bind(self):
        n = self.night(cfg(max_trades_per_night=2))
        self.first(n, "t1", 0.8)
        self.first(n, "t2", 0.8)
        self.assertEqual(self.first(n, "t3", 0.8).status, "rejected: snipe not bought: 2 trades tonight (max)")
        m = self.night(cfg(), folder=tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, m.dir, True)
        m.state["night"]["cooldown_until"] = m.epoch() + 120
        self.assertEqual(self.first(m, "p1", 0.8).status, "rejected: snipe not bought: pause after a losing streak")


class Texts(unittest.TestCase):
    def prompt(self, c):
        return mb.panel({"balance": 100.0, "positions": {}}, c, {}, None)["prompt"]

    def test_the_page_shows_the_snipe_prompt_in_snipe_mode_and_the_old_one_without(self):
        with open(mb.PROMPT_SNIPE, encoding="utf-8") as fh:
            snipe = fh.read()
        with open(mb.PROMPT, encoding="utf-8") as fh:
            momentum = fh.read()
        self.assertEqual(self.prompt(CONFIG), snipe)
        self.assertEqual(self.prompt(dict(CONFIG, np_snipe=False)), momentum)
        self.assertEqual(self.prompt(dict(CONFIG, strategies=["migrated"])), momentum)
        for gone in ("Never before 45 seconds", "a RugCheck report within", "it skips the coin"):
            self.assertNotIn(gone, snipe)

    def test_the_snipe_prompt_says_what_config_json_does(self):
        c = CONFIG
        with open(mb.PROMPT_SNIPE, encoding="utf-8") as fh:
            p = fh.read()
        tiers = {t["name"]: t for t in c["hold_tiers"]}

        def says(t):
            steps = ["%.0f%% at +%g%%" % (frac * 100, gain) for gain, frac in t["ladder"][:-1]]
            steps.append("the rest at +%g%%" % t["ladder"][-1][0])
            return (", " if len(steps) > 2 else " and ").join(steps)

        self.assertEqual((c["np_snipe"], c["np_snipe_max_age_s"], c["np_snipe_stop_pct"], c["np_max_holder_pct"],
                          c["np_max_dev_pct"], c["stop_pct"]), (True, 10, -10.0, 70.0, 70.0, -7.0))
        for text in ("starting balance of $%g." % c["start_balance"], "sell at a 10% loss",
                     "Every new coin is watched for %d seconds (%d at once" % (c["np_watch_s"], c["np_max_watch"]),
                     "every %g seconds while anything is held, every second otherwise" % c["curve_poll_sec"],
                     "Buy on the coin's first curve reading", "within %d seconds of the launch" % c["np_snipe_max_age_s"],
                     "No momentum trigger, no minimum age, no wait for RugCheck", "Each coin once",
                     "Why the bot used to wait %d seconds" % c["np_min_age_s"],
                     "the creator bought at most %g%% of the supply" % c["np_max_dev_pct"],
                     "A single holder may hold up to %g%%" % c["np_max_holder_pct"],
                     "no single sell of %g SOL or more" % c["np_dump_sol"],
                     "%d or more other launches in the last %d minutes" % (c["np_serial_max"] + 1, c["np_serial_window_min"]),
                     "the curve not complete", "about %d seconds after the launch of every snipe (held or already "
                     "sold) and every snipe shadow" % mb.SNIPE_RUG_S,
                     # the review's findings: the fills as the code makes them, and what the sell simulation's silence does
                     "the bot decides within those %d seconds too" % c["np_snipe_max_age_s"],
                     "when nothing is held and Jupiter has a route for the coin now",
                     "While a coin is held its exits come first: the buy asks Jupiter nothing",
                     "at most %g second old and with no newer reading of the coin already in" % c["np_snipe_fresh_s"],
                     "a slow Jupiter answer or a slow loop leaves the coin unbought",
                     "assumes the order lands at that reading's state plus the 1% haircut",
                     "with no answer from Jupiter for the simulation the buy goes through, the sale unchecked",
                     "every coin that reached the buy counts as checked",
                     "never on reserves older than %g seconds" % c["np_stale_s"],
                     "in a memory of its own (memory_snipe.json)",
                     "may lose at most %g%%" % c["max_roundtrip_loss_pct"],
                     "%g%% of the balance per trade, never less than $%g" % (c["position_pct"], c["min_order_usd"]),
                     "under a $%g book it buys nothing" % (c["min_order_usd"] * 100 / c["position_pct"]),
                     "\"fill curve\"", "never an earlier one",
                     "up to %d open positions and %d trades a night" % (c["max_open"], c["max_trades_per_night"]),
                     "a %g minute pause after %d losses in a row" % (c["cooldown_min"], c["loss_streak"]),
                     "no new entries in the last %d minutes" % c["no_entries_last_min"],
                     "stop for the night at %g%%" % c["night_stop_pct"],
                     "the new order could lose another %g%% in one dump" % c["dump_loss_pct"],
                     "the last %d minutes of a job" % c["job_no_entries_min"],
                     "it never skips a coin and never changes a size", "It still learns from every closed snipe",
                     "Snipes that RISK blocks are followed as shadow trades, up to %d at once" % c["shadow_max_open"],
                     "at %s the weight of a real trade" % {0.5: "half", 1.0: "full"}[c["shadow_weight"]],
                     "Stop loss at %g%% until the first step" % c["np_snipe_stop_pct"],
                     "the momentum strategy's is %g%%" % c["stop_pct"],
                     "the stop moves to break-even (+%g%%)" % c["breakeven_pct"],
                     "(%g%%, %g%% and %g%% under it for swing, runner and moon)" % tuple(
                         tiers[k]["trail_pct"] for k in ("swing", "runner", "moon")),
                     "take the whole profit at +%g%% (scalp, NEWS 0-2)" % tiers["scalp"]["ladder"][0][0],
                     "swing (NEWS %d-4) %s;" % (tiers["swing"]["min_score"], says(tiers["swing"])),
                     "runner (%d-6) %s;" % (tiers["runner"]["min_score"], says(tiers["runner"])),
                     "moon (%d+) %s." % (tiers["moon"]["min_score"], says(tiers["moon"])),
                     "Out after %g minutes unless up %g%% (%g, %g and %g minutes for swing, runner and moon)" % (
                         (tiers["scalp"]["time_stop_min"], c["time_stop_below_pct"]) +
                         tuple(tiers[k]["time_stop_min"] for k in ("swing", "runner", "moon"))),
                     "Out when the curve has had no trade for %g minutes" % c["np_quiet_min"],
                     "one sell of %g SOL or more after the entry" % c["np_dump_sol"],
                     "buys under %.0f%% of the last %d minutes" % (c["fade_buy_ratio"] * 100, c["fade_window_s"] / 60),
                     "pump.fun's fee is about %g%% each way" % c["curve_fee_pct"],
                     "the %g%% latency haircut each way" % c["latency_slip_pct"], "Run around the clock, 24/7"):
            self.assertIn(text, p)

    def test_the_snipe_prompts_and_notes_costs_are_the_engines(self):
        """The engine's own maths (tests/test_levers.py's Book) at a snipe's curve, about 31 SOL of virtual reserves
        (30 + the creator's buy + the first buyers), SOL $71.14: what prompt_snipe.txt and config.json's _snipe say."""
        import test_levers as L
        rows = {}
        for order in (5, 10):
            b = L.Book()
            try:
                pos = b.buy(order, 31.0)
                rows[order] = {"rt": b.sale_pct(pos, b.spot_gain(pos, 31.0)), 10: b.sale_pct(pos, 10.0),
                               -10: b.sale_pct(pos, -10.0)}
            finally:
                b.close()
        with open(mb.PROMPT_SNIPE, encoding="utf-8") as fh:
            p = fh.read()
        with open(mb.CONFIG, encoding="utf-8") as fh:
            note = json.load(fh)["_snipe"]
        ten, five = rows[10], rows[5]
        self.assertIn("a $10 snipe's round trip at an unchanged price costs about %.1f%%" % -ten["rt"], p)
        self.assertIn("+10%% nets about +%.1f%% and the -10%% stop costs about %.1f%%" % (ten[10], ten[-10]), p)
        self.assertIn("about %.1f%% a round trip against %.1f%% for $10, so +10%% nets about +%.1f%% and the -10%% stop "
                      "costs about %.0f%%" % (-five["rt"], -ten["rt"], five[10], five[-10]), p)
        sk = lambda x: ("%.1f" % x).replace(".", ",")                  # noqa: E731
        self.assertIn("a $10 to je asi %s %% po nákladoch, pri $5 asi %s %%" % (sk(ten[-10]), sk(five[-10])), note)
        self.assertIn("asi %s %% tam a späť (pri $10)" % sk(-ten["rt"]), note)
        # a scalp: +10 % against the -10 % stop needs this share of trades to reach +10 % first
        need = -ten[-10] / (ten[10] - ten[-10])
        self.assertEqual(round(need * 10), 7)
        self.assertIn("A scalp needs about 7 trades in 10 to reach +10% before the stop", p)
        self.assertIn("scalp potrebuje asi 7 z 10 obchodov", note)

    def test_config_json_says_the_snipe_keys_with_the_codes_defaults(self):
        with open(mb.CONFIG, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual((raw["np_snipe"], raw["np_snipe_max_age_s"], raw["np_snipe_stop_pct"], raw["np_snipe_fresh_s"]),
                         (True, 10, -10.0, 1.0))
        self.assertEqual(mb.DEFAULTS["np_snipe_fresh_s"], 1.0)
        self.assertEqual((mb.DEFAULTS["np_snipe"], mb.DEFAULTS["np_snipe_max_age_s"], mb.DEFAULTS["np_snipe_stop_pct"]),
                         (False, 10, -10.0))                         # a config without them: the momentum rules
        self.assertEqual((npm.DEFAULTS["np_snipe"], npm.DEFAULTS["np_snipe_max_age_s"]), (False, 10.0))
        self.assertEqual((raw["np_max_holder_pct"], raw["np_max_dev_pct"], raw["stop_pct"]), (70.0, 70.0, -7.0))
        note = raw["_snipe"]
        for k in ("np_snipe", "np_snipe_max_age_s", "np_snipe_stop_pct", "np_max_dev_pct", "np_max_holder_pct",
                  "np_min_age_s", "np_serial_window_min", "np_serial_max", "shadow_on_risk", "stop_pct -7"):
            self.assertIn(k, note)
        for text in ("PRVOM čítaní krivky", "bez čakania na RugCheck", "„fill curve“", "-10 %", "45 s", "6,6 %",
                     "-30 %", "nie fit", "väčšina nových mincí sa nikdy nepohne", "LEARNER v tomto režime nič nepreskočí",
                     "np_snipe_fresh_s", "memory_snipe.json", "kým niečo drží, Jupitera sa nepýta",
                     # the review: without np_snipe the caps are not the old ones, and one holder's real cap is 35 %
                     "bez np_snipe platí momentum vstup ako predtým (stop_pct -7), no s np_max_dev_pct a np_max_holder_pct "
                     "70 (predtým 8 a 12", "skutočný strop jedného držiteľa je tam 35 %", "rozhodnutie majiteľa"):
            self.assertIn(text, note)
        self.assertNotIn("bez np_snipe je všetko ako predtým", note)
        self.assertIn("SNIPE", raw["_about"])
        self.assertIn("prompt_snipe.txt", raw["_about"])
        self.assertEqual((raw["np_max_top10_pct"], raw["np_rug_bad_risks"]), (35.0, ["rugged tokens", "single holder ownership"]))
        self.assertNotIn("len extrémneho držiteľa", raw["_entry"])

    def test_the_momentum_prompt_says_the_real_one_holder_cap(self):
        """The review: with np_max_holder_pct 70 and np_max_top10_pct 35, one holder above 35 % fails the top-10 rule
        first (newpairs.rug_check_np), so prompt.txt says that, not 70 % alone."""
        with open(mb.PROMPT, encoding="utf-8") as fh:
            p = fh.read()
        self.assertIn("no single holder above 70%, the top 10 holders at most 35% (the curve left out; so in practice no "
                      "single holder, the creator included, above 35%", p)
        rep = {"mint": "one", "rugged": False, "risks": [], "totalHolders": 30, "graphInsidersDetected": 0, "markets": [],
               "knownAccounts": {}, "fileMeta": {},
               "topHolders": [{"address": "h0", "owner": "o0", "pct": 50.0, "insider": False}]
               + [{"address": "h%d" % i, "owner": "o%d" % i, "pct": 0.5, "insider": False} for i in range(1, 25)]}
        ok, bad, _ = npm.rug_check_np(rep, npm.Coin(mint="one", curve="cv"), dict(CONFIG, np_min_holders=0,
                                                                              np_max_sol_per_holder=0))
        self.assertFalse(ok)
        self.assertTrue(bad[0].startswith("top 10 hold"), bad)


class Replay(unittest.TestCase):
    """replay.py on a snipe run: the engine's own snipe on the tape's first readings, a run from before np_snipe as
    momentum; backtest.py's default grid for a snipe base has the snipe's levers, not the momentum's."""

    def live(self, minutes, seed, over=None, no_route_s=0.0):
        import test_endtoend as T
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        real = T.Market.quote

        def quote(market, input_mint, output_mint, amount, slippage_bps=300):
            c = market.coins.get(output_mint if input_mint == T.SOL else input_mint)
            if no_route_s and input_mint == T.SOL and c is not None and market.clock.epoch() - c.t0 < no_route_s:
                market.quote_why = "no Jupiter route"                 # a coin this young: Jupiter has no route yet
                return None
            return real(market, input_mint, output_mint, amount, slippage_bps)

        with unittest.mock.patch.object(T.Market, "quote", quote):
            trades, c = T.live_run(d, minutes, seed, "reading", over=over)
        return T, d, trades, c

    def test_a_snipe_run_replays_into_the_same_trades_with_the_live_bots_function(self):
        T, d, live, c = self.live(20, 11, over={"np_snipe": True}, no_route_s=4.0)
        self.assertGreaterEqual(len(live), 8)
        self.assertTrue(all(t["reason"].startswith("snipe · first reading") for t in live))
        self.assertTrue(any(t["reason"].endswith("fill curve") for t in live))
        R = T.R
        tape = R.read_tape(d)
        self.assertTrue(tape.live_start()["cfg"]["np_snipe"])
        buys = [f for _, k, _, f in tape.live if k == "buy"]
        fires = [f for _, k, _, f in tape.live if k == "fire"]
        self.assertGreaterEqual(len(buys), len(live))              # (a coin still held at the end: a buy, no sale)
        self.assertTrue(buys and all(str(f.get("why")).startswith("snipe · first reading") for f in buys))
        self.assertTrue(any(str(f.get("why")).endswith(" · fill curve") for f in buys))
        self.assertTrue(fires and all(str(f.get("why")).startswith("snipe ·") for f in fires))
        chk = R.live_check(tape, T.bot.load_config(T.bot.CONFIG))
        self.assertEqual((chk["live"], chk["replay"], chk["same_entry"], chk["only_live"], chk["only_replay"]),
                         (len(live), len(live), len(live), [], []))
        self.assertEqual(chk["same_exit"], len(live))
        self.assertLessEqual(abs(chk["pnl_pct_gap_max"]), 0.05)
        # the replay's log says it: the snipe, by the same function
        res = R.replay(tape, c, keep_log=True)
        self.assertTrue(any("· snipe · first in" in line for line in res["log"]))
        self.assertEqual(res["stats"]["rug_missing"], 0)

    def test_a_run_from_before_np_snipe_is_replayed_as_momentum_and_one_with_it_as_snipe(self):
        R = mb_replay()
        got = []

        def fake(tape_, c, since, until, cfg_at=None, **kw):
            got.append((c, cfg_at(1500.0)))
            return {"trades": []}

        for line, want in (({"np_min_age_s": 45}, False), ({"np_min_age_s": 45, "np_snipe": True}, True)):
            tape = R.tape_of([{"k": "start", "t": 1000.0, "v": 2, "cfg": line, "bal": 100.0},
                              {"k": "create", "m": "a", "t": 1001.0, "bondingCurveKey": "ck"},
                              {"k": "buy", "m": "a", "t": 1002.0, "usd": 10.0},
                              {"k": "sell", "m": "a", "t": 1003.0, "usd": 9.0, "final": True, "pnl": -1.0,
                               "pnl_pct": -10.0},
                              {"k": "stop", "t": 2000.0}])
            got.clear()
            with unittest.mock.patch.object(R, "replay", fake):
                R.live_check(tape, dict(CONFIG))
            self.assertTrue(got, line)
            for c in got[0]:
                self.assertEqual(bool(c["np_snipe"]), want, line)

    def test_the_backtests_default_grid_for_a_snipe_base_has_the_snipes_levers(self):
        bt = mb_backtest()
        snipe = bt.build_grid("default", dict(CONFIG), 400)
        keys = {k for lv in snipe for k in lv}
        self.assertTrue({"np_snipe_stop_pct", "np_snipe_max_age_s"} <= keys)
        self.assertFalse(keys & set(bt.SNIPE_IDLE))
        momentum = bt.build_grid("default", dict(CONFIG, np_snipe=False), 400)
        mkeys = {k for lv in momentum for k in lv}
        self.assertFalse(mkeys & {"np_snipe_stop_pct", "np_snipe_max_age_s"})
        self.assertTrue({"np_min_sol", "stop_pct", "np_min_holders"} <= mkeys)
        self.assertEqual(bt.default_grid(dict(CONFIG, np_snipe=False)), bt.DEFAULT_GRID)

    def test_a_snipe_base_backtest_says_its_mode_and_the_levers_a_snipe_never_reads(self):
        import test_backtest as TB
        bt = mb_backtest()

        def fake(tape, tasks, jobs=None, budget_s=None, progress=None):
            return {key: {"trades": [], "metrics": bt.replay.metrics([], 100.0), "memory": None, "stats": {}}
                    for key, cfg_, since, until, mem, opts in tasks}

        with unittest.mock.patch.object(bt, "run_many", fake):
            rep = bt.backtest(cfg=dict(CONFIG), tape=TB.KeepRule.Tape(), jobs=1, live_check=False,
                              grid={"combos": [{"stop_pct": -10.0}, {"np_min_age_s": 30}, {"np_snipe_stop_pct": -15.0}]})
            mom = bt.backtest(cfg=dict(CONFIG, np_snipe=False), tape=TB.KeepRule.Tape(), jobs=1, live_check=False,
                              grid={"combos": [{"stop_pct": -10.0}]})
        self.assertTrue(rep["settings"]["mode"].startswith("snipe: the base config buys each coin on its first reading"))
        self.assertEqual(rep["settings"]["idle_levers"], ["np_min_age_s", "stop_pct"])
        self.assertIn("levers in this grid a snipe never reads (each one replays the base under another name): "
                      "np_min_age_s, stop_pct", rep["settings"]["mode"])
        self.assertIn(rep["settings"]["mode"], rep["lines"])
        self.assertFalse({"mode", "idle_levers"} & set(mom["settings"]))
        self.assertFalse([x for x in mom["lines"] if x.startswith("snipe:")])


class Fresh(Base):
    """The review's findings on stale fills: a curve fill prices the reading the bot holds, so it is taken only on a
    fresh one (np_snipe_fresh_s), never with a newer reading of the coin already in, and the snipe's age cap
    (np_snipe_max_age_s) is checked when the bot decides, not on the reading's own clock. A quote fill is priced by
    Jupiter at the decision (the fake: the latest reading handed out), so it needs no such guard."""

    def ev(self, mint, rsol, t):
        self.slot += 1
        e = E.reading(mint, rsol, self.slot)
        e["t"] = t
        return e

    def test_a_newer_reading_in_the_same_turn_is_never_a_curve_fill_at_the_older_price(self):
        n = self.night(cfg())
        self.m.no_route.add("q")
        self.turn(n, E.create("q", "Q"), sec=0.5)
        t0 = n.epoch()
        self.turn(n, self.ev("q", 0.8, t0 + 1.0), self.ev("q", 6.0, t0 + 1.5), sec=1.5)
        self.assertNotIn("q", n.state["positions"])
        self.assertEqual(n.state["night"]["trades"], [])               # no +27 % booked on a price never seen
        self.assertEqual(n.wl.get("q").status, "rejected: snipe not bought: a newer reading of it came in the same "
                                               "turn (the curve maths would fill an old price)")
        self.assertNotIn("q", n.shadows)
        # with a Jupiter route the same turn is a quote fill at the chain's state then: the newer reading's
        self.turn(n, E.create("r", "R"), sec=0.5)
        t1 = n.epoch()
        self.turn(n, self.ev("r", 0.8, t1 + 1.0), self.ev("r", 6.0, t1 + 1.5), sec=1.5)
        pos = n.state["positions"]["r"]
        self.assertEqual(pos["entry_fill"], "quote")
        self.assertGreater(pos["entry_price"], 36.0 ** 2 / E.K * self.m.sol)     # over the newer reading's price
        self.assertEqual(n.state["night"]["trades"], [])

    def test_a_jupiter_timeout_leaves_the_reading_too_old_for_a_curve_fill(self):
        n = self.night(cfg())
        real, clock = self.m.quote, self.clock

        def slow(input_mint, output_mint, amount, slippage_bps=300, wait=1.5, why="Jupiter timeout"):
            if input_mint == mb.SOL and output_mint in ("slow", "fast"):
                clock.advance(1.5 if output_mint == "slow" else 0.3)
                self.m.quote_why = why if output_mint == "slow" else "no Jupiter route"
                return None
            return real(input_mint, output_mint, amount, slippage_bps)

        self.m.quote = slow
        coin = self.first(n, "slow", 0.8)
        self.assertNotIn("slow", n.state["positions"])
        self.assertEqual(coin.status, "rejected: snipe not bought: its reading is 1.5 s old (a curve fill takes one "
                                      "at most 1 s old: np_snipe_fresh_s)")
        # a quick no-route answer leaves it fresh: the curve maths fills it, the ENTER line says how long Jupiter took
        coin = self.first(n, "fast", 0.8)
        pos = n.state["positions"]["fast"]
        self.assertEqual((pos["entry_fill"], pos["fill_why"]), ("curve", "Jupiter: no Jupiter route · 0.3 s"))
        self.assertIn("0.3 s after the first reading", self.lines(n, "EXECUTOR", "enter")[-1])
        self.assertIn("fill curve (Jupiter: no Jupiter route · 0.3 s)", self.lines(n, "EXECUTOR", "enter")[-1])

    def test_the_age_cap_is_checked_when_the_bot_decides(self):
        n = self.night(cfg())
        for mint, route in (("old", False), ("oldq", True)):
            if not route:
                self.m.no_route.add(mint)
            self.turn(n, E.create(mint, mint.upper()), sec=0.5)
            t0 = n.epoch()
            self.turn(n, self.ev(mint, 0.8, t0 + 1.0), sec=20.0)       # read at 1 s, handled 20 s after the create
            self.assertNotIn(mint, n.state["positions"])
            self.assertEqual(n.wl.get(mint).status, "rejected: snipe not bought: decided 20.0 s after the create "
                                                    "(max 10 s)")
            self.assertNotIn(mint, n.shadows)
        self.assertEqual([q for q in self.m.quotes if q[0] == mb.SOL], [])     # never asked

    def test_a_slow_jupiter_never_curve_fills_the_coins_behind_it_on_their_old_readings(self):
        """The review's b_stale_fill.py: c1 (a route) and c2 (none) read in one poll round, Jupiter 1.4 s a quote; c2's
        +40 % reading comes 0.5 s later. c1 is bought by its quote; c2's reading is 2.8 s old when its turn comes."""
        n = self.night(cfg())
        real = self.m.quote

        def slow(*a, **k):
            self.clock.advance(1.4)
            return real(*a, **k)

        self.m.quote = slow
        self.m.no_route.add("c2")
        for m in ("c1", "c2"):
            self.turn(n, E.create(m, m.upper()), sec=0.0)
        base = (self.clock.now() - E.T0).total_seconds()
        T = E.Clock().epoch()
        self.feeds.at(base + 1.0, self.ev("c1", 0.8, T + base + 1.0))       # each one's t: when it was read
        self.feeds.at(base + 1.0, self.ev("c2", 0.8, T + base + 1.0))
        self.feeds.at(base + 1.5, self.ev("c2", 13.6, T + base + 1.5))
        for _ in range(10):
            n.now = self.clock.now()
            n.np_events(0.25, 400)
        self.assertEqual(n.state["positions"]["c1"]["entry_fill"], "quote")
        self.assertNotIn("c2", n.state["positions"])
        self.assertTrue(n.wl.get("c2").status.startswith("rejected: snipe not bought: its reading is 2.8 s old"),
                        n.wl.get("c2").status)
        self.assertEqual(n.state["night"]["trades"], [])

    def test_the_shadow_of_a_risk_blocked_snipe_has_the_same_guards(self):
        n = self.night(cfg(max_open=1))
        self.first(n, "a", 0.8)
        self.turn(n, E.create("b", "B"), sec=0.5)
        t0 = n.epoch()
        self.turn(n, self.ev("b", 0.8, t0 + 1.0), self.ev("b", 6.0, t0 + 1.5), sec=1.5)
        self.assertEqual(n.wl.get("b").status, "rejected: snipe not bought: 1 positions open (max)")
        self.assertNotIn("b", n.shadows)                                # no RISK shadow on the old price either
        self.turn(n, E.create("b2", "B2"), sec=0.5)
        self.turn(n, self.ev("b2", 0.8, n.epoch() + 1.0), sec=20.0)      # decided 20 s after the create
        self.assertEqual(n.wl.get("b2").status, "rejected: snipe not bought: decided 20.0 s after the create (max 10 s)")
        self.assertNotIn("b2", n.shadows)
        c = self.first(n, "c", 0.8)                                     # a fresh one: RISK blocks it, shadowed
        self.assertEqual(c.status, "rejected: snipe not bought: 1 positions open (max)")
        self.assertIn("c", n.shadows)


class HeldFirst(Base):
    def test_while_a_coin_is_held_a_snipe_asks_jupiter_nothing_and_the_held_stop_waits_for_nothing(self):
        """The review's a2_exit_latency.py: a held coin's -12 % reading 0.2 s after a new coin's first reading, Jupiter
        1.4 s a quote. The snipe is filled by the curve maths at once (no quote), so the stop goes out after its own
        sale quote only (1.4 s), not 4.0 s."""
        n = self.night(cfg())
        self.first(n, "h", 3.0)
        self.assertEqual(n.state["positions"]["h"]["entry_fill"], "quote")      # nothing held then: Jupiter quotes
        self.assertIn("fill quote · Jupiter 0.0 s · curve maths 0.0% off Jupiter", self.lines(n, "EXECUTOR", "enter")[0])
        real = self.m.quote

        def slow(*a, **k):
            self.clock.advance(1.4)
            return real(*a, **k)

        self.m.quote = slow
        before = len(self.m.quotes)
        self.turn(n, E.create("nw", "NW"), sec=0.5)
        base = (self.clock.now() - E.T0).total_seconds()
        T = E.Clock().epoch()
        self.slot += 1
        e1 = E.reading("nw", 0.8, self.slot)
        e1["t"] = T + base + 1.0
        self.slot += 1
        e2 = E.reading("h", self.rsol(n.state["positions"]["h"], -12.0), self.slot)
        e2["t"] = T + base + 1.2
        self.feeds.at(base + 1.0, e1)
        self.feeds.at(base + 1.2, e2)
        for _ in range(8):
            n.now = self.clock.now()
            n.np_events(0.25, 400)
        pos = n.state["positions"]["nw"]
        self.assertEqual((pos["entry_fill"], pos["fill_why"]), ("curve", "Jupiter not asked: 1 coin held, its exits first"))
        self.assertEqual([q for q in self.m.quotes[before:] if q[0] == mb.SOL], [])     # only the held coin's sale
        out = [t for t in self.lines(n, "EXECUTOR", "exit") if t.startswith("EXIT H ")]
        self.assertTrue(out and out[0].endswith("out · 1.4 s after the reading"), out)
        self.assertIn("fill curve (Jupiter not asked: 1 coin held, its exits first)", self.lines(n, "EXECUTOR", "enter")[-1])


class StaleSale(Base):
    def test_a_curve_filled_snipe_is_never_sold_on_stale_reserves_but_at_the_jobs_end(self):
        n = self.night(cfg())
        self.m.no_route.add("q")
        self.first(n, "q", 3.0)
        self.assertEqual(n.state["positions"]["q"]["entry_fill"], "curve")
        for _ in range(185):                                            # the poller goes dark for 3 minutes
            self.tick(n, 1)
        self.assertIn("q", n.state["positions"])                        # the quiet exit wants it out: no price
        self.assertIn("can't sell (no Jupiter route): no curve reading for 3 min · retrying",
                      self.lines(n, "EXECUTOR", "stuck"))
        self.assertEqual(n.state["night"]["trades"], [])
        self.to_gain(n, "q", -11.0)                                     # a fresh reading: sold by the maths on it
        for _ in range(5):                                              # (after the failed sale's 5 s wait)
            self.tick(n, 1)
        t = n.state["night"]["trades"][-1]
        self.assertEqual((t["mint"], t["why"], t["fill"]), ("q", "stop -11%", "curve"))
        # at a job's end the last reserves sell it all the same, and the line says they are stale
        self.m.no_route.add("z")
        self.first(n, "z", 3.0)
        for _ in range(10):
            self.tick(n, 1)
        self.assertIn("z", n.state["positions"])
        n.job_left = 0.5
        self.tick(n, 1)
        self.assertNotIn("z", n.state["positions"])
        ex = self.lines(n, "EXECUTOR", "exit")[-1]
        self.assertIn("the job ends", ex)
        self.assertIn("by the curve maths on stale reserves (", ex)

    def test_the_unchecked_curve_maths_never_sells_a_quote_filled_position(self):
        """The review's mutant M31: only a CURVE-filled snipe is sold by the unchecked maths; a position bought on a quote
        whose curve maths was never checked against it ({} or all None) waits for Jupiter."""
        n = self.night(cfg())
        for mint, chk in (("e1", {}), ("e2", {"buy_pct": None, "sell_pct": None}), ("e3", None)):
            self.first(n, mint, 3.0)
            pos = n.state["positions"][mint]
            self.assertEqual(pos["entry_fill"], "quote" if mint == "e1" else "curve")
            pos["entry_fill"], pos["curve_check"] = "quote", chk
            self.m.no_route.add(mint)
            self.to_gain(n, mint, -11.0)
            self.assertIn(mint, n.state["positions"], mint)
        self.assertEqual(len([t for t in self.lines(n, "EXECUTOR", "stuck") if t.startswith("can't sell")]), 3)


class RecordsAndTape(Base):
    def tape(self, n, mint=None):
        self.assertTrue(n.rec.drain(5.0))
        return [x for _, p in mb.recorder.tape_files(os.path.join(self.dir, "tapes")) for x in mb.recorder.read_lines(p)
                if mint is None or x.get("m") == mint]

    def test_a_snipes_trade_record_says_so_and_its_fill_is_the_sales(self):
        n = self.night(cfg())
        self.m.no_route.add("fk")
        self.first(n, "fk", 3.0)
        pos = n.state["positions"]["fk"]
        self.assertEqual((pos["entry_fill"], pos.get("fill"), pos["curve_check"]), ("curve", None, None))
        self.m.no_route.discard("fk")                                   # Jupiter routes it by the exit
        self.to_gain(n, "fk", 12.0)
        t = n.state["night"]["trades"][-1]
        self.assertNotIn("curve maths", self.lines(n, "EXECUTOR", "exit")[-1])
        self.assertNotIn("fill", t)                                     # no sale went by the curve maths
        self.assertEqual((t["snipe"], t["entry_fill"], t["stop_pct"]), (True, "curve", -10.0))
        R = mb_replay()
        self.assertTrue({"snipe", "entry_fill", "stop_pct"} <= set(R.TRADE_KEYS))
        rows = [x for x in self.tape(n, "fk") if x["k"] in ("fire", "buy")]
        self.assertEqual([(x["k"], x.get("entry")) for x in rows], [("fire", "snipe"), ("buy", "snipe")])
        self.assertEqual((rows[1]["fill"], rows[1]["check"]), ("curve", None))

    def test_a_quote_snipes_buy_line_and_a_risk_shadows_open_line_are_marked(self):
        n = self.night(cfg(max_open=1))
        self.first(n, "qq", 0.8)
        self.first(n, "sh", 0.8)                                        # RISK: 1 open (max) -> a snipe shadow
        rows = self.tape(n)
        buy = [x for x in rows if x["k"] == "buy" and x["m"] == "qq"][0]
        self.assertEqual((buy["entry"], buy["fill"], buy["check"]["buy_pct"]), ("snipe", "quote", 0.0))
        opened = [x for x in rows if x["k"] == "shadow" and x["m"] == "sh"][0]
        self.assertEqual((opened["act"], opened["kind"], opened["entry"], opened["snipe"], opened["stop_pct"]),
                         ("open", "risk", "snipe", True, -10.0))

    def test_momentum_lines_and_trades_carry_no_snipe_marker(self):
        n = self.night(S.cfg(np_snipe=False))
        self.launch(n, "uzim", E.RUNNER, social=(True, True, True))
        self.to_gain(n, "uzim", -9.0)
        t = n.state["night"]["trades"][-1]
        self.assertFalse({"snipe", "entry_fill", "stop_pct"} & set(t))
        rows = [x for x in self.tape(n, "uzim") if x["k"] in ("fire", "buy")]
        self.assertEqual([x["k"] for x in rows], ["fire", "buy"])
        self.assertFalse([x for x in rows if "entry" in x])

    def test_rugcheck_is_asked_for_every_snipe_and_snipe_shadow_once_25_s_after_its_launch(self):
        n = self.night(cfg(max_open=2))
        self.first(n, "fast", 3.0)
        self.to_gain(n, "fast", -11.0)                                  # stopped out 2 s after its launch
        self.assertNotIn("fast", n.state["positions"])
        self.first(n, "held", 3.0)
        self.first(n, "two", 3.0)
        self.first(n, "shd", 3.0)                                       # RISK: 2 open -> a shadow
        self.assertIn("shd", n.shadows)
        for _ in range(30):
            self.tick(n, 1)
        self.assertTrue(E.mb_until(lambda: len(self.m.rug_calls) >= 4, 2.0))
        self.assertEqual(sorted(self.m.rug_calls), ["fast", "held", "shd", "two"])
        for _ in range(30):
            self.tick(n, 1)
        self.assertEqual(len(self.m.rug_calls), 4)                      # once each
        E.mb_until(lambda: all(j[0].done() for j in list(n.np_jobs.values())), 2.0)
        self.turn(n, sec=0.1)                                           # the loop takes the reports (np_reports)
        rugs = [x for x in self.tape(n) if x["k"] == "rug"]
        self.assertEqual(sorted((x["m"], x["why"]) for x in rugs), [("fast", "snipe"), ("held", "snipe"),
                                                                     ("shd", "snipe"), ("two", "snipe")])

    def test_the_rug_check_counters_count_the_snipes(self):
        n = self.night(cfg())
        self.m.tax["pot"] = 0.5
        self.first(n, "pot", 0.8)                                       # the sell simulation fails: filtered
        self.first(n, "ok1", 0.8)
        self.first(n, "ok2", 0.8)
        night = n.state["night"]
        self.assertEqual((night["checked"], night["filtered"], night["last_rug"]), (3, 1, {"coin": "OK2", "pass": True}))

    def test_a_held_snipes_links_come_from_rugcheck_when_its_metadata_failed(self):
        n = self.night(cfg())
        self.m.reports["mm"] = E.rug_report("mm", file_meta={"name": "MM", "twitter": "https://x.com/mm"})
        self.first(n, "mm", 0.8)
        self.assertEqual(n.wl.get("mm").status, "entered")
        self.m.rug_gate = threading.Event()                             # RugCheck answers after the next 1 s step
        self.addCleanup(self.m.rug_gate.set)
        self.turn(n, E.meta("mm", ok=False), sec=0.3)                   # its uri failed after the buy
        self.assertIn("mm", n.np_meta_rug)
        self.tick(n, 1)
        self.assertIn("mm", n.np_meta_rug)                              # held: still asking (it was dropped)
        self.m.rug_gate.set()
        self.assertTrue(E.mb_until(lambda: all(j[0].done() for j in list(n.np_jobs.values())), 2.0))
        self.turn(n, sec=0.1)
        coin = n.wl.get("mm")
        self.assertIsInstance(coin.meta, dict)
        self.assertTrue(coin.meta.get("x"))
        self.assertNotIn("mm", n.np_meta_rug)


class LearnerApart(Base):
    def test_snipe_results_have_their_own_memory_and_never_change_a_momentum_decision(self):
        n = self.night(cfg(loss_streak=100))
        for i in range(10):
            self.first(n, "l%d" % i, 3.0)
            self.to_gain(n, "l%d" % i, -11.0)
            self.tick(n, 1)
        self.assertEqual(len(n.state["night"]["trades"]), 10)
        self.assertEqual((n.mem["samples"], n.mem["labels"]), (0, {}))          # memory.json: the momentum entry's
        self.assertEqual(n.mem_snipe["samples"], 10)
        self.assertEqual(lr.load(os.path.join(self.dir, "memory_snipe.json"))["samples"], 10)
        self.assertEqual(lr.load(os.path.join(self.dir, "memory.json"))["samples"], 0)
        s = n.state["learner_summary"]
        self.assertEqual((s["memory"], s["samples"]), ("snipe", 10))
        self.assertTrue(s["worst"] and all(r["label"].startswith("snipe · ") for r in s["best"] + s["worst"]), s)
        n.save()
        # the same data dir with np_snipe off: the momentum LEARNER never saw a snipe (memory.json, and the backfill
        # of a new memory.json from state.json's trades leaves the snipes out)
        try:
            os.remove(os.path.join(self.dir, "memory.json"))
        except OSError:
            pass
        m = mb.Night(self.dir, S.cfg(np_snipe=False), self.m, self.clock.now(), self.out.append)
        m.clock_fn = self.clock.now
        m.begin("2026-11-25")
        m.attach(self.feeds)
        self.addCleanup(m.np_close)
        self.assertEqual(m.mem["samples"], 0)
        self.assertEqual(m.mem_snipe["samples"], 10)
        self.assertNotIn("memory", m.state["learner_summary"])
        self.launch(m, "mom", E.SCALP)
        self.assertIn("mom", m.state["positions"])
        self.assertFalse([t for t in self.lines(m, "LEARNER") if t.startswith(("skip", "explore"))])

    def test_a_risk_shadow_of_a_snipe_teaches_the_snipe_memory(self):
        n = self.night(cfg(max_open=1))
        self.first(n, "a", 3.0)
        self.first(n, "b", 3.0)
        self.assertIn("b", n.shadows)
        self.to_gain(n, "b", -11.0)
        self.assertNotIn("b", n.shadows)
        self.assertEqual((n.mem_snipe.get("shadows"), n.mem.get("shadows")), (1, None))


class ShadowPins(Base):
    def test_only_a_risk_block_makes_a_snipe_shadow_and_shadow_on_risk_off_makes_none(self):
        n = self.night(cfg())
        self.m.tax["pot"] = 0.5
        self.first(n, "pot", 0.8)
        self.assertNotIn("pot", n.shadows)                              # a sell-simulation reject: none
        n.state["balance"] = n.state["night"]["start_equity"] = 45.0
        self.first(n, "e", 0.8)
        self.assertNotIn("e", n.shadows)                                # the SIZER's cash skip: none
        m = self.night(cfg(max_open=1, shadow_on_risk=False), folder=tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, m.dir, True)
        self.first(m, "x1", 0.8)
        self.assertEqual(self.first(m, "x2", 0.8).status, "rejected: snipe not bought: 1 positions open (max)")
        self.assertEqual(m.shadows, {})


_MODS = {}


def mb_backtest():
    if "bt" not in _MODS:
        from _load import load
        _MODS["bt"] = load("memebot", "backtest")
    return _MODS["bt"]


def mb_replay():
    return mb_backtest().replay


if __name__ == "__main__":
    unittest.main()
