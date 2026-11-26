"""memebot/bot.py's New Pairs engine, offline: a FakeFeeds with feeds.Feeds' API (get / drain / watch /
unwatch / fetch_meta / set_holding / status / text) on a pinned clock, and a market that quotes from the
coin's bonding curve like Jupiter's "Pump.fun" route. Numbers from the live probe (probe-result.md):
creates start at 30 SOL / 1.073e9 virtual tokens, readings come every 0.5-1 s, a coin like UZIM went
5 -> 27 SOL in 90 s, a mayhem-mode coin went -89 %."""

import collections
import datetime
import json
import os
import shutil
import tempfile
import threading
import time
import types
import unittest

from _load import load

os.environ["MEMEBOT_LIVE"] = "0"
os.environ["MEMEBOT_FEEDS"] = "0"
mb = load("memebot", "bot")
plan = load("memebot", "plan")
SOL = mb.SOL
K = 30.0 * 1.073e9                      # pump.fun's constant product, SOL x whole tokens
EPOCH = datetime.datetime(1970, 1, 1)
T0 = datetime.datetime(2026, 11, 25, 22, 30)            # 23:30 in Bratislava


def vsol_for(price):
    """The virtual SOL of a pump.fun curve at this price (SOL per whole token): price = vsol^2 / K."""
    return (price * K) ** 0.5


class Clock(object):
    def __init__(self, start=T0):
        self.t = start

    def now(self):
        return self.t

    def epoch(self):
        return (self.t - EPOCH).total_seconds()

    def advance(self, sec):
        self.t += datetime.timedelta(seconds=sec)


def create(mint, sym="UZIM", dev_sol=0.3, creator=None, mayhem=False, pool="pump"):
    vsol = 30.0 + dev_sol
    vtok = K / vsol
    return {"kind": "create", "txType": "create", "signature": "sig-" + mint, "mint": mint,
            "traderPublicKey": creator or "dev-" + mint, "initialBuy": 1.073e9 - vtok, "solAmount": dev_sol,
            "bondingCurveKey": "curve-" + mint, "vTokensInBondingCurve": vtok, "vSolInBondingCurve": vsol,
            "marketCapSol": vsol / vtok * 1e9, "name": sym + " coin", "symbol": sym,
            "uri": "https://meta.example/" + mint, "is_mayhem_mode": mayhem, "pool": pool}


def reading(mint, rsol, slot, complete=False, zero=False):
    """feeds.py's curve event: raw lamports and token units."""
    if zero:
        return {"kind": "curve", "mint": mint, "key": "curve-" + mint, "price_sol": 0.0, "vtok": 0, "vsol": 0, "rtok": 0,
                "rsol": 0, "complete": complete, "slot": slot, "src": "poll"}
    vsol = 30.0 + rsol
    vtok = K / vsol
    return {"kind": "curve", "mint": mint, "key": "curve-" + mint, "price_sol": vsol / vtok, "vtok": int(vtok * 1e6),
            "vsol": int(vsol * 1e9), "rtok": int((vtok - 279.9e6) * 1e6), "rsol": int(rsol * 1e9), "complete": complete,
            "slot": slot, "src": "poll"}


def meta(mint, x=True, tg=True, web=True, ok=True):
    return {"kind": "meta", "mint": mint, "twitter": "https://x.com/" + mint if x else "",
            "telegram": "https://t.me/" + mint if tg else "", "website": "https://%s.fun" % mint if web else "",
            "description": "", "x_kind": "profile" if x else "", "ok": ok, "source": "meta.example", "ms": 90,
            "error": None if ok else "meta.example: HTTP 403"}


def rug_report(mint, risks=(), top=(3.0, 2.0, 1.5), insiders=0, rugged=False, file_meta=None):
    th = [{"address": "curve-ata-" + mint, "owner": "curve-" + mint, "pct": 80.0, "insider": False}]
    th += [{"address": "h%d" % i, "owner": "o%d" % i, "pct": p, "insider": False} for i, p in enumerate(top)]
    return {"mint": mint, "rugged": rugged, "risks": list(risks), "topHolders": th, "totalHolders": 14,
            "graphInsidersDetected": insiders, "markets": [], "knownAccounts": {}, "fileMeta": file_meta or {}}


class CurveMarket(object):
    """Jupiter on the curve: quotes by the pump.fun maths on the coin's last reading (fee_pct each way),
    prices from it, SOL at $100, RugCheck reports from a dict (rug_gate: they wait for it)."""

    def __init__(self):
        self.errors = []
        self.curve = {}
        self.reports = {}
        self.no_route = set()
        self.fee_pct = 1.25
        self.tax = {}                           # mint -> extra cut on sales (a quote the curve maths can't explain)
        self.quote_why = None
        self.quotes = []
        self.rug_calls = []
        self.rug_gate = None
        self.priced = []
        self.sol = 100.0                        # SOL in dollars

    def set_curve(self, mint, vsol, vtok):
        self.curve[mint] = (vsol, vtok)

    def rug_report(self, mint):
        self.rug_calls.append(mint)
        if self.rug_gate is not None:
            self.rug_gate.wait(10)
        return self.reports.get(mint)

    def quote(self, input_mint, output_mint, amount, slippage_bps=300):
        self.quotes.append((input_mint, output_mint, amount))
        self.quote_why = None
        mint = output_mint if input_mint == SOL else input_mint
        if mint in self.no_route or mint not in self.curve:
            self.quote_why = "no Jupiter route"
            return None
        vsol, vtok = self.curve[mint]
        if input_mint == SOL:
            out = mb.curve_buy_tokens(vsol, vtok, amount / 1e9, self.fee_pct) * 1e6
        else:
            out = mb.curve_sell_sol(vsol, vtok, amount / 1e6, self.fee_pct) * 1e9 * (1 - self.tax.get(mint, 0.0))
        return {"outAmount": str(int(out)), "priceImpactPct": "0.012"}

    def prices(self, mints):
        self.priced.append(list(mints))
        out = {SOL: {"usd": self.sol}}
        for m in mints:
            if m in self.curve:
                vsol, vtok = self.curve[m]
                out[m] = {"usd": vsol / vtok * self.sol}
        return out


class FakeFeeds(object):
    """feeds.Feeds' API on a script: an event is due at its time on the test's clock, get() waits by
    moving that clock (never real time), and a curve reading moves the market's curve as it is handed out."""

    def __init__(self, clock, market=None, pump_error=None):
        self.clock, self.market, self.pump_error = clock, market, pump_error
        self.script = []
        self.q = collections.deque()
        self.watching, self.metas, self.holding, self.gets, self.unwatched = {}, [], [], [], []
        self.started = self.stopped = 0
        self.poller = types.SimpleNamespace(ok=0)
        self.handed = 0
        self.answer_meta = {}                   # mint -> the meta event fetch_meta() queues

    def push(self, *evs):
        self.q.extend(evs)

    def at(self, sec, ev):
        """ev is due sec seconds after the clock's start."""
        self.script.append((sec, ev))
        self.script.sort(key=lambda x: x[0])

    def _due(self):
        while self.script and (T0 + datetime.timedelta(seconds=self.script[0][0])) <= self.clock.now():
            self.q.append(self.script.pop(0)[1])

    def _out(self, ev):
        ev = dict(ev)
        ev.setdefault("t", self.clock.epoch())
        if ev.get("kind") == "create":
            ev.setdefault("_rx", self.clock.epoch())
        if ev.get("kind") == "curve" and self.market is not None and ev.get("vtok"):
            self.market.set_curve(ev["mint"], ev["vsol"] / 1e9, ev["vtok"] / 1e6)
        self.handed += 1
        return ev

    def get(self, timeout=0.25):
        self.gets.append(timeout)
        self._due()
        if not self.q and timeout and timeout > 0:
            time.sleep(0.001)                   # the real queue's wait lets the worker threads (RugCheck) run
            if self.script:
                nxt = (T0 + datetime.timedelta(seconds=self.script[0][0]) - self.clock.now()).total_seconds()
                self.clock.advance(max(0.0, min(timeout, nxt)))
            else:
                self.clock.advance(timeout)
            self._due()
        return self._out(self.q.popleft()) if self.q else None

    def drain(self, limit=1000):
        self._due()
        out = []
        while self.q and len(out) < limit:
            out.append(self._out(self.q.popleft()))
        return out

    def watch(self, mint, key):
        self.watching[mint] = key
        return True

    def unwatch(self, mint):
        self.unwatched.append(mint)
        return self.watching.pop(mint, None) is not None

    def fetch_meta(self, mint, uri):
        self.metas.append((mint, uri))
        if mint in self.answer_meta:
            self.q.append(self.answer_meta[mint])

    def set_holding(self, holding):
        self.holding.append(bool(holding))

    def status(self):
        return [{"name": "pumpportal", "connected": self.pump_error is None, "events": self.handed,
                 "last_event_age_s": 0.4, "errors": 0 if self.pump_error is None else 1, "last_error": self.pump_error},
                {"name": "curve-poll", "connected": True, "events": self.handed, "last_event_age_s": 0.2, "errors": 0,
                 "last_error": None},
                {"name": "metadata", "connected": True, "events": len(self.metas), "last_event_age_s": 3.0, "errors": 0,
                 "last_error": None}]

    def text(self):
        return "pumpportal live · curve poll 0.5 s · %d watched · ws off" % len(self.watching)

    def start(self):
        self.started += 1
        return self

    def stop(self, timeout=3.0):
        self.stopped += 1
        return True


# hold-spec.md's tiers as the user first set them (the first step at +7 %): the engine tests below walk these.
# config.json and plan.DEFAULT_TIERS moved the first step to +10 % for the costs (tests/test_levers.py).
SPEC_TIERS = [
    {"name": "scalp", "min_score": 0, "ladder": [[7.0, 1.0]], "trail_pct": 0.0, "time_stop_min": 5.0},
    {"name": "swing", "min_score": 3, "ladder": [[7.0, 0.5], [20.0, 1.0]], "trail_pct": 6.0, "time_stop_min": 10.0},
    {"name": "runner", "min_score": 5, "ladder": [[7.0, 0.34], [20.0, 0.5], [60.0, 1.0]], "trail_pct": 10.0,
     "time_stop_min": 15.0},
    {"name": "moon", "min_score": 7, "ladder": [[7.0, 0.25], [20.0, 0.34], [60.0, 0.5], [100.0, 1.0]],
     "trail_pct": 15.0, "time_stop_min": 20.0},
]


def np_cfg(**kw):
    """The New Pairs settings of config.json as they were when these tests were written (the +7 % first step and
    $3 orders at the least; tests/test_levers.py runs today's config.json), on the code's defaults."""
    c = mb.load_config(None)
    c.update(strategies=["new_pairs"], start_balance=100.0, position_pct=10.0, max_open=8, max_trades_per_night=250,
             loss_streak=5, cooldown_min=2, reenter_min=5, no_entries_last_min=10, stop_pct=-7.0, tp1_pct=7.0,
             tp1_sell_frac=1.0, time_stop_min=5, time_stop_below_pct=2.0, min_order_usd=3.0,
             hold_tiers=json.loads(json.dumps(SPEC_TIERS)), breakeven_pct=1.0, news_refresh_sec=30,
             fade_buy_ratio=0.45)
    c.update(kw)
    return c


# a scalp: Telegram only (1) and every reading a buy (1); 4 SOL in at the trigger (no inflow point)
SCALP = [0.8, 1.3, 1.8, 2.3, 2.8, 3.3, 3.8, 4.3]
# a runner, the probe's UZIM: X, Telegram, web (3), 6.5 SOL in (1), all buys (1)
RUNNER = [1.0, 1.8, 2.5, 3.3, 4.0, 4.9, 5.8, 6.8]


class Engine(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.clock = Clock()
        self.m = CurveMarket()
        self.feeds = FakeFeeds(self.clock, self.m)
        self.out = []
        self.slot = 1000

    def night(self, c=None):
        n = mb.Night(self.dir, c or np_cfg(), self.m, self.clock.now(), self.out.append)
        n.clock_fn = self.clock.now
        n.begin("2026-11-25")
        n.attach(self.feeds)
        self.addCleanup(n.np_close)
        return n

    def turn(self, n, *evs, sec=1.0, left=400):
        """One loop turn: the clock moves sec, the events come in, the bot handles them. RugCheck's calls
        run on worker threads in real time while this clock is simulated: each one not held by rug_gate
        has finished before the turn (on Actions a turn is 0.25 s or more of real time)."""
        if self.m.rug_gate is None or self.m.rug_gate.is_set():
            mb_until(lambda: all(j[0].done() for j in list(n.np_jobs.values())), 2.0)
        self.clock.advance(sec)
        n.now = self.clock.now()
        self.feeds.push(*evs)
        n.np_events(0.25, left)

    def woken(self, mint):
        """Waits for the event a finished RugCheck call puts on the feeds' queue (it wakes the loop)."""
        return mb_until(lambda: any(isinstance(e, dict) and e.get("kind") == "rug" and e.get("mint") == mint
                                    for e in list(self.feeds.q)), 2.0)

    def read(self, n, mint, rsol, **kw):
        self.slot += 1
        self.turn(n, reading(mint, rsol, self.slot, **{k: v for k, v in kw.items() if k not in ("sec", "left")}),
                  **{k: v for k, v in kw.items() if k in ("sec", "left")})

    def launch(self, n, mint, path, social=(False, True, False), sym=None, report=None, **kw):
        """A create, its metadata, its readings; RugCheck has a clean report. -> the turn it was bought on."""
        self.m.reports[mint] = report or rug_report(mint)
        x, tg, web = social
        self.turn(n, create(mint, sym or mint.upper(), **kw))
        self.turn(n, meta(mint, x, tg, web), sec=0.3)
        for k, rsol in enumerate(path):
            self.slot += 1
            self.turn(n, reading(mint, rsol, self.slot))
            if mint in n.state["positions"]:
                return k
        return None

    def gain_rsol(self, n, mint, gain_pct):
        """The real SOL at which the coin's curve price is gain_pct over its entry price (the fill's)."""
        pos = n.state["positions"][mint]
        return vsol_for(pos["entry_price"] / 100.0 * (1 + gain_pct / 100.0)) - 30.0

    def lines(self, n, agent=None):
        return [r["text"] for r in n.state["night"]["decisions"] if agent in (None, r["agent"])]

    # ---- first in
    def test_create_readings_trigger_rugcheck_and_the_buy_on_the_same_loop_turn(self):
        n = self.night()
        k = self.launch(n, "uzim", RUNNER, social=(True, True, True))
        self.assertEqual(k, 7)                                          # the 8th buy reading fires the trigger
        pos = n.state["positions"]["uzim"]
        coin = n.wl.get("uzim")
        self.assertEqual((coin.status, pos["strategy"], pos["plan"]["tier"]), ("entered", "new_pairs", "runner"))
        self.assertEqual(self.feeds.watching["uzim"], "curve-uzim")
        self.assertEqual(self.feeds.metas, [("uzim", "https://meta.example/uzim")])
        self.assertEqual(self.feeds.holding[-1], True)
        self.assertEqual(self.m.rug_calls, ["uzim"])                    # asked half way there, once
        self.assertEqual((pos["sol_in"], pos["dev_pct"], pos["age_s"]), (6.5, 1.06, 8))     # 0.3 SOL: 1.06 % of the supply
        self.assertAlmostEqual(pos["mcap_sol"], 42.1, places=1)
        self.assertAlmostEqual(pos["cost"], 10.0, places=2)             # 10 % of $100, the fees inside
        self.assertEqual(pos["curve_check"], {"buy_pct": 0.0, "sell_pct": 0.0})
        enter = [t for t in self.lines(n, "EXECUTOR") if t.startswith("ENTER")][0]
        self.assertIn("first in · 0.0 s after the trigger · 8 s after launch", enter)
        self.assertIn("hold plan · 5/10 (X, Telegram, web, inflow 6.5 SOL, buys 100%) · runner: +7 → +20 → +60",
                      self.lines(n, "NEWS"))
        self.assertTrue(any(t.startswith("PASS · 14 holders") for t in self.lines(n, "RUG CHECK")))
        self.assertTrue(any(t.startswith("momentum · 6.5 SOL in · 8/8 buys") for t in self.lines(n, "SCANNER")))
        self.assertEqual(n.state["night"]["np_speed"]["first_in"], [0])
        self.assertIn("np:sol:6-12", pos["learn"])
        self.assertIn("tier:runner", pos["learn"])

    def test_without_a_prefetch_the_fire_asks_rugcheck_and_buys_on_the_turn_its_report_lands(self):
        n = self.night(np_cfg(np_rug_prefetch=False))
        # fired: RugCheck asked, never waited for; the buy comes on this turn if the worker thread answered
        # before the turn's end (np_reports), else on the turn its answer wakes
        if self.launch(n, "aaa", SCALP) is None:
            self.assertIn("aaa", n.np_fired)
            self.assertTrue(self.woken("aaa"))
            self.turn(n, sec=0.1)
        self.assertEqual(self.m.rug_calls, ["aaa"])
        self.assertEqual(n.state["positions"]["aaa"]["plan"]["tier"], "scalp")
        enter = [t for t in self.lines(n, "EXECUTOR") if t.startswith("ENTER")][0]
        self.assertRegex(enter, r"first in · 0\.[01] s after the trigger")

    def test_a_slow_rugcheck_report_buys_when_it_lands_within_its_timeout(self):
        self.m.rug_gate = threading.Event()
        n = self.night(np_cfg(np_rug_prefetch=False))
        self.assertIsNone(self.launch(n, "aaa", SCALP))                 # fired, no report yet: no entry yet
        self.assertIn("aaa", n.np_fired)
        self.m.rug_gate.set()
        self.assertTrue(self.woken("aaa"))
        self.turn(n, sec=0.5)                                           # no reading: the report alone buys it
        self.assertIn("aaa", n.state["positions"])
        enter = [t for t in self.lines(n, "EXECUTOR") if t.startswith("ENTER")][0]
        self.assertIn("first in · 0.5 s after the trigger", enter)      # the turn the report woke

    def test_a_rugcheck_fail_is_final(self):
        n = self.night()
        rep = rug_report("bad", risks=[{"name": "Creator history of rugged tokens", "level": "warn"}])
        self.assertIsNone(self.launch(n, "bad", SCALP, report=rep))
        self.assertEqual(n.state["positions"], {})
        self.assertIn("FAIL · risk: Creator history of rugged tokens", self.lines(n, "RUG CHECK"))
        self.assertEqual(n.wl.get("bad").status, "rejected: risk: Creator history of rugged tokens")
        self.read(n, "bad", 5.0)
        self.assertEqual(n.state["positions"], {})

    def test_a_mayhem_coin_is_never_bought(self):
        n = self.night()
        self.assertIsNone(self.launch(n, "scam", RUNNER, social=(True, True, True), mayhem=True))
        self.assertEqual(n.state["positions"], {})
        self.assertNotIn("scam", self.feeds.watching)                   # not even watched
        row = [r for r in n.np_rows() if r["mint"] == "scam"][0]
        self.assertEqual(row["status"], "rejected: mayhem mode")
        self.assertEqual(self.m.rug_calls, [])

    def test_a_learner_skip(self):
        n = self.night()
        labels = ["np:dev:0-2", "np:sol:3-6", "np:buys:90+", "np:age:<30s", "np:social:tg", "news:0-2", "tier:scalp",
                  "hour:23-01"]
        for _ in range(10):
            mb.learner.update(n.mem, labels, -15.0)                     # 10 scalps like it lost 15 % each
        self.assertIsNone(self.launch(n, "lll", SCALP))
        self.assertEqual(n.state["positions"], {})
        self.assertEqual(n.wl.get("lll").status, "rejected: learner: skip")
        self.assertTrue(any(t.startswith("skip · past trades like this lost avg") for t in self.lines(n, "LEARNER")))
        self.read(n, "lll", 4.8)                                        # asked once per trigger, not per reading
        self.assertEqual(n.mem["skipped"], 1)

    def test_risk_full_book_retries_on_the_next_reading(self):
        n = self.night(np_cfg(max_open=0))
        self.assertIsNone(self.launch(n, "aaa", SCALP))
        self.assertIn("no entry: 0 positions open (max)", self.lines(n, "RISK"))
        self.assertEqual(n.wl.get("aaa").status, "watching")
        n.cfg["max_open"] = 8
        self.read(n, "aaa", 4.8)
        self.assertIn("aaa", n.state["positions"])

    def test_a_honeypot_is_rejected(self):
        n = self.night()
        self.m.tax["hp"] = 0.5                                          # half of any sale gone
        self.assertIsNone(self.launch(n, "hp", SCALP))
        self.assertTrue(any("sell simulation: would lose" in t for t in self.lines(n, "RUG CHECK")))
        self.assertEqual(n.wl.get("hp").status, "rejected: sell simulation")

    # ---- first out
    def test_a_reading_past_the_stop_sells_in_the_same_turn(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        self.read(n, "aaa", self.gain_rsol(n, "aaa", -6))               # -6 % against the fill: not yet
        self.assertIn("aaa", n.state["positions"])
        self.read(n, "aaa", self.gain_rsol(n, "aaa", -9))               # -9 %: under the stop, sold on this turn
        self.assertEqual(n.state["positions"], {})
        t = n.state["night"]["trades"][0]
        self.assertEqual(t["why"], "stop -9%")
        self.assertEqual((t["strategy"], t["tier"], t["score"], t["steps"], t["exit_ms"]), ("new_pairs", "scalp", 2, 0, 0))
        self.assertAlmostEqual(t["proceeds"] - t["cost"], t["pnl"], places=1)
        self.assertIn("out · 0.0 s after the reading", [x for x in self.lines(n, "EXECUTOR") if x.startswith("EXIT")][0])
        self.assertEqual(self.feeds.holding[-1], False)
        self.assertEqual(n.wl.get("aaa").status, "exited: %s" % t["why"])

    def test_a_scalp_takes_everything_at_plus_7(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        self.read(n, "aaa", self.gain_rsol(n, "aaa", 7.3))
        t = n.state["night"]["trades"][0]
        self.assertEqual((t["why"], t["steps"]), ("TP +7%", 1))
        self.assertGreater(t["pnl"], 0)

    def test_the_runner_ladder_sells_34_then_50_then_the_rest(self):
        n = self.night()
        self.launch(n, "uzim", RUNNER, social=(True, True, True))
        raw0 = n.state["positions"]["uzim"]["raw"]
        self.read(n, "uzim", self.gain_rsol(n, "uzim", 8))
        pos = n.state["positions"]["uzim"]
        self.assertEqual(pos["raw"], raw0 - int(raw0 * 0.34))
        # stop to break-even: where the rest's sale leaves the whole trade at 0 after its costs (about +4.3 %)
        be = n.np_plan_cfg(pos, pos["plan"], n.wl.get("uzim"), 100.0)["breakeven_pct"]
        self.assertEqual((pos["plan"]["step"], pos["plan"]["stop_pct"]), (1, round(be, 2)))
        self.assertTrue(3.5 < be < 5.0, be)
        take = [x for x in self.lines(n, "EXECUTOR") if x.startswith("TP")]
        self.assertRegex(take[0], r"^TP \+7% · sold 34% for \$\d+\.\d\d · stop to break-even · out · 0\.0 s after")
        raw1 = pos["raw"]
        self.read(n, "uzim", self.gain_rsol(n, "uzim", 21))
        self.assertEqual(n.state["positions"]["uzim"]["raw"], raw1 - int(raw1 * 0.5))
        self.read(n, "uzim", self.gain_rsol(n, "uzim", 61))
        self.assertEqual(n.state["positions"], {})
        t = n.state["night"]["trades"][0]
        self.assertEqual((t["why"], t["tier"], t["steps"], t["score"]), ("TP +60%", "runner", 3, 5))
        self.assertGreater(t["pnl_pct"], 15)

    def test_after_the_first_step_the_rest_never_turns_into_a_loss(self):
        n = self.night()
        self.launch(n, "uzim", RUNNER, social=(True, True, True))
        self.read(n, "uzim", self.gain_rsol(n, "uzim", 8))
        self.read(n, "uzim", self.gain_rsol(n, "uzim", 5))
        self.assertIn("uzim", n.state["positions"])
        # break-even counted in dollars: the rest's sale (pump.fun's fee, the impact, the haircut, $0.10 an
        # order) at this gain leaves the whole trade at 0; a stop at +1 % of the price lost 2.2 %
        pos = n.state["positions"]["uzim"]
        be = n.np_plan_cfg(pos, pos["plan"], n.wl.get("uzim"), 100.0)["breakeven_pct"]
        self.read(n, "uzim", self.gain_rsol(n, "uzim", be - 0.001))
        t = n.state["night"]["trades"][0]
        self.assertEqual((t["why"], t["steps"]), ("break-even stop (peak +8%)", 1))
        self.assertGreaterEqual(t["pnl"], -0.01)
        self.assertLess(abs(t["pnl_pct"]), 0.2)
        self.assertEqual(n.state["night"]["streak"], 0)

    def test_a_dump_exits_at_once(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        self.read(n, "aaa", 4.9)
        self.read(n, "aaa", 1.8)                                        # one reading 3.1 SOL lower
        t = n.state["night"]["trades"][0]
        self.assertEqual(t["why"], "dump: 3.1 SOL sold")

    def test_the_curve_completing_sells_into_its_last_state_by_the_checked_curve_maths(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        self.read(n, "aaa", 4.5)
        raw = n.state["positions"]["aaa"]["raw"]
        self.m.no_route.add("aaa")                                      # migrating: no route for a while
        self.read(n, "aaa", 0.0, zero=True, complete=True)
        t = n.state["night"]["trades"][0]
        self.assertEqual((t["why"], t["fill"]), ("migrating: take it", "curve"))
        line = [x for x in self.lines(n, "EXECUTOR") if x.startswith("EXIT")][0]
        self.assertIn("by the curve maths (Jupiter: no Jupiter route; it was 0.0% off a quote at the entry)", line)
        # the last usable reading (34.5 SOL), pump.fun's fee, the 1 % haircut, the network and priority fees
        want = mb.curve_sell_sol(34.5, K / 34.5, raw / 1e6, 1.25) * 100 * 0.99 - 0.001 * 100
        self.assertAlmostEqual(t["proceeds"], want, places=2)

    def test_an_unchecked_curve_maths_never_prices_a_sale(self):
        n = self.night(np_cfg(max_roundtrip_loss_pct=15.0))           # the 6 % "tax" passes the honeypot test
        self.m.tax["aaa"] = 0.06                                        # Jupiter's sale 6 % under the curve maths
        self.launch(n, "aaa", SCALP)
        pos = n.state["positions"]["aaa"]
        self.assertAlmostEqual(pos["curve_check"]["sell_pct"], 6.4, places=1)            # 1 / 0.94: over 5 %
        self.m.no_route.add("aaa")
        self.read(n, "aaa", 0.0, zero=True, complete=True)
        self.assertIn("aaa", n.state["positions"])                      # stuck, tried again in 5 s
        self.assertTrue(any(x.startswith("can't sell (no Jupiter route)") for x in self.lines(n, "EXECUTOR")))

    def test_a_migrate_event_sells_a_held_coin(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        self.turn(n, {"kind": "migrate", "txType": "migrate", "mint": "aaa", "pool": "pump-amm", "signature": "s"})
        self.assertEqual(n.state["night"]["trades"][0]["why"], "migrating: take it")
        self.assertIn("migrated: the curve is complete (pump-amm)", self.lines(n, "SCANNER"))

    def test_a_price_of_zero_is_never_a_price(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        last = n.state["positions"]["aaa"]["last"]
        self.read(n, "aaa", 0.0, zero=True)                             # zeroed, not complete: a broken reading
        self.assertEqual((n.state["positions"]["aaa"]["last"], n.state["night"]["trades"]), (last, []))

    def test_the_time_stop_and_the_stale_guard(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        self.clock.advance(6)
        n.now = self.clock.now()
        n.watch(400)                                                    # no reading for 6 s: Jupiter's price
        self.assertIn(["aaa"], self.m.priced)
        self.assertTrue(any(x.startswith("no curve reading for 5 s · Jupiter's price") for x in self.lines(n, "EXECUTOR")))
        self.assertIn("aaa", n.state["positions"])
        self.clock.advance(5 * 60)
        n.now = self.clock.now()
        n.watch(400)
        t = n.state["night"]["trades"][0]
        self.assertRegex(t["why"], r"^5 min, only -\d%$")

    def test_a_quiet_curve_the_poller_still_reads_is_not_stale(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        for _ in range(8):
            self.feeds.poller.ok += 1                                   # good rounds, nothing changed
            self.turn(n, sec=1.0)
            n.watch(400)
        self.assertTrue(all(p == [] for p in self.m.priced), self.m.priced)     # SOL only, never the coin
        self.assertFalse(any("no curve reading" in x for x in self.lines(n, "EXECUTOR")))

    def test_the_end_of_the_night_sells_new_pairs(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        self.clock.advance(1)
        n.now = self.clock.now()
        n.watch(4)                                                      # 4 minutes left
        self.assertEqual(n.state["night"]["trades"][0]["why"], "end of night")

    def test_news_fading_sells_a_coin_in_profit(self):
        n = self.night()
        self.launch(n, "uzim", RUNNER, social=(True, True, True))
        pos, coin = n.state["positions"]["uzim"], n.wl.get("uzim")
        self.assertEqual(pos["plan"]["entry_score"], 5)
        # five minutes on: the last 5 minutes had 3 buys against 9 sells and 1 SOL out; the price still +5 %
        now = self.clock.epoch() + 330
        n.np_win["uzim"] = [[coin.t0, 0, 0, coin.rsol0], [now - 301, coin.buys, coin.sells, coin.rsol]]
        coin.buys, coin.sells, coin.rsol = coin.buys + 3, coin.sells + 9, coin.rsol - 1.0
        self.clock.advance(330)
        n.now = self.clock.now()
        pos["last"] = pos["entry_price"] * 1.05
        info = n.np_now_info("uzim", coin)
        self.assertEqual((info["buys"], info["sells"], info["sol_in"]), (3, 9, -1.0))
        n.np_exit("uzim", coin, n.epoch(), 400, px_usd=pos["entry_price"] * 1.05)     # the refresh is due
        t = n.state["night"]["trades"][0]
        self.assertTrue(t["why"].startswith("fading: buys 25%"), t["why"])
        self.assertIn("downgrade runner → swing (inflow slowed, fewer buys)", self.lines(n, "NEWS"))

    def test_a_restart_keeps_the_book_and_watches_the_held_coin_again(self):
        n = self.night()
        self.launch(n, "aaa", SCALP)
        n.save()
        feeds2 = FakeFeeds(self.clock, self.m)
        n2 = mb.Night(self.dir, np_cfg(), self.m, self.clock.now(), self.out.append)
        n2.clock_fn = self.clock.now
        n2.begin("2026-11-25")
        n2.attach(feeds2)
        self.addCleanup(n2.np_close)
        self.assertEqual((feeds2.watching, feeds2.holding[-1]), ({"aaa": "curve-aaa"}, True))
        self.assertEqual(n2.wl.get("aaa").status, "entered")
        self.feeds = feeds2
        self.read(n2, "aaa", 2.7)
        self.assertEqual(n2.state["night"]["trades"][0]["why"][:5], "stop ")

    # ---- the page
    def test_panel_watch_plan_and_speed(self):
        n = self.night()
        self.launch(n, "uzim", RUNNER, social=(True, True, True))
        self.turn(n, create("nxt", "NXT"))
        n.watch(400)
        p = n.panel_doc()
        self.assertEqual([r["symbol"] for r in p["watch"]], ["NXT", "UZIM"])
        self.assertEqual(set(p["watch"][0]), {"symbol", "name", "mint", "age_s", "sol_in", "buys", "sells", "rise_pct",
                                              "mcap_sol", "dev_pct", "big_sell", "x", "tg", "web", "status", "why"})
        self.assertEqual(p["watch"][1]["status"], "entered")
        pos = p["positions"][0]
        self.assertEqual((pos["strategy"], pos["tier"], pos["score"], pos["steps"], pos["sol_in"], pos["age_s"]),
                         ("new_pairs", "runner", 5, 0, 6.5, 8))
        self.assertEqual(set(pos["plan"]), {"tier", "score", "parts", "ladder", "step", "stop_pct", "trail_pct"})
        sp = p["speed"]
        self.assertEqual((sp["first_in_ms"], sp["first_in_last_ms"], sp["exit_ms"], sp["exit_last_ms"]), (0, 0, None, None))
        self.assertEqual(sp["feeds"][0]["name"], "pumpportal")
        self.assertEqual(sp["feed"], "pumpportal live · curve poll 0.5 s · 2 watched · ws off · live 1s")
        self.assertEqual((sp["watching"], sp["followed"], sp["strategies"]), (1, 2, ["new_pairs"]))
        self.assertGreater(sp["creates_per_min"], 0)
        self.assertEqual(p["strategies"], ["new_pairs"])
        self.read(n, "uzim", self.gain_rsol(n, "uzim", -9))
        p = n.panel_doc()
        t = p["trades"][0]
        self.assertEqual((t["strategy"], t["tier"], t["score"], t["steps"], t["sol_in"], t["dev_pct"]),
                         ("new_pairs", "runner", 5, 0, 6.5, 1.06))
        self.assertNotIn("learn", t)
        self.assertEqual(p["speed"]["exit_last_ms"], 0)

    def test_the_tick_carries_watch_and_speed_and_stays_small(self):
        n = self.night(np_cfg(max_open=8))
        for i in range(8):                                              # a full book: 8 runners (44-character mints)
            self.launch(n, ("c%02d" % i).ljust(40, "x") + "pump", RUNNER, social=(True, True, True), sym="COIN%d" % i)
        self.assertEqual(len(n.state["positions"]), 8)
        for i in range(20):                                             # and 20 newer coins on the watch list
            mint = ("w%02d" % i).ljust(40, "x") + "pump"
            self.turn(n, create(mint, "WATCH%d" % i), meta(mint), sec=0.1)
        n.watch(400)
        memo = {}
        t1 = mb.tick_msg(n.state, n.cfg, {}, n.ts(), 1, memo, n.speed, watch=n.np_rows())
        self.assertEqual(len(t1["watch"]), 20)
        self.assertEqual(set(t1["watch"][0]) - {"why"}, {"symbol", "name", "mint", "age_s", "sol_in", "buys", "sells",
                                                         "rise_pct", "mcap_sol", "dev_pct", "x", "tg", "web", "status"})
        self.assertEqual((t1["watch"][0]["x"], t1["watch"][0]["name"]), (True, "WATCH19 coin"))   # the panel has the link
        self.assertEqual(n.panel_doc()["watch"][0]["x"], "https://x.com/w19" + "x" * 37 + "pump")
        self.assertIn("first_in_ms", t1["speed"])
        self.assertTrue(all("plan" in p and "hist" not in p for p in t1["positions"]))
        size = len(json.dumps(t1, separators=(",", ":"), allow_nan=False))
        t2 = mb.tick_msg(n.state, n.cfg, {}, n.ts(), 2, memo, n.speed, watch=n.np_rows())
        size2 = len(json.dumps(t2, separators=(",", ":"), allow_nan=False))
        # the worst case, a full book of 8 and 20 rows with real-length mints, no new log rows: about 12 KB
        # (the 20 rows about 5 KB, each held coin about 0.7 KB; the ticks before New Pairs were about 3 KB)
        self.assertLess(size2, 13000, size2)
        self.assertLess(len(json.dumps(t2["watch"], separators=(",", ":"))), 5500)
        self.assertGreater(size, size2)

    def test_the_minute_line_and_the_feeds_health(self):
        n = self.night()
        self.turn(n, create("aaa", "AAA"), create("bbb", "BBB", dev_sol=3.0))      # 3 SOL: a 9 % dev buy
        n.watch(400)
        line = [x for x in self.lines(n, "SCANNER") if x.startswith("new pairs ·")][0]
        self.assertIn("1 rejected (dev bought 1)", line)
        self.assertIn("feeds · pumpportal live · curve poll 0.5 s · 2 watched · ws off", self.lines(n, "SCANNER"))
        self.feeds.pump_error = "closed 1002 invalid status code"
        for _ in range(62):
            self.clock.advance(1)
            n.now = self.clock.now()
            n.watch(400)
        down = [x for x in self.lines(n, "SCANNER") if x.startswith("pumpportal down")]
        self.assertEqual(len(down), 1)
        self.assertIn("closed 1002 invalid status code", down[0])
        self.feeds.pump_error = None
        n.watch(400)
        self.assertTrue(any(x.startswith("pumpportal live again") for x in self.lines(n, "SCANNER")))


def mb_until(cond, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return bool(cond())


class Rec(object):
    def __init__(self):
        self.sent = []

    def panel(self, doc):
        self.sent.append(("panel", doc))

    def tick(self, doc):
        self.sent.append(("tick", doc))

    def stats(self):
        return {}

    def stop(self, wait=3.0):
        pass


class Run(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_a_run_driven_by_the_feeds_buys_and_sells_on_the_turn_of_the_reading(self):
        start = datetime.datetime(2026, 11, 25, 12, 0)
        clock = Clock(start)
        m = CurveMarket()
        feeds = FakeFeeds(clock, m)
        m.reports["aaa"] = rug_report("aaa")
        global T0
        old, T0 = T0, start                                             # the script's seconds count from the run's start
        try:
            feeds.at(2, create("aaa", "AAA"))
            feeds.at(2.4, meta("aaa", False, True, False))
            for k, rsol in enumerate(SCALP + [4.8, 5.5, 6.0]):
                feeds.at(3 + 0.7 * k, reading("aaa", rsol, 2000 + k))
            live = Rec()
            night = mb.run(self.dir, np_cfg(), minutes=2, anytime=True, market=m, now_fn=clock.now, sleep=clock.advance,
                           out=lambda *a: None, live=live, feeds=feeds)
        finally:
            T0 = old
        self.assertEqual(night, "2026-11-25-test")
        self.assertEqual((feeds.started, feeds.stopped), (1, 1))
        self.assertTrue(feeds.gets and max(feeds.gets) <= 0.25)       # it waited on the queue, a quarter second at most
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        trades = st["last_night"]["trades"]
        self.assertEqual([t["why"] for t in trades], ["TP +7%"])        # +7 % on the 11th reading (6 SOL)
        rows = st["last_night"]["decisions"]
        enter = [r for r in rows if r["text"].startswith("ENTER")][0]
        exit_ = [r for r in rows if r["text"].startswith("EXIT")][0]
        self.assertEqual(enter["t"], "2026-11-25T12:00:07Z")            # the 8th reading's second (3 + 7 x 0.7 = 7.9 s)
        self.assertEqual(exit_["t"], "2026-11-25T12:00:10Z")            # the 11th's (10.0 s)
        self.assertIn("first in · 0.0 s after the trigger", enter["text"])
        self.assertIn("out · 0.0 s after the reading", exit_["text"])
        ticks = [d for k, d in live.sent if k == "tick"]
        self.assertTrue(any(t["watch"] for t in ticks))
        self.assertTrue(110 <= len(ticks) <= 130, len(ticks))          # one a second, and one after each trade
        panels = [d for k, d in live.sent if k == "panel"]
        self.assertEqual(panels[-1]["trades"][-1]["why"], "TP +7%")
        self.assertEqual(st["positions"], {})

    def test_without_feeds_the_night_runs_on_and_says_why_once(self):
        start = datetime.datetime(2026, 11, 25, 12, 0)
        clock = Clock(start)
        night = mb.run(self.dir, np_cfg(), minutes=1, anytime=True, market=CurveMarket(), now_fn=clock.now,
                       sleep=clock.advance, out=lambda *a: None)
        self.assertEqual(night, "2026-11-25-test")
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        off = [r["text"] for r in st["last_night"]["decisions"] if "no feeds" in r["text"]]
        self.assertEqual(off, ["new pairs off: no feeds (MEMEBOT_FEEDS=0) · the night goes on without them"])
        p = mb.read_json(os.path.join(self.dir, "panel.json"), {})
        self.assertEqual(p["speed"]["feeds"][0]["last_error"], "MEMEBOT_FEEDS=0")
        self.assertIn("new pairs off", p["speed"]["feed"])

    def test_without_websockets_pumpportal_is_off_and_the_rest_runs(self):
        start = datetime.datetime(2026, 11, 25, 12, 0)
        clock = Clock(start)
        feeds = FakeFeeds(clock, CurveMarket(), pump_error="websockets not installed (pip install websockets==17.4)")
        mb.run(self.dir, np_cfg(), minutes=1, anytime=True, market=CurveMarket(), now_fn=clock.now, sleep=clock.advance,
               out=lambda *a: None, feeds=feeds)
        st = mb.read_json(os.path.join(self.dir, "state.json"), {})
        said = [r["text"] for r in st["last_night"]["decisions"] if r["text"].startswith("pumpportal off")]
        self.assertEqual(len(said), 1)
        self.assertIn("pip install websockets==17.4", said[0])
        p = mb.read_json(os.path.join(self.dir, "panel.json"), {})
        self.assertIn("not installed", p["speed"]["feeds"][0]["last_error"])

    def test_feeds_py_missing_is_reported_not_raised(self):
        real = mb.feeds_mod
        try:
            mb.feeds_mod = None
            os.environ["MEMEBOT_FEEDS"] = "1"
            feeds, why = mb.feeds_for(np_cfg())
        finally:
            mb.feeds_mod = real
            os.environ["MEMEBOT_FEEDS"] = "0"
        self.assertIsNone(feeds)
        self.assertTrue(why.startswith("feeds.py did not load"), why)
        self.assertEqual(mb.feeds_for(np_cfg(strategies=["migrated"])), (None, "not in config.json strategies"))
        self.assertEqual(mb.strategies({"strategies": "migrated"}), ["migrated"])
        self.assertEqual(mb.strategies({}), ["new_pairs"])
        self.assertEqual(mb.strategies({"strategies": ["new_pairs", "bogus", "migrated"]}), ["new_pairs", "migrated"])

    def test_a_broken_newpairs_py_switches_new_pairs_off_and_says_so(self):
        real = mb.newpairs, mb.NEWPAIRS_OFF
        try:
            mb.newpairs, mb.NEWPAIRS_OFF = mb.load_part("newpairs", ("Watchlist",), path=os.path.join(self.dir, "nope.py"))
            n = mb.Night(self.dir, np_cfg(), CurveMarket(), T0, lambda *a: None)
            n.begin("2026-11-25")
            n.watch(400)
        finally:
            mb.newpairs, mb.NEWPAIRS_OFF = real
        said = [r["text"] for r in n.state["night"]["decisions"] if r["action"] == "off"]
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith("new pairs off: newpairs.py did not load (FileNotFoundError"), said[0])
        self.assertIsNone(n.wl)

    def test_the_real_feeds_bundle_is_built_from_the_config(self):
        os.environ["MEMEBOT_FEEDS"] = "1"
        try:
            feeds, why = mb.feeds_for(np_cfg(curve_poll_sec=0.5, curve_idle_sec=1.0))
        finally:
            os.environ["MEMEBOT_FEEDS"] = "0"
        self.assertIsNone(why)
        for name in ("get", "drain", "watch", "unwatch", "fetch_meta", "set_holding", "status", "text", "start", "stop"):
            self.assertTrue(callable(getattr(feeds, name)), name)
        feeds.set_holding(True)
        self.assertEqual(feeds.poller.interval, 0.5)                    # never started: no network in this test


if __name__ == "__main__":
    unittest.main()
