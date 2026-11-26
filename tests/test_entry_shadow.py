"""ENTRY QUALITY and SHADOW TRADES together in bot.py's New Pairs engine (offline: tests/test_newpairs_engine.py's
FakeFeeds and CurveMarket, tests/test_shadow.py's harness). The entry rules (newpairs.py: RugCheck must SEE enough
wallets holding the coin, at least np_min_holders and one for every np_max_sol_per_holder SOL that came in) are about
the coin: a coin they reject or keep waiting is never bought and never followed as a SHADOW, whatever the LEARNER or
RISK say. A RugCheck report that leaves a coin waiting is asked for again after np_rug_recheck_s (not reused for
np_rug_cache_s), and the answer is judged on the turn it lands. The LEARNER
keeps the new labels (learner.KNOWN), a shadow teaches them, and a live check replays a run recorded before these
rules without them. Paper on paper: these tests say the code does what it says, never that it makes money."""

import os
import threading
import types
import unittest
import unittest.mock

import test_newpairs_engine as E
import test_shadow as S

os.environ["MEMEBOT_LIVE"] = "0"
os.environ["MEMEBOT_FEEDS"] = "0"
mb = E.mb
npm = mb.newpairs
lr = mb.learner

# a crowd: 11 buy readings of 0.3 SOL (3.3 SOL in): under np_max_sol_per_holder's 0.5 a holder from 7 holders on
CROWD = [round(0.3 + 0.3 * k, 2) for k in range(1, 12)]
# a few big buys: 8 buy readings of 1 SOL (STOREB's kind of coin, only bigger): 8 SOL in, 16 holders needed
BIG = [1.3, 2.3, 3.3, 4.3, 5.3, 6.3, 7.3, 8.3]


def cfg(**kw):
    """tests/test_shadow.py's settings with today's entry-quality rules on (config.json's values), RugCheck asked at
    the fire only (no prefetch) and the 30 s cache of config.json."""
    c = S.cfg(np_min_holders=10, np_max_sol_per_holder=0.5, np_max_buy_sol=0.0, np_rug_recheck_s=4.0, np_rug_cache_s=30,
              np_rug_prefetch=False)
    c.update(kw)
    return c


def seen(mint, holders, top=(3.0, 2.0, 1.5), empty=0):
    """A clean RugCheck report whose list shows `holders` wallets holding the coin (the top ones' %, the rest at 0.4 %)
    and `empty` accounts of wallets that sold out, the curve's account too: totalHolders counts them all, as the real
    API does when it lists every account (fewer than 20)."""
    top = list(top)[:holders] + [0.4] * max(0, holders - len(top))
    r = E.rug_report(mint, top=top)
    r["topHolders"] += [{"address": "e%d" % i, "owner": "eo%d" % i, "pct": 0, "amount": 0, "insider": False}
                        for i in range(empty)]
    r["totalHolders"] = len(r["topHolders"])
    return r


class Recheck(S.Base):
    def walk(self, n, mint, path, report, sec=1.0):
        """A create, its links, its readings until the trigger fires (the coin then has a RugCheck verdict)."""
        self.m.reports[mint] = report
        self.turn(n, E.create(mint, mint.upper()))
        self.turn(n, E.meta(mint, False, True, False), sec=0.3)
        for rsol in path:
            self.read(n, mint, rsol, sec=sec)
            coin = n.wl.get(mint)
            if mint in n.state["positions"] or mint in n.shadows or coin.status != "watching" or npm.rug_waiting(coin):
                return coin
        return n.wl.get(mint)

    def test_a_report_that_sees_too_few_holders_is_asked_again_after_the_recheck_not_the_30_s_cache(self):
        n = self.night(cfg())
        coin = self.walk(n, "slow", CROWD, seen("slow", 3, (3.0,)))
        self.read(n, "slow", CROWD[-1] + 0.3)                           # the report is in: judged on it
        self.assertTrue(npm.rug_waiting(coin), coin.rug)
        self.assertEqual((coin.status, n.state["positions"], n.shadows), ("watching", {}, {}))
        waits = self.lines(n, "RUG CHECK", "wait")
        self.assertEqual(waits, ["waiting: RugCheck sees 3 holders (min 10) · no entry until RugCheck sees enough: asked "
                                 "again every 4 s while the trigger holds"])
        self.assertEqual(self.lines(n, "RUG CHECK", "pass"), [])
        self.assertEqual(n.state["night"].get("checked", 0), 0)        # no verdict yet: not a check of the night
        first = len(self.m.rug_calls)
        self.m.reports["slow"] = seen("slow", 14)                       # RugCheck's index caught up
        r, bought = CROWD[-1] + 0.3, None
        for k in range(12):
            r += 0.3                                                    # the crowd goes on: 0.3 SOL a buy reading
            self.read(n, "slow", r, sec=0.5)
            if "slow" in n.state["positions"]:
                bought = 0.5 * (k + 1)
                break
        self.assertIsNotNone(bought)                                    # with the 30 s cache: not before 30 s
        self.assertLessEqual(bought, 5.0)
        self.assertLessEqual(len(self.m.rug_calls) - first, 2)          # one call a recheck, not one a reading
        self.assertEqual(len(self.lines(n, "SCANNER", "flag")), 1)      # flagged once, wait or not
        self.assertEqual(n.state["night"]["flagged"], 1)
        pos = n.state["positions"]["slow"]
        self.assertEqual(pos["holders"], 14)
        self.assertIn("np:holders:10-20", pos["learn"])
        self.assertIn("np:solh:.25-.5", pos["learn"])                  # 3.9-4.8 SOL in over 14 wallets
        self.assertEqual(len(self.lines(n, "RUG CHECK", "wait")), 1)    # said once
        self.assertEqual(len(self.lines(n, "RUG CHECK", "pass")), 1)

    def test_a_waiting_coin_is_flagged_once_while_its_trigger_lapses_and_holds_again(self):
        """The post-fix review's finding: the SCANNER flag (the page's line, the night's "flagged" count, the tape's
        "fire" line) was said once a TRIGGER, and a waiting coin's trigger lapses and holds again whenever its SOL dips
        under np_min_sol and comes back (each a new trigger_t): one more flag line and one more flagged coin each time
        (30 in 60 s for one coin). A coin waiting for RugCheck to see its holders is flagged once in all."""
        n = self.night(cfg())
        coin = self.walk(n, "chop", CROWD, seen("chop", 3, (3.0,)))
        r = CROWD[-1] + 0.3
        self.read(n, "chop", r)
        self.assertTrue(npm.rug_waiting(coin), coin.rug)
        fires, holds = n.np_stats["fired"], 0
        for k in range(12):
            r += -0.8 if k % 2 == 0 else 0.8                            # under np_min_sol (3), then over it again
            self.read(n, "chop", r)
            holds += coin.trigger_t is not None
        self.assertTrue(npm.rug_waiting(coin), coin.rug)
        self.assertEqual((coin.status, n.state["positions"]), ("watching", {}))
        self.assertGreaterEqual(holds, 5)                               # the trigger held again, and again
        self.assertEqual(len(self.lines(n, "SCANNER", "flag")), 1)
        self.assertEqual(n.state["night"]["flagged"], 1)
        self.assertEqual(n.np_stats["fired"], fires)
        self.assertEqual(len(self.lines(n, "RUG CHECK", "wait")), 1)
        self.m.reports["chop"] = seen("chop", 14)                       # RugCheck sees its holders: bought, no new flag
        for _ in range(8):
            r += 0.3
            self.read(n, "chop", r)
            if "chop" in n.state["positions"]:
                break
        self.assertIn("chop", n.state["positions"])
        self.assertEqual(len(self.lines(n, "SCANNER", "flag")), 1)

    def test_a_list_that_shows_empty_accounts_is_not_counted_past_it(self):
        """The post-fix review's finding, in the engine: RugCheck lists the curve's account, 3 wallets holding and 16
        that sold out (its 20 biggest, the empty ones last) of 30 accounts. The 10 it does not list are empty too:
        3 wallets, a wait, never "PASS · 13 holders" and a buy."""
        rep = seen("few", 3, empty=16)
        rep["totalHolders"] = 30
        n = self.night(cfg())
        coin = self.walk(n, "few", CROWD, rep)
        self.read(n, "few", CROWD[-1] + 0.3)
        self.assertEqual((n.state["positions"], n.shadows), ({}, {}))
        self.assertTrue(npm.rug_waiting(coin), coin.rug)
        self.assertEqual(coin.rug["holders"], 3)
        self.assertEqual(self.lines(n, "RUG CHECK", "pass"), [])
        self.assertEqual(self.lines(n, "RUG CHECK", "wait"), [
            "waiting: RugCheck sees 3 holders (min 10) · no entry until RugCheck sees enough: asked again every 4 s "
            "while the trigger holds"])

    def test_a_recheck_that_lands_between_two_readings_buys_on_that_turn(self):
        """np_reports() judges a waiting coin again when its fresh report lands, even past np_rug_timeout_s after the
        fire (the wait is RugCheck's index, not a late answer): the buy does not wait for the curve's next reading."""
        n = self.night(cfg())
        coin = self.walk(n, "slow", CROWD, seen("slow", 3, (3.0,)))
        self.read(n, "slow", CROWD[-1] + 0.3)
        self.assertTrue(npm.rug_waiting(coin))
        self.m.rug_gate = threading.Event()                             # the next RugCheck call hangs until set
        r = CROWD[-1] + 0.3
        for _ in range(8):
            r += 0.1
            self.read(n, "slow", r)
            if "slow" in n.np_jobs:
                break
        self.assertIn("slow", n.np_jobs)                                # the recheck is on its way
        self.assertGreater(n.epoch() - n.np_fired["slow"], mb.f(n.cfg["np_rug_timeout_s"]))
        self.m.reports["slow"] = seen("slow", 14)
        self.m.rug_gate.set()
        self.turn(n, sec=0.4)                                           # no reading: only RugCheck's answer
        self.assertIn("slow", n.state["positions"])
        self.assertEqual(n.state["positions"]["slow"]["holders"], 14)

    def test_a_waiting_coin_is_no_shadow_until_rugcheck_sees_its_holders(self):
        """RISK blocks every entry (max_open 0): the shadow needs a PASS like a real entry, so a coin RugCheck sees 3
        holders of is not followed; once it sees 14 the RISK shadow opens, with the new labels, and teaches them."""
        n = self.night(cfg(max_open=0))
        coin = self.walk(n, "thin", CROWD, seen("thin", 3, (3.0,)))
        self.read(n, "thin", CROWD[-1] + 0.3)
        self.assertTrue(npm.rug_waiting(coin))
        self.assertEqual(n.shadows, {})
        self.assertEqual(self.lines(n, "RUG CHECK"), [])               # the RISK path is quiet
        self.m.reports["thin"] = seen("thin", 14)
        r = CROWD[-1] + 0.3
        for _ in range(10):
            r += 0.3
            self.read(n, "thin", r, sec=0.5)
            if n.shadows:
                break
        sh = n.shadows["thin"]
        self.assertEqual((sh["shadow"], sh["shadow_why"]), ("risk", "0 positions open (max)"))
        self.assertIn("np:holders:10-20", sh["learn"])
        self.assertIn("np:solh:.25-.5", sh["learn"])
        self.read(n, "thin", self.rsol(sh, -9))                         # its stop
        self.assertEqual(n.shadows, {})
        for lab in ("np:holders:10-20", "np:solh:.25-.5"):
            self.assertAlmostEqual(n.mem["labels"][lab]["n"], 0.5)      # learned, at half a trade's weight
        self.assertEqual((n.state["balance"], n.state["night"]["trades"]), (100.0, []))


class Concentrated(S.Base):
    """8 SOL in from a few wallets: RugCheck sees 14 holders, 16 are needed at 0.5 SOL a holder. Judged on RugCheck's
    wallets at the fire (never on curve readings, whose count is the poll rate's): the coin waits, is asked about at
    its fire and again every np_rug_recheck_s while its trigger holds (those reports are on the tape: a looser value
    can be judged by the backtest), is never bought and never a SHADOW while it waits, and passes once RugCheck sees
    enough wallets."""

    def big(self, n, mint="conc", report=None, **kw):
        self.launch(n, mint, path=BIG, report=report or seen(mint, 14), **kw)
        return n.wl.get(mint)

    def test_a_few_big_wallets_wait_no_buy_and_no_shadow_when_the_learner_skips(self):
        n = self.night(cfg())
        self.skip_tg(n)                                                 # the LEARNER would skip it: a shadow, if anything
        coin = self.big(n)
        self.read(n, "conc", BIG[-1] + 0.2, sec=0.5)                    # RugCheck's answer is in
        self.assertTrue(npm.rug_waiting(coin), coin.rug)
        self.assertEqual(coin.status, "watching")                       # a wait, not a reject: more wallets may come
        self.assertEqual((n.state["positions"], n.shadows), ({}, {}))
        self.assertEqual(self.lines(n, "RUG CHECK", "wait"), [
            "waiting: 14 holders for 8.0 SOL in (0.57 SOL a holder, max 0.5) · no entry until RugCheck sees enough: "
            "asked again every 4 s while the trigger holds"])
        self.assertEqual(self.m.rug_calls, ["conc"])                    # asked at its fire: the report is on the tape
        self.assertEqual(self.lines(n, "LEARNER", "shadow"), [])
        self.assertEqual(self.lines(n, "SCANNER", "reject"), [])
        r = BIG[-1] + 0.2
        for _ in range(9):                                              # 4.5 s more of the trigger: asked again
            r += 0.05
            self.read(n, "conc", r, sec=0.5)
        self.assertGreaterEqual(len(self.m.rug_calls), 2)
        self.assertEqual((n.state["positions"], n.shadows), ({}, {}))
        self.m.reports["conc"] = seen("conc", 20)                       # 8.5 SOL over 20 wallets: 0.43 a holder
        for _ in range(10):
            r += 0.05
            self.read(n, "conc", r, sec=0.5)
            if n.shadows:
                break
        self.assertEqual(n.shadows["conc"]["shadow"], "learner")        # PASS, then the LEARNER's skip: a shadow now
        self.assertIn("np:solh:.25-.5", n.shadows["conc"]["learn"])
        self.assertIn("np:holders:20-40", n.shadows["conc"]["learn"])

    def test_its_waits_are_on_the_tape_for_the_backtest(self):
        """The adversarial review's finding: a coin the old rule REJECTED at its trigger (before RugCheck) left only the
        prefetch's early report on the tape, so loosening the rule could not be judged there. Now the wait is
        RugCheck's: a coin whose last report is old is asked at its fire, again every np_rug_recheck_s while it waits,
        and every answer is a "rug" line on the tape."""
        n = self.night(cfg(np_rug_prefetch=True, np_min_age_s=45))
        self.m.reports["late"] = seen("late", 3, (3.0,))                 # the early prefetch sees 3 wallets
        self.turn(n, E.create("late", "LATE"))
        self.turn(n, E.meta("late", False, True, False), sec=0.3)
        for k in range(1, 7):
            self.read(n, "late", 0.3 + 1.0 * k)                         # 6 big buys in its first 6 s
        self.assertEqual(self.m.rug_calls, ["late"])
        self.m.reports["late"] = seen("late", 12)                       # 12 wallets later: 6.3+ SOL over them
        self.clock.advance(38)
        r = 6.3
        for _ in range(12):                                             # quiet until the 45 s floor, then small buys
            r += 0.15
            self.read(n, "late", r, sec=0.5)
        coin = n.wl.get("late")
        self.assertTrue(npm.rug_waiting(coin))
        self.assertEqual(coin.rug["holders"], 12)
        self.assertGreaterEqual(len(self.m.rug_calls), 3)               # asked again near the fire, and while waiting
        self.assertTrue(n.rec.drain(5.0))
        rows = [x for _, path in mb.recorder.tape_files(os.path.join(self.dir, "tapes"))
                for x in mb.recorder.read_lines(path) if x.get("m") == "late" and x.get("k") == "rug"]
        self.assertEqual(len(rows), len(self.m.rug_calls))
        self.assertIn("fire", [x["why"] for x in rows])
        self.assertEqual([x["held"] for x in rows][-1], 12)

    def test_the_same_coin_without_the_rule_is_the_learners_shadow(self):
        """The control: np_max_sol_per_holder 0 (the rule off) and the LEARNER's skip makes it a SHADOW at once."""
        n = self.night(cfg(np_max_sol_per_holder=0.0))
        self.skip_tg(n)
        self.big(n)
        self.assertEqual(n.wl.get("conc").status, "rejected: learner: skip")
        self.assertEqual(n.shadows["conc"]["shadow"], "learner")
        self.assertIn("np:solh:.5-1", n.shadows["conc"]["learn"])       # the bucket only a looser config fills

    def test_the_empty_accounts_of_wallets_that_sold_are_no_holders(self):
        """RugCheck's totalHolders counts every open token account: 14 holding and 10 empty (flippers that sold out
        and kept their account) read 25 accounts with the curve's. The rule counts 14 wallets: still waiting."""
        n = self.night(cfg())
        coin = self.big(n, report=seen("conc", 14, empty=10))
        self.read(n, "conc", BIG[-1] + 0.2, sec=0.5)
        self.assertEqual(self.m.reports["conc"]["totalHolders"], 25)
        self.assertTrue(npm.rug_waiting(coin))
        self.assertEqual(coin.rug["holders"], 14)
        self.assertEqual(n.state["positions"], {})

    def test_a_risk_block_does_not_shadow_a_concentrated_coin_either(self):
        n = self.night(cfg(max_open=0))
        coin = self.big(n)
        for k in range(3):
            self.read(n, "conc", BIG[-1] + 0.1 * (k + 1), sec=0.5)
        self.assertTrue(npm.rug_waiting(coin))
        self.assertEqual(n.shadows, {})
        self.assertEqual(self.m.rug_calls, ["conc"])                    # RISK's path asked RugCheck: no PASS, no shadow
        self.assertEqual(self.lines(n, "LEARNER", "shadow"), [])

    def test_one_big_buy_reading_rejects_when_np_max_buy_sol_is_on(self):
        n = self.night(cfg(np_max_sol_per_holder=0.0, np_max_buy_sol=2.0))
        path = [2.8] + [round(2.8 + 0.25 * k, 2) for k in range(1, 9)]  # 2.5 SOL in the first reading, then small buys
        self.launch(n, "snip", path=path, report=seen("snip", 14))
        self.assertEqual(n.wl.get("snip").status, "rejected: concentrated: one buy of 2.5 SOL (max 2)")
        self.assertEqual((n.state["positions"], n.shadows), ({}, {}))
        said = self.lines(n, "SCANNER", "reject")
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith("concentrated: one buy of 2.5 SOL (max 2) · the trigger held, not bought"))
        self.assertEqual(self.m.rug_calls, [])                         # rejected before RugCheck (off by default)


class Labels(unittest.TestCase):
    def test_the_learner_keeps_every_entry_quality_label_newpairs_gives(self):
        for table, known in ((npm.NP_SOLHOLD, lr.NP_SOLHOLD), (npm.NP_HOLDERS, lr.NP_HOLDERS)):
            self.assertEqual([lab for _, lab in table], list(known))
            self.assertTrue(set(known) <= lr.KNOWN)
        mem = lr.fresh()
        lr.update(mem, ["np:solh:.25-.5", "np:holders:20-40", "np:bogus"], -4.0, weight=0.5, shadow=True)
        self.assertEqual(sorted(mem["labels"]), ["np:holders:20-40", "np:solh:.25-.5"])
        self.assertEqual((mem["shadows"], mem["samples"]), (1, 0))


class LiveCheck(unittest.TestCase):
    """replay.live_check replays each recorded run with the settings on its start line; a rule the line does not name
    did not exist in that run's code: replayed off (replay.LIVE_OFF), so a tape from before it is replayed as traded."""

    def check(self, line_cfg):
        R = E.mb and __import__("test_endtoend").R
        trade = {"mint": "m", "opened": "2026-11-26T09:00:00Z", "closed": "2026-11-26T09:01:00Z", "pnl": -1.0,
                 "pnl_pct": -10.0, "why": "stop -9%"}
        tape = types.SimpleNamespace(
            span=lambda: (1000.0, 2000.0), live_trades=lambda since=None, until=None: [trade],
            run_settings=lambda since, until: [{"t": 900.0, "from": 900.0, "to": 2000.0, "cfg": line_cfg, "bal": 100.0,
                                               "mem": None}],
            summary=lambda: {"lost_lines": 0}, gaps=[])
        got = []

        def fake(tape_, c, since, until, cfg_at=None, **kw):
            got.append((c, cfg_at(1500.0)))
            return {"trades": []}

        base = dict(mb.load_config(), shadow_on=True)
        with unittest.mock.patch.object(R, "replay", fake):
            R.live_check(tape, base)
        return got[0]

    def test_a_run_from_before_the_rules_is_replayed_without_them(self):
        first, run = self.check({"np_min_age_s": 45, "position_pct": 10.0})
        for c in (first, run):
            self.assertEqual((c["np_min_holders"], c["np_max_sol_per_holder"], c["np_max_buy_sol"], c["shadow_on"]),
                             (0, 0.0, 0.0, False))

    def test_a_run_that_names_them_is_replayed_with_its_own(self):
        own = {"np_min_holders": 5, "np_max_sol_per_holder": 0.8, "np_max_buy_sol": 0.0, "shadow_on": True}
        first, run = self.check(own)
        for c in (first, run):
            self.assertEqual({k: c[k] for k in own}, own)


if __name__ == "__main__":
    unittest.main()
