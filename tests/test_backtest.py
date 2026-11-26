"""memebot/replay.py and memebot/backtest.py: recorded tapes through the real New Pairs engine (bot.run() on a
simulated clock), and the walk-forward grid on top. Offline: synthetic tapes made of pump.fun curve maths (the
probe's numbers: 30 SOL / 1.073e9 virtual tokens at launch, a reading every 0.5-1 s), made up for the tests and
never a claim about real coins."""

import concurrent.futures
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

from _load import ROOT, load

os.environ["MEMEBOT_LIVE"] = "0"
os.environ["MEMEBOT_FEEDS"] = "0"
bt = load("memebot", "backtest")
R = bt.replay
bot = R.bot
K = R.K
START = R.parse_time("2026-11-26T08:00:00Z")


def mint(i, tag="c"):
    return ("%s%04d" % (tag, i)).ljust(40, "x") + "pump"


def base_cfg(**kw):
    """config.json's rules on the code's defaults, with a LEARNER that neither skips nor resizes (so a test sees
    the rule it is about), unless kw says otherwise. np_min_age_s 0: the synthetic coins here reach the trigger
    5 s after their launch (config.json waits 45 s; tests/test_newpairs.py tests that rule, and
    tests/test_endtoend.py replays a realistic stream with config.json as it is)."""
    c = bot.load_config(bot.CONFIG)
    c.update(learn_skip_pct=-1000.0, learn_mult_min=1.0, learn_mult_max=1.0, np_min_age_s=0)
    c.update(kw)
    return c


def first_step(cfg, tier="scalp"):
    """The gain % of a tier's first take-profit step in this config (config.json: +10)."""
    return next(t for t in bot.holdplan.tiers_from_cfg(cfg) if t["name"] == tier)["ladder"][0][0]


def entry_rsol(dev=0.3):
    """The real SOL of the default trigger in R.pump_path (6 buys of 0.5 SOL from the creator's 0.3)."""
    return 3.3


def gain_rsol(gain_pct, at=3.3):
    """Real SOL at which the curve price is gain_pct over the price at real SOL `at` (price = vsol^2 / K)."""
    return R.vsol_at_gain(30.0 + at, gain_pct) - 30.0


def trailing_tiers():
    """The scalp tier sells a quarter at its first step and trails the rest 10 % under its top (the other tiers as
    config)."""
    ts = bt.tiers(bot.load_config(bot.CONFIG))
    for t in ts:
        if t["name"] == "scalp":
            t["ladder"], t["trail_pct"] = [[t["ladder"][0][0], 0.25], [200.0, 1.0]], 10.0
    return ts


class Tapes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_the_recorders_v1_lines_and_the_generic_ones_read_alike(self):
        path = R.pump_path(peak_gain=30)
        v1 = R.tape_of(R.synth_coin(mint(1), START, path, social=(True, True, False)) +
                       [{"k": "hb", "t": START, "sol": 210.0}])
        gen = R.tape_of(R.synth_coin(mint(1), START, path, social=(True, True, False), fmt="generic") +
                        [{"k": "sol", "t": START, "usd": 210.0}])
        self.assertEqual(v1.states, gen.states)
        self.assertEqual([e[2] for e in v1.events], [e[2] for e in gen.events])
        self.assertEqual(v1.sol, gen.sol)
        for tape in (v1, gen):
            ev = R.curve_event(mint(1), "curve-" + mint(1), 1.0, tape.curves[mint(1)][1][0])
            self.assertEqual((ev["kind"], ev["vsol"], ev["rsol"], ev["complete"]), ("curve", int(30.8e9), int(0.8e9), False))
            meta = tape.metas[mint(1)][0][1]
            self.assertEqual((meta["twitter"], meta["telegram"], meta["website"]),
                             ("https://x.com/" + mint(1), "https://t.me/" + mint(1), ""))
            rep = tape.rugs[mint(1)][0][1]
            coin = bot.newpairs.Coin(mint=mint(1), curve="curve-" + mint(1))
            ok, why, holders = bot.newpairs.rug_check_np(rep, coin, bot.load_config(bot.CONFIG))
            self.assertEqual((ok, holders, coin.rug["top1_pct"], coin.rug["top10_pct"]), (True, 14, 3.0, 6.5))
        create = [e for e in v1.events if e[2] == "create"][0][4]
        self.assertEqual(create["traderPublicKey"], "dev-" + mint(1))     # the serial-launcher check needs it

    def test_the_rugcheck_summary_is_judged_by_the_engines_own_rules(self):
        cfg = bot.load_config(bot.CONFIG)
        cases = [({"ok": True, "holders": 9, "top1": 15.0, "top10": 20.0, "top": [15.0, 5.0], "risks": [], "insiders": 0},
                  False, "a holder has 15.0%"),
                 ({"ok": True, "holders": 9, "top1": 3.0, "top10": 3.0, "top": [3.0],
                   "risks": [["Creator history of rugged tokens", "warn"]], "insiders": 0}, False, "risk: Creator"),
                 ({"ok": True, "holders": 9, "top1": 3.0, "top10": 3.0, "top": [3.0], "risks": [["Low liquidity", "danger"]],
                   "insiders": 0}, False, "risk: Low liquidity"),
                 ({"ok": True, "holders": 9, "top1": 3.0, "top10": 3.0, "top": [3.0], "risks": [], "insiders": 2},
                  False, "2 insiders"),
                 ({"ok": True, "holders": 9, "top1": 6.0, "top10": 30.0, "top": [6.0] * 5, "risks": [], "insiders": 0},
                  True, None)]
        for fields, want, why in cases:
            rep = R.rug_report_of(fields, "mmm")
            ok, bad, _ = bot.newpairs.rug_check_np(rep, bot.newpairs.Coin(mint="mmm", curve="cv"), cfg)
            self.assertEqual(ok, want, (fields, bad))
            if why:
                self.assertTrue(bad[0].startswith(why), bad)
        self.assertIsNone(R.rug_report_of({"ok": False, "why": "fire", "ms": 3000}, "mmm"))   # a failed ask: no report
        rep = R.rug_report_of({"ok": False, "reasons": ["risk: Single holder ownership"], "top1_pct": 2.0}, "mmm")
        self.assertFalse(bot.newpairs.rug_check_np(rep, bot.newpairs.Coin(mint="mmm"), cfg)[0])

    def test_a_tolerant_reader_files_by_hour_junk_and_unknown_kinds(self):
        recs = R.synth_tape(START, [(10, mint(1), R.pump_path(30), {}), (3700, mint(2), R.pump_path(30), {})])
        R.write_tape(self.dir, recs)
        with open(os.path.join(self.dir, "tapes", "2026-11-26T08.jsonl"), "a") as fh:
            fh.write('{"k":"weird","t":1795680001,"m":"x"}\nnot json\n{"k":"c","m":"y"}\n\n[1,2]\n')
        tape = R.read_tape(self.dir)
        s = tape.summary()
        self.assertEqual((s["coins"], s["unknown_kinds"], s["bad_lines"]), (2, {"weird": 1}, 3))
        self.assertEqual(s["files"], 2)
        late = R.read_tape(self.dir, since=START + 4300, until=START + 7200)       # the first hour file is skipped
        self.assertEqual((late.summary()["coins"], late.summary()["files"]), (1, 1))   # (10 minutes before since are read)
        self.assertEqual(list(late.created), [mint(2)])

    def test_runs_coverage_and_the_live_bots_trades(self):
        m = mint(1)
        recs = [{"k": "start", "t": START, "v": 1, "cfg": {"np_watch_s": 180, "np_min_sol": 2.5}},
                {"k": "hb", "t": START + 10, "sol": 200.0},
                {"k": "create", "t": START + 20, "m": m, "symbol": "A", "name": "A", "creator": "d", "bondingCurveKey": "cv",
                 "vSolInBondingCurve": 30.3, "vTokensInBondingCurve": K / 30.3, "pool": "pump"},
                {"k": "c", "t": START + 21, "m": m, "s": 5, "vs": int(31e9), "vt": int(K / 31 * 1e6), "rs": int(1e9)},
                {"k": "buy", "t": START + 30, "m": m, "usd": 10.0, "tier": "scalp", "sol": 201.0},
                {"k": "sell", "t": START + 60, "m": m, "final": True, "pnl": -1.2, "pnl_pct": -12.0, "why": "stop -8%",
                 "fill": "quote", "sol": 202.0},
                {"k": "follow", "t": START + 200, "m": m, "until": START + 920, "why": "traded"},
                {"k": "end", "t": START + 500, "m": m, "why": "follow"},
                {"k": "stop", "t": START + 3000},
                {"k": "start", "t": START + 3200, "v": 1, "cfg": {}},
                {"k": "hb", "t": START + 3300, "sol": 199.0}]
        tape = R.tape_of(recs)
        self.assertEqual(tape.runs, [(START, START + 3000), (START + 3200, START + 3310)])
        self.assertEqual(tape.cov[m], START + 500)                      # its "end" line
        self.assertTrue(tape.covered(m, START + 499) and not tape.covered(m, START + 501))
        self.assertEqual(tape.sol_at(START + 45), 201.0)                # the buy's SOL price, at or before
        self.assertEqual(tape.sol_at(START - 100), 200.0)               # before the first: the first one
        lt = tape.live_trades()
        self.assertEqual([(t["pnl"], t["why"], t["tier"]) for t in lt], [(-1.2, "stop -8%", "scalp")])
        self.assertEqual(R.jobs_for(tape, START + 100, START + 3250), [(START + 100, START + 3000), (START + 3200, START + 3250)])
        chain = R.jobs_for(tape, START, START + 30000, job_min=340, job_gap_s=120, align="chain")
        self.assertEqual(chain[:2], [(START, START + 20400), (START + 20520, START + 30000)])
        self.assertEqual(R.jobs_for(tape, START, START + 600, job_min=0, align="chain"), [(START, START + 600)])
        # a coin of a run that died (no "end" line): covered until the run's last heartbeat
        tape2 = R.tape_of(recs[:4] + [{"k": "hb", "t": START + 400}])
        self.assertEqual(tape2.cov[m], START + 410)


class Market(unittest.TestCase):
    def setUp(self):
        self.m = mint(7)
        recs = R.synth_coin(self.m, START, [1.0, 2.0, 30.0], dev_sol=0.3) + [{"k": "hb", "t": START, "sol": 200.0}]
        self.tape = R.tape_of(recs)
        self.clock = R.SimClock(START)
        self.market = R.ReplayMarket(self.tape, self.clock, START + 3600, {"curve_fee_pct": 1.25})

    def test_the_fill_model_is_the_engines_curve_maths_on_the_last_reading_at_or_before_the_fill(self):
        c = self.clock
        c.set(START + 1.69)                                             # after the 1st reading (t+1.0), before the 2nd (t+1.7)
        q = self.market.quote(bot.SOL, self.m, 0.05e9)
        _, vsol, vtok = self.tape.state_at(self.m, c.t)
        self.assertAlmostEqual(vsol, 31.0, places=6)
        self.assertEqual(int(q["outAmount"]), int(bot.curve_buy_tokens(vsol, vtok, 0.05, 1.25) * 1e6))
        s = self.market.quote(self.m, bot.SOL, 1e6 * 1e6)
        self.assertEqual(int(s["outAmount"]), int(bot.curve_sell_sol(vsol, vtok, 1e6, 1.25) * 1e9))
        # the future spike (30 SOL in at t+2.4) is not seen before its time ...
        c.set(START + 2.39)
        self.assertEqual(self.market.quote(self.m, bot.SOL, 1e12)["outAmount"], self.market.quote(self.m, bot.SOL, 1e12)["outAmount"])
        before = int(self.market.quote(self.m, bot.SOL, 1e12)["outAmount"])
        c.set(START + 2.4)
        after = int(self.market.quote(self.m, bot.SOL, 1e12)["outAmount"])
        self.assertGreater(after, before * 1.5)                         # ... and is, at its time
        # before the first reading: the create's own state; no coin: no route
        c2 = R.SimClock(START + 0.5)
        m2 = R.ReplayMarket(self.tape, c2, START + 3600, {"curve_fee_pct": 1.25})
        self.assertEqual(int(m2.quote(bot.SOL, self.m, 0.05e9)["outAmount"]),
                         int(bot.curve_buy_tokens(30.3, K / 30.3, 0.05, 1.25) * 1e6))
        self.assertIsNone(m2.quote(bot.SOL, "nothere", 0.05e9))
        self.assertEqual(m2.quote_why, "no Jupiter route")
        self.assertEqual(m2.sol_price(), 200.0)
        self.assertEqual(m2.prices(["x"]), {bot.SOL: {"usd": 200.0}})   # no Jupiter price of a coin on the tape

    def test_the_horizon_caps_what_the_market_knows(self):
        m = R.ReplayMarket(self.tape, self.clock, START + 2.0, {"curve_fee_pct": 1.25})
        self.clock.set(START + 10)                                      # the clock ran past the end of the part
        _, vsol, vtok = self.tape.state_at(self.m, START + 2.0)
        self.assertEqual(int(m.quote(self.m, bot.SOL, 1e12)["outAmount"]), int(bot.curve_sell_sol(vsol, vtok, 1e6, 1.25) * 1e9))

    def test_rugcheck_answers_only_with_what_was_recorded_and_never_before_it_existed(self):
        t_rep = self.tape.rugs[self.m][0][0]                            # START + 2.0
        land, rep = self.market.rug_answer(self.m, t_rep - 3.0)         # asked 3 s early: it lands when it existed
        self.assertEqual((land, rep["mint"]), (t_rep, self.m))
        land, rep = self.market.rug_answer(self.m, t_rep - 10.0)        # too early for RugCheck's 6 s: no report
        self.assertEqual((land, rep), (t_rep - 10.0 + 0.4, None))
        land, rep = self.market.rug_answer(self.m, t_rep + 1.0)         # later: its latency
        self.assertAlmostEqual(land, t_rep + 1.4)
        self.assertEqual(rep["totalHolders"], 14)
        land, rep = self.market.rug_answer("nothere", START)            # none recorded: none, never invented
        self.assertIsNone(rep)
        self.assertIn("nothere", self.market.rug_missing)


    def test_an_ask_at_the_live_bots_moment_gets_the_live_answer_failures_too(self):
        m, t = mint(8), START + 100.0
        rows = [{"k": "rug", "t": t + 0.43, "m": m, "ok": False, "why": "prefetch", "ms": 430},      # asked at t
                {"k": "rug", "t": t + 5.38, "m": m, "ok": True, "why": "fire", "ms": 380, "holders": 9, "top1": 3.0,
                 "top10": 3.0, "top": [3.0], "risks": [], "insiders": 0},                            # asked at t + 5
                {"k": "rug", "t": t + 40.2, "m": m, "ok": True, "why": "prefetch", "ms": 200, "holders": 30, "top1": 15.0,
                 "top10": 15.0, "top": [15.0], "risks": [], "insiders": 0}]                          # asked at t + 40
        tape = R.tape_of(R.synth_coin(m, START, [1.0], rug=None) + rows)
        market = R.ReplayMarket(tape, R.SimClock(t), START + 3600, {}, rug_latency_s=0.4)
        self.assertEqual(market.rug_answer(m, t), (t + 0.43, None))                   # live's failure, when it came
        land, rep = market.rug_answer(m, t + 5.0)
        self.assertEqual((land, rep["totalHolders"]), (t + 5.38, 9))                   # live's answer, its latency
        land, rep = market.rug_answer(m, t + 20.0)                                      # a moment live never asked
        self.assertEqual((land, rep["totalHolders"]), (t + 20.4, 9))                   # the latest report, 0.4 s later
        land, rep = market.rug_answer(m, t + 40.0)
        self.assertEqual((round(land, 6), rep["totalHolders"]), (round(t + 40.2, 6), 30))   # not the older report
        self.assertEqual((market.stats["rug_same_ask"], market.stats["rug_failed"]), (3, 1))


class Feeds(unittest.TestCase):
    def test_a_failed_uri_and_rugchecks_links_replay_the_way_live_took_them(self):
        # live: the uri failed at +2 s, RugCheck was asked for its copy of the metadata, and the loop took the links
        # from that report at +2.43 s ("meta" with src "rugcheck" and the "rug" line, the same turn)
        m = mint(9)
        recs = R.synth_coin(m, START, [1.0, 2.0], meta=False, rug=None) + [
            {"k": "meta", "t": START + 2.0, "m": m, "ok": False, "x": "", "tg": "", "web": "", "xk": "", "src": "", "ms": 2000},
            {"k": "rug", "t": START + 2.43, "m": m, "ok": True, "why": "meta", "ms": 430, "holders": 9, "top1": 3.0,
             "top10": 3.0, "top": [3.0], "risks": [], "insiders": 0},
            {"k": "meta", "t": START + 2.43, "m": m, "ok": True, "x": "", "tg": "https://t.me/" + m, "web": "", "xk": "",
             "src": "rugcheck", "ms": None}]
        tape = R.tape_of(recs)
        rep = tape.rugs[m][0][1]
        self.assertEqual(bot.Night.np_rug_meta(None, rep)["telegram"], "https://t.me/" + m)    # the engine's own read
        clock = R.SimClock(START)
        ctx = R.Context(tape, clock, R.ReplayMarket(tape, clock, START + 60, {}))
        feeds = R.ReplayFeeds(ctx, START, START + 60)
        feeds.fetch_meta(m, "")
        got = []
        while clock.t < START + 3:
            ev = feeds.get(0.25)
            if ev is not None and ev["kind"] == "meta":
                got.append((round(ev["t"] - START, 3), ev["ok"], ev.get("telegram")))
        self.assertEqual(got, [(2.0, False, "")])                       # the uri's failure, like live: no links yet
        self.assertEqual(ctx.market.rug_answer(m, START + 2.0)[0], START + 2.43)

    def test_events_come_at_their_time_curves_only_while_watched_and_metadata_when_asked(self):
        a, b = mint(1), mint(2)
        recs = R.synth_coin(a, START + 5, [1.0, 2.0, 3.0]) + R.synth_coin(b, START + 6, [1.5, 2.5]) + \
            [{"k": "hb", "t": START, "sol": 200.0}]
        tape = R.tape_of(recs)
        clock = R.SimClock(START)
        ctx = R.Context(tape, clock, R.ReplayMarket(tape, clock, START + 60, {}))
        feeds = R.ReplayFeeds(ctx, START, START + 60)
        got = []
        while clock.t < START + 20:
            ev = feeds.get(0.25)
            if ev is None:
                continue
            self.assertLessEqual(ev["t"], clock.t + 1e-6)               # never before its time
            got.append((round(clock.t - START, 3), ev["kind"], ev["mint"]))
            if ev["kind"] == "create" and ev["mint"] == a:
                self.assertEqual(ev["_rx"], START + 5)
                feeds.watch(a, ev["bondingCurveKey"])                   # b is never watched
                feeds.fetch_meta(a, "")
        self.assertEqual(got, [(5.0, "create", a), (5.4, "meta", a), (6.0, "curve", a), (6.0, "create", b),
                               (6.7, "curve", a), (7.4, "curve", a)])         # a tie keeps the tape's line order
        self.assertEqual(feeds.drain(), [])
        # no metadata on the tape: a failed read 2 s later (the engine then asks RugCheck), never made-up links
        asked = clock.t
        feeds.fetch_meta("nometa", "")
        ev = feeds.get(5.0)
        self.assertEqual((ev["kind"], ev["ok"], ev["twitter"], round(clock.t - asked, 3)), ("meta", False, "", 2.0))

    def test_a_rugcheck_answer_wakes_the_loop_at_its_time(self):
        m = mint(3)
        tape = R.tape_of(R.synth_coin(m, START, [1.0]))
        clock = R.SimClock(START + 1.0)
        ctx = R.Context(tape, clock, R.ReplayMarket(tape, clock, START + 60, {}, rug_latency_s=0.4))
        feeds = R.ReplayFeeds(ctx, START + 1.0, START + 60)
        pool = R.SimPool(ctx)
        fut = pool.submit(ctx.market.rug_report_np, m)                  # recorded at START + 2.0
        fut.add_done_callback(lambda f: feeds.q.append({"kind": "rug", "mint": m}))
        self.assertFalse(fut.done())
        ev = feeds.get(5.0)
        self.assertEqual((ev["kind"], clock.t), ("rug", START + 2.0))
        self.assertEqual(fut.result(timeout=0)["mint"], m)
        quick = R.SimPool(ctx).submit(lambda: 42)                       # anything else: answered at once
        self.assertEqual((quick.done(), quick.result()), (True, 42))


class Replays(unittest.TestCase):
    def test_a_trade_through_the_real_engine_is_priced_by_the_curve_maths_with_every_cost(self):
        m = mint(1)
        jump = gain_rsol(30.0)                                          # the reading after the trigger: +30 %
        path = R.path_to(0.3, 3.3, 0.5) + [jump, jump + 0.2]
        tape = R.tape_of(R.synth_tape(START, [(5, m, path, {})], minutes=10))
        res = R.replay(tape, base_cfg(), keep_log=True)
        self.assertEqual(len(res["trades"]), 1, res["log"])
        tr = res["trades"][0]
        self.assertEqual((tr["why"], tr["tier"], tr["blind"]), ("TP +%g%%" % first_step(base_cfg()), "scalp", False))
        self.assertTrue(any("curve maths 0.0% off Jupiter" in line for line in res["log"]))
        # by hand: $10 a trade (10 % of $100), 0.001 SOL of fees an order at SOL $200, pump.fun's 1.25 % each way,
        # the 1 % haircut each way, on the trigger reading (3.3 SOL in) and the next one (+30 %)
        t_in = START + 5 + 1.0 + 5 * 0.7
        _, vs_in, vt_in = tape.state_at(m, t_in)
        _, vs_out, vt_out = tape.state_at(m, t_in + 0.7)
        sol_in = (10.0 - 0.001 * 200) / 200.0
        raw = int(int(bot.curve_buy_tokens(vs_in, vt_in, sol_in, 1.25) * 1e6) * 0.99)
        got = int(bot.curve_sell_sol(vs_out, vt_out, raw / 1e6, 1.25) * 1e9) / 1e9 * 200 * 0.99 - 0.001 * 200
        self.assertAlmostEqual(tr["cost"], 10.0, places=2)
        self.assertAlmostEqual(tr["proceeds"], round(got, 2), places=2)
        self.assertLess(tr["pnl_pct"], 30.0 - 6.0)                      # +30 % on the curve nets about +21 %

    def test_no_look_ahead_the_same_tape_cut_later_gives_the_same_trades_before_the_cut(self):
        a, b = mint(1, "a"), mint(2, "b")
        flat = [3.3 + (0.05 if k % 2 else 0.0) for k in range(60)]      # held, going nowhere, for ~40 s
        coins = [(5, a, R.path_to(0.3, 3.3, 0.5) + flat + [gain_rsol(150.0)], {}),      # the future spike, ~50 s in
                 (12, b, R.path_to(0.3, 3.3, 0.5) + [gain_rsol(-9.0)], {})]           # a stop inside the cut
        recs = R.synth_tape(START, coins, minutes=10, fmt="generic")
        cut = START + 50.0
        self.assertTrue(any(r["t"] > cut and r.get("m") == a for r in recs))
        full = R.replay(R.tape_of(recs), base_cfg(), START, START + 600, job_min=0, align="chain", keep_log=True)
        short = R.replay(R.tape_of([r for r in recs if r["t"] <= cut]), base_cfg(), START, START + 600, job_min=0,
                         align="chain", keep_log=True)
        before = lambda res: [x for x in res["trades"] if R.ts_epoch(x["closed"]) < cut]
        self.assertEqual(before(full), before(short))
        self.assertEqual([x["mint"] for x in before(full)], [b])
        hhmmss = (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=cut + 3600)).strftime("%H:%M:%S")
        lines = lambda res: [x for x in res["log"] if x[:8] < hhmmss]
        self.assertEqual(lines(full), lines(short))                     # every decision before the cut, word for word
        self.assertTrue(any("ENTER A0001" in x for x in lines(full)))   # the coin was bought before its spike ...
        spike = [x for x in full["trades"] if x["mint"] == a][0]
        self.assertGreater(spike["pnl_pct"], 50)                        # ... and sold on it, after the cut
        quiet = [x for x in short["trades"] if x["mint"] == a][0]
        self.assertLess(quiet["pnl_pct"], 0)                            # the cut tape never saw it

    def test_a_coin_held_past_its_recorded_span_goes_stale_and_its_exit_is_blind(self):
        m = mint(4)
        path = R.path_to(0.3, 3.3, 0.5) + [3.4, 3.5]                    # bought, then the tape stops at ~7 s
        recs = R.synth_tape(START, [(5, m, path, {})], minutes=15, fmt="generic")
        tape = R.tape_of(recs)
        self.assertEqual(tape.cov[m], START + 5 + R.TAPE_WATCH_S)        # generic tape: the bot's 180 s window
        res = R.replay(tape, base_cfg(), START, START + 900, job_min=0, align="chain", keep_log=True)
        tr = res["trades"][0]
        self.assertTrue(tr["blind"], tr)
        self.assertTrue(any("no curve reading for 5 s" in x for x in res["log"]))
        self.assertEqual(R.metrics(res["trades"])["blind"], 1)

    def test_jobs_end_flat_and_the_replay_is_deterministic(self):
        coins = [(30 * i + 5, mint(i), R.pump_path(peak_gain=40), {}) for i in range(20)]
        tape = R.tape_of(R.synth_tape(START, coins, minutes=12, fmt="generic"))
        one = R.replay(tape, base_cfg(), START, START + 720, job_min=4, job_gap_s=30, align="chain")
        two = R.replay(tape, base_cfg(), START, START + 720, job_min=4, job_gap_s=30, align="chain")
        self.assertEqual(one["trades"], two["trades"])
        self.assertEqual((one["stats"]["jobs"], one["open"]), (3, []))
        opened = [R.ts_epoch(t["opened"]) - START for t in one["trades"]]
        # no entries in the last 5 minutes of a 4-minute job: none at all; jobs of 12 minutes buy in their first 7
        self.assertEqual(opened, [])
        long = R.replay(tape, base_cfg(), START, START + 720, job_min=0, align="chain")
        self.assertTrue(long["trades"])
        self.assertTrue(all(R.ts_epoch(t["opened"]) < START + 420 + 1 for t in long["trades"]))


class Grid(unittest.TestCase):
    def test_the_default_grid(self):
        base = bot.load_config(bot.CONFIG)
        g = bt.build_grid("default", base, 200, seed=7)
        self.assertEqual(g[0], {})
        self.assertLessEqual(len(g), 200)
        self.assertEqual(len({json.dumps(x, sort_keys=True) for x in g}), len(g))
        self.assertEqual(g, bt.build_grid("default", base, 200, seed=7))  # the same grid every run
        one = [x for x in g if len(x) == 1]
        self.assertEqual(len(one), sum(len(v) for v in bt.DEFAULT_GRID.values()))      # every lever alone first
        for lv in g:
            c = bt.apply(base, lv)
            ts = bot.holdplan.tiers_from_cfg(c)
            self.assertEqual([t["name"] for t in ts], ["scalp", "swing", "runner", "moon"])
            for t in ts:
                self.assertEqual(t["ladder"], sorted(t["ladder"]))
                self.assertTrue(all(g_ > 0 and 0 < f <= 1 for g_, f in t["ladder"]))
        self.assertEqual(len(bt.build_grid("default", base, 30)), 30)

    def test_the_pseudo_levers(self):
        base = bot.load_config(bot.CONFIG)
        t = lambda c, name: next(x for x in bot.holdplan.tiers_from_cfg(c) if x["name"] == name)
        c = bt.apply(base, {"tp_x": 2.0, "trail_x": 1.5, "time_stop_x": 0.5})
        self.assertEqual(t(c, "runner")["ladder"], [[g * 2.0, f] for g, f in t(base, "runner")["ladder"]])
        self.assertEqual(t(base, "runner")["ladder"][1:], [[20.0, 0.5], [60.0, 1.0]])     # x2: 40 and 120
        self.assertEqual((t(c, "swing")["trail_pct"], t(c, "moon")["time_stop_min"]),
                         (t(base, "swing")["trail_pct"] * 1.5, t(base, "moon")["time_stop_min"] * 0.5))
        self.assertEqual(t(bt.apply(base, {"scalp_tp": 15.0}), "scalp")["ladder"], [[15.0, 1.0]])
        sc = t(bt.apply(base, {"scalp_frac": 0.5}), "scalp")
        tp = first_step(base)
        self.assertEqual((sc["ladder"], sc["trail_pct"]), ([[tp, 0.5], [max(20.0, 3 * tp), 1.0]], t(base, "swing")["trail_pct"]))
        sw = t(bt.apply(base, {"swing_tp": 10.0, "swing_frac": 0.25}), "swing")
        self.assertEqual(sw["ladder"], [[10.0, 0.25], [25.0, 1.0]])
        c = bt.apply(base, {"np_min_sol": 4.0, "stop_pct": -10.0})
        self.assertEqual(bt.config_diff(base, c), {"np_min_sol": 4.0, "stop_pct": -10.0})
        self.assertEqual(set(bt.config_diff(base, bt.apply(base, {"tp_x": 1.5}))), {"hold_tiers"})
        self.assertEqual(bt.config_diff(base, bt.apply(base, {})), {})

    def test_a_grid_file(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "g.json")
        with open(p, "w") as fh:
            json.dump({"combos": [{"np_min_sol": 3.0}, {"stop_pct": -10.0}, {"np_min_sol": 3.0}]}, fh)
        self.assertEqual(bt.build_grid(p, {}), [{}, {"np_min_sol": 3.0}, {"stop_pct": -10.0}])
        with open(p, "w") as fh:
            json.dump({"levers": {"np_min_sol": [2.5, 3.0]}, "random": False}, fh)
        self.assertEqual(bt.build_grid(p, {"np_min_sol": 2.5}), [{}, {"np_min_sol": 3.0}])


def pump_coins(n, at0, every, peak, kind="p", **kw):
    """n coins every `every` s from at0: the default trigger, then up to peak % and a slow bleed (no dump)."""
    return [(at0 + every * i, mint(i, kind), R.pump_path(peak_gain=peak, **kw), {}) for i in range(n)]


class WalkForward(unittest.TestCase):
    def test_a_known_rule_wins_a_pump_that_pays_only_with_a_trailing_exit(self):
        # 2 h of coins that run +60 % after the trigger and bleed back: the scalp's first step nets a few % after
        # the costs (the -0.4 SOL steps of the bleed are no dump); the trailing rest catches most of the run
        coins = pump_coins(140, 5, 50, 60)
        tape = R.tape_of(R.synth_tape(START, coins, minutes=120))
        trail = {"hold_tiers": trailing_tiers()}
        rep = bt.backtest(tape=tape, grid={"combos": [trail, {"scalp_tp": 3.0}]}, cfg=base_cfg(), jobs=1, min_trades=30)
        base, top = rep["base"], rep["top"]
        self.assertEqual(top[0]["levers"], trail)                       # ranked first on the first part
        self.assertGreater(top[0]["first_part"]["total_pnl"], base["first_part"]["total_pnl"] + 20)
        self.assertGreaterEqual(top[0]["held_out"]["trades"], 30)
        self.assertGreater(top[0]["held_out"]["mean_pnl"], 0)
        self.assertGreater(top[0]["held_out"]["by_exit"]["trailing stop"]["trades"], 25)
        self.assertEqual(rep["decision"]["action"], "change", rep["decision"])
        self.assertEqual(rep["decision"]["config_diff"], {"hold_tiers": trailing_tiers()})
        self.assertIn("DECISION: CHANGE", "\n".join(rep["lines"]))
        self.assertEqual(json.loads(json.dumps(R.json_safe(rep), allow_nan=False))["decision"]["action"], "change")

    def test_the_held_out_part_stops_a_first_part_winner_that_does_not_hold_up(self):
        # first part: coins run +60 %; the held-out rest: they stop at +20 % and bleed. "Sell everything at +30 %"
        # wins the first part and never takes a profit after the split: keep.
        minutes = 120
        split_t = 0.7 * minutes * 60
        first = pump_coins(int((split_t - 60) // 50), 5, 50, 60)
        rest = pump_coins(int((minutes * 60 - split_t - 30) // 50), split_t + 5, 50, 20, kind="q")
        tape = R.tape_of(R.synth_tape(START, first + rest, minutes=minutes))
        rep = bt.backtest(tape=tape, grid={"combos": [{"scalp_tp": 30.0}]}, cfg=base_cfg(), jobs=1, min_trades=20)
        self.assertEqual(rep["range"]["split_at"], R.iso(START + split_t))
        c = rep["top"][0]
        self.assertEqual(c["levers"], {"scalp_tp": 30.0})
        self.assertGreater(c["first_part"]["total_pnl"], rep["base"]["first_part"]["total_pnl"])
        self.assertEqual(c["first_part"]["win_rate"], 1.0)              # the first part only saw the +60 % coins
        self.assertLess(c["held_out"]["mean_pnl"], 0)                   # the held-out part only the +20 % ones
        self.assertFalse(c["passes"])
        self.assertEqual(rep["decision"]["action"], "keep")
        self.assertIn("held-out mean", rep["decision"]["why"])

    def test_keep_when_the_data_is_thin(self):
        tape = R.tape_of(R.synth_tape(START, pump_coins(12, 5, 50, 60), minutes=12))
        rep = bt.backtest(tape=tape, grid={"combos": [{"hold_tiers": trailing_tiers()}]}, cfg=base_cfg(), jobs=1)
        self.assertEqual(rep["decision"]["action"], "keep")
        self.assertIn("no config made 30 trades on the first part", rep["decision"]["why"])
        empty = bt.backtest(tape=R.tape_of([]), cfg=base_cfg(), jobs=1)
        self.assertEqual(empty["decision"]["action"], "keep")

    def test_the_first_part_never_sees_the_held_out_part(self):
        coins = pump_coins(40, 5, 30, 60)
        tape = R.tape_of(R.synth_tape(START, coins, minutes=20))
        split = START + 600
        res = R.replay(tape, base_cfg(), START, split)
        self.assertLessEqual(res["stats"]["max_event_t"], split)
        self.assertTrue(all(R.ts_epoch(t["closed"]) <= split + 1 for t in res["trades"]))


class ImmediatePool(object):
    """A RugCheck / SOL-price pool that answers at once (a live run whose RugCheck never lags: deterministic)."""

    def submit(self, fn, *args):
        fut = concurrent.futures.Future()
        try:
            fut.set_result(fn(*args))
        except Exception as e:                  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def shutdown(self, wait=True, cancel_futures=False):
        pass


@unittest.skipIf(bot.recorder is None, "memebot/recorder.py did not load")
class RoundTrip(unittest.TestCase):
    """The contract between the recorder and the replay: the real engine records a tape while it trades on a
    scripted feed (the engine tests' FakeFeeds and CurveMarket), and the replay of that tape, same config, makes
    the very same trades."""

    def test_the_replay_of_a_recorded_run_makes_the_same_trades(self):
        eng = load("tests", "test_newpairs_engine")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        start = datetime.datetime(2026, 11, 26, 8, 0)
        old_t0, eng.T0 = eng.T0, start
        self.addCleanup(setattr, eng, "T0", old_t0)
        clock = eng.Clock(start)
        market = eng.CurveMarket()
        feeds = eng.FakeFeeds(clock, market)
        cfg = base_cfg(live_enabled=False, rec_on=True, feed_wait_sec=1.0)    # 1 s loop waits: the same trades, faster
        slot = 5000
        for i in range(24):
            m = mint(i, "rt")
            path = [R.pump_path(peak_gain=60), R.pump_path(peak_gain=5, down=0.3), R.pump_path(peak_gain=25),
                    R.path_to(0.3, 1.5, 0.3) + R.path_to(1.5, 0.4, 0.3)][i % 4]
            at = 10 + 70 * i
            market.reports[m] = eng.rug_report(m, risks=[{"name": "Creator history of rugged tokens", "level": "warn"}]
                                               if i == 6 else ())
            feeds.at(at, eng.create(m, "RT%d" % i))
            feeds.at(at + 0.4, eng.meta(m, x=i % 3 == 0, tg=True, web=i % 2 == 0))
            for k, rsol in enumerate(path):
                slot += 1
                feeds.at(at + 1.0 + 0.7 * k, eng.reading(m, rsol, slot))

        class LiveNight(bot.Night):
            def __init__(self, *a, **kw):
                super(LiveNight, self).__init__(*a, **kw)
                self.rug_pool = ImmediatePool()
                self.sol_pool = ImmediatePool()

        env = dict(os.environ)
        env.pop("MEMEBOT_REC", None)
        with unittest.mock.patch.dict(os.environ, env, clear=True), R.engine(LiveNight):
            bot.run(d, cfg, minutes=32, anytime=False, market=market, now_fn=clock.now, sleep=clock.advance,
                    out=lambda *a: None, live=None, feeds=feeds)
        live = (bot.read_json(os.path.join(d, "state.json"), {}).get("night") or {}).get("trades") or []
        self.assertGreaterEqual(len(live), 12)
        self.assertTrue(os.listdir(os.path.join(d, "tapes")))
        tape = R.read_tape(d)
        s = tape.summary()
        self.assertEqual((s["coins"], s["bad_lines"], s["unknown_kinds"], len(tape.runs)), (24, 0, {}, 1))
        res = R.replay(tape, cfg, rug_latency_s=0.0)
        key = lambda t: (t["mint"], t["opened"], t["closed"], t["why"], t["pnl"], t.get("tier"))
        self.assertEqual(sorted(key(t) for t in res["trades"]), sorted(key(t) for t in live))
        # and the tape's own buy / sell lines say the same as the live book
        self.assertEqual(sorted((t["mint"], round(t["pnl"], 2)) for t in tape.live_trades()),
                         sorted((t["mint"], t["pnl"]) for t in live))


class CommandLine(unittest.TestCase):
    def test_the_cli_end_to_end_on_a_tiny_tape(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        py = sys.executable
        r = subprocess.run([py, os.path.join(ROOT, "memebot", "replay.py"), "--synth", d, "--coins", "40", "--seed", "3"],
                           capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(d, "tapes", "2026-11-26T08.jsonl"), "a") as fh:
            fh.write('{"k":"brand-new-kind","t":1795680100}\n{broken\n')
        grid = os.path.join(d, "grid.json")
        with open(grid, "w") as fh:
            json.dump({"combos": [{"np_min_sol": 3.0}, {"stop_pct": -10.0}, {"tp_x": 1.5}]}, fh)
        out = os.path.join(d, "report.json")
        r = subprocess.run([py, os.path.join(ROOT, "memebot", "backtest.py"), "--data", d, "--grid", grid, "--split", "0.6",
                            "--out", out, "--jobs", "2", "--min-trades", "5"],
                           capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("DECISION:", r.stdout)
        with open(out) as fh:
            rep = json.load(fh)
        self.assertIn(rep["decision"]["action"], ("keep", "change"))
        self.assertEqual(rep["tape"]["unknown_kinds"], {"brand-new-kind": 1})
        self.assertEqual(rep["tape"]["bad_lines"], 1)
        self.assertEqual(rep["grid"]["configs"], 4)
        self.assertEqual(len(rep["top"]), min(3, rep["grid"]["ranked"]))
        for key in ("trades", "win_rate", "total_pnl", "mean_pnl", "mean_pnl_pct", "max_drawdown", "profit_factor",
                    "by_tier", "by_exit", "blind"):
            self.assertIn(key, rep["base"]["first_part"])
        self.assertTrue(rep["assumptions"])
        r = subprocess.run([py, os.path.join(ROOT, "memebot", "replay.py"), "--data", d, "--out", os.path.join(d, "one.json")],
                           capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("replay:", r.stdout)
        for root, dirs, _ in os.walk(os.path.join(ROOT, "memebot")):
            self.assertNotIn("memebot-replay", " ".join(dirs))


if __name__ == "__main__":
    unittest.main()
