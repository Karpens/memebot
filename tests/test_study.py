"""memebot/study.py, the edge study: the cost model (the engine's own curve maths), the fills (the next poll after the
latency), no look-ahead (a tape cut right after a decision makes the same decision), the coverage (blind exits when
the tape loses a coin, a completion sold into its last curve state), the split by create time, the calibration
against the bot's booked trades (a hand-made pair and the real engine's own tape), the PASS rule (a planted edge
passes, noise does not) and the command line end to end. Offline: synthetic tapes written as JSONL in the
recorder's format (memebot/recorder.py, version 2), made up for the tests and never a claim about real coins."""

import contextlib
import datetime
import io
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

from _load import ROOT, load

os.environ["MEMEBOT_LIVE"] = "0"
os.environ["MEMEBOT_FEEDS"] = "0"
st = load("memebot", "study")
bot = st.bot
K = 30.0 * 1.073e9
START = 1795680000.0                    # 2026-11-26T08:00:00Z
EPOCH = datetime.datetime(1970, 1, 1)
CFG = {"np_watch_s": 180, "np_min_sol": 2.5, "np_min_buys": 6, "np_max_age_s": 180, "np_min_age_s": 45,
       "np_min_buy_share": 0.65, "np_min_rise_pct": 12.0, "np_max_mcap_sol": 70.0, "np_dump_sol": 2.0,
       "np_max_dev_pct": 8.0, "np_need_social": False, "np_serial_max": 1, "np_serial_window_min": 60}


def mint(i, tag="s"):
    return ("%s%04d" % (tag, i)).ljust(40, "x") + "pump"


def ramp(a, b, step):
    """Real SOL from a to b in steps of `step` (b included, a left out)."""
    out, r = [], a
    while abs(b - r) > 1e-9:
        r = min(b, r + step) if b > r else max(b, r - step)
        out.append(round(r, 6))
    return out


class Tape(object):
    """A synthetic tape in the recorder's format: one run (start, a heartbeat every 10 s with the poller's good calls
    growing and nothing lost, SOL's price, stop), coins with their create, change-only curve readings, follow / end /
    gap lines, and any live lines a test adds. lost: [(a, b)], the recorder lost lines in every heartbeat stretch
    that ends in (a, b]; poll: {heartbeat t: (good calls, all calls)} of the stretch that heartbeat closes (else 10
    good of 10: one call a second)."""

    def __init__(self, start=START, cfg=None, sol=71.0):
        self.start, self.sol = start, sol
        self.cfg = dict(CFG if cfg is None else cfg)
        self.lines = []
        self.last = start
        self.slot = 10 ** 6
        self.lost = []
        self.poll = {}

    def coin(self, m, t0, path, dt=1.0, dev=0.3, creator=None, follow=None, end=None, complete=None, gap=None,
             fires=(), sym=None):
        """A coin created at t0. path: the real SOL in its curve, read at t0 + 1 + k * dt (or (age, real SOL) pairs);
        only changes go on the tape. follow None: the recorder's rule (half of np_min_sol and of np_min_buys within
        180 s: followed from 181 s to 900 s, else its watch ends at 181 s); True / False forces it. end: (age, why) of
        its last "end" line instead. complete: the index of the reading that completes the curve (its SOL moves out:
        x=1, "end complete"). gap: the age of a "gap" line (its next reading closes it). fires: ages of fire lines."""
        sym = sym or m[:6].upper()
        v0 = 30.0 + dev
        lines = [{"k": "create", "t": round(t0, 6), "m": m, "symbol": sym, "name": sym + " coin",
                  "creator": creator or "dev-" + m, "initialBuy": 1.073e9 - K / v0, "solAmount": dev,
                  "bondingCurveKey": "curve-" + m, "vSolInBondingCurve": v0, "vTokensInBondingCurve": K / v0,
                  "marketCapSol": v0 / (K / v0) * 1e9, "is_mayhem_mode": False, "pool": "pump", "_rx": round(t0, 6)}]
        pts = [(p[0], p[1]) if isinstance(p, tuple) else (1.0 + k * dt, p) for k, p in enumerate(path)]
        if follow is None:                              # the recorder's _near: 1.25 SOL in and 3 buy readings by 180 s
            prev, buys, follow = dev, 0, False
            for age, r in pts:
                if age > 180:
                    break
                if r > prev + 1e-6:
                    buys += 1
                prev = r
                if r - dev >= self.cfg["np_min_sol"] / 2 - 1e-9 and buys >= self.cfg["np_min_buys"] / 2:
                    follow = True
                    break
        if end is None:
            end = (900.0, "follow") if follow else (181.0, "watch")
        if follow:
            lines.append({"k": "follow", "t": round(t0 + 181.0, 6), "m": m, "until": round(t0 + 900, 6), "why": "near"})
        prev = None
        gap_open = gap is not None
        for k, (age, r) in enumerate(pts):
            if age >= end[0]:
                break
            if gap_open and age >= gap:
                lines.append({"k": "gap", "m": m, "t": round(t0 + gap, 6)})
                gap_open = False
                prev = None                             # the next reading after a gap goes on the tape again
            self.slot += 1
            if complete is not None and k == complete:
                lines.append({"k": "c", "m": m, "t": round(t0 + age, 6), "s": self.slot, "vs": 0, "vt": 0, "rs": 0,
                              "x": 1})
                end = (age + 0.5, "complete")
                break
            if prev is not None and abs(r - prev) < 1e-9:
                continue
            v = 30.0 + r
            lines.append({"k": "c", "m": m, "t": round(t0 + age, 6), "s": self.slot, "vs": int(round(v * 1e9)),
                          "vt": int(round(K / v * 1e6)), "rs": int(round(r * 1e9))})
            prev = r
        for a in fires:
            lines.append({"k": "fire", "m": m, "t": round(t0 + a, 6), "tt": round(t0 + a, 6), "age": a, "why": "momentum"})
        lines.append({"k": "end", "m": m, "t": round(t0 + end[0], 6), "why": end[1]})
        self.lines += lines
        self.last = max(self.last, max(float(x["t"]) for x in lines))
        return m

    def add(self, *lines):
        self.lines += list(lines)
        self.last = max([self.last] + [float(x["t"]) for x in lines])

    def all_lines(self, until=None):
        sysl = [{"k": "start", "t": self.start, "v": 2, "cfg": self.cfg}, {"k": "sol", "t": self.start + 0.5, "usd": self.sol}]
        t, ok, calls, lost = self.start + 10, 0, 0, 0
        while t <= self.last + 20:
            good, all_ = self.poll.get(round(t, 6), (10, 10))
            ok, calls = ok + good, calls + all_
            lost += 2 if any(a < t <= b for a, b in self.lost) else 0
            sysl.append({"k": "hb", "t": round(t, 6), "sol": self.sol, "poll_ok": ok, "poll_calls": calls,
                         "lost": lost, "drop": lost, "capped": 0})
            t += 10
        sysl.append({"k": "stop", "t": round(t, 6)})
        out = sorted(sysl + self.lines, key=lambda x: float(x["t"]))
        return [x for x in out if until is None or float(x["t"]) <= until + 1e-9]

    def write(self, folder, until=None, jobs=1):
        """The lines into folder/<job>/YYYY-MM-DDTHH.jsonl (the workflow's artifact layout: one subfolder a job; with
        jobs 2 the lines alternate between two folders, an hour two jobs shared). -> folder"""
        files = {}
        for k, x in enumerate(self.all_lines(until)):
            h = (EPOCH + datetime.timedelta(seconds=float(x["t"]))).strftime("%Y-%m-%dT%H")
            path = os.path.join(folder, "tapes-%d-1" % (1 + k % jobs), h + ".jsonl")
            files.setdefault(path, []).append(json.dumps(x, separators=(",", ":")))
        for path, lines in files.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        return folder


def views_of(folder, cfg=None):
    cfg = cfg or bot.load_config(bot.CONFIG)
    tapes, system, _, _ = st.load(folder)
    mk = st.Market(system, cfg)
    views, _ = st.views_of(tapes, mk, cfg)
    return views, mk, cfg


def entries():
    combos, ents = st.build_grid()
    return {c["entry"]: ents[c["entry"]] for c in combos}


def decisions(folder):
    """{entry key: {mint: decision time}} of every entry rule on the tape in folder."""
    views, mk, _ = views_of(folder)
    dec = st.decide(views, mk, entries())
    return {ek: {views[i].mint: t for i, t in d.items()} for ek, d in dec.items()}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def folder(self, name="tape"):
        d = os.path.join(self.dir, name)
        os.makedirs(d, exist_ok=True)
        return d


class Costs(Base):
    def test_a_round_trip_at_an_unchanged_price_costs_about_six_and_a_half_percent_by_the_engines_maths(self):
        c = st.Costs(bot.load_config(bot.CONFIG), 8.5)
        rt = c.round_trip(40.0, 71.0)
        self.assertAlmostEqual(rt["total_pct"], -6.6, delta=0.3)
        parts = rt["pumpfun_fee_pct"] + rt["haircut_pct"] + rt["fixed_fee_pct"] + rt["impact_pct"]
        self.assertAlmostEqual(parts, rt["total_pct"], delta=0.2)       # the parts nearly add up (they compound)
        self.assertAlmostEqual(rt["fixed_fee_pct"], -2 * 0.001 * 71 / 8.5 * 100, delta=0.1)
        tok, cost, entry_px = c.buy(40.0, K / 40.0, 71.0)
        self.assertAlmostEqual(tok, bot.curve_buy_tokens(40.0, K / 40.0, (8.5 - 0.001 * 71) / 71.0, 1.25) * 0.99)
        self.assertAlmostEqual(cost, 8.5)                              # the order's fee inside it, as np_fire sizes it
        self.assertAlmostEqual(entry_px, (8.5 - 0.071) / tok)          # the bot's pos["entry_price"]
        self.assertAlmostEqual(c.sell(40.0, K / 40.0, tok, 71.0),
                               bot.curve_sell_sol(40.0, K / 40.0, tok, 1.25) * 71.0 * 0.99 - 0.071)
        # the bot's own cost note: +10 % on the cost basis nets about +5.6 % at $10, a -7 % stop about -10.8 %
        c10 = st.Costs(bot.load_config(bot.CONFIG), 10.0)
        tok, cost, epx = c10.buy(35.0, K / 35.0, 71.0)
        for gain, want in ((10.0, 5.6), (-7.0, -10.8)):
            px_sol = epx * (1 + gain / 100.0) / 71.0                      # the curve price at that gain
            v = math.sqrt(px_sol * K)                                   # price = vsol^2 / K on the curve
            got = (c10.sell(v, K / v, tok, 71.0) - cost) / cost * 100
            self.assertAlmostEqual(got, want, delta=0.3)


class Fills(Base):
    def setUp(self):
        super().setUp()
        self.t0 = START + 60
        tp = Tape()
        self.m = tp.coin(mint(1), self.t0, [(100.0, 5.0), (100.6, 6.0), (101.4, 7.0), (103.0, 8.0), (170.0, 8.5)],
                         follow=False)
        self.views, self.mk, self.cfg = views_of(tp.write(self.folder()))
        self.cv = self.views[0]
        self.at = {round(t - self.t0, 1): i for i, t in enumerate(self.cv.ts)}

    def test_an_order_fills_on_the_next_poll_after_the_latency_never_on_the_deciding_reading(self):
        cv, at, t0 = self.cv, self.at, self.t0
        # decided on the reading at 100 s, sent 1 s later: the poll at 101.4 s is the fill (not 100 s, not 100.6 s)
        self.assertEqual(st.fill_index(cv, t0 + 101.0, 1.0, "next"), at[101.4])
        self.assertEqual(st.fill_index(cv, t0 + 101.0, 1.0, "at"), at[100.6])       # --fill at: the last state then
        # sent at 101.7 s: no reading within the next poll (103 s is 1.3 s later): the curve had not moved, 101.4's
        self.assertEqual(st.fill_index(cv, t0 + 101.7, 1.0, "next"), at[101.4])
        self.assertEqual(st.fill_index(cv, t0 + 102.5, 1.0, "next"), at[103.0])
        self.assertEqual(st.fill_index(cv, t0 + 100.0, 1.0, "next"), at[100.0])     # no latency: the reading itself
        # a whole trade: decided at 100 s, filled on 101.4's state with the engine's maths, held 60 s (sold on 103's)
        costs = st.Costs(self.cfg, 8.5)
        sim = st.Sim(costs, 1.0, 1.0, 1.0, "next")
        tr, why = st.simulate(cv, self.mk, t0 + 100.0, st.Hold(60), sim)
        self.assertIsNone(why)
        self.assertAlmostEqual(tr.fill, t0 + 101.4)
        tok, cost, _ = costs.buy(cv.vs[at[101.4]], cv.vt[at[101.4]], 71.0)
        usd = costs.sell(cv.vs[at[103.0]], cv.vt[at[103.0]], tok, 71.0)
        self.assertAlmostEqual(tr.pnl, (usd - cost) / cost * 100, places=9)
        self.assertAlmostEqual(tr.exit, t0 + 101.4 + 60 + 1.0)
        # with no latency the fill is the deciding reading's own state: a cheaper entry on a rising curve
        tr0, _ = st.simulate(cv, self.mk, t0 + 100.0, st.Hold(60), sim.but(l_in=0.0, l_out=0.0))
        self.assertGreater(tr0.pnl, tr.pnl)


class NoLookAhead(Base):
    """Every entry rule decides on lines at or before its decision: the tape cut right after a decision (or anywhere)
    gives the same decisions up to the cut."""

    def rich_tape(self):
        tp = Tape()
        t = START + 30
        rng = random.Random(4)
        n = 0
        for shape in ("pump", "pullback", "survivor", "dip", "organic", "graduation", "breakout", "fire", "chop",
                      "pump", "survivor", "graduation", "dip", "dud"):
            n += 1
            m = mint(n)
            if shape in ("pump", "fire"):
                path = ramp(0.3, 6.0, 0.1) + ramp(6.0, 9.0, 0.02) + ramp(9.0, 7.0, 0.05)
                tp.coin(m, t, path, fires=(50.0,) if shape == "fire" else ())
            elif shape == "pullback":
                path = ramp(0.3, 8.0, 0.15) + [7.5, 7.0, 6.5, 6.0] + ramp(6.0, 6.4, 0.1) + [6.4] * 30 + ramp(6.4, 9.0, 0.05)
                tp.coin(m, t, path)
            elif shape == "survivor":
                path = ramp(0.3, 4.0, 0.1) + [4.0] * 150 + ramp(4.0, 30.0, 0.05) + [30.0] * 100
                tp.coin(m, t, path)
            elif shape == "dip":
                path = ramp(0.3, 6.0, 0.1) + [6.0] * 25 + [2.8] + ramp(2.8, 6.0, 0.1) + ramp(6.0, 4.0, 0.05)
                tp.coin(m, t, path)
            elif shape == "organic":
                path = ramp(0.3, 1.2, 0.03) + ramp(1.2, 12.0, 0.05)
                tp.coin(m, t, path)
            elif shape == "graduation":
                path = ramp(0.3, 50.0, 0.15) + ramp(50.0, 60.0, 0.1)
                tp.coin(m, t, path)
            elif shape == "breakout":
                # a 2 % new high in one reading (several buys in one poll) after a 130 s base
                path = ramp(0.3, 7.0, 0.2) + [6.0, 5.5] + [5.5] * 130 + ramp(5.5, 7.0, 0.1) + ramp(7.0, 12.0, 0.5) + \
                    [12.0] * 60
                tp.coin(m, t, path)
            elif shape == "chop":
                r, path = 3.0, ramp(0.3, 3.0, 0.1)
                for _ in range(700):
                    r = max(0.2, r + rng.choice((-0.3, 0.3)))
                    path.append(round(r, 6))
                tp.coin(m, t, path)
            else:
                tp.coin(m, t, ramp(0.3, 0.6, 0.1))
            t += 240.0 + rng.uniform(0, 60)
        return tp

    def test_a_tape_cut_right_after_a_decision_makes_the_same_decisions_up_to_the_cut(self):
        tp = self.rich_tape()
        full = decisions(tp.write(self.folder("full")))
        fams = {ek.split(" ")[0] for ek, d in full.items() if d}
        for fam in ("SURV", "DIP", "ORG", "GRAD", "BRK", "MOM", "BASE-A", "BASE-B", "C0", "REF"):
            self.assertIn(fam, fams, "no %s decision on the test tape: the test would prove nothing" % fam)
        self.assertTrue(full["MOM PB"] and full["MOM S5"] and full["MOM LOOSE"])
        times = sorted({t for d in full.values() for t in d.values()})
        rng = random.Random(9)
        cuts = sorted(set(rng.sample(times, min(12, len(times))) + [rng.uniform(START, tp.last) for _ in range(4)]))
        for k, cut in enumerate(cuts):
            part = decisions(tp.write(self.folder("cut%d" % k), until=cut))
            for ek, d in full.items():
                want = {m: t for m, t in d.items() if t <= cut + 1e-9}
                got = {m: t for m, t in part.get(ek, {}).items() if t <= cut + 1e-9}
                self.assertEqual(got, want, "%s cut at %+.1f s" % (ek, cut - START))

    def test_a_lost_lines_stretch_over_several_heartbeats_is_known_stretch_by_stretch(self):
        # the recorder lost lines in 4 heartbeat stretches in a row, the coin's ages (40, 50] ... (70, 80] s: each one
        # is known from its own closing heartbeat (50, 60, 70, 80 s), never only from the last one (80 s)
        tp = Tape()
        t0 = START + 30
        m = tp.coin(mint(1), t0, ramp(0.3, 1.2, 0.03) + ramp(1.2, 12.0, 0.05))      # the "organic" coin
        tp.lost = [(t0 + 40.0, t0 + 80.0)]
        full = decisions(tp.write(self.folder("full")))
        views, mk, cfg = views_of(self.folder("full"))
        self.assertEqual([(a - t0, b - t0) for a, b, _ in mk.holes], [(40.0, 80.0)])   # merged for scoring
        self.assertTrue(mk.hole_known(t0 + 1.0, t0 + 61.0, t0 + 61.0))                # (40, 60] shown by 60 s
        self.assertFalse(mk.hole_known(t0 + 61.0, t0 + 65.0, t0 + 65.0))              # (60, 70] only from 70 s
        self.assertTrue(mk.hole_known(t0 + 61.0, t0 + 65.0, t0 + 70.0))
        times = sorted({t for d in full.values() for t in d.values()})
        cuts = sorted(set(times + [t0 + a for a in (45.0, 52.0, 59.0, 61.0, 65.0, 71.0, 79.0, 85.0)]))
        for k, cut in enumerate(cuts):
            part = decisions(tp.write(self.folder("cut%d" % k), until=cut))
            for ek, d in full.items():
                want = {mm: t for mm, t in d.items() if t <= cut + 1e-9}
                got = {mm: t for mm, t in part.get(ek, {}).items() if t <= cut + 1e-9}
                self.assertEqual(got, want, "%s cut at %+.1f s of age" % (ek, cut - t0))
        # no decision inside a stretch the tape had already shown, and the coin's later valid entry is traded
        dec = full["ORG f1.5 m0.5"][m]
        self.assertGreaterEqual(dec - t0, 80.0 + 60.0 - 1e-6)                          # its 60 s window clear of holes
        sim = st.Sim(st.Costs(cfg, 8.5), 1.0, 1.0)
        tr, why = st.simulate(views[0], mk, dec, st.exit_policies(cfg)["E2"][0], sim)
        self.assertIsNotNone(tr, why)

    def test_an_entry_after_the_watch_window_needs_the_follow_line_before_it(self):
        tp = Tape()
        quiet = ramp(0.3, 4.0, 0.1) + [4.0] * 150 + ramp(4.0, 30.0, 0.05) + [30.0] * 100
        a = tp.coin(mint(1), START + 30, quiet)                                   # near: followed at 181 s
        b = tp.coin(mint(2), START + 1200, quiet, follow=False, end=(900.0, "follow"))   # readings, no follow line
        c = tp.coin(mint(3), START + 2400, quiet, end=(250.0, "cap"))            # followed, then cut by the cap at 250 s
        dec = decisions(tp.write(self.folder()))
        self.assertIn(a, dec["SURV A180 f1 S0"])
        self.assertNotIn(b, dec["SURV A180 f1 S0"])
        self.assertTrue(all(b not in dec[k] for k in dec if k.startswith(("SURV", "BASE-B 300s", "BASE-B 480s"))))
        self.assertIn(b, dec["BASE-B 120s"])                                     # inside the watch window it is polled
        self.assertNotIn(c, dec["SURV A300 f1 S0"])                              # no entry after its end line
        self.assertTrue(all(t - (START + 2400) < 250 for k, d in dec.items() for m, t in d.items() if m == c))
        # before its first reading a coin's curve is unknown (the poller writes the first reading of every watch)
        tp = Tape()
        late = tp.coin(mint(4), START + 30, [(50.0, 0.4), (51.0, 0.6)] + [(52.0 + k, 0.6 + 0.1 * k) for k in range(60)])
        dec = decisions(tp.write(self.folder("late")))
        self.assertNotIn(late, dec["BASE-A 30s"])
        self.assertIn(late, dec["BASE-A 60s"])


class Coverage(Base):
    def test_a_trade_the_tape_loses_is_sold_blind_at_the_last_known_state_and_counted(self):
        tp = Tape()
        t0 = START + 30
        path = ramp(0.3, 6.0, 0.1) + ramp(6.0, 12.0, 0.02)
        m = tp.coin(mint(1), t0, path, end=(400.0, "cap"))
        views, mk, cfg = views_of(tp.write(self.folder()))
        cv = views[0]
        costs = st.Costs(cfg, 8.5)
        sim = st.Sim(costs, 1.0, 1.0)
        tr, _ = st.simulate(cv, mk, t0 + 300.0, st.Hold(300), sim)
        self.assertTrue(tr.blind)
        self.assertTrue(tr.why.startswith("blind: end cap"), tr.why)
        self.assertAlmostEqual(tr.exit, t0 + 400.0)
        k = cv.at(t0 + 400.0 - 1e-3)                                             # the last state before the end
        fi = st.fill_index(cv, t0 + 301.0, 1.0)
        tok, cost, _ = costs.buy(cv.vs[fi], cv.vt[fi], 71.0)
        self.assertAlmostEqual(tr.pnl, (costs.sell(cv.vs[k], cv.vt[k], tok, 71.0) - cost) / cost * 100, places=9)
        self.assertLess(tr.pess, tr.pnl)                                         # the pessimistic price: x0.7
        # an exit the tape still knows is not blind; a trade on a coin whose watch window ends (not followed) is
        tr2, _ = st.simulate(cv, mk, t0 + 100.0, st.Hold(60), sim)
        self.assertFalse(tr2.blind)
        # the blind share gate: over 10 % blind trades never pass
        ok = [st.Trade(0, START + i * 3600, 0, 0, 0, 20.0 + i % 3, 20.0, False, False, 20.0, "x") for i in range(40)]
        bad = ok[:34] + [st.Trade(0, START + i * 3600, 0, 0, 0, 20.0, 20.0, True, False, 10.0, "blind") for i in range(6)]
        first = st.stats(ok, 8.5)
        stress = {"latency +2 s": {"mean": 5.0}, "complete x0.85": {"mean": 5.0}}
        g_ok = st.gates(st.stats(ok, 8.5, boot=500, z=3.0), first, stress, 15)
        g_bad = st.gates(st.stats(bad, 8.5, boot=500, z=3.0), first, stress, 15)
        self.assertTrue(g_ok["blind_share"]["ok"])
        self.assertFalse(g_bad["blind_share"]["ok"])
        # the report counts every blind trade by its cause, so the 10 % gate can be judged on a real tape
        self.assertEqual(st.blind_cause(tr.why), "end cap")
        self.assertEqual(st.blind_cause("stop -12% (filled blind: run end)"), "run end")
        self.assertEqual(st.blind_cause("blind: tape hole"), "tape hole")
        self.assertEqual(st.blind_cause("no exit"), "no exit")
        got = st.blind_causes([tr, tr2, tr])
        self.assertEqual((got["trades"], got["blind"], got["causes"]), (3, 2, {"end cap": 2}))
        self.assertAlmostEqual(got["share"], 0.667)
        self.assertEqual(st.blind_causes([]), {"trades": 0, "blind": 0, "share": None, "causes": {}})

    def test_the_summary_says_why_trades_ended_blind_and_how_much_tape_is_unknown(self):
        tp = planted_tape(n=60, every_s=120.0, edge=True)
        rep = st.study(tp.write(self.folder()), split=0.7, boot=100, max_rules=40, embargo_s=0.0)
        bl = rep["coverage"]["blind"]
        self.assertGreater(bl["trades"], 0)
        self.assertEqual(bl["blind"], sum(bl["causes"].values()))
        self.assertIn("holes_by_cause", rep["coverage"])
        self.assertIn("**Blind exits** (every trade of every combination, overlays aside): %d of %d" % (
            bl["blind"], bl["trades"]), rep["markdown"])

    def test_a_poller_outage_inside_one_heartbeat_stretch_is_a_hole_never_a_flat_curve(self):
        # readings every second to 50 s of age, then none until 58.5 s (a 429 pause or a time-out), the curve at 9 SOL
        t0 = START + 30
        up = [(1.0 + k, r) for k, r in enumerate(ramp(0.3, 6.0, 0.1))]
        path = [p for p in up if p[0] <= 50.0] + [(58.5, 9.0), (59.5, 9.2)] + [(60.0 + k, 9.2) for k in range(5)]
        hb = START + 90.0                                                        # closes the ages (50, 60]
        cfg = bot.load_config(bot.CONFIG)
        sim = st.Sim(st.Costs(cfg, 8.5), 1.0, 1.0)
        for k, (poll, hole) in enumerate((({hb: (10, 10)}, False),              # healthy: no reading = unchanged
                                          ({hb: (1, 1)}, True),                 # 1 good call of 10
                                          ({hb: (5, 6)}, True),                 # a failed call (the 429): 5 of 6
                                          ({hb: (0, 0)}, True),                 # stood still
                                          ({hb: (5, 6), hb + 10: (7, 7)}, True))):   # the pause ran past the hb
            tp = Tape()
            tp.poll = poll
            tp.coin(mint(1), t0, path)
            views, mk, _ = views_of(tp.write(self.folder("p%d" % k)), cfg)
            tr, why = st.simulate(views[0], mk, t0 + 50.0, st.Hold(15), sim)    # sent at 51 s, into the outage
            if not hole:
                self.assertEqual(mk.holes, [])
                self.assertIsNotNone(tr)
                continue
            self.assertTrue(mk.holes, "no hole for the outage: %s" % poll)
            self.assertEqual(mk.holes[0][:2], (t0 + 50.0, t0 + (70.0 if len(poll) > 1 else 60.0)), poll)
            self.assertIsNone(tr, "an order into the outage filled on the stale state: %s" % poll)
            self.assertEqual(why, "not polled at the fill")
        # 6 good calls of 10 is no hole (a slow poller, no silence), nor a quick failed call the next round answered a
        # second later (9 of 10: 1 s unaccounted), nor the stretch after it; a time-out (5 good of 6, 5 s missing) is
        tp = Tape()
        tp.poll = {hb: (6, 6), hb + 10: (9, 10), hb + 20: (10, 10), hb + 30: (5, 6), hb + 40: (10, 10)}
        tp.coin(mint(1), t0, path)
        _, mk, _ = views_of(tp.write(self.folder("ok")), cfg)
        self.assertEqual([(a - t0, b - t0) for a, b, _ in mk.holes], [(80.0, 90.0)])   # only the time-out's
        self.assertEqual(mk.poll_stretches["failed_call"], 2)
        self.assertEqual(mk.poll_stretches["hole"], 1)
        # the edge: a failed call with exactly 3 s unaccounted is a hole, with 2 s it is not
        for good, hole in ((7, True), (8, False)):
            tp = Tape()
            tp.poll = {hb: (good, good + 1)}
            tp.coin(mint(1), t0, path)
            _, mk, _ = views_of(tp.write(self.folder("edge%d" % good)), cfg)
            self.assertEqual(bool(mk.holes), hole, good)

    def test_a_curve_that_completes_while_held_is_sold_into_its_last_state_and_counted_apart(self):
        tp = Tape()
        t0 = START + 30
        path = ramp(0.3, 60.0, 0.15) + ramp(60.0, 84.0, 0.2) + [84.0]
        m = tp.coin(mint(1), t0, path, complete=len(path) - 1)
        views, mk, cfg = views_of(tp.write(self.folder()))
        cv = views[0]
        self.assertTrue(cv.cx[-1])
        self.assertAlmostEqual(cv.rs[-1], 84.0)                                  # the state before the SOL moved out
        costs = st.Costs(cfg, 8.5)
        sim = st.Sim(costs, 1.0, 1.0)
        t_dec = t0 + 400.0
        tr, _ = st.simulate(cv, mk, t_dec, st.Hold(300), sim)
        self.assertTrue(tr.complete)
        self.assertFalse(tr.blind)
        fi = st.fill_index(cv, t_dec + 1.0, 1.0)
        tok, cost, _ = costs.buy(cv.vs[fi], cv.vt[fi], 71.0)
        last = len(cv.ts) - 1
        self.assertAlmostEqual(tr.pnl, (costs.sell(cv.vs[last], cv.vt[last], tok, 71.0) - cost) / cost * 100, places=9)
        hc, _ = st.simulate(cv, mk, t_dec, st.Hold(300), sim.but(haircut=0.85))   # the stress: x0.85
        self.assertLess(hc.pnl, tr.pnl - 5)
        # no entry on a completed curve
        none, why = st.simulate(cv, mk, cv.ts[-1] + 1.0, st.Hold(60), sim)
        self.assertIsNone(none)
        self.assertIn(why, ("curve complete", "not polled at the fill"))
        # PumpPortal's migrate and the recorder's "end complete" without a last x=1 reading: a completion too, never blind
        tp2 = Tape()
        m2 = tp2.coin(mint(2), t0, path[:-1], end=(len(path) + 0.5, "complete"))
        tp2.add({"k": "migrate", "m": m2, "t": t0 + len(path), "pool": "pump-amm"})
        views, mk, cfg = views_of(tp2.write(self.folder("migrate")))
        tr2, _ = st.simulate(views[0], mk, t_dec, st.Hold(300), sim)
        self.assertTrue(tr2.complete and not tr2.blind, tr2.why)
        self.assertAlmostEqual(tr2.pnl, tr.pnl, delta=0.5)

    def test_a_rule_that_wins_only_on_full_value_completion_fills_never_passes(self):
        # every coin climbs to 65 SOL real and completes at 69-71: bought at 65 (GRAD G65), the trade's whole gain
        # is the completion sold into the curve's last state at the completing reading, a fill the tape cannot show
        tp = Tape()
        rng = random.Random(5)
        for i in range(110):
            c = round(rng.uniform(69.0, 71.0), 2)
            path = ramp(0.3, 65.0, 0.45) + ramp(65.0, c, 0.15) + [c + 0.5]
            tp.coin(mint(i), START + 60 + i * 120.0, path, creator="d%d" % i, complete=len(path) - 1)
        rep = st.study(tp.write(self.folder()), split=0.7, boot=300, max_rules=77, top=20)
        rows = {r["key"]: r for r in rep["held_out"]["rows"]}
        g65 = [k for k in rows if k.startswith("GRAD G65")]
        self.assertTrue(g65)
        for k in g65:
            self.assertGreaterEqual(rows[k]["n"], 15)
            self.assertGreater(rows[k]["mean"], 0)
            self.assertFalse(rows[k]["passes"], k)
            self.assertEqual(rows[k]["failed"], ["complete_x085"], k)              # every other gate holds
            self.assertNotIn(k, rep["verdict"]["pass"])
        f = next(f for f in rep["finalists"] if f["key"] in g65)
        self.assertGreater(f["held"]["lower"], 0)
        self.assertLess(f["stress"]["complete x0.85"]["mean"], 0)
        self.assertEqual(f["completions"]["n"], f["held"]["n"])
        self.assertEqual(f["completions"]["share"], 1.0)
        self.assertIn("completed (mean at x0.85)", rep["markdown"])
        self.assertIn("| 100%% (%+.2f) |" % f["stress"]["complete x0.85"]["mean"], rep["markdown"])


class Controls(Base):
    def test_the_random_time_control_is_one_uniform_draw_per_coin(self):
        # a near coin followed and polled over its whole window: its C0 time is uniform in [max(45 s, near), 600 s],
        # not the earliest of several draws (that leans to the early ages, whose returns differ)
        tp = Tape()
        t0 = START + 30
        tp.coin(mint(1), t0, ramp(0.3, 4.0, 0.1) + [(700.0, 4.5)])
        tp.coin(mint(2), t0 + 1200, ramp(0.3, 4.0, 0.1) + [(700.0, 4.5)], end=(300.0, "cap"))   # lost at 300 s
        views, mk, _ = views_of(tp.write(self.folder()))
        full, capped = views
        lo = max(45.0, full.near_t - full.t0)
        cfgs = [("C0 random", {})]
        pos, got_capped = [], []
        for k in range(2000):
            full.mint = capped.mint = "c0-%d" % k
            d = st.scan_c0(full, mk, cfgs)
            self.assertEqual(set(d), {"C0 random"})                             # always polled: always a decision
            pos.append((d["C0 random"] - full.t0 - lo) / (600.0 - lo))
            got_capped.append(st.scan_c0(capped, mk, cfgs).get("C0 random"))
        self.assertAlmostEqual(sum(pos) / len(pos), 0.5, delta=0.03)          # the earliest of 3: 0.25
        self.assertAlmostEqual(sum(1 for p in pos if p < 0.25) / len(pos), 0.25, delta=0.04)
        self.assertTrue(all(0.0 <= p <= 1.0 for p in pos))
        # a coin the tape lost at 300 s: decided only when its one draw fell before then, never drawn again
        kept = [t for t in got_capped if t is not None]
        self.assertTrue(all(t - capped.t0 < 300.0 for t in kept))
        self.assertAlmostEqual(len(kept) / len(got_capped), (300.0 - lo) / (600.0 - lo), delta=0.04)


def planted_tape(n=70, every_s=360.0, edge=True, seed=3):
    """n coins every every_s: with edge, each one keeps being bought after its trigger (a steady climb every entry
    rule profits from); without, a random walk with no drift after it (noise: costs only)."""
    tp = Tape()
    rng = random.Random(seed)
    for i in range(n):
        t0 = START + 60 + i * every_s
        path = ramp(0.3, 4.5, 0.1)
        if edge:
            path += ramp(4.5, 4.5 + 0.06 * 560, 0.06)
        else:
            r = 4.5
            for _ in range(800):
                r = max(0.3, min(40.0, r + rng.choice((-0.4, -0.2, 0.2, 0.4))))
                path.append(round(r, 6))
        tp.coin(mint(i), t0, path, creator="dev%d" % i)
    return tp


def stress_of(mean):
    """A stress table with the gated rows only (2 s more latency, completions at x0.85), both at `mean`."""
    return {"latency +2 s": {"mean": mean}, "complete x0.85": {"mean": mean}}


class PassRule(Base):
    def test_the_pass_gates_a_planted_edge_passes_noise_does_not(self):
        rng = random.Random(1)
        mk_trades = lambda mu, sd, n: [st.Trade(0, START + (k % 24) * 3600, 0, 0, 0, rng.gauss(mu, sd), 0.0, False,
                                                False, 0.0, "x") for k in range(n)]
        z = st.zq(1 - 0.025 / 220)
        edge, first = mk_trades(12.0, 6.0, 60), mk_trades(11.0, 6.0, 80)
        g = st.gates(st.stats(edge, 8.5, boot=1000, z=z), st.stats(first, 8.5), stress_of(8.0), 15)
        self.assertTrue(all(x["ok"] for x in g.values()), g)
        noise = mk_trades(-6.5, 15.0, 60)
        g = st.gates(st.stats(noise, 8.5, boot=1000, z=z), st.stats(first, 8.5), stress_of(-7.0), 15)
        self.assertFalse(g["held_out_mean"]["ok"] or g["bonferroni_lower"]["ok"])
        # a positive mean that is noise: under the Bonferroni bound for 220 combinations
        lucky = mk_trades(3.0, 30.0, 40)
        g = st.gates(st.stats(lucky, 8.5, boot=1000, z=z), st.stats(first, 8.5), stress_of(1.0), 15)
        self.assertFalse(g["bonferroni_lower"]["ok"])
        # too few held-out trades, or a first part that lost, never pass
        few = mk_trades(12.0, 6.0, 10)
        self.assertFalse(st.gates(st.stats(few, 8.5, boot=500, z=z), st.stats(first, 8.5),
                                  stress_of(8.0), 15)["held_out_n"]["ok"])
        self.assertFalse(st.gates(st.stats(edge, 8.5, boot=500, z=z), st.stats(mk_trades(-3.0, 6.0, 80), 8.5),
                                  stress_of(8.0), 15)["first_part_mean"]["ok"])
        # the Bonferroni z: two-sided (1 - 0.05 / M)
        self.assertAlmostEqual(st.zq(1 - 0.025 / 200), 3.662, places=3)

    def test_end_to_end_a_planted_edge_passes_on_held_out_time_and_noise_does_not(self):
        edge = st.study(planted_tape(edge=True).write(self.folder("edge")), split=0.7, boot=400, embargo_s=0.0)
        self.assertTrue(edge["verdict"]["pass"], edge["verdict"])
        self.assertTrue(edge["verdict"]["text"].startswith("PASS: "))
        f = next(f for f in edge["finalists"] if f["passes"])
        self.assertGreaterEqual(f["held"]["n"], 15)
        self.assertGreater(f["held"]["lower"], 0)
        noise = st.study(planted_tape(edge=False).write(self.folder("noise")), split=0.7, boot=400, embargo_s=0.0)
        self.assertEqual(noise["verdict"]["pass"], [])
        self.assertEqual(noise["verdict"]["text"], "no rule beats costs on held-out data")
        # the base rows are there on both, never selected
        self.assertTrue(any(b["key"].startswith("BASE-B") for b in noise["base_rows"]))
        self.assertTrue(all(not f["key"].startswith(("BASE", "C0", "REF")) for f in noise["finalists"]))

    def test_every_ranked_combination_is_judged_on_held_out_time_not_only_the_top_ones(self):
        # coins every 120 s, two kinds in turn. M: the live trigger fires at 45 s; before the split it climbs on and
        # momentum wins, after it a 3.5 SOL dump at 62 s (a regime that ends: its near-copies fill the whole top 15).
        # S: near, never a momentum fire, flat to 300 s, then steady buying in BOTH parts (a SURV edge ranked lower)
        n, every = 170, 120.0
        lo, hi = START + 60, START + 60 + (n - 1) * every + 1e-3
        split_t = lo + 0.7 * (hi - lo)
        tp = Tape()
        rng = random.Random(11)
        for i in range(n):
            t0 = START + 60 + i * every
            if i % 2 == 0:
                pts = [(1.0 + k, r) for k, r in enumerate(ramp(0.3, 4.5, 0.1))]
                if t0 < split_t:
                    pts += [(43.0 + k, round(4.5 + 10.5 * (k + 1) / 123.0, 6)) for k in range(123)] + [(170.0, 9.0)]
                else:
                    pts += [(43.0 + k, round(4.5 + (k + 1) / 18.0, 6)) for k in range(18)]
                    pts += [(62.0, 2.0)] + [(63.0 + k, round(2.0 - 0.005 * (k + 1), 6)) for k in range(100)]
            else:
                slope = rng.uniform(0.010, 0.014)
                pts = [(1.0 + k, r) for k, r in enumerate(ramp(0.3, 1.8, 0.1))]
                pts += [(301.0 + k, round(1.8 + slope * (k + 1), 6)) for k in range(400)]
            tp.coin(mint(i), t0, pts, creator="dev%d" % i)
        rep = st.study(tp.write(self.folder()), split=0.7, boot=1000)
        shown = [f for f in rep["finalists"] if f["shown"]]
        self.assertEqual(len(shown), 15)
        self.assertLessEqual({f["family"] for f in shown}, {"MOM", "Q"})        # the crowd: one family's copies
        self.assertTrue(all(not f["passes"] for f in shown))
        surv = [f for f in rep["finalists"] if f["passes"] and f["family"] == "SURV"]
        self.assertTrue(surv, rep["verdict"])                                   # same M, same z: found anyway
        self.assertEqual(rep["held_out"]["judged"], rep["tried"]["ranked"])
        self.assertTrue(all(f["rank"] > 15 and not f["shown"] for f in surv))
        self.assertTrue(rep["verdict"]["text"].startswith("PASS: "))
        self.assertTrue(all(f["key"] in rep["verdict"]["pass"] for f in surv))
        self.assertIn("every one of the %d ranked combinations judged" % rep["tried"]["ranked"], rep["markdown"])


class SplitAndCalibration(Base):
    def test_the_split_is_by_create_time_with_the_embargo_left_out_of_both(self):
        tp = planted_tape(n=40, every_s=300.0, edge=True)
        rep = st.study(tp.write(self.folder()), split=0.5, boot=200, embargo_s=900.0, top=5)
        lo, hi = START + 60, START + 60 + 39 * 300.0 + 1e-3
        split_t = lo + 0.5 * (hi - lo)
        self.assertEqual(rep["settings"]["split_at"], st.iso(split_t))
        t0s = [START + 60 + i * 300.0 for i in range(40)]
        self.assertEqual(rep["tape"]["held_out_coins"], sum(1 for t in t0s if t >= split_t))
        self.assertEqual(rep["tape"]["embargo_coins"], sum(1 for t in t0s if split_t - 900 <= t < split_t))
        self.assertEqual(rep["tape"]["first_part_coins"], sum(1 for t in t0s if t < split_t - 900))
        held = {mint(i) for i, t in enumerate(t0s) if t >= split_t}
        for f in rep["finalists"]:
            self.assertTrue(f["trades"])
            self.assertTrue(all(x["mint"] in held for x in f["trades"]), f["key"])
            self.assertLessEqual(f["first"]["n"], rep["tape"]["first_part_coins"])

    def test_the_calibration_reproduces_a_live_buy_sell_pair_and_a_shadow(self):
        tp = Tape(sol=80.0)
        t0 = START + 30
        path = ramp(0.3, 6.0, 0.1) + ramp(6.0, 9.0, 0.02) + ramp(9.0, 5.0, 0.05)
        m = tp.coin(mint(1), t0, path)
        s = tp.coin(mint(2), t0 + 1000, path)
        folder = self.folder()
        views, mk, cfg = views_of(Tape.write(tp, os.path.join(self.dir, "pre")))
        costs = st.Costs(cfg, 8.5)
        fee = 0.001

        def state(cv, t):
            i = cv.at(t)
            return cv.vs[i], cv.vt[i]

        # a live trade as the engine books it: $8.50 with the fee inside, half sold at +x, the rest later
        cv = next(v for v in views if v.mint == m)
        tb, ts1, ts2 = t0 + 70.0, t0 + 120.0, t0 + 200.0
        usd_in = 8.5 - fee * 80.0
        raw = int(bot.curve_buy_tokens(*state(cv, tb), usd_in / 80.0, 1.25) * 1e6 * 0.99)
        cost = usd_in + fee * 80.0
        u1 = bot.curve_sell_sol(*state(cv, ts1), raw // 2 / 1e6, 1.25) * 80.0 * 0.99 - fee * 80.0
        u2 = bot.curve_sell_sol(*state(cv, ts2), (raw - raw // 2) / 1e6, 1.25) * 80.0 * 0.99 - fee * 80.0
        pnl = round((u1 + u2 - cost) / cost * 100, 2)
        tp.add({"k": "buy", "m": m, "t": tb, "usd": round(cost, 6), "raw": raw, "tok": raw / 1e6, "px": usd_in / (raw / 1e6),
                "sol": 80.0, "fee_sol": fee, "tt": tb - 0.4, "tier": "swing"},
               {"k": "sell", "m": m, "t": ts1, "frac": 0.5, "raw": raw // 2, "usd": round(u1, 6), "sol": 80.0,
                "why": "TP +10%", "final": False, "lat_ms": 300},
               {"k": "sell", "m": m, "t": ts2, "frac": 1.0, "raw": raw - raw // 2, "usd": round(u2, 6), "sol": 80.0,
                "why": "stop -7%", "final": True, "pnl": round(u1 + u2 - cost, 4), "pnl_pct": pnl, "lat_ms": 500,
                "px": 1e-6})
        # a shadow: the curve maths on its own reading, every cost
        sv = next(v for v in views if v.mint == s)
        to, tso = t0 + 1000 + 60.0, t0 + 1000 + 150.0
        sraw = int(bot.curve_buy_tokens(*state(sv, to), usd_in / 80.0, 1.25) * 1e6 * 0.99)
        su = bot.curve_sell_sol(*state(sv, tso), sraw / 1e6, 1.25) * 80.0 * 0.99 - fee * 80.0
        tp.add({"k": "shadow", "m": s, "t": to, "act": "open", "kind": "learner", "usd": cost, "raw": sraw, "sol": 80.0},
               {"k": "shadow", "m": s, "t": tso, "act": "sell", "frac": 1.0, "raw": sraw, "usd": su, "final": True,
                "pnl": su - cost, "pnl_pct": round((su - cost) / cost * 100, 2), "why": "time"})
        views, mk, cfg = views_of(tp.write(folder))
        cal = st.calibrate(views, mk, costs, 1.0, 0.4, 0.4)
        live = [r for r in cal["rows"] if r["kind"] == "live"]
        shadow = [r for r in cal["rows"] if r["kind"] == "shadow"]
        self.assertEqual((len(live), len(shadow)), (1, 1))
        self.assertAlmostEqual(live[0]["at"], pnl, delta=0.02)
        self.assertAlmostEqual(live[0]["tokens_vs_raw_pct"], 0.0, delta=0.01)
        self.assertAlmostEqual(shadow[0]["at"], shadow[0]["booked"], delta=0.02)
        self.assertLessEqual(cal["mae"]["live"]["at"][0], 0.02)
        self.assertEqual(cal["mae"]["live"]["at"][1], 1)
        lin, lout, waits = st.latency(views)
        self.assertEqual([round(x, 3) for x in lin], [0.4])
        self.assertEqual(waits, [0.0])                                  # no RugCheck line after its trigger
        self.assertEqual(sorted(round(x, 3) for x in lout), [0.3, 0.5])

    def test_the_entry_latency_is_the_fills_own_and_the_rugcheck_wait_counts_on_the_ref_rows_only(self):
        # the live bot's flow (bot.py np_fire / np_reports): a prefetch report; 8 of 14 buys saw too few holders and
        # were asked again at the fire and every 4 s while the trigger held; each buy comes 0.45-0.9 s (the quotes,
        # the sell simulation) after the report that let it go. The study's rules leave RugCheck out.
        tp = Tape()
        rng = random.Random(3)
        fills, waits = [], []

        def rug(m, t, why, holders):
            return {"k": "rug", "m": m, "t": round(t, 6), "ok": True, "why": why, "ms": 400, "holders": holders,
                    "top": [5.0], "top1": 5.0, "top10": 20.0, "risks": [], "score": 1}

        for i in range(40):
            t0 = START + 60 + i * 300.0
            up = [(1.0 + k, r) for k, r in enumerate(ramp(0.3, 6.0, 0.1))]
            a = up[-1][0]
            m = tp.coin(mint(i), t0, up + [(a + 1.0 + k, round(6.0 + 0.35 * (k + 1), 6)) for k in range(12)],
                        creator="dev%d" % i)
            if i >= 14:
                continue
            tt = last = t0 + a
            lines = [rug(m, tt - 8.0, "prefetch", 6 if i < 8 else 14)]
            if i < 8:
                last = tt + 0.4
                lines.append(rug(m, last, "fire", 7))
                for _ in range(1 if i % 2 == 0 else 2):
                    last += 4.4
                    lines.append(rug(m, last, "fire", 12))
            q = rng.uniform(0.45, 0.9)
            fills.append(q)
            waits.append(last - tt)
            lines += [{"k": "buy", "m": m, "t": round(last + q, 6), "tt": round(tt, 6), "usd": 8.5, "raw": 1,
                       "sol": 71.0, "fee_sol": 0.001},
                      {"k": "sell", "m": m, "t": round(last + q + 40, 6), "frac": 1.0, "final": True, "pnl_pct": -10.0,
                       "usd": 7.6, "lat_ms": rng.randint(250, 600), "sol": 71.0}]
            tp.add(*lines)
        folder = tp.write(self.folder())
        views, mk, cfg = views_of(folder)
        lin, lout, w = st.latency(views)
        self.assertEqual(sorted(round(x, 4) for x in lin), sorted(round(x, 4) for x in fills))
        self.assertEqual(sorted(round(x, 4) for x in w), sorted(round(x, 4) for x in waits))
        rep = st.study(folder, split=0.7, boot=100, max_rules=40, embargo_s=0.0)
        self.assertAlmostEqual(rep["settings"]["latency_in_s"], st.median(fills), delta=1e-3)     # not ~5.4 s
        self.assertLess(rep["settings"]["latency_in_s"], 1.0)
        self.assertAlmostEqual(rep["settings"]["rugcheck_wait_s"], 4.8, delta=1e-3)
        self.assertAlmostEqual(rep["latency"]["used"]["ref_in"], st.median(fills) + 4.8, delta=1e-3)
        self.assertEqual((rep["latency"]["rugcheck_wait"]["n"], rep["latency"]["rugcheck_wait"]["waited"]), (14, 8))
        # the latency stress is never easier than the base: 2 s MORE each way, whatever the tape measured (3.1 s here)
        sim = st.Sim(st.Costs(cfg, 8.5), 3.1, 0.3)
        exits = st.exit_policies(cfg)
        held = [st.simulate(cv, mk, cv.mom["BASE"], exits["X3_60"][0], sim)[0] for cv in views if "BASE" in cv.mom]
        self.assertGreaterEqual(len(held), 30)
        base = sum(t.pnl for t in held) / len(held)
        s = st.stress({"key": "MOM BASE | X3_60", "family": "MOM", "entry": "MOM BASE"}, held, views, mk, sim, exits,
                      8.5, cfg)
        lat = [v for k, v in s.items() if k.startswith("latency") and k != "latency 0 s"]
        self.assertEqual(len(lat), 1)
        self.assertLess(lat[0]["mean"], base - 0.5)                    # a rising curve: later fills cost more
        self.assertGreater(s["latency 0 s"]["mean"], base)

    def test_the_real_engines_own_tape_is_reproduced_and_its_fires_recomputed(self):
        import test_endtoend as e2e                     # the live engine on a realistic made-up stream
        d = self.folder("live")
        trades, cfg = e2e.live_run(d, 30, 11, "reading")
        self.assertGreaterEqual(len(trades), 5)
        rep = st.study(d, split=0.5, boot=200, min_trades=3, top=3, embargo_s=0.0)
        mae = rep["calibration"]["mae"]["live"]
        self.assertEqual(mae["at"][1], len(trades))
        self.assertLessEqual(mae["at"][0], 0.05)                       # Jupiter quoted the curve maths: to the cent
        self.assertGreaterEqual(rep["fire_check"]["share"], 0.9)        # the live trigger, recomputed from the readings
        booked = next(b for b in rep["base_rows"] if b["key"] == "BOOKED live")
        self.assertEqual(booked["all"]["n"], len(trades))


class Workflow(unittest.TestCase):
    """.github/workflows/study.yml: read-only, on demand, the backtest's tape download, the study's flags."""

    def setUp(self):
        with open(os.path.join(ROOT, ".github", "workflows", "study.yml"), encoding="utf-8") as fh:
            self.text = fh.read()

    def test_the_yaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        doc = yaml.safe_load(self.text)
        on = doc.get("on", doc.get(True))               # YAML 1.1 reads a bare `on` as true
        self.assertEqual(list(on), ["workflow_dispatch"])
        self.assertEqual({k: v["default"] for k, v in on["workflow_dispatch"]["inputs"].items()},
                         {"hours": "24", "split": "0.7"})
        self.assertEqual(doc["permissions"], {"contents": "read", "actions": "read"})
        job = doc["jobs"]["study"]
        self.assertEqual({k: job["env"][k] for k in ("MEMEBOT_LIVE", "MEMEBOT_REC", "MEMEBOT_FEEDS",
                                                     "PYTHONDONTWRITEBYTECODE")}, {k: "0" for k in (
            "MEMEBOT_LIVE", "MEMEBOT_REC", "MEMEBOT_FEEDS")} | {"PYTHONDONTWRITEBYTECODE": "1"})
        up = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact"))
        self.assertEqual((up["with"]["name"], up["with"]["retention-days"]), ("study-${{ github.run_id }}", 30))
        self.assertEqual(up["with"]["path"].split(), ["study.json", "fetched.json"])

    def test_it_is_read_only_and_wired_to_the_study(self):
        body = "\n".join(l for l in self.text.splitlines() if not l.lstrip().startswith("#"))
        for word in ("meme-data", "git push", "git commit", "gh workflow run", "contents: write", "actions: write"):
            self.assertNotIn(word, body)
        for word in ("workflow_dispatch:", "contents: read", "actions: read", 'MEMEBOT_LIVE: "0"', 'MEMEBOT_REC: "0"',
                     'MEMEBOT_FEEDS: "0"', 'PYTHONDONTWRITEBYTECODE: "1"', "memebot/fetch_tapes.py fetch",
                     "--workflow memebot.yml", "--out tapes --manifest fetched.json",
                     'python3 memebot/study.py --data tapes --split "${SPLIT:-0.7}" --out study.json --md '
                     '"$GITHUB_STEP_SUMMARY"', "name: study-${{ github.run_id }}", "retention-days: 30"):
            self.assertIn(word, body)
        with open(os.path.join(ROOT, ".github", "workflows", "backtest.yml"), encoding="utf-8") as fh:
            bt = fh.read()
        fetch = lambda t: t[t.index("python3 memebot/fetch_tapes.py fetch"):].split("\n\n")[0]
        self.assertEqual(fetch(self.text), fetch(bt))                   # the very same download as the backtest
        for line in self.text.splitlines():                             # inputs through env, never pasted in a script
            if "${{ inputs." in line:
                self.assertRegex(line.strip(), r"^[A-Z_]+: \$\{\{ inputs\.\w+ \}\}$")


class CommandLine(Base):
    def test_the_cli_end_to_end_on_a_folder_of_job_artifacts(self):
        tp = planted_tape(n=30, every_s=400.0, edge=False, seed=5)
        data = tp.write(self.folder("tapes"), jobs=2)
        before = sorted(os.path.join(r, f) for r, _, fs in os.walk(data) for f in fs)
        out_json = os.path.join(self.dir, "study.json")
        md = os.path.join(self.dir, "summary.md")
        with open(md, "w") as fh:
            fh.write("# earlier steps\n")
        said = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = st.main(["--data", data, "--out", out_json, "--md", md, "--max-rules", "120", "--boot", "300",
                            "--min-trades", "5"], out=said.append)
        self.assertEqual(code, 0)
        with open(out_json, encoding="utf-8") as fh:
            rep = json.load(fh)
        self.assertLessEqual(rep["tried"]["combos"], 120)
        self.assertGreater(rep["tried"]["left_out_by_cap"], 0)
        self.assertEqual(rep["tried"]["selectable"], sum(1 for r in rep["first_part"] if r["selectable"]))
        for key in ("settings", "tape", "coverage", "costs", "latency", "calibration", "sanity", "fire_check", "tried",
                    "ranking", "first_part", "finalists", "base_rows", "verdict", "rules", "limits"):
            self.assertIn(key, rep)
        self.assertEqual(rep["tape"]["coins"], 30)
        with open(md, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("# earlier steps\n## Edge study"))          # appended, never overwritten
        self.assertEqual(said, [rep["markdown"]])                                    # the same summary on stdout
        self.assertIn("Verdict:", rep["markdown"])
        self.assertIn("combinations tried", rep["markdown"])
        after = sorted(os.path.join(r, f) for r, _, fs in os.walk(data) for f in fs)
        self.assertEqual(before, after)                                              # nothing written into the data
        # as a script, the way the workflow runs it
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "memebot", "study.py"), "--data", data, "--max-rules", "40",
                            "--boot", "100"], capture_output=True, text=True, timeout=300, env=env)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("## Edge study", r.stdout)
        self.assertIn("combinations tried", r.stdout)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "memebot", "study.py"), "--data", data, "--split", "0.99"],
                           capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 2)

    def test_an_empty_folder_says_so_and_writes_its_report(self):
        out_json = os.path.join(self.dir, "s.json")
        said = []
        with contextlib.redirect_stderr(io.StringIO()):
            st.main(["--data", self.folder("none"), "--out", out_json], out=said.append)
        with open(out_json, encoding="utf-8") as fh:
            rep = json.load(fh)
        self.assertEqual(rep["verdict"]["text"], "no rule beats costs on held-out data")
        self.assertIn("no coins", said[0])


if __name__ == "__main__":
    unittest.main()
