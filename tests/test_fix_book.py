"""memebot/fix_book.py, the one-off correction of a night booked on impossible curve readings (2026-11-27): the voids
from the trade records alone (each rule, and the six records of that night as their kinds), the replay of the kept
trades with the bot's own sizing and RISK (bot.Night.why_no_entries on the replayed book), the corrected balance
worked out by hand, the files it writes (state.json, panel.json, memory_snipe.json rebuilt by the LEARNER's own
backfill, memory.json with a voided momentum trade unlearned exactly), the archives of the originals, the dry run that
writes nothing, and its refusals (open positions, a night fixed already). A planted state, made up for the tests."""

import copy
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

from _load import load

os.environ["MEMEBOT_LIVE"] = "0"
os.environ["MEMEBOT_FEEDS"] = "0"
fb = load("memebot", "fix_book")
bot = fb.bot
lr = fb.learner
CFG = bot.load_config(bot.CONFIG)
CFG.update(position_pct=10.0, min_order_usd=5.0, max_open=8, max_trades_per_night=250, night_stop_pct=-30.0,
           dump_loss_pct=30.0, loss_streak=5, cooldown_min=2, np_snipe=True, strategies=["new_pairs"])
DAY = "2026-11-27T"
LEARN = ["np:dev:0-2", "np:sol:<3", "np:buys:90+", "np:age:<30s", "np:social:x", "news:0-2", "tier:scalp", "hour:03-05"]
SHADOW_LEARN = ["np:dev:0-2", "np:social:tg", "hour:03-05"]


def trade(sym, opened, closed, cost, pnl_pct, rise=3, mcap=30.0, slip=0.5, dev=0.0, entry=2.9e-6, ratio=0.97,
          snipe=True, steps=0, **kw):
    """A closed New Pairs trade's record as close_part() writes it, at a standard curve state unless the arguments say
    otherwise: its proceeds are its tokens (cost / entry) at its exit price x ratio (a sale's costs)."""
    proceeds = round(cost * (1 + pnl_pct / 100.0), 2)
    tokens = cost / entry
    t = {"symbol": sym, "mint": sym.lower() + "pump", "opened": DAY + opened + "Z", "closed": DAY + closed + "Z",
         "cost": round(cost, 2), "proceeds": proceeds, "pnl": round(proceeds - cost, 2), "pnl_pct": round(pnl_pct, 1),
         "why": "stop -10%" if pnl_pct < 0 else "TP +10%", "url": None,
         "reason": "%s · first reading 0.3 s after the create · 0.5 SOL in · %+d%% since the create · mcap %d SOL · "
                   "fill curve" % ("snipe" if snipe else "momentum", rise, mcap),
         "size_pct": 10.0, "entry_price": entry, "exit_price": proceeds / (tokens * ratio), "slip": slip,
         "strategy": "new_pairs", "tier": "scalp", "score": 0, "steps": steps,
         "plan": {"tier": "scalp", "score": 0, "parts": [], "ladder": [[10.0, 1.0]], "step": 0, "stop_pct": -10.0,
                  "trail_pct": 0.0},
         "age_s": 0, "mcap_sol": mcap, "dev_pct": dev, "sol_in": 0.5, "learn": list(LEARN)}
    if snipe:
        t.update(snipe=True, entry_fill="curve", stop_pct=-10.0)
    t.update(kw)
    return t


def the_six():
    """The six records of 2026-11-27 as their kinds (their own numbers), and the mint of the one voided by hand."""
    return [
        trade("Evelyn", "01:01:00", "01:01:00", 6.93, -94.2, rise=-100, mcap=0.0, slip=86.64, entry=7.456267e-09,
              fill="curve", exit_price=9.739707e-10, proceeds=0.40, pnl=-6.53),
        trade("530A", "01:03:00", "01:03:00", 6.38, 22092.2, rise=-100, mcap=0.0, slip=77.23, dev=2.25,
              entry=7.898752e-09, exit_price=1.758423e-09, proceeds=1416.50, pnl=1410.12),
        trade("SpaceXers", "01:05:30", "01:05:30", 114.08, -99.3, rise=-100, mcap=0.0, slip=98.43,
              entry=1.0904286e-07, fill="curve", exit_price=1.6779903e-09, proceeds=0.80, pnl=-113.28),
        trade("Pumpers", "01:09:00", "01:10:00", 37.79, -96.6, rise=4432, mcap=1267.1, slip=0.04, entry=9.404339e-05,
              exit_price=9.605633e-05, proceeds=1.30, pnl=-36.49),
        trade("Coke-Cola", "01:11:00", "01:16:00", 20.62, 1130.7, rise=-88, mcap=3.4, slip=7.4, entry=2.763652e-07,
              exit_price=2.768154e-07, proceeds=253.74, pnl=233.13),
        trade("Thanksgiving", "01:17:00", "01:17:04", 130.35, 10.4, rise=771, mcap=330.2, slip=1.7, dev=15.17,
              entry=2.4783118e-05, ratio=0.975),
    ]


def planted():
    """A night started at $100: six normal snipes (two of them open at the same time) between the six odd records,
    sized on the inflated book as the real night was; in closing order, as night["trades"] keeps them."""
    six = the_six()
    norm = {"N1": trade("N1", "01:00:00", "01:00:30", 12.0, -10.0), "N2": trade("N2", "01:02:00", "01:02:20", 11.0, 20.0),
            "N3": trade("N3", "01:04:00", "01:05:00", 150.0, -5.0), "N4a": trade("N4a", "01:06:00", "01:08:00", 140.0, -20.0),
            "N4b": trade("N4b", "01:07:00", "01:07:30", 140.0, 10.0), "N5": trade("N5", "01:20:00", "01:21:00", 160.0, 0.0)}
    trades = [norm["N1"], six[0], norm["N2"], six[1], norm["N3"], six[2], norm["N4b"], norm["N4a"], six[3], six[4],
              six[5], norm["N5"]]
    return trades


def state_of(trades, start=100.0, positions=None, closed=False):
    night = {"id": "2026-11-27", "start_equity": start, "started": "2026-11-26T23:00:00Z", "decisions": [],
             "trades": trades, "entered": [], "streak": 2, "cooldown_until": 0, "scans": 0, "flagged": 12, "checked": 12,
             "filtered": 0, "lines": 40, "flags": [], "curve": [[int(fb.epoch(DAY + "00:59:00Z")), start],
                                                               [int(fb.epoch(DAY + "01:04:30Z")), 1500.0],
                                                               [int(fb.epoch(DAY + "01:30:00Z")), 1600.0]],
             "last_rug": None}
    bal = start + sum(t["pnl"] for t in trades)
    st = {"balance": bal, "positions": positions or {}, "nights": [], "holders": {}, "rug_cache": {}, "shadow": {},
          "night": night}
    if closed:
        st["night"], st["last_night"] = None, night
        st["nights"] = [{"id": "2026-11-27", "start": start, "end": round(bal, 2), "pnl": round(bal - start, 2),
                         "trades": len(trades), "wins": 0, "best": 0, "worst": 0, "flagged": 12, "scans": 0}]
    return st


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def folder(self, st, panel=True, mem=None, mem_snipe=None):
        d = os.path.join(self.dir, "data%d" % len(os.listdir(self.dir)))
        os.makedirs(d)
        bot.write_json(os.path.join(d, "state.json"), st)
        if panel:
            bot.write_json(os.path.join(d, "panel.json"), {"balance": round(st["balance"], 2), "equity": 1.0,
                                                           "trades": [], "other": "kept"})
        if mem is not None:
            lr.save(os.path.join(d, "memory.json"), mem)
        if mem_snipe is not None:
            lr.save(os.path.join(d, "memory_snipe.json"), mem_snipe)
        return d

    @staticmethod
    def snapshot(d):
        out = {}
        for root, _, files in os.walk(d):
            for name in files:
                p = os.path.join(root, name)
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, d)] = (fh.read(), os.stat(p).st_mtime_ns)
        return out


class Voids(Base):
    def test_each_rule_alone(self):
        ok = trade("OK", "01:00:00", "01:00:30", 10.0, -10.0)
        self.assertEqual(fb.void_reasons(ok), [])
        cases = [
            (dict(ok, mcap_sol=12.0), "entry mcap 12.0 SOL: under a standard curve's launch mcap 27.96 SOL"),
            (dict(ok, mcap_sol=420.0), "entry mcap 420.0 SOL: over a standard curve's completion mcap 411 SOL"),
            (trade("R", "01:00:00", "01:00:30", 10.0, -10.0, rise=-30, dev=15.0, mcap=28.5),
             "-30% since the create: a curve is never under its launch price (-26.0% with the creator's 15.0% buy)"),
            (trade("H", "01:00:00", "01:00:30", 10.0, -10.0, rise=1500, mcap=400.0),
             "+1500% since the create: more than a curve rises before it completes (+1370%)"),
            (dict(ok, slip=40.0), "a curve fill with 40.0% impact for a $10.00 order: a 30+ SOL curve moves at most 1.6% "
                                  "for it (even at SOL $20)"),
            (dict(ok, entry=1e-8, entry_price=1e-8), "1000 M tokens bought: more than the 793.1 M a curve ever sells"),
            (dict(ok, proceeds=18.0), "proceeds $18.00 are 1.94x what its tokens give at its own exit price"),
            (dict(ok, proceeds=4.0), "proceeds $4.00 are 0.431x what its tokens give at its own exit price"),
        ]
        for t, want in cases:
            got = fb.void_reasons(t)
            self.assertTrue(any(g.startswith(want) for g in got), (want, got))
        # the rules are about a curve: a migrated pair's record, a quote fill's impact and a ladder's proceeds are not
        self.assertEqual(fb.void_reasons(dict(ok, strategy="migrated", mcap_sol=1.0)), [])
        self.assertEqual(fb.void_reasons(dict(ok, slip=100.0, entry_fill="quote")), [])
        self.assertEqual(fb.void_reasons(dict(ok, proceeds=18.0, steps=2, plan=dict(ok["plan"], step=1))), [])
        # a momentum record says "since launch"
        self.assertTrue(fb.void_reasons(trade("M", "01:00:00", "01:00:30", 10.0, -1.0, rise=-90, snipe=False,
                                              reason="momentum · 3 SOL in · -90% since launch · mcap 3 SOL")))

    def test_the_six_records_of_2026_11_27(self):
        six = {t["symbol"]: fb.void_reasons(t) for t in the_six()}
        self.assertEqual(six["Thanksgiving"], [])       # a standard curve 86 % through: possible, voided by hand only
        for sym in ("Evelyn", "530A", "SpaceXers", "Pumpers", "Coke-Cola"):
            self.assertTrue(six[sym], sym)
        self.assertTrue(any("under a standard curve's launch mcap" in w for w in six["Evelyn"]))
        self.assertTrue(any(w.startswith("proceeds $1416.50 are 99") for w in six["530A"]), six["530A"])
        self.assertTrue(any("more than the 793.1 M a curve ever sells" in w for w in six["SpaceXers"]))
        self.assertTrue(any("over a standard curve's completion mcap" in w for w in six["Pumpers"]))
        self.assertTrue(any(w.startswith("-88% since the create") for w in six["Coke-Cola"]))
        self.assertTrue(any(w.startswith("proceeds $253.74 are 12.3x") for w in six["Coke-Cola"]))


class Fix(Base):
    def plan(self, d, marks=("thanksgivingpump",)):
        return fb.plan_fix(d, None, list(marks), cfg=CFG)

    def test_the_planted_night_corrected_by_hand(self):
        trades = planted()
        st = state_of(trades)
        mem = lr.fresh()
        ms0 = lr.fresh()                                # the snipes' memory as the bot left it: every trade, and
        for t in trades:                                # three shadow lessons
            lr.update(ms0, t["learn"], t["pnl_pct"])
        for p in (-5.0, -8.0, 4.0):
            lr.update(ms0, SHADOW_LEARN, p, 0.5, True)
        d = self.folder(st, mem=mem, mem_snipe=ms0)
        lines, doc, why = self.plan(d)
        self.assertIsNone(why)
        self.assertEqual(sorted(trades[i]["symbol"] for i in doc["voids"]),
                         sorted(["Evelyn", "530A", "SpaceXers", "Pumpers", "Coke-Cola", "Thanksgiving"]))
        self.assertIn("voided by hand (--void)", doc["voids"][10])
        # by hand, the bot's sizing (10 % of the equity, the open positions at their cost) on the kept six, in time order
        cash = 100.0
        n1 = 10.0;              cash += n1 * -0.10                       # noqa: E702  01:00:00-01:00:30  -10 %
        n2 = cash * 0.10;       cash += n2 * 0.20                        # noqa: E702  01:02:00-01:02:20  +20 %
        n3 = cash * 0.10;       cash += n3 * -0.05                       # noqa: E702  01:04:00-01:05:00   -5 %
        n4a = cash * 0.10                                                # 01:06:00, open until 01:08:00
        n4b = (cash - n4a + n4a) * 0.10                                  # 01:07:00: the equity counts n4a at its cost
        cash += n4b * 0.10 + n4a * -0.20                                 # n4b +10 % at 01:07:30, n4a -20 % at 01:08
        n5 = cash * 0.10                                                 # 01:20:00-01:21:00  0 %
        self.assertAlmostEqual(cash, 99.470349, places=6)
        r = doc["replay"]
        self.assertEqual(r["dropped"], {})
        got = {trades[i]["symbol"]: c for i, c in r["taken"].items()}
        for sym, want in (("N1", n1), ("N2", n2), ("N3", n3), ("N4a", n4a), ("N4b", n4b), ("N5", n5)):
            self.assertAlmostEqual(got[sym], want, places=9, msg=sym)
        self.assertAlmostEqual(doc["new_balance"], cash, places=9)
        self.assertAlmostEqual(doc["new_pnl"], cash - 100.0, places=9)
        self.assertEqual(r["streak"], 0)
        self.assertIn("BALANCE %s -> $99.47 (the night: %s booked -> -$0.53" % (fb.usd(st["balance"]),
                                                                              fb.usd(st["balance"] - 100.0)), "\n".join(lines))
        out = fb.apply_fix(d, doc, now=bot.utc_now())
        self.assertIn("wrote state.json, the original archived to archive/state-2026-11-27-before-fix.json", out)
        st2 = bot.read_json(os.path.join(d, "state.json"), None)
        n = st2["night"]
        self.assertAlmostEqual(st2["balance"], cash, places=9)
        self.assertEqual([t["symbol"] for t in n["trades"]], ["N1", "N2", "N3", "N4b", "N4a", "N5"])
        t3 = n["trades"][2]
        self.assertEqual((t3["cost"], t3["proceeds"], t3["pnl"], t3["pnl_pct"]), (10.1, 9.59, -0.5, -5.0))
        self.assertEqual(t3["fix_was"], {"cost": 150.0, "proceeds": 142.5, "pnl": -7.5, "size_pct": 10.0})
        self.assertEqual((len(n["fix"]["void"]), n["fix"]["dropped"], n["fix"]["balance_was"]),
                         (6, [], round(st["balance"], 2)))
        self.assertEqual(n["curve"][0][1], 100.0)                                # cash and open positions at cost
        self.assertAlmostEqual(n["curve"][1][1], round(cash_at_0104_30(), 2), places=2)
        self.assertAlmostEqual(n["curve"][-1][1], round(cash, 2), places=2)
        self.assertTrue(n["decisions"][-1]["text"].startswith("book fixed (memebot/fix_book.py): 6 trades voided"))
        # the panel: its balance, equity and trades; what it had besides stays
        p = bot.read_json(os.path.join(d, "panel.json"), None)
        self.assertEqual((p["balance"], p["equity"], len(p["trades"]), p["other"]), (99.47, 99.47, 6, "kept"))
        self.assertNotIn("learn", p["trades"][0])
        # the snipes' memory: the six voided trades unlearned (530A's +22092 % was learned capped at +100, and is
        # unlearned so), the kept six and the three shadow lessons as they were: what learning only those gives
        ms = lr.load(os.path.join(d, "memory_snipe.json"))
        want = lr.fresh()
        lr.backfill(want, n["trades"], CFG["timezone"])
        for p in (-5.0, -8.0, 4.0):
            lr.update(want, SHADOW_LEARN, p, 0.5, True)
        self.assertEqual((ms["samples"], ms["shadows"]), (6, 3))
        self.assertEqual(sorted(ms["labels"]), sorted(want["labels"]))
        for lab, stat in want["labels"].items():
            for k, v in stat.items():
                self.assertAlmostEqual(ms["labels"][lab][k], v, places=3, msg=(lab, k))
        self.assertEqual(st2["learner_summary"]["memory"], "snipe")
        # the archives are the originals, byte for byte
        for name in ("state", "panel", "memory_snipe"):
            a = os.path.join(d, "archive", "%s-2026-11-27-before-fix.json" % name)
            self.assertTrue(os.path.exists(a), name)
        self.assertEqual(bot.read_json(os.path.join(d, "archive", "state-2026-11-27-before-fix.json"), None), st)
        self.assertFalse(os.path.exists(os.path.join(d, "archive", "memory-2026-11-27-before-fix.json")))  # unchanged
        # once only
        _, doc2, why2 = self.plan(d)
        self.assertIsNone(doc2)
        self.assertIn("was fixed already", why2)

    def test_the_dry_run_writes_nothing(self):
        d = self.folder(state_of(planted()), mem=lr.fresh(), mem_snipe=lr.fresh())
        before = self.snapshot(d)
        cfg_path = os.path.join(self.dir, "cfg.json")
        with open(cfg_path, "w") as fh:
            json.dump({k: v for k, v in CFG.items()}, fh)
        out = []
        self.assertEqual(fb.main(["--data", d, "--dry-run", "--config", cfg_path], out=out.append), 0)
        text = "\n".join(out)
        self.assertIn("VOID 5", text)                       # Thanksgiving only by hand: not asked here
        self.assertIn("would write state.json, the original archived to archive/state-2026-11-27-before-fix.json", text)
        self.assertTrue(text.rstrip().endswith("DRY RUN: nothing written"))
        self.assertEqual(self.snapshot(d), before)
        self.assertFalse(os.path.exists(os.path.join(d, "archive")))
        out = []                                            # the real run, by the command line, with the hand void
        self.assertEqual(fb.main(["--data", d, "--config", cfg_path, "--void", "thanksgivingpump@" + DAY + "01:17:00Z"],
                                 out=out.append), 0)
        self.assertIn("VOID 6", "\n".join(out))
        self.assertTrue(os.path.exists(os.path.join(d, "archive", "state-2026-11-27-before-fix.json")))

    def test_the_bots_risk_drops_what_the_corrected_book_would_not_have_taken(self):
        # each night has one voided trade at its end (Evelyn's kind: voided, never replayed; a night with nothing to
        # void is left alone), the rest is what RISK does on the replayed book
        # nothing under a $50 book: $52 less half of a $5.20 order leaves $49.40, whose 10 % is under the $5 minimum
        trades = with_void([trade("A", "01:00:00", "01:00:10", 30.0, -50.0), trade("B", "01:01:00", "01:01:10", 30.0, 10.0)])
        _, doc, _ = self.plan(self.folder(state_of(trades, start=52.0)), ())
        self.assertEqual(doc["replay"]["dropped"], {1: "only $4.94 to size"})
        self.assertAlmostEqual(doc["new_balance"], 52.0 - 2.6, places=9)
        # the pause after 5 losses in a row (2 min): an entry inside it is dropped, one after it taken
        trades = [trade("L%d" % k, "01:0%d:00" % k, "01:0%d:10" % k, 10.0, -10.0) for k in range(5)]
        trades = with_void(trades + [trade("P", "01:05:00", "01:05:10", 10.0, 5.0),
                                     trade("Q", "01:06:30", "01:06:40", 10.0, 5.0)])
        _, doc, _ = self.plan(self.folder(state_of(trades)), ())
        self.assertEqual(doc["replay"]["dropped"], {5: "pause after a losing streak"})
        self.assertIn(6, doc["replay"]["taken"])
        # the night stop: three rugs (-100 %) take $100 to $72.90, the fourth still goes (its dump guard holds), the
        # fifth not: the night is at -34 %
        trades = [trade("X%d" % k, "02:0%d:00" % k, "02:0%d:10" % k, 10.0, -100.0, ratio=0.97) for k in range(5)]
        for t in trades:
            t.update(proceeds=0.0, exit_price=1e-9, steps=1, plan=dict(t["plan"], step=1))     # a ladder: no ratio rule
        _, doc, _ = self.plan(self.folder(state_of(with_void(trades))), ())
        self.assertEqual(list(doc["voids"]), [5])
        self.assertEqual(sorted(doc["replay"]["taken"]), [0, 1, 2, 3])
        self.assertEqual(doc["replay"]["dropped"], {4: "night at -34%: stopped for the night"})
        self.assertAlmostEqual(doc["new_balance"], 100 * 0.9 ** 4, places=9)
        # at most 8 open: nine snipes opened 01:00:00-01:00:08, all open until 01:10, the ninth not taken
        nine = [trade("O%d" % k, "01:00:0%d" % k, "01:10:00", 10.0, 1.0) for k in range(9)]
        _, doc, _ = self.plan(self.folder(state_of(with_void(nine))), ())
        self.assertEqual(sorted(doc["replay"]["taken"]), list(range(8)))
        self.assertEqual(doc["replay"]["dropped"], {8: "8 positions open (max)"})
        # max_trades_per_night: a cap of 3 takes the first three, the 4th and 5th not
        five = [trade("T%d" % k, "01:0%d:00" % k, "01:0%d:10" % k, 10.0, 1.0) for k in range(5)]
        _, doc, _ = fb.plan_fix(self.folder(state_of(with_void(five))), None, [], cfg=dict(CFG, max_trades_per_night=3))
        self.assertEqual(sorted(doc["replay"]["taken"]), [0, 1, 2])
        self.assertEqual(doc["replay"]["dropped"], {3: "3 trades tonight (max)", 4: "3 trades tonight (max)"})
        # the same second: a trade's exit is counted before another's entry (the bot's loop sells first), so with one
        # position at most, B opened in the second A closed is taken
        ab = [trade("A", "01:00:00", "01:00:10", 10.0, 1.0), trade("B", "01:00:10", "01:00:20", 10.0, 1.0)]
        _, doc, _ = fb.plan_fix(self.folder(state_of(with_void(ab))), None, [], cfg=dict(CFG, max_open=1))
        self.assertEqual((sorted(doc["replay"]["taken"]), doc["replay"]["dropped"]), ([0, 1], {}))

    def test_a_voided_momentum_trade_is_unlearned_from_memory_json_exactly(self):
        mom = trade("MOM", "01:30:00", "01:31:00", 8.0, 900.0, snipe=False, mcap=3.0, rise=-88,
                    learn=["np:dev:0-2", "hour:03-05"])
        mom.update(reason="momentum · 3 SOL in · 8/8 buys (100%) · -88% since launch · mcap 3 SOL · 50 s old")
        trades = planted()[:3] + [mom]
        mem = lr.fresh()
        for p in (-5.0, -7.0, 3.0):
            lr.update(mem, ["np:dev:0-2", "hour:03-05"], p)
        want = copy.deepcopy(mem)
        lr.update(mem, ["np:dev:0-2", "hour:03-05", "np:social:web"], 900.0)    # the voided trade's lesson (capped
        mom["learn"].append("np:social:web")                                   # at +100); it alone taught "web"
        lr.update(mem, ["np:social:tg"], 50.0)                   # another trade's: it stays
        lr.update(want, ["np:social:tg"], 50.0)
        d = self.folder(state_of(trades), mem=mem)
        _, doc, _ = self.plan(d, ())
        self.assertIn(3, doc["voids"])
        out = fb.apply_fix(d, doc)
        self.assertTrue([x for x in out if x.startswith("memory.json: 1 voided unlearned, 0 dropped re-weighted")], out)
        got = lr.load(os.path.join(d, "memory.json"))
        self.assertEqual(got["labels"]["np:dev:0-2"], want["labels"]["np:dev:0-2"])
        self.assertEqual(got["labels"]["hour:03-05"], want["labels"]["hour:03-05"])
        self.assertEqual(got["samples"], 4)
        self.assertEqual(got["labels"]["np:social:tg"], {"n": 1, "sum": 50, "sumsq": 2500, "wins": 1})
        self.assertNotIn("np:social:web", got["labels"])
        self.assertEqual(got["labels"], want["labels"])
        self.assertTrue(os.path.exists(os.path.join(d, "archive", "memory-2026-11-27-before-fix.json")))

    def test_a_closed_nights_summary_is_corrected_too(self):
        trades = planted()
        st0 = state_of(trades, closed=True)
        st0["night"] = {"id": "2026-11-28", "start_equity": round(st0["balance"], 2), "trades": [], "curve": []}
        d = self.folder(st0)
        _, doc, why = self.plan(d)
        self.assertIsNone(why)
        out = fb.apply_fix(d, doc)
        self.assertIn("night 2026-11-28 (no trades yet) now starts at $99.47, its equity curve flat there", out)
        st = bot.read_json(os.path.join(d, "state.json"), None)
        s = st["nights"][-1]
        self.assertEqual((s["end"], s["pnl"], s["trades"], s["wins"]), (99.47, -0.53, 6, 2))
        self.assertEqual(len(st["last_night"]["trades"]), 6)
        self.assertEqual(st["night"]["start_equity"], 99.47)     # its night stop is judged from the corrected book
        # a later night with trades of its own (sized on the book being corrected): the closed one is not fixed
        st0["night"]["trades"] = [trade("Z", "02:00:00", "02:00:10", 30.0, 1.0)]
        _, doc, why = fb.plan_fix(self.folder(st0), "2026-11-27", [], cfg=CFG)
        self.assertIsNone(doc)
        self.assertIn("a later night has trades", why)

    def test_it_refuses_while_positions_are_open(self):
        d = self.folder(state_of(planted(), positions={"x": {"symbol": "X"}}))
        _, doc, why = self.plan(d)
        self.assertIsNone(doc)
        self.assertIn("position(s) open", why)
        out = []
        self.assertEqual(fb.main(["--data", d, "--dry-run"], out=out.append), 2)
        self.assertTrue(out[-1].startswith("NOT FIXED: 1 position(s) open"))


class Review(Base):
    """The review of the first version (2026-11-27): each finding as a planted case."""

    def plan(self, d, marks=("thanksgivingpump",)):
        return fb.plan_fix(d, None, list(marks), cfg=CFG)

    def test_a_bare_symbol_that_names_two_trades_is_refused_with_the_candidates(self):
        # the real night has two Thanksgiving trades (two coins): --void Thanksgiving voided both before
        two = [trade("Thanksgiving", "02:13:59", "02:14:03", 130.35, 10.4, rise=771, mcap=330.2, dev=15.17, slip=1.7,
                     ratio=0.975, mint="BRao2pump"),
               trade("Thanksgiving", "02:14:27", "02:15:00", 50.0, 19.1, mint="6CivJJpump")]
        d = self.folder(state_of(with_void(two)))
        _, doc, why = fb.plan_fix(d, None, ["Thanksgiving"], cfg=CFG)
        self.assertIsNone(doc)
        self.assertEqual(why, "--void Thanksgiving names 2 trades: name one as MINT@OPENED (BRao2pump@2026-11-27T02:13:59Z, "
                              "6CivJJpump@2026-11-27T02:14:27Z)")
        out = []
        self.assertEqual(fb.main(["--data", d, "--dry-run", "--void", "Thanksgiving"], out=out.append), 2)
        self.assertTrue(out[-1].startswith("NOT FIXED: --void Thanksgiving names 2 trades"))
        for mark in ("BRao2pump", "Thanksgiving@2026-11-27T02:13:59Z", "BRao2pump@2026-11-27T02:13:59Z"):
            _, doc, why = fb.plan_fix(d, None, [mark], cfg=CFG)
            self.assertEqual(sorted(doc["voids"]), [0, 2], mark)            # the one named, and the record's own void
        _, doc, why = fb.plan_fix(d, None, ["NOPE"], cfg=CFG)
        self.assertEqual((doc, why), (None, "--void NOPE names no trade of this night"))

    def test_a_night_with_nothing_to_void_is_left_alone_and_the_right_night_is_picked(self):
        # (1) nothing to void: no rescaling, no "fixed" mark, no memory rewritten; exit 0
        d = self.folder(state_of([trade("A", "01:00:00", "01:00:10", 10.0, 1.0), trade("B", "01:01:00", "01:01:10", 10.0, -3.0)]),
                        mem_snipe=lr.fresh())
        before = self.snapshot(d)
        out = []
        self.assertEqual(fb.main(["--data", d], out=out.append), 0)
        self.assertIn("VOID 0: every trade reads as a standard curve's", out)
        self.assertEqual(out[-1], "NOT FIXED: nothing to void in night 2026-11-27: nothing written")
        self.assertEqual(self.snapshot(d), before)
        # (2) rolled over: the closed night has the voids, the new night one trade already: the default run never fixes
        # the new night (nothing to void there) and the closed one is refused (a later night has trades)
        st = state_of(planted(), closed=True)
        st["night"] = {"id": "2026-11-28", "start_equity": round(st["balance"], 2), "curve": [], "decisions": [],
                       "trades": [trade("Z", "23:10:00", "23:10:30", 30.0, 1.0)]}
        d = self.folder(st, mem_snipe=lr.fresh())
        before = self.snapshot(d)
        _, doc, why = fb.plan_fix(d, None, [], cfg=CFG)
        self.assertIsNone(doc)
        self.assertEqual(why, "night 2026-11-27 is closed and a later night has trades (sized on its book): not fixed")
        _, doc, why = fb.plan_fix(d, "2026-11-28", [], cfg=CFG)
        self.assertEqual((doc, why), (None, "nothing to void in night 2026-11-28: nothing written"))
        self.assertEqual(self.snapshot(d), before)
        # (3) both nights have something to void: it asks which
        st["night"]["trades"].append(the_six()[0])
        _, doc, why = fb.plan_fix(self.folder(st), None, [], cfg=CFG)
        self.assertEqual((doc, why), (None, "the current night 2026-11-28 and the last one 2026-11-27 both have trades: "
                                            "name one with --night"))

    def test_the_memories_keep_the_dropped_trades_as_shadows_and_every_old_lesson(self):
        # a night stop drops the last snipe (still a real market outcome): its lesson is re-weighted to shadow_weight as
        # the RISK shadow the corrected book would have followed; the voided one is unlearned; old shadows stay
        trades = [trade("X%d" % k, "02:0%d:00" % k, "02:0%d:10" % k, 10.0, -100.0, learn=["np:dev:0-2", "hour:03-05"])
                  for k in range(5)]
        for t in trades:
            t.update(proceeds=0.0, exit_price=1e-9, steps=1, plan=dict(t["plan"], step=1))
        trades[4]["learn"] = ["np:dev:0-2", "np:social:web"]
        trades = with_void(trades)
        trades[5]["learn"] = ["np:dev:0-2", "np:social:x"]
        ms = lr.fresh()
        for t in trades:
            lr.update(ms, t["learn"], t["pnl_pct"])
        lr.update(ms, ["np:social:tg"], 20.0, 0.5, True)                    # an old shadow lesson
        d = self.folder(state_of(trades), mem_snipe=ms)
        _, doc, _ = self.plan(d, ())
        self.assertEqual((list(doc["voids"]), doc["replay"]["dropped"]), ([5], {4: "night at -34%: stopped for the night"}))
        out = fb.apply_fix(d, doc)
        self.assertTrue([x for x in out if x.startswith("memory_snipe.json: 1 voided unlearned, 1 dropped re-weighted to "
                                                        "x0.5 (the RISK shadows the corrected book would have followed), "
                                                        "the rest kept · samples 6 -> 4, shadow lessons 1 -> 2")], out)
        got = lr.load(os.path.join(d, "memory_snipe.json"))
        want = lr.fresh()
        for t in trades[:4]:
            lr.update(want, t["learn"], t["pnl_pct"])
        lr.update(want, ["np:social:tg"], 20.0, 0.5, True)
        lr.update(want, trades[4]["learn"], trades[4]["pnl_pct"], 0.5, True)
        self.assertEqual((got["samples"], got["shadows"]), (want["samples"], want["shadows"]))
        self.assertEqual(sorted(got["labels"]), sorted(want["labels"]))       # np:social:x (the voided one's) is gone
        for lab, stat in want["labels"].items():
            for k, v in stat.items():
                self.assertAlmostEqual(got["labels"][lab][k], v, places=3, msg=(lab, k))
        # shadow_on_risk off: the corrected book would not have followed it: unlearned outright
        d = self.folder(state_of(trades), mem_snipe=ms)
        _, doc, _ = fb.plan_fix(d, None, [], cfg=dict(CFG, shadow_on_risk=False))
        fb.apply_fix(d, doc)
        self.assertEqual(lr.load(os.path.join(d, "memory_snipe.json"))["shadows"], 1)
        self.assertNotIn("np:social:web", lr.load(os.path.join(d, "memory_snipe.json"))["labels"])

    def test_no_memory_snipe_json_is_said_so_and_made_as_the_bot_would(self):
        d = self.folder(state_of(planted()))
        _, doc, _ = self.plan(d)
        out = fb.apply_fix(d, doc, dry_run=True)
        self.assertIn("memory_snipe.json: there is none: made as the bot would (learner.backfill of the 6 kept trades) "
                      "plus the 0 dropped at x0.5 · 6 samples, 0 shadow lessons", out)
        self.assertFalse([x for x in out if "had 0 samples" in x])

    def test_a_failed_write_changes_nothing_and_a_rerun_finishes_the_job(self):
        trades = planted()
        ms = lr.fresh()
        for t in trades:
            lr.update(ms, t["learn"], t["pnl_pct"])
        d = self.folder(state_of(trades), mem_snipe=ms)
        before = self.snapshot(d)
        _, doc, _ = self.plan(d)
        real_save = lr.save
        with unittest.mock.patch.object(fb.learner, "save", lambda path, mem: False):
            with self.assertRaises(OSError):
                fb.apply_fix(d, doc)
        self.assertEqual(self.snapshot(d), before)                         # no fix mark, no archive, no temp file
        self.assertIs(fb.learner.save, real_save)
        _, doc, why = self.plan(d)                                          # so the next run does it all
        self.assertIsNone(why)
        fb.apply_fix(d, doc)
        st = bot.read_json(os.path.join(d, "state.json"), None)
        self.assertIn("fix", st["night"])
        self.assertEqual(lr.load(os.path.join(d, "memory_snipe.json"))["samples"], 6)
        self.assertFalse([p for p in os.listdir(d) if p.endswith(".tmp")])
        # the order: the memories and the panel are in place before state.json, whose fix mark says it is done
        order = []
        real_replace = os.replace
        d2 = self.folder(state_of(planted()), mem_snipe=ms)
        _, doc, _ = self.plan(d2)
        with unittest.mock.patch.object(fb.os, "replace", lambda a, b: (order.append(os.path.basename(b)),
                                                                        real_replace(a, b))[1]):
            fb.apply_fix(d2, doc)
        self.assertEqual([x for x in order if not x.endswith(".tmp")], ["memory_snipe.json", "panel.json", "state.json"])

    def test_one_corrected_balance_for_the_book_the_summary_and_the_report(self):
        # the old trades' pnl are kept to the cent: the old balance is 4 cents off start + their sum (rounding, the
        # night started and ended flat): the corrected balance is the replay's cash, and the closed night's end is it
        trades = planted()
        st = state_of(trades, closed=True)
        st["balance"] -= 0.04
        st["nights"][-1]["end"] = round(st["balance"], 2)
        d = self.folder(st)
        lines, doc, _ = self.plan(d)
        cash = 99.470349
        self.assertTrue(doc["flat"])
        self.assertAlmostEqual(doc["new_balance"], cash, places=5)
        text = "\n".join(lines)
        self.assertIn("BALANCE %s -> $99.47 (the night: %s booked -> -$0.53; $100.00 -> $99.47 at the end" % (
            fb.usd(st["balance"]), fb.usd(sum(t["pnl"] for t in trades))), text)
        self.assertIn("the old balance less the start less the old trades' P&L: -$0.04, the old trades' cents", text)
        fb.apply_fix(d, doc)
        st2 = bot.read_json(os.path.join(d, "state.json"), None)
        s = st2["nights"][-1]
        self.assertEqual((s["end"], s["pnl"]), (round(st2["balance"], 2), round(round(st2["balance"], 2) - 100.0, 2)))
        # more than the cents (a night that started with positions held from the night before: $20 of them): the old
        # balance less the old P&L plus the new
        st = state_of(trades)
        st["balance"] += 20.0
        lines, doc, _ = self.plan(self.folder(st))
        self.assertFalse(doc["flat"])
        self.assertAlmostEqual(doc["new_balance"], cash + 20.0, places=5)
        self.assertIn("more than the cents explain", "\n".join(lines))

    def test_launch_states_and_a_creators_buy_are_never_void(self):
        # a snipe at the launch reads mcap 27.96 (the record rounds it: 28.0) or a hair over; the creator's own buy
        # (15 % of the supply) puts the create's price 37 % over the launch, so a coin back at the launch reads -26 %
        for mcap in (27.9, 28.0, 28.1):
            self.assertEqual(fb.void_reasons(trade("L", "01:00:00", "01:00:10", 10.0, -10.0, mcap=mcap)), [], mcap)
        self.assertEqual(fb.void_reasons(trade("D", "01:00:00", "01:00:10", 10.0, -10.0, rise=-20, dev=15.0, mcap=28.5)), [])
        self.assertTrue(fb.void_reasons(trade("L", "01:00:00", "01:00:10", 10.0, -10.0, mcap=27.7)))
        # the night's trades at the launch are kept and replayed
        trades = with_void([trade("L%d" % k, "01:0%d:00" % k, "01:0%d:10" % k, 10.0, -5.0, mcap=28.0) for k in range(3)])
        _, doc, _ = self.plan(self.folder(state_of(trades)), ())
        self.assertEqual((list(doc["voids"]), sorted(doc["replay"]["taken"])), ([3], [0, 1, 2]))

    def test_a_closed_nights_fix_flattens_the_next_nights_curve_and_the_panel_shows_the_current_night(self):
        st0 = state_of(planted(), closed=True)
        t0 = int(fb.epoch(DAY + "23:00:00Z"))
        st0["night"] = {"id": "2026-11-28", "start_equity": round(st0["balance"], 2), "trades": [], "lines": 1,
                        "curve": [[t0, round(st0["balance"], 2)], [t0 + 60, round(st0["balance"], 2)]],
                        "decisions": [{"agent": "LOGGER", "action": "start", "text": "night 2026-11-28 · start $1497.62"}]}
        d = self.folder(st0)
        _, doc, _ = self.plan(d)
        fb.apply_fix(d, doc)
        st = bot.read_json(os.path.join(d, "state.json"), None)
        self.assertEqual(st["night"]["curve"], [[t0, 99.47], [t0 + 60, 99.47]])
        self.assertEqual(st["night"]["decisions"][-1]["text"], "start corrected to $99.47: night 2026-11-27 was fixed "
                                                               "(memebot/fix_book.py)")
        p = bot.read_json(os.path.join(d, "panel.json"), None)
        self.assertEqual((p["night"]["id"], p["trades"], p["curve"], p["balance"]),
                         ("2026-11-28", [], [[t0, 99.47], [t0 + 60, 99.47]], 99.47))
        self.assertEqual(p["log"][-1]["text"], st["night"]["decisions"][-1]["text"])


def with_void(trades):
    """trades and, after the last, one voided by its record (Evelyn's kind: mcap 0, "-100 % since the create")."""
    ev = trade("VOIDED", "03:00:00", "03:00:05", 6.93, -94.2, rise=-100, mcap=0.0, slip=86.64, entry=7.456267e-09,
               fill="curve", exit_price=9.739707e-10, proceeds=0.40, pnl=-6.53)
    return list(trades) + [ev]


def cash_at_0104_30():
    """The planted night's replayed equity at 01:04:30 (N3 open since 01:04:00, at its cost): the cash after N2."""
    return 100.0 - 1.0 + 9.9 * 0.2


if __name__ == "__main__":
    unittest.main()
