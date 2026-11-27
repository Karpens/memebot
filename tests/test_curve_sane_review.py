"""The review of the odd-reading gate (tests/test_curve_sane.py, the night of 2026-11-27): each finding as a case that
failed on the first version and passes now. A held coin is sold by the curve maths at the job's end only on a recent
sane state, and its trade's exit price is the state's that priced the sale (every trade the bot books passes
memebot/fix_book.py's void rules); a SHADOW is never sold on a state the real path would refuse (odd or stale), and one
nobody can price is dropped, not learned; a create's odd reserves are never the launch state; the odd counts are about
the coins the bot trades, once a coin a night, across restarts; a broken night["odd"] never stops a save; a restored
coin with an odd saved state is re-judged; one verdict for an odd reading wherever it goes (np_curve, the watch lists,
the replay's "o"); the sale cap is a safety net for curve-filled entries only (never a bias on ordinary moves); an odd
reading's complete flag counts only from a withdrawn pump.fun curve; the study's SNIPE runs recompute no momentum trigger
and count the odd readings as the live gate does. Offline, made-up coins (tests/test_snipe.py's harness)."""

import copy
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

from _load import load

import test_curve_sane as CS
import test_newpairs_engine as E
import test_shadow as S
import test_snipe as SN

os.environ["MEMEBOT_LIVE"] = "0"
os.environ["MEMEBOT_FEEDS"] = "0"
mb = E.mb
npm = mb.newpairs
fb = load("memebot", "fix_book")
K = E.K
odd_ev = CS.odd_ev
FORK = "ForkLaunchpad111111111111111111111111111111"


class Base(SN.Base):
    def odd(self, n, mint, vs=25508000, vt=1044500000000000, rs=0, sec=1.0, left=400, **kw):
        """An odd reading of mint (530A's kind unless said); Jupiter's market stays where it was."""
        self.slot += 1
        before = self.m.curve.get(mint)
        ev = odd_ev(mint, self.slot, vs, vt, rs, complete=kw.pop("complete", False))
        ev.update(kw)
        self.turn(n, ev, sec=sec, left=left)
        if before is not None:
            self.m.curve[mint] = before

    def crash(self, mint, real_sol=0.05):
        """The real market (Jupiter's quotes and price v3) falls to this real SOL."""
        v = 30.0 + real_sol
        self.m.curve[mint] = (v, K / v)

    def shadow(self, n, mint, rsol=0.8):
        """A snipe RISK said no to: followed as a SHADOW from its first reading."""
        with unittest.mock.patch.object(n, "why_no_entries", return_value="8 positions open (max)"):
            self.turn(n, E.create(mint, mint.upper()), sec=0.5)
            self.read(n, mint, rsol, sec=0.4)
        return n.shadows[mint]


class JobEnd(Base):
    """F1: at the job's end the curve maths sold a held coin on a last sane state of any age (2 hours old in the
    proof), and booked Jupiter's crashed price as its exit price: proceeds 2.9x what its tokens give at it."""

    def test_an_hours_old_sane_state_is_never_sold_at_the_jobs_end_the_coin_waits_for_the_next_job(self):
        n = self.night(SN.cfg())
        self.first(n, "hold", 20.0)                                   # bought on Jupiter's quote, mcap ~78
        pos = n.state["positions"]["hold"]
        self.read(n, "hold", self.rsol(pos, 5.0), sec=0.5)            # a sane +5 %
        self.odd(n, "hold", sec=0.5)                                  # then odd readings for good
        self.m.no_route.add("hold")
        self.crash("hold")                                            # the market falls (Jupiter's price v3 sees it)
        for _ in range(120):
            self.tick(n, 60)
        self.assertIn("hold", n.state["positions"])
        n.job_left = 0.5                                              # the job's last minute
        self.tick(n, 6)
        self.assertIn("hold", n.state["positions"])                   # held: the next job adopts and watches it
        said = [x for x in self.lines(n, "EXECUTOR", "stuck") if x.startswith("not sold by the curve maths")]
        self.assertEqual(len(said), 1)
        self.assertRegex(said[0], r"^not sold by the curve maths: its last sane curve reading is \d+ s old \(at most 60 s "
                                  r"at the end\) · held for the next job, which watches its curve again$")
        self.assertEqual(n.state["night"]["trades"], [])
        # the next reading is sane (the crashed market) and Jupiter routes again: sold, and the record is one state's
        n.job_left = None
        self.m.no_route.discard("hold")
        self.tick(n, 6)
        self.read(n, "hold", 0.05, sec=0.5)
        self.assertNotIn("hold", n.state["positions"])
        tr = n.state["night"]["trades"][-1]
        self.assertLess(tr["pnl_pct"], -50)
        self.assertEqual(fb.void_reasons(tr), [])

    def test_a_recent_sane_state_is_sold_at_the_jobs_end_and_the_trade_is_priced_on_it(self):
        n = self.night(SN.cfg())
        coin = self.first(n, "hold", 20.0)
        pos = n.state["positions"]["hold"]
        self.read(n, "hold", self.rsol(pos, 5.0), sec=0.5)
        state = (coin.vsol, coin.vtok, coin.price)
        self.odd(n, "hold", sec=0.5)
        self.m.no_route.add("hold")
        self.crash("hold")                                            # Jupiter's price v3 now says -95 %
        self.tick(n, 6)                                               # stale: Jupiter's price v3, -66 %
        self.assertLess(pos["last"], state[2] * 100.0 * 0.4)
        self.tick(n, 10)
        n.job_left = 0.5
        self.tick(n, 6)                                               # 22 s after the last sane reading
        self.assertNotIn("hold", n.state["positions"])
        tr = n.state["night"]["trades"][-1]
        self.assertEqual((tr["why"], tr["fill"]), ("the job ends · the next job takes over", "curve"))
        self.assertAlmostEqual(tr["exit_price"], state[2] * 100.0)    # the state that priced the sale, not Jupiter's
        tokens = pos["tokens_ui"]
        want = mb.curve_sell_sol(state[0], state[1], tokens, 1.25) * 100.0 * 0.99 - n.fee_usd(100.0, pos)
        self.assertAlmostEqual(tr["proceeds"], round(want, 2), places=2)
        self.assertEqual(fb.void_reasons(tr), [])
        self.assertIn("by the curve maths on stale reserves (no sane curve reading for 22 s)",
                      self.lines(n, "EXECUTOR", "exit")[-1])


class Shadows(Base):
    """F2 / F22: a shadow was sold by the curve maths on its last sane state while its newest reading was odd (or no
    reading came), a sale the real path refuses, and the LEARNER learned it (+1.4 % where the real trade made -62 %)."""

    def test_a_shadow_with_an_odd_newest_reading_is_never_sold_on_its_last_state_and_is_dropped_unlearned(self):
        n = self.night(SN.cfg())
        sh = self.shadow(n, "sh")
        self.read(n, "sh", self.rsol(sh, 6.0), sec=0.5)               # +6 %, under the +10 % step
        self.odd(n, "sh", sec=0.5)
        self.crash("sh", 2.0)                                         # the real market falls about 70 %
        shadows_before = n.mem_snipe.get("shadows", 0)
        for _ in range(8):
            self.tick(n, 60)
        self.assertNotIn("sh", n.shadows)
        self.assertEqual(n.state["shadow"]["closed"], [])             # never closed with a result
        self.assertEqual(n.mem_snipe.get("shadows", 0), shadows_before)   # nothing learned
        dropped = [x for x in self.lines(n, "LEARNER", "shadow") if x.startswith("SHADOW SH dropped")]
        self.assertEqual(len(dropped), 1)
        self.assertRegex(dropped[0], r"^SHADOW SH dropped: not priced: odd or stale curve \(no sane curve reading for "
                                     r"\d+ s\) · not learned$")

    def test_the_real_trade_and_its_shadow_on_the_same_readings_never_teach_two_stories(self):
        results = []
        for shadow in (False, True):
            self.setUp()
            n = self.night(SN.cfg())
            if shadow:
                pos = self.shadow(n, "cc", 20.0)
            else:
                self.first(n, "cc", 20.0)
                pos = n.state["positions"]["cc"]
            self.read(n, "cc", self.rsol(pos, 6.0), sec=0.5)
            self.odd(n, "cc", sec=0.5)
            self.crash("cc", 2.0)
            for _ in range(8):
                self.tick(n, 60)
            results.append((n.state["night"]["trades"], n.state["shadow"]["closed"]))
        (real, _), (_, shadow_rows) = results
        self.assertLess(real[-1]["pnl_pct"], -50)                     # Jupiter's sale of the real market
        self.assertEqual(shadow_rows, [])                             # the shadow: no +1.4 % lesson

    def test_a_shadow_whose_sale_waits_is_sold_on_the_next_sane_reading(self):
        n = self.night(SN.cfg())
        sh = self.shadow(n, "sh", 20.0)
        self.read(n, "sh", self.rsol(sh, 4.0), sec=0.5)
        self.odd(n, "sh", sec=0.5)
        for _ in range(4):
            self.tick(n, 50)                                          # its quiet-curve exit wants it out: it waits
        self.assertIn("sh", n.shadows)
        self.read(n, "sh", self.rsol(sh, -12.0), sec=0.5)             # sane again, under its stop
        self.polled_tick(n, 5)                                        # the retry: sold on that fresh, sane state
        self.assertNotIn("sh", n.shadows)
        row = n.state["shadow"]["closed"][-1]
        self.assertEqual((row["fill"], row["weight"]), ("curve", 0.5))
        self.assertLess(row["pnl_pct"], -10)


class Creates(Base):
    """F3: a create's own reserves (PumpPortal's vSol / vTokens) were the coin's launch state unjudged."""

    def odd_create(self, mint):
        cr = E.create(mint, mint.upper())
        cr.update(vSolInBondingCurve=3.5266, vTokensInBondingCurve=1.0149e9, marketCapSol=3.47)
        return cr

    def test_odd_create_reserves_are_never_the_launch_state(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS, np_need_social=False))
        coin = wl.on_create(self.odd_create("cc"), 0.0)
        self.assertEqual((coin.odd, wl.odd), (1, 1))
        self.assertTrue(coin.odd_why.startswith("the create's reserves: virtual SOL 3.527 is not 30 + real SOL 0"))
        self.assertFalse(coin.odd_now)
        self.assertAlmostEqual(coin.price0, 30.3 ** 2 / K)            # the creator's 0.3 SOL on a standard curve
        self.assertAlmostEqual(coin.rsol0, 0.3)
        wl.on_curve("cc", E.reading("cc", 1.0, 5), 1.0)
        self.assertLess(abs(coin.rise_pct - ((31.0 / 30.3) ** 2 - 1) * 100), 0.01)    # +4.7 %, not +759 %
        # a create without reserves: its market cap only when a standard curve can have it
        cr = E.create("mc", "MC")
        for k in ("vSolInBondingCurve", "vTokensInBondingCurve"):
            del cr[k]
        cr["marketCapSol"] = 3.47
        self.assertAlmostEqual(wl.on_create(cr, 0.0).price0, 30.3 ** 2 / K)

    def test_the_bot_counts_and_says_it(self):
        n = self.night(SN.cfg())
        self.turn(n, self.odd_create("cc"), sec=0.5)
        self.assertEqual({k: n.state["night"]["odd"][k] for k in ("readings", "coins")}, {"readings": 1, "coins": 1})
        said = self.lines(n, "SCANNER", "odd")
        self.assertEqual(len(said), 1)
        self.assertIn("odd curve reading: the create's reserves: virtual SOL 3.527 is not 30 + real SOL 0", said[0])
        self.assertIn("its launch state is the creator's buy on a standard curve instead", said[0])
        self.assertEqual(n.wl.get("cc").status, "watching")           # judged on its own readings from here


class Counts(Base):
    """F4 / F5: the odd counts were about curves nobody trades too, again after each restart; a broken night["odd"]
    stopped every save."""

    def test_curves_nobody_trades_are_counted_apart_and_never_said(self):
        n = self.night(SN.cfg())
        for i in range(3):
            self.odd(n, "ghost%d" % i, sec=0.2)
        self.assertEqual(n.state["night"]["odd"], {"readings": 0, "coins": 0, "followed": 3, "mints": []})
        self.assertEqual(self.lines(n, "SCANNER", "odd"), [])
        sp = n.panel_doc()["speed"]
        self.assertEqual((sp["odd_readings"], sp["odd_coins"]), (0, 0))
        self.assertNotIn("odd curve readings", sp["feed"])

    def test_a_coin_is_counted_and_said_once_a_night_across_a_restart(self):
        n = self.night(SN.cfg())
        self.first(n, "hh", 0.8)
        self.odd(n, "hh", sec=0.5)
        self.assertEqual(n.state["night"]["odd"], {"readings": 1, "coins": 1, "followed": 0, "mints": ["hh"]})
        n.save()
        n.np_close()
        n2 = self.night(SN.cfg())                                     # the next job: the same night, the coin held
        self.assertIn("hh", n2.state["positions"])
        self.odd(n2, "hh", sec=0.5)
        self.assertEqual(n2.state["night"]["odd"], {"readings": 2, "coins": 1, "followed": 0, "mints": ["hh"]})
        self.assertEqual(len(self.lines(n2, "SCANNER", "odd")), 1)    # the night's log: said once

    def test_a_broken_odd_or_speed_record_never_stops_a_save(self):
        n = self.night(SN.cfg())
        n.state["night"]["odd"] = 5
        n.state["night"]["np_speed"] = "x"
        doc = n.panel_doc()
        self.assertEqual((doc["speed"]["odd_readings"], doc["speed"]["odd_coins"]), (0, 0))
        n.save()
        self.assertTrue(os.path.exists(os.path.join(self.dir, "state.json")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "panel.json")))
        self.odd(n, "ghost", sec=0.2)                                 # and np_odd mends it
        self.assertEqual(n.state["night"]["odd"]["followed"], 1)


class Restore(Base):
    """F6: a coin restored from state.json (a held position's "np") was never judged by curve_sane."""

    def test_a_restored_coin_with_an_odd_saved_state_is_priced_by_jupiter_until_a_sane_reading(self):
        n = self.night(SN.cfg())
        self.m.no_route.add("hold")
        self.first(n, "hold", 0.8)                                    # filled by the curve maths (no route)
        n.save()
        n.np_close()
        path = os.path.join(self.dir, "state.json")
        st = mb.read_json(path, None)
        st["positions"]["hold"]["np"].update(vsol=3.5266, vtok=1.0149e9, rsol=0.19, price=3.5266 / 1.0149e9)
        mb.write_json(path, st)                                       # Coke-Cola's state, as an older bot saved it
        n2 = self.night(SN.cfg())
        coin = n2.wl.get("hold")
        self.assertEqual((coin.odd_now, coin.last_t), (True, 0.0))
        self.assertTrue(coin.odd_why.startswith("virtual SOL 3.527 is not 30 + real SOL 0.19"))
        self.assertTrue(npm.stale(coin, n2.cfg, n2.epoch()))
        said = self.lines(n2, "EXECUTOR", "stale")
        self.assertTrue(said and said[0].startswith("restored with a curve state no standard pump.fun curve is in: HOLD "
                                                    "(virtual SOL 3.527"), said)
        pos = n2.state["positions"]["hold"]
        self.assertIsNone(n2.sell_quote(pos, pos["raw"], 100.0))     # no curve-maths sale on it, nor at the end
        n2.np_ending = True
        self.assertIsNone(n2.sell_quote(pos, pos["raw"], 100.0))
        n2.np_ending = False
        self.read(n2, "hold", 1.0, sec=0.5)                           # a sane reading: the coin's state again
        self.assertFalse(coin.odd_now)
        self.assertIsNotNone(n2.sell_quote(pos, pos["raw"], 100.0))


class OneVerdict(Base):
    """F7: the replay dropped the tape's "o"; np_curve and the watch list each worked out their own verdict."""

    def test_a_reading_carries_its_verdict_everywhere(self):
        ev = dict(E.reading("rr", 1.0, 5), odd="not a pump.fun bonding curve (the account's owner is not the pump.fun "
                                             "program)")
        self.assertEqual(npm.curve_sane(ev), ev["odd"])               # standard reserves, the verdict it carries
        self.assertIsNone(npm.curve_sane(dict(ev, odd="")))
        rp = load("memebot", "replay")
        tp = rp.Tape()
        tp.add({"k": "c", "m": "rr", "t": 10.0, "s": 1, "vs": 31 * 10 ** 9, "vt": round(K / 31 * 1e6), "rs": 10 ** 9,
                "o": ev["odd"]})
        tp.finish()
        self.assertIsNone(tp.state_at("rr", 12.0))                    # the replay's Jupiter never prices it
        rev = rp.curve_event("rr", "k", 10.0, tp.events[-1][4])
        self.assertEqual(npm.curve_sane(rev), ev["odd"])
        # the replayed Night's gate: the watched coin is rejected on it, by np_curve and the watch list alike
        n = self.night(SN.cfg())
        self.turn(n, E.create("rr", "RR"), sec=0.5)
        self.slot += 1
        self.turn(n, dict(rev, slot=self.slot), sec=0.4)
        self.assertEqual(n.state["positions"], {})
        self.assertEqual(n.wl.get("rr").status, "rejected: odd curve reading: " + ev["odd"])


class SaleCap(Base):
    """F15 / F19: the cap booked ordinary market moves of Jupiter-filled positions by the curve maths (only ever down),
    and its guards were not pinned."""

    def test_a_quote_filled_momentum_sale_is_booked_as_quoted_either_way(self):
        # an ordinary move either way, and even 12x: Jupiter checked the curve maths for this mint at the entry
        for move in (0.08, -0.08, 11.0):
            self.setUp()
            n = self.night(S.cfg())
            self.launch(n, "mom", E.RUNNER, social=(True, True, True))
            pos, coin = n.state["positions"]["mom"], n.wl.get("mom")
            self.assertEqual(pos.get("entry_fill"), None)            # a momentum entry: Jupiter's quote
            self.m.tax["mom"] = -move                                 # the market moved since the last reading
            q = mb.curve_sell_sol(coin.vsol, coin.vtok, pos["tokens_ui"], 1.25) * (1 + move)
            self.assertTrue(n.close_part("mom", 1.0, "TP +10%", 100.0))
            tr = n.state["night"]["trades"][-1]
            self.assertAlmostEqual(tr["proceeds"], round(q * 100.0 * 0.99 - n.fee_usd(100.0, pos), 2), places=2)
            self.assertNotIn("fill", tr)
            self.assertNotIn("curve state that decided", self.lines(n, "EXECUTOR", "exit")[-1])

    def test_a_curve_filled_snipe_is_capped_only_far_beyond_its_state_and_only_on_a_fresh_sane_state(self):
        n = self.night(SN.cfg())
        for mint, tax, capped in (("x15", -0.5, False), ("x12", -11.0, True)):
            self.m.no_route.add(mint)
            coin = self.first(n, mint, 0.8)
            pos = n.state["positions"][mint]
            self.assertEqual(pos["entry_fill"], "curve")
            self.m.no_route.discard(mint)
            self.m.tax[mint] = tax
            state = mb.curve_sell_sol(coin.vsol, coin.vtok, pos["tokens_ui"], 1.25)
            self.assertTrue(n.close_part(mint, 1.0, "TP +10%", 100.0))
            tr = n.state["night"]["trades"][-1]
            sol = state if capped else state * (1 - tax)
            self.assertAlmostEqual(tr["proceeds"], round(sol * 100.0 * 0.99 - n.fee_usd(100.0, pos), 2), places=2)
            self.assertEqual(tr.get("fill"), "curve" if capped else None, mint)
        # stale, or its newest reading odd: Jupiter's price decided the sale, its quote stands uncapped
        for mint, how in (("st", "stale"), ("od", "odd")):
            self.m.no_route.add(mint)
            coin = self.first(n, mint, 0.8)
            pos = n.state["positions"][mint]
            self.m.no_route.discard(mint)
            if how == "stale":
                self.clock.advance(8)
                n.now = self.clock.now()
                self.assertTrue(npm.stale(coin, n.cfg, n.epoch()))
            else:
                self.odd(n, mint, sec=0.5)
                self.assertTrue(coin.odd_now)
            self.m.tax[mint] = -11.0
            state = mb.curve_sell_sol(coin.vsol, coin.vtok, pos["tokens_ui"], 1.25)
            self.assertAlmostEqual(n.np_sale_cap(pos, pos["raw"], state * 12.0), state * 12.0, msg=how)

    def test_a_quote_filled_position_on_stale_sane_reserves_waits_and_sells_at_the_end_only_on_a_recent_state(self):
        n = self.night(S.cfg())
        self.launch(n, "mom", E.RUNNER, social=(True, True, True))
        pos, coin = n.state["positions"]["mom"], n.wl.get("mom")
        self.m.no_route.add("mom")
        self.clock.advance(8)                                         # the poller is dark: stale, a sane state
        n.now = self.clock.now()
        self.assertTrue(npm.stale(coin, n.cfg, n.epoch()))
        self.assertIsNone(n.sell_quote(pos, pos["raw"], 100.0))       # "can't sell ... retrying"
        n.np_ending = True                                            # the job's end, the state 8 s old: sold on it
        self.assertIsNotNone(n.sell_quote(pos, pos["raw"], 100.0))
        self.clock.advance(60)
        n.now = self.clock.now()
        self.assertIsNone(n.sell_quote(pos, pos["raw"], 100.0))       # 68 s old: not even at the end
        n.np_ending = False


class Completion(Base):
    """F16: an odd reading's complete flag was trusted from any account: a foreign layout's byte 48 forced a
    "migrating: take it" sale, uncounted."""

    def test_a_foreign_accounts_complete_flag_is_odd_and_decides_nothing(self):
        n = self.night(SN.cfg())
        coin = self.first(n, "hold", 0.8)
        self.odd(n, "hold", sec=0.5, complete=True, owner_ok=False)
        self.assertIn("hold", n.state["positions"])
        self.assertEqual((coin.complete, coin.odd_now), (False, True))
        self.assertEqual(n.state["night"]["odd"]["readings"], 1)
        self.assertIn("owner is not the pump.fun program", self.lines(n, "SCANNER", "odd")[0])
        # no owner known, but the reserves are not a withdrawn curve's (530A's kind, its byte 48 set): odd too
        self.odd(n, "hold", sec=0.5, complete=True)
        self.assertEqual((coin.complete, n.state["night"]["odd"]["readings"]), (False, 2))
        self.assertEqual(n.state["night"]["trades"], [])
        # a pump.fun curve the migration withdrew (no real SOL, the completion's virtual tokens): its completion
        self.odd(n, "hold", vs=115005000000, vt=279900000000000, rs=0, sec=0.5, complete=True, owner_ok=True)
        self.assertNotIn("hold", n.state["positions"])
        self.assertEqual(n.state["night"]["trades"][-1]["why"], "migrating: take it")

    def test_complete_flag(self):
        self.assertTrue(npm.complete_flag({"complete": True, "vsol": 31e9, "vtok": K / 31 * 1e6, "rsol": 1e9}))
        withdrawn = odd_ev("m", 1, 115005000000, 279900000000000, 0, complete=True)
        zeroed = odd_ev("m", 1, 0, 0, 0, complete=True)
        for ev, want in ((withdrawn, True), (zeroed, True), (dict(withdrawn, owner_ok=False), False),
                         (dict(zeroed, disc_ok=False), False), (odd_ev("m", 1, 25508000, 1044500000000000, 0, True), False),
                         (odd_ev("m", 1, 115005000000, 279900000000000, 5e9, True), False), (dict(withdrawn, complete=False),
                                                                                             False)):
            self.assertEqual(npm.complete_flag(ev, npm.curve_sane(ev)), want, ev)


class Steady(unittest.TestCase):
    """F24: the panel's steady view (bot.steady_view: a panel that differs only in counters is not rewritten)."""

    def test_odd_counts_alone_never_make_the_panel_change(self):
        a = {"balance": 1.0, "speed": {"odd_readings": 1, "odd_coins": 1, "watching": 3,
                                       "feed": "poll · 3 watched · odd curve readings 1 (1 coin, never a price)"}}
        b = copy.deepcopy(a)
        b["speed"].update(odd_readings=7, odd_coins=2, feed="poll · 4 watched · odd curve readings 7 (2 coins, never a "
                                                            "price)")
        self.assertEqual(mb.steady_view(a), mb.steady_view(b))
        c = copy.deepcopy(b)
        c["speed"]["feed"] = "ws · 4 watched · odd curve readings 7 (2 coins, never a price)"   # a feed changed: shown
        self.assertNotEqual(mb.steady_view(a), mb.steady_view(c))


class Study(unittest.TestCase):
    """F20: a SNIPE run's RUN trigger and the odd count's completions were not pinned."""

    def setUp(self):
        self.st = load("memebot", "study")
        import test_study as TS
        self.TS = TS
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def views(self, tp):
        folder = os.path.join(self.dir, "t%d" % len(os.listdir(self.dir)))
        os.makedirs(folder)
        return self.TS.views_of(tp.write(folder))

    def tape(self, cfg, fires=()):
        TS = self.TS
        tp = TS.Tape(cfg=cfg)
        # a coin whose readings meet the momentum trigger at 45 s (3.6 SOL in, 9 buys, +26 %)
        path = [(5.0 * k, 0.3 + 0.4 * k) for k in range(1, 15)]
        tp.coin(TS.mint(1), TS.START + 100, path, fires=fires)
        return tp

    def test_a_snipe_run_recomputes_no_momentum_trigger(self):
        TS, st = self.TS, self.st
        views, _, _ = self.views(self.tape(dict(TS.CFG)))
        self.assertIn("RUN", views[0].mom)                            # a momentum run: its trigger recomputed
        self.assertEqual(st.fire_check(views)["recomputed_without_a_line"], 1)
        views, _, _ = self.views(self.tape(dict(TS.CFG, np_snipe=True), fires=(5.0,)))
        self.assertNotIn("RUN", views[0].mom)
        fc = st.fire_check(views)
        self.assertEqual((fc["fire_lines"], fc["recomputed_without_a_line"], fc["snipe_fire_lines"]), (0, 0, 1))

    def test_the_odd_count_leaves_completions_out_and_takes_the_bots_own_verdict(self):
        TS, st = self.TS, self.st
        tp = TS.Tape(cfg=dict(TS.CFG))
        tp.coin(TS.mint(1), TS.START + 100, [0.8, 1.5, 2.5], complete=2)          # zeroed at its completion (x 1)
        m2 = tp.coin(TS.mint(2), TS.START + 200, [0.8, 1.5])
        tp.add({"k": "c", "m": m2, "t": TS.START + 204.0, "s": 9, "vs": 115005000000, "vt": 279900000000000, "rs": 0,
                "x": 1})                                                           # withdrawn at its completion
        m3 = tp.coin(TS.mint(3), TS.START + 300, [0.8, 1.5])
        tp.add({"k": "c", "m": m3, "t": TS.START + 304.0, "s": 9, "vs": 25508000, "vt": 1044500000000000, "rs": 0,
                "x": 1})                                                           # 530A's kind with byte 48 set: odd
        m4 = tp.coin(TS.mint(4), TS.START + 400, [0.8, 1.5])
        tp.add({"k": "c", "m": m4, "t": TS.START + 404.0, "s": 9, "vs": 32 * 10 ** 9, "vt": round(K / 32 * 1e6),
                "rs": 2 * 10 ** 9, "o": "not a pump.fun bonding curve (its Anchor discriminator)"})   # the bot's verdict
        views, _, _ = self.views(tp)
        san = st.sanity(views)
        self.assertEqual((san["odd_readings"], san["odd_coins"]), (2, 2))
        self.assertEqual(sorted(cv.mint for cv in views if cv.odd), sorted([m3, m4]))


if __name__ == "__main__":
    unittest.main()
