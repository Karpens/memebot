"""memebot/recorder.py, the tape recorder for the backtester, and its hooks in memebot/bot.py: the tape's lines,
one file per UTC hour, the size cap, old tapes deleted, the loader (a truncated last line, since / until), the
writer thread (a slow or stuck disk never holds the loop), coins near a trigger followed after the bot's own
watch window (the cap, the bot's held coins first), and a recorder error that never reaches the trading."""

import datetime
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from _load import load

import test_newpairs_engine as eng      # the engine's FakeFeeds and CurveMarket (a module, so its tests don't run twice)

mb = eng.mb
rec_mod = load("memebot", "recorder")
np_mod = load("memebot", "newpairs")
K = eng.K
EPOCH = datetime.datetime(1970, 1, 1)
T0 = (eng.T0 - EPOCH).total_seconds()  # 2026-11-25 22:30 UTC


class Clock(object):
    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self):
        return self.t


def curve(mint, rsol, slot, t, complete=False):
    vsol = 30.0 + rsol
    return {"kind": "curve", "mint": mint, "key": "curve-" + mint, "price_sol": vsol / (K / vsol), "vsol": int(vsol * 1e9),
            "vtok": int(K / vsol * 1e6), "rtok": 0, "rsol": int(rsol * 1e9), "complete": complete, "slot": slot, "t": t,
            "src": "poll"}


def create(mint, t, **kw):
    ev = eng.create(mint, mint.upper(), **kw)
    ev.update(_rx=t, t=t)
    return ev


def wait_for(fn, sec=5.0):
    end = time.time() + sec
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.005)
    return fn()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.tapes = os.path.join(self.dir, "tapes")
        self.said = []

    def rec(self, clock, write=None, **cfg):
        r = rec_mod.Recorder(self.tapes, cfg, clock=clock, log=self.said.append, write=write)
        self.addCleanup(r.close, 5)
        return r

    def lines(self):
        out = []
        for _, path in rec_mod.tape_files(self.tapes):
            out += rec_mod.read_lines(path)
        return out


class Files(Base):
    def test_one_file_per_utc_hour_and_the_loader_puts_a_coin_back_together(self):
        clock = Clock(T0 + 1800 - 2)                                    # 22:59:58 UTC
        r = self.rec(clock)
        r.event(create("aaa", clock.t))
        for k in range(4):
            r.event(curve("aaa", 0.5 + k, 100 + k, clock.t))
            clock.t += 1
            r.tick(clock.t)
        self.assertTrue(r.close(5))
        names = [os.path.basename(p) for _, p in rec_mod.tape_files(self.tapes)]
        self.assertEqual(names, ["2026-11-25T22.jsonl", "2026-11-25T23.jsonl"])
        first = rec_mod.read_lines(os.path.join(self.tapes, names[0]))
        self.assertEqual([x["k"] for x in first][:2], ["start", "create"])
        self.assertEqual(first[0]["v"], 1)
        self.assertEqual(first[0]["cfg"]["rec_follow_s"], 900)
        tapes = rec_mod.load_tapes(self.tapes)
        tp = tapes["aaa"]
        self.assertEqual(tp["t0"], T0 + 1800 - 2)
        self.assertEqual(tp["create"]["bondingCurveKey"], "curve-aaa")
        self.assertEqual([x["slot"] for x in tp["readings"]], [100, 101, 102, 103])
        r0 = tp["readings"][0]
        self.assertAlmostEqual(r0["vsol"], 30.5, places=6)              # SOL, whole tokens: like newpairs.read_curve
        self.assertAlmostEqual(r0["rsol"], 0.5, places=6)
        self.assertAlmostEqual(r0["vtok"], K / 30.5, delta=1e-3)
        self.assertAlmostEqual(r0["price"], 30.5 ** 2 / K, delta=1e-15)
        self.assertEqual(tp["end"], T0 + 1800 + 2)                      # close(): "end" with why "stop"
        self.assertEqual(tp["events"][-1], {"k": "end", "m": "aaa", "t": T0 + 1800 + 2, "why": "stop"})
        hourly = [x for x in self.said if x.startswith("tape 2026-11-25T22 · ")]
        self.assertEqual(len(hourly), 1)                                # the hour's line in the log, when it is over
        self.assertIn("(2 curve readings)", hourly[0])
        kinds = [x["k"] for x in rec_mod.load_system(self.tapes)]
        self.assertEqual((kinds[0], kinds[-1]), ("start", "stop"))
        self.assertIn("hb", kinds)

    def test_an_hours_file_stops_taking_curve_readings_at_its_cap_and_says_so_once(self):
        clock = Clock()
        r = self.rec(clock, rec_max_mb_hour=0.02)                       # 20 kB
        r.event(create("aaa", clock.t))
        for k in range(400):                                            # ~100 bytes each: 40 kB
            r.event(curve("aaa", 0.01 * k, k, clock.t + k * 0.5))
        r.decide("aaa", "reject", "dump: 2.1 SOL sold")                 # a decision still goes on it
        self.assertTrue(r.close(5))
        path = rec_mod.tape_files(self.tapes)[0][1]
        self.assertLessEqual(os.path.getsize(path), 20000 * 1.05)
        got = self.lines()
        n = sum(1 for x in got if x["k"] == "c")
        self.assertTrue(150 < n < 220, n)
        self.assertEqual([x["why"] for x in got if x["k"] == "dec"], ["dump: 2.1 SOL sold"])
        capped = [s for s in self.said if "passed 0.02 MB" in s]
        self.assertEqual(len(capped), 1)
        self.assertIn("no more curve readings this hour", capped[0])
        self.assertGreater(r.info()["drop"], 150)

    def test_a_truncated_last_line_is_left_out_and_the_next_run_starts_on_a_clean_line(self):
        os.makedirs(self.tapes)
        path = os.path.join(self.tapes, "2026-11-25T22.jsonl")
        good = [{"k": "create", "m": "aaa", "t": T0, "symbol": "AAA"},
                {"k": "c", "m": "aaa", "t": T0 + 1, "s": 5, "vs": 31000000000, "vt": 1038387096774193, "rs": 1000000000}]
        with open(path, "w") as fh:
            fh.write("\n".join(json.dumps(x) for x in good) + "\n")
            fh.write('{"k":"c","m":"aaa","t":%s,"s":6,"vs":3200' % (T0 + 2))       # the run was killed here
        tp = rec_mod.load_tapes(self.tapes)["aaa"]
        self.assertEqual(len(tp["readings"]), 1)
        clock = Clock(T0 + 60)
        r = self.rec(clock)                                             # the next job, the same hour
        r.event(curve("aaa", 2.0, 7, clock.t))                          # a mint this run never saw created: not taken
        r.event(create("bbb", clock.t))
        r.event(curve("bbb", 1.0, 8, clock.t + 1))
        self.assertTrue(r.close(5))
        tapes = rec_mod.load_tapes(self.tapes)
        self.assertEqual(len(tapes["aaa"]["readings"]), 1)              # the cut line is gone, nothing else
        self.assertEqual([x["slot"] for x in tapes["bbb"]["readings"]], [8])
        self.assertEqual(tapes["bbb"]["create"]["symbol"], "BBB")
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertIn(b'"vs":3200\n{"k":"start"', raw)                 # a new line after the cut one

    def test_tapes_older_than_rec_keep_days_are_deleted_when_it_starts(self):
        os.makedirs(self.tapes)
        old = time.strftime("%Y-%m-%dT%H", time.gmtime(T0 - 8 * 86400))
        recent = time.strftime("%Y-%m-%dT%H", time.gmtime(T0 - 2 * 86400))
        for name in (old + ".jsonl", recent + ".jsonl", "notes.txt"):
            with open(os.path.join(self.tapes, name), "w") as fh:
                fh.write("{}\n")
        r = self.rec(Clock())
        r.event(create("aaa", T0))
        self.assertTrue(r.close(5))
        left = sorted(os.listdir(self.tapes))
        self.assertEqual(left, sorted([recent + ".jsonl", "notes.txt", "2026-11-25T22.jsonl"]))

    def test_since_and_until_pick_coins_by_their_create_and_keep_their_later_lines(self):
        clock = Clock()
        r = self.rec(clock)
        r.event(create("early", T0))
        r.event(create("late", T0 + 7200))
        r.event(curve("early", 1.0, 1, T0 + 3700))                      # an hour later: the next file
        self.assertTrue(r.close(5))
        self.assertEqual(list(rec_mod.load_tapes(self.tapes)), ["early", "late"])
        got = rec_mod.load_tapes(self.tapes, since=T0, until=T0 + 3600)
        self.assertEqual(list(got), ["early"])
        self.assertEqual(len(got["early"]["readings"]), 1)
        got = rec_mod.load_tapes(self.tapes, since=datetime.datetime(2026, 11, 25, 23, 0), until="2026-11-26T02")
        self.assertEqual(list(got), ["late"])

    def test_rug_summary_leaves_the_curve_and_pools_out_and_rug_report_judges_the_same(self):
        cfg = eng.np_cfg()
        rep = eng.rug_report("aaa", risks=[{"name": "Single holder ownership", "level": "warn"}], top=(13.0, 2.0))
        rep["markets"] = [{"pubkey": "pool-1", "liquidityA": "la", "liquidityB": "lb"}]
        rep["topHolders"].append({"address": "x", "owner": "pool-1", "pct": 30.0})
        coin = np_mod.Coin(mint="aaa", curve="curve-aaa")
        doc = rec_mod.rug_summary(rep, "curve-aaa", np_mod._curve_accounts(rep, coin))
        self.assertEqual((doc["top"], doc["top1"], doc["top10"], doc["holders"]), ([13.0, 2.0], 13.0, 15.0, 14))
        self.assertEqual(doc["risks"], [["Single holder ownership", "warn"]])
        line = dict(doc, k="rug", m="aaa", t=T0, ok=True)
        want = np_mod.rug_check_np(rep, np_mod.Coin(mint="aaa", curve="curve-aaa"), cfg)
        got = np_mod.rug_check_np(rec_mod.rug_report(line), np_mod.Coin(mint="aaa", curve="curve-aaa"), cfg)
        self.assertEqual(got, want)
        self.assertFalse(got[0])
        self.assertIsNone(rec_mod.rug_report({"k": "rug", "m": "aaa", "t": T0, "ok": False}))
        self.assertIsNone(rec_mod.rug_summary(None))


class Writer(Base):
    def test_a_slow_disk_never_holds_the_loop(self):
        def slow(path, data):
            time.sleep(0.3)
            rec_mod.append(path, data)
        clock = Clock()
        r = self.rec(clock, write=slow)
        r.event(create("aaa", clock.t))
        most = 0.0
        for k in range(600):                                            # 30 s of readings, 6 hand-offs
            clock.t += 0.05
            t = time.perf_counter()
            r.event(curve("aaa", 0.001 * k, k, clock.t))
            r.tick(clock.t)
            most = max(most, time.perf_counter() - t)
        self.assertLess(most, 0.05)                                     # the writer thread waits, the loop doesn't
        self.assertTrue(r.close(10))
        self.assertEqual(sum(1 for x in self.lines() if x["k"] == "c"), 600)

    def test_a_stuck_disk_bounds_the_queue_drops_and_says_so_once(self):
        gate = threading.Event()

        def stuck(path, data):
            gate.wait(10)
            rec_mod.append(path, data)
        clock = Clock()
        r = self.rec(clock, write=stuck, rec_max_queue_mb=0.01, rec_flush_s=0)
        r.event(create("aaa", clock.t))
        for k in range(1000):                                           # ~100 kB against a 10 kB queue
            clock.t += 0.1
            t = time.perf_counter()
            r.event(curve("aaa", 0.001 * k, k, clock.t))
            r.tick(clock.t)
            self.assertLess(time.perf_counter() - t, 0.05)
            self.assertLessEqual(r.q_bytes, 10000)
        self.assertGreater(r.info()["drop"], 500)
        self.assertEqual(sum(1 for s in self.said if "the disk is behind" in s), 1)
        gate.set()
        self.assertTrue(r.close(10))

    def test_a_write_error_is_said_once_and_nothing_raises(self):
        def broken(path, data):
            raise OSError(28, "No space left on device")
        clock = Clock()
        r = self.rec(clock, write=broken, rec_flush_s=0)
        r.event(create("aaa", clock.t))
        r.tick(clock.t)
        self.assertTrue(wait_for(lambda: r.werr is not None))
        for _ in range(3):
            clock.t += 1
            r.event(curve("aaa", 1.0, 1, clock.t))
            r.tick(clock.t)
        said = [s for s in self.said if s.startswith("tape writer:")]
        self.assertEqual(len(said), 1)
        self.assertIn("No space left on device", said[0])


class Hooks(unittest.TestCase):
    """The recorder inside the engine (test_newpairs_engine's harness, its helpers borrowed, not its tests)."""

    setUp = eng.Engine.setUp
    night = eng.Engine.night
    turn = eng.Engine.turn
    read = eng.Engine.read
    launch = eng.Engine.launch
    gain_rsol = eng.Engine.gain_rsol
    lines = eng.Engine.lines

    def tape(self, n, close=True):
        if close:
            n.np_close()
        else:
            self.assertTrue(n.rec.drain(5))
        return rec_mod.load_tapes(os.path.join(self.dir, "tapes"))

    def step(self, n, sec):
        """The clock moves sec, then the 1 s step (the watch list's expiry, the recorder's follow list)."""
        self.clock.advance(sec)
        n.now = self.clock.now()
        n.np_second({}, 400)

    def near(self, n, mint):
        """A coin half way to its trigger (np_cfg: 3 SOL, 8 buys -> 2 SOL in, 4 buys), never firing."""
        self.m.reports[mint] = eng.rug_report(mint)
        self.turn(n, eng.create(mint, mint.upper()))
        self.turn(n, eng.meta(mint, False, True, False), sec=0.3)
        for rsol in (0.8, 1.3, 1.8, 2.3):
            self.read(n, mint, rsol)

    # ---- what goes on the tape
    def test_a_trade_on_the_tape_create_readings_rugcheck_fire_buy_sell(self):
        n = self.night()
        self.launch(n, "aaa", eng.SCALP)
        self.read(n, "aaa", self.gain_rsol(n, "aaa", -9))               # the stop: sold on this reading
        trade = n.state["night"]["trades"][0]
        tp = self.tape(n)["aaa"]
        kinds = [e["k"] for e in tp["events"]]
        self.assertEqual(kinds, ["meta", "rug", "fire", "buy", "sell", "end"])
        self.assertEqual(tp["create"]["creator"], "dev-aaa")
        self.assertEqual(tp["create"]["solAmount"], 0.3)
        self.assertEqual(len(tp["readings"]), 9)
        self.assertEqual(tp["meta"]["tg"], "https://t.me/aaa")
        self.assertEqual((tp["rug"]["why"], tp["rug"]["top"], tp["rug"]["holders"]), ("prefetch", [3.0, 2.0, 1.5], 14))
        ev = {e["k"]: e for e in tp["events"]}
        self.assertTrue(ev["fire"]["why"].startswith("momentum · 4.0 SOL in · 8/8 buys"))
        buy, sell = ev["buy"], ev["sell"]
        self.assertAlmostEqual(buy["usd"], 10.0, places=2)
        self.assertEqual((buy["sol"], buy["tier"], buy["check"]), (100.0, "scalp", {"buy_pct": 0.0, "sell_pct": 0.0}))
        self.assertGreater(buy["raw"], 0)
        self.assertAlmostEqual(buy["tok"], buy["raw"] / 1e6, places=5)
        self.assertGreater(buy["px"] / buy["sol"], buy["px_sol"])       # the fill over the curve's price: the costs
        self.assertEqual((sell["why"], sell["final"], sell["fill"], sell["lat_ms"]), ("stop -9%", True, "quote", 0))
        self.assertAlmostEqual(sell["pnl"], trade["pnl"], places=1)
        self.assertAlmostEqual(sell["pnl_pct"], trade["pnl_pct"], places=0)
        self.assertLess(sell["pnl_pct"], -9)                            # the costs on top of a -9 % move, honestly
        self.assertEqual(tp["end"], ev["end"]["t"])

    def test_the_decisions_skip_reject_learner_and_risk_once(self):
        n = self.night(eng.np_cfg(max_open=0))
        self.turn(n, eng.create("scam", "SCAM", mayhem=True))
        self.launch(n, "dev", eng.SCALP, dev_sol=3.0)                   # 9.8 % of the supply
        self.launch(n, "full", eng.SCALP)                               # fires: RISK says no (max_open 0) ...
        self.read(n, "full", 4.8)
        self.read(n, "full", 5.1)                                       # ... on each reading: once on the tape
        n.cfg["max_open"] = 8
        labels = ["np:dev:0-2", "np:sol:3-6", "np:buys:90+", "np:age:<30s", "np:social:tg", "news:0-2", "tier:scalp",
                  "hour:23-01"]
        for _ in range(10):
            mb.learner.update(n.mem, labels, -15.0)
        self.launch(n, "lll", eng.SCALP)
        tapes = self.tape(n)
        dec = {m: [(e["d"], e["why"]) for e in tp["events"] if e["k"] == "dec"] for m, tp in tapes.items()}
        self.assertEqual(dec["scam"], [("skip", "mayhem mode")])
        self.assertEqual(dec["dev"], [("reject", "dev bought 9.8% (max 8%)")])
        self.assertEqual(dec["full"], [("risk", "0 positions open (max)")])
        self.assertEqual(dec["lll"], [("learner", "learner: skip")])
        self.assertEqual(tapes["scam"]["readings"], [])                 # never watched
        self.assertEqual([e["k"] for e in tapes["scam"]["events"]], ["dec", "end"])

    def test_a_restart_adopts_the_held_coin_on_the_tape(self):
        n = self.night()
        self.launch(n, "aaa", eng.SCALP)
        n.save()
        n.np_close()
        feeds2 = eng.FakeFeeds(self.clock, self.m)
        n2 = mb.Night(self.dir, eng.np_cfg(), self.m, self.clock.now(), self.out.append)
        n2.clock_fn = self.clock.now
        n2.begin("2026-11-25")
        n2.attach(feeds2)
        self.feeds = feeds2
        self.read(n2, "aaa", self.gain_rsol(n2, "aaa", -9))
        tp = self.tape(n2)["aaa"]
        self.assertEqual([e["k"] for e in tp["events"]][-3:], ["end", "sell", "end"])      # stop, the new run's sale
        self.assertEqual(len(tp["readings"]), 9)

    # ---- following
    def test_a_coin_near_its_trigger_stays_polled_after_the_watch_window_a_far_one_does_not(self):
        n = self.night()
        self.near(n, "near")
        self.turn(n, eng.create("far", "FAR"))
        self.read(n, "far", 0.4)
        t_near = n.wl.get("near").t0
        self.step(n, 182)                                               # both past np_watch_s (180 s)
        self.assertIsNone(n.wl.get("near"))
        self.assertIn("near", self.feeds.watching)                      # the recorder keeps it on the poller
        self.assertNotIn("far", self.feeds.watching)
        self.read(n, "near", 6.0, sec=100)                              # 5 minutes after its launch: on the tape
        self.step(n, t_near + 899 - self.clock.epoch())
        self.assertIn("near", self.feeds.watching)
        self.step(n, 2)                                                 # rec_follow_s (900 s) after its create
        self.assertNotIn("near", self.feeds.watching)
        self.assertEqual(self.feeds.unwatched.count("near"), 1)
        tapes = self.tape(n)
        near, far = tapes["near"], tapes["far"]
        fol = [e for e in near["events"] if e["k"] == "follow"][0]
        self.assertEqual((fol["why"], fol["until"]), ("near", round(t_near + 900, 3)))
        self.assertTrue(near["followed"])
        self.assertEqual([e["why"] for e in near["events"] if e["k"] == "end"], ["follow"])
        self.assertAlmostEqual(near["readings"][-1]["rsol"], 6.0, places=6)
        self.assertGreater(near["readings"][-1]["t"] - t_near, 280)
        self.assertEqual([e["why"] for e in far["events"] if e["k"] == "end"], ["watch"])
        self.assertFalse(far["followed"])

    def test_a_bought_coin_is_followed_after_its_sale_too(self):
        n = self.night(eng.np_cfg(rec_follow_s=60))
        self.launch(n, "aaa", eng.SCALP)
        self.read(n, "aaa", self.gain_rsol(n, "aaa", -9))
        sold = n.epoch()
        self.step(n, 182)
        self.assertIn("aaa", self.feeds.watching)
        tp = self.tape(n, close=False)["aaa"]
        fol = [e for e in tp["events"] if e["k"] == "follow"][0]
        self.assertEqual((fol["why"], fol["until"]), ("traded", round(sold + 300, 3)))      # rec_follow_exit_s
        self.step(n, sold + 301 - self.clock.epoch())
        self.assertNotIn("aaa", self.feeds.watching)

    def test_the_follow_cap_keeps_a_bought_coin_before_near_ones_the_newest_first(self):
        n = self.night(eng.np_cfg(rec_max_follow=2))
        self.launch(n, "trd", eng.SCALP)
        self.read(n, "trd", self.gain_rsol(n, "trd", -9))               # bought and sold
        for mint in ("n1", "n2", "n3"):
            self.near(n, mint)
        self.step(n, 182)                                               # all four leave the watch list at once
        self.assertEqual(sorted(m for m in self.feeds.watching), ["n3", "trd"])
        self.assertEqual(sorted(self.feeds.unwatched), ["n1", "n2"])
        tapes = self.tape(n)
        ends = {m: [e["why"] for e in tp["events"] if e["k"] == "end"] for m, tp in tapes.items()}
        self.assertEqual((ends["n1"], ends["n2"], ends["n3"], ends["trd"]), (["cap"], ["cap"], ["stop"], ["stop"]))

    def test_the_bots_watch_list_and_its_held_coin_always_come_first(self):
        tier = [{"name": "hold", "min_score": 0, "ladder": [[80.0, 1.0]], "trail_pct": 0.0, "time_stop_min": 999.0}]
        n = self.night(eng.np_cfg(rec_max_keys=2, hold_tiers=tier, np_quiet_min=0, time_stop_late_x=0))
        self.launch(n, "held", eng.SCALP)
        self.assertIn("held", n.state["positions"])
        self.near(n, "n1")
        self.step(n, 60)
        self.near(n, "n2")                                              # the watch list: held, n2 (n1 still too)
        self.step(n, 120)                                               # n1 leaves: held + n2 = 2 keys, no room
        self.assertNotIn("n1", self.feeds.watching)
        self.step(n, 60)                                                # n2 leaves: held = 1 key, room for one
        self.assertIn("n2", self.feeds.watching)
        self.turn(n, eng.create("new", "NEW"))                          # a new coin takes the room back
        self.assertNotIn("n2", self.feeds.watching)
        self.assertIn("new", self.feeds.watching)
        self.assertIn("held", self.feeds.watching)
        self.assertNotIn("held", self.feeds.unwatched)
        self.assertIn("held", n.state["positions"])
        self.assertLessEqual(len(self.feeds.watching), 2)
        ends = {m: [e["why"] for e in tp["events"] if e["k"] == "end"] for m, tp in self.tape(n).items()}
        self.assertEqual((ends["n1"], ends["n2"]), (["cap"], ["cap"]))

    # ---- never in the way
    def test_an_error_in_the_recorder_is_swallowed_said_once_and_its_curves_let_go(self):
        n = self.night()
        self.near(n, "near")
        self.step(n, 182)
        self.assertIn("near", self.feeds.watching)                      # followed

        def boom(*a):
            raise KeyError("tape bug")
        rec = n.rec
        rec.event = boom
        self.launch(n, "aaa", eng.SCALP)
        self.assertIn("aaa", n.state["positions"])                      # the trade went on
        self.assertIsNone(n.rec)
        self.assertNotIn("near", self.feeds.watching)                   # the poller is the exits' again
        said = [t for t in self.lines(n, "LOGGER") if t.startswith("tape recorder off")]
        self.assertEqual(len(said), 1)
        self.assertIn("event: KeyError", said[0])
        self.read(n, "aaa", self.gain_rsol(n, "aaa", -9))
        self.assertEqual(n.state["night"]["trades"][0]["why"], "stop -9%")
        self.assertTrue(rec.close(5))
        tp = rec_mod.load_tapes(os.path.join(self.dir, "tapes"))
        self.assertNotIn("aaa", tp)                                     # nothing after the error
        self.assertEqual([e["why"] for e in tp["near"]["events"] if e["k"] == "end"], ["off"])

    def test_a_stuck_disk_never_holds_the_trading(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        n = self.night()

        def stuck(path, data):
            gate.wait(10)
            rec_mod.append(path, data)
        n.rec.write = stuck
        n.rec.cfg["rec_flush_s"] = 0                                    # a hand-off on every turn
        most = 0.0
        real = n.np_events

        def timed(*a):
            t = time.perf_counter()
            try:
                return real(*a)
            finally:
                nonlocal most
                most = max(most, time.perf_counter() - t)
        n.np_events = timed
        self.launch(n, "aaa", eng.SCALP)
        self.read(n, "aaa", self.gain_rsol(n, "aaa", -9))
        self.assertEqual(n.state["night"]["trades"][0]["why"], "stop -9%")
        self.assertLess(most, 0.2)
        gate.set()
        self.assertEqual([e["k"] for e in self.tape(n)["aaa"]["events"]], ["meta", "rug", "fire", "buy", "sell", "end"])

    def test_off_by_config_by_environment_or_without_its_module(self):
        n = self.night(eng.np_cfg(rec_on=False))
        self.assertIsNone(n.rec)
        os.environ["MEMEBOT_REC"] = "0"
        try:
            self.assertIsNone(self.night().rec)
        finally:
            del os.environ["MEMEBOT_REC"]
        real = mb.recorder
        mb.recorder = None
        try:
            n = self.night()
            self.assertIsNone(n.rec)
            self.launch(n, "aaa", eng.SCALP)
            self.assertIn("aaa", n.state["positions"])
        finally:
            mb.recorder = real
        self.assertFalse(os.path.exists(os.path.join(self.dir, "tapes")))


class RunTape(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_a_run_leaves_a_closed_tape_with_its_trade(self):
        start = datetime.datetime(2026, 11, 25, 12, 0)
        clock = eng.Clock(start)
        m = eng.CurveMarket()
        feeds = eng.FakeFeeds(clock, m)
        m.reports["aaa"] = eng.rug_report("aaa")
        old, eng.T0 = eng.T0, start                                     # FakeFeeds' script counts from the run's start
        try:
            feeds.at(2, eng.create("aaa", "AAA"))
            feeds.at(2.4, eng.meta("aaa", False, True, False))
            for k, rsol in enumerate(eng.SCALP + [4.8, 5.5, 6.0]):
                feeds.at(3 + 0.7 * k, eng.reading("aaa", rsol, 2000 + k))
            mb.run(self.dir, eng.np_cfg(), minutes=2, anytime=True, market=m, now_fn=clock.now, sleep=clock.advance,
                   out=lambda *a: None, live=eng.Rec(), feeds=feeds)
        finally:
            eng.T0 = old
        tapes = os.path.join(self.dir, "tapes")
        self.assertEqual([os.path.basename(p) for _, p in rec_mod.tape_files(tapes)], ["2026-11-25T12.jsonl"])
        tp = rec_mod.load_tapes(tapes)["aaa"]
        kinds = [e["k"] for e in tp["events"]]
        self.assertEqual([k for k in kinds if k in ("buy", "sell")], ["buy", "sell"])
        self.assertEqual([e["why"] for e in tp["events"] if e["k"] == "sell"], ["TP +7%"])
        self.assertEqual(len(tp["readings"]), 11)
        sysl = rec_mod.load_system(tapes)
        self.assertEqual((sysl[0]["k"], sysl[-1]["k"]), ("start", "stop"))
        hbs = [x for x in sysl if x["k"] == "hb"]
        self.assertTrue(10 <= len(hbs) <= 14, len(hbs))                 # every 10 s of a 2-minute run
        self.assertEqual(hbs[-1]["sol"], 100.0)
        self.assertEqual(hbs[0]["poll_ok"], 0)
        st = sysl[0]                                                    # the run's settings, cash and LEARNER memory
        self.assertEqual((st["cfg"]["hold_tiers"], st["cfg"]["stop_pct"], st["bal"], st["held"]),
                         (eng.np_cfg()["hold_tiers"], -7.0, 100.0, 0))
        self.assertIsInstance(st["mem"], dict)
        sols = [x for x in sysl if x["k"] == "sol"]                     # SOL/USD when the bot's changes: once here
        self.assertEqual([x["usd"] for x in sols], [100.0])


class StartAndSol(Base):
    def test_the_start_line_keeps_every_setting_a_replay_needs(self):
        # the whole config at once went through _clean, which keeps 60 keys of a dict: hold_tiers, np_min_age_s,
        # breakeven_pct ... were cut off the start line
        cfg = mb.load_config(mb.CONFIG)
        r = rec_mod.Recorder(self.tapes, cfg, clock=Clock(), log=self.said.append,
                             start={"bal": 87.5, "held": 1, "mem": {"labels": {"np:sol:3-6": {"n": 3, "sum": -9.0}}, "k": 5}})
        self.assertTrue(r.close(5))
        st = rec_mod.load_system(self.tapes)[0]
        want = {k for k in cfg if k in rec_mod.CFG_KEYS or k.startswith(rec_mod.CFG_PREFIX)}
        self.assertGreater(len(want), 60)
        self.assertEqual(set(st["cfg"]), want | {k for k in rec_mod.DEFAULTS})
        for k in ("hold_tiers", "np_min_age_s", "breakeven_pct", "np_serial_max", "night_stop_pct", "loss_streak",
                  "cooldown_min", "job_no_entries_min", "job_flat_min", "min_order_usd", "learn_skip_pct"):
            self.assertEqual(st["cfg"][k], cfg[k], k)
        self.assertEqual((st["bal"], st["held"], st["mem"]["labels"]["np:sol:3-6"]["n"]), (87.5, 1, 3))

    def test_sol_goes_on_the_tape_each_time_it_changes(self):
        clock = Clock()
        r = self.rec(clock)
        for px in (71.1, 71.1, 71.3, None, 0, float("nan"), 71.3, 70.9):
            r.sol(px)
            clock.t += 1
        self.assertTrue(r.close(5))
        sols = [(x["t"] - T0, x["usd"]) for x in self.lines() if x["k"] == "sol"]
        self.assertEqual(sols, [(0.0, 71.1), (2.0, 71.3), (7.0, 70.9)])


if __name__ == "__main__":
    unittest.main()
