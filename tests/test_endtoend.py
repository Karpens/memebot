"""The whole chain, offline: the live engine (bot.run with config.json as it is, the tape recorder on, MQTT off)
trades a realistic synthetic pump.fun stream and records its tapes; memebot/replay.py and memebot/backtest.py
then replay those tapes, and the replay must make the live run's trades again.

The stream is made up (never a claim about real coins), shaped by the live facts of 2026-11-26: about 3 creates
a minute, most rejected (the creator's buy over 8 %, mayhem mode, serial launchers), duds, and pumpers whose
momentum holds past the 45 s minimum age, then run, fade, dump or chop. Each coin is a list of TRADES on
pump.fun's constant product; the curve poller reads the WATCHED curves every 0.5 s while something is held, 1 s
otherwise, answers 0.15-0.45 s later with the state at its request, and sends changes only (feeds.CurvePoller);
RugCheck answers 0.28-0.46 s after an ask on the simulated clock (the probe), sometimes with no report; some
metadata uris fail after 2 s (ipfs.io's 403) and RugCheck's copy of the metadata gives the links (or not); SOL
drifts around $71. Jupiter quotes either the curve as of the last poll answered (what the live log showed:
"curve maths 0.0% off Jupiter") or the TRUE curve at the quote's time (trades after the last poll included, like
the live HOGZ fill 1.9 % under its reading): the replay only has the readings, so then its fills can differ."""

import bisect
import collections
import copy
import datetime
import heapq
import json
import math
import os
import random
import shutil
import tempfile
import types
import unittest
import unittest.mock

from _load import load

os.environ["MEMEBOT_LIVE"] = "0"
os.environ["MEMEBOT_FEEDS"] = "0"
bt = load("memebot", "backtest")
R = bt.replay
bot = R.bot
SOL = bot.SOL
K = 30.0 * 1.073e9
EPOCH = datetime.datetime(1970, 1, 1)
START = datetime.datetime(2026, 11, 26, 9, 0)
END_RSOL = bot.newpairs.END_RSOL        # a standard curve completes at about 85 real SOL (its last tokens sold)


class Clock(object):
    """The run's clock: a naive UTC datetime, moved in whole microseconds (what a datetime holds)."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def epoch(self):
        return (self.t - EPOCH).total_seconds()

    def set_epoch(self, e):
        if e > self.epoch():
            self.t = EPOCH + datetime.timedelta(microseconds=int(math.ceil(e * 1e6 - 1e-3)))

    def advance(self, sec):
        self.t += datetime.timedelta(seconds=max(0.0, sec))


# ---------------------------------------------------------------- the coins

class SimCoin(object):
    def __init__(self, mint, t0, sym, dev_sol, creator, mayhem=False, kind="dud"):
        self.mint, self.t0, self.sym, self.dev_sol, self.creator, self.mayhem, self.kind = \
            mint, t0, sym, dev_sol, creator, mayhem, kind
        self.trades = []            # (t, SOL): + a buy, - a sell
        self.meta_ok, self.meta_s, self.links = True, 0.1, (False, False, False)
        self.report = None
        self._ts = self._cum = None

    def finish(self):
        """The trades in time order and the real SOL after each. A curve completes when its real SOL reaches
        END_RSOL (pump.fun sells its last tokens there): from then on it reads that state with complete set, and no
        trade moves it again (the coin migrates). A standard curve never reads past it: the stream is sane, every
        reading (newpairs.curve_sane)."""
        self.trades.sort()
        self._ts = [t for t, _ in self.trades]
        self._cum, s = [], 0.0
        self.t_done = None
        for t, x in self.trades:
            s = max(0.0, s + x)
            if self.t_done is None and self.dev_sol + s >= END_RSOL:
                self.t_done = t
            self._cum.append(s)

    def done_at(self, t):
        return self.t_done is not None and t >= self.t_done

    def rsol_at(self, t):
        if self.done_at(t):
            return END_RSOL
        i = bisect.bisect_right(self._ts, t) - 1
        return self.dev_sol + (self._cum[i] if i >= 0 else 0.0)

    def create_event(self):
        vsol = 30.0 + self.dev_sol
        vtok = K / vsol
        return {"kind": "create", "txType": "create", "signature": "sig-" + self.mint, "mint": self.mint,
                "traderPublicKey": self.creator, "initialBuy": 1.073e9 - vtok, "solAmount": self.dev_sol,
                "bondingCurveKey": "curve-" + self.mint, "vTokensInBondingCurve": vtok, "vSolInBondingCurve": vsol,
                "marketCapSol": vsol / vtok * 1e9, "name": self.sym + " coin", "symbol": self.sym,
                "uri": "https://meta.example/" + self.mint, "is_mayhem_mode": self.mayhem, "pool": "pump"}


def arrivals(rng, a, b, rate):
    out, t = [], a
    while rate > 0:
        t += rng.expovariate(rate)
        if t >= b:
            return out
        out.append(t)
    return out


def flow(rng, c, a, b, rate, buy_share, lo, hi):
    for t in arrivals(rng, a, b, rate):
        x = rng.uniform(lo, hi)
        c.trades.append((t, x if rng.random() < buy_share else -x * rng.uniform(0.6, 1.2)))


def make_coin(rng, i, t0):
    mint = ("e%05d" % i).ljust(40, "x") + "pump"
    roll = rng.random()
    kind = "devheavy" if roll < 0.30 else "mayhem" if roll < 0.38 else "dud" if roll < 0.66 else "pumper"
    dev = rng.uniform(3.0, 6.0) if kind == "devheavy" else rng.uniform(0.05, 1.2)
    c = SimCoin(mint, t0, "E%d" % i, dev, "dev-%d" % i, mayhem=(kind == "mayhem"), kind=kind)
    c.links = (rng.random() < 0.4, rng.random() < 0.55, rng.random() < 0.3)
    file_meta = None
    if rng.random() < 0.18:                     # the uri fails; RugCheck's copy of the metadata may have the links
        c.meta_ok, c.meta_s = False, 2.0
        if rng.random() < 0.6:
            x, tg, web = c.links
            file_meta = {"name": c.sym + " coin", "symbol": c.sym, "twitter": "https://x.com/" + mint if x else "",
                         "telegram": "https://t.me/" + mint if tg else "", "website": "https://%s.fun" % mint if web else ""}
    else:
        c.meta_s = rng.uniform(0.07, 0.12)
    top = sorted([rng.uniform(1.0, 9.0) for _ in range(rng.randint(3, 9))], reverse=True)
    if rng.random() < 0.12:
        top[0] = rng.uniform(13.0, 22.0)        # a big holder: RUG CHECK says no
    risks = [{"name": "Creator history of rugged tokens", "level": "danger"}] if rng.random() < 0.08 else []
    holders = [{"address": "curve-ata-" + mint, "owner": "curve-" + mint, "pct": 80.0, "insider": False}]
    holders += [{"address": "h%d-%s" % (k, mint), "owner": "o%d-%s" % (k, mint), "pct": round(p, 3), "insider": False}
                for k, p in enumerate(top)]
    c.report = {"mint": mint, "rugged": False, "risks": risks, "topHolders": holders, "totalHolders": rng.randint(5, 40),
                "graphInsidersDetected": 1 if rng.random() < 0.05 else 0, "markets": [], "knownAccounts": {},
                "score_normalised": rng.randint(1, 30), "fileMeta": file_meta or {}}
    if rng.random() < 0.05:
        c.report = None                         # not indexed: RugCheck has no report
    if kind in ("devheavy", "mayhem"):
        flow(rng, c, t0 + 1, t0 + 240, rng.uniform(0.2, 1.5), 0.55, 0.05, 0.8)
    elif kind == "dud":
        near = rng.random() < 0.4
        flow(rng, c, t0 + 1, t0 + 60, rng.uniform(0.2, 0.9), 0.7 if near else 0.55, 0.05, 0.5)
        flow(rng, c, t0 + 60, t0 + 400, rng.uniform(0.05, 0.4), 0.4, 0.05, 0.4)
    else:
        flow(rng, c, t0 + 0.5, t0 + 15, rng.uniform(0.3, 1.0), 0.9, 0.1, 0.6)          # the snipers
        if rng.random() < 0.35:
            c.trades.append((t0 + rng.uniform(12, 40), -rng.uniform(1.0, 2.6)))       # one sells into it
        tm = t0 + rng.uniform(20, 55)
        flow(rng, c, tm, tm + rng.uniform(40, 80), rng.uniform(0.8, 2.0), rng.uniform(0.72, 0.9), 0.08, 0.55)
        te = max(t for t, _ in c.trades) if c.trades else t0 + 60
        story = rng.random()
        c.kind = "pumper/" + ("runner" if story < 0.3 else "fade" if story < 0.55 else "dump" if story < 0.8 else "chop")
        if story < 0.3:
            flow(rng, c, te, te + rng.uniform(60, 200), rng.uniform(1.0, 2.5), 0.78, 0.1, 0.9)
            te2 = max(t for t, _ in c.trades)
            flow(rng, c, te2, te2 + 300, rng.uniform(0.5, 1.5), 0.3, 0.1, 0.8)
            if rng.random() < 0.5:
                c.trades.append((te2 + rng.uniform(5, 60), -rng.uniform(2.5, 6.0)))
        elif story < 0.55:
            flow(rng, c, te, te + 30, 1.0, 0.65, 0.05, 0.5)
            flow(rng, c, te + 30, te + 300, rng.uniform(0.3, 1.0), 0.3, 0.05, 0.5)
        elif story < 0.8:
            c.trades.append((te + rng.uniform(3, 25), -rng.uniform(2.2, 4.5)))
            flow(rng, c, te + 25, te + 200, rng.uniform(0.2, 0.8), 0.35, 0.05, 0.4)
        else:
            flow(rng, c, te, te + rng.uniform(60, 200), rng.uniform(0.3, 1.0), 0.5, 0.05, 0.3)
    c.finish()
    return c


def make_stream(seed, minutes, t_start, per_min=3.0):
    rng = random.Random(seed)
    coins, last = [], None
    for i, t in enumerate(arrivals(rng, t_start + 2, t_start + minutes * 60 - 5, per_min / 60.0)):
        c = make_coin(rng, i, t)
        if last is not None and rng.random() < 0.06:
            c.creator = last                    # a serial launcher
        last = c.creator
        coins.append(c)
    return coins


# ---------------------------------------------------------------- RugCheck and SOL's price on the simulated clock

class SimFut(object):
    def __init__(self, clock, ready, value=None, exc=None):
        self.clock, self.ready, self.value, self.exc = clock, ready, value, exc
        self.cbs, self._cancelled, self.fired = [], False, False

    def done(self):
        return self._cancelled or self.clock.epoch() >= self.ready - 1e-9

    def cancelled(self):
        return self._cancelled

    def cancel(self):
        if self.done():
            return False
        self._cancelled = True
        return True

    def result(self, timeout=None):
        if self._cancelled or not self.done():
            raise TimeoutError()
        if self.exc is not None:
            raise self.exc
        return self.value

    def add_done_callback(self, fn):
        if self.done():
            self.fired = True
            fn(self)
        else:
            self.cbs.append(fn)

    def fire(self):
        if not (self.fired or self._cancelled):
            self.fired = True
            for fn in self.cbs:
                fn(self)


class SimPool(object):
    def __init__(self, feeds, rng):
        self.feeds, self.rng = feeds, rng

    def submit(self, fn, *args):
        now = self.feeds.clock.epoch()
        lat = self.rng.uniform(0.28, 0.46) if getattr(fn, "__name__", "") in ("rug_report", "rug_report_np") else 0.06
        try:
            fut = SimFut(self.feeds.clock, now + lat, fn(*args))
        except Exception as e:                  # noqa: BLE001
            fut = SimFut(self.feeds.clock, now, exc=e)
        if not fut.done():
            self.feeds.at(fut.ready, "timer", fut)
        return fut

    def shutdown(self, wait=True, cancel_futures=False):
        pass


class Market(object):
    """Jupiter on the curve (pump.fun's maths, 1.25 % fee): jup "reading" quotes the curve as of the last poll
    answered, "true" the curve at the quote's time. RugCheck: each coin's report. SOL about $71."""

    def __init__(self, coins, clock, feeds, jup):
        self.coins = {c.mint: c for c in coins}
        self.clock, self.feeds, self.jup = clock, feeds, jup
        self.errors, self.quote_why = [], None

    def sol_now(self):
        return round(71.14 * (1 + 0.004 * math.sin(self.clock.epoch() / 400.0)), 4)

    def state(self, mint):
        c = self.coins.get(mint)
        if c is None:
            return None
        t = self.clock.epoch()
        if self.jup == "reading" and self.feeds.answered is not None:
            t = self.feeds.answered
        v = 30.0 + c.rsol_at(t)
        return v, K / v

    def quote(self, input_mint, output_mint, amount, slippage_bps=300):
        self.quote_why = None
        st = self.state(output_mint if input_mint == SOL else input_mint)
        if st is None:
            self.quote_why = "no Jupiter route"
            return None
        if input_mint == SOL:
            out = bot.curve_buy_tokens(st[0], st[1], amount / 1e9, 1.25) * 1e6
        else:
            out = bot.curve_sell_sol(st[0], st[1], amount / 1e6, 1.25) * 1e9
        return {"outAmount": str(int(out)), "priceImpactPct": "0.012"}

    def prices(self, mints):
        out = {SOL: {"usd": self.sol_now()}}
        for m in mints:
            st = self.state(m)
            if st is not None:
                out[m] = {"usd": st[0] / st[1] * self.sol_now()}
        return out

    def rug_report(self, mint):
        c = self.coins.get(mint)
        return json.loads(json.dumps(c.report)) if c is not None and c.report is not None else None


class StreamFeeds(object):
    """feeds.Feeds' API on the stream: creates at their time, the metadata fetch_meta asks for, the curve poller."""

    def __init__(self, coins, clock, seed):
        self.coins = {c.mint: c for c in coins}
        self.clock = clock
        self.rng = random.Random(seed + 99)
        self.heap, self.seq = [], 0
        self.q, self.ready = collections.deque(), collections.deque()
        self.watching, self.sent, self.forget = {}, {}, set()
        self.holding = False
        self.answered = None                    # the request time of the last poll answered
        self.poller = types.SimpleNamespace(ok=0, calls=0, last_ms=0)
        self.slot = 300000000
        for c in coins:
            self.at(c.t0, "create", c)

    def at(self, t, kind, payload):
        self.seq += 1
        heapq.heappush(self.heap, (math.ceil(t * 1e6) / 1e6, self.seq, kind, payload))

    def start(self):
        self.at(self.clock.epoch() + 0.5, "poll", None)
        return self

    def stop(self, timeout=3.0):
        return True

    def set_holding(self, holding):
        self.holding = bool(holding)

    def watch(self, mint, key):
        if mint not in self.watching:
            self.forget.add(mint)
        self.watching[mint] = key
        return True

    def unwatch(self, mint):
        self.forget.discard(mint)
        return self.watching.pop(mint, None) is not None

    def watched(self):
        return dict(self.watching)

    def fetch_meta(self, mint, uri):
        c = self.coins.get(mint)
        if c is None:
            return False
        x, tg, web = c.links if c.meta_ok else (False, False, False)
        self.at(self.clock.epoch() + c.meta_s, "meta", {
            "kind": "meta", "mint": mint, "twitter": "https://x.com/" + mint if x else "",
            "telegram": "https://t.me/" + mint if tg else "", "website": "https://%s.fun" % mint if web else "",
            "description": "", "x_kind": "profile" if x else "", "ok": c.meta_ok,
            "source": "meta.example" if c.meta_ok else "", "ms": int(c.meta_s * 1000),
            "error": None if c.meta_ok else "ipfs.io: HTTP 403"})
        return True

    def status(self):
        return [{"name": n, "connected": True, "events": self.seq, "last_event_age_s": 0.2, "errors": 0, "last_error": None}
                for n in ("pumpportal", "curve-poll")]

    def text(self):
        return "stream · %d watched" % len(self.watching)

    def get(self, timeout=0.25):
        deadline = self.clock.epoch() + max(0.0, timeout or 0.0)
        while True:
            self._run(self.clock.epoch())
            if self.q or self.ready:
                return self._out()
            nxt = self.heap[0][0] if self.heap else None
            if nxt is None or nxt > deadline + 2e-6:
                self.clock.set_epoch(deadline)
                self._run(self.clock.epoch())
                return self._out() if (self.q or self.ready) else None
            self.clock.set_epoch(nxt)

    def drain(self, limit=1000):
        self._run(self.clock.epoch())
        out = []
        while (self.q or self.ready) and len(out) < limit:
            out.append(self._out())
        return out

    def _out(self):
        return self.q.popleft() if self.q else self.ready.popleft()

    def _run(self, now):
        while self.heap and self.heap[0][0] <= now + 2e-6:
            t, _, kind, p = heapq.heappop(self.heap)
            if kind == "create":
                self.ready.append(dict(p.create_event(), _rx=t, t=t))
            elif kind == "meta":
                self.ready.append(dict(p, t=t))
            elif kind == "timer":
                p.fire()
            elif kind == "poll":
                lat = self.rng.uniform(0.15, 0.45)
                snap = {m: (self.coins[m].rsol_at(t), self.coins[m].done_at(t)) for m in self.watching if m in self.coins}
                self.slot += 2
                self.at(t + lat, "answer", (snap, self.slot, int(lat * 1000), t))
                self.at(t + (0.5 if self.holding else 1.0), "poll", None)
            elif kind == "answer":
                snap, slot, ms, req = p
                self.answered = req
                self.poller.calls += 1
                self.poller.ok += 1
                self.poller.last_ms = ms
                for m, (rsol, done) in snap.items():
                    if m in self.watching and (m in self.forget or abs(rsol - self.sent.get(m, -1.0)) > 1e-12):
                        self.forget.discard(m)
                        self.sent[m] = rsol
                        vsol = 30.0 + rsol
                        vtok = K / vsol
                        self.ready.append({"kind": "curve", "mint": m, "key": self.watching[m],
                                           "price_sol": (int(vsol * 1e9) / 1e9) / (int(vtok * 1e6) / 1e6),
                                           "vtok": int(vtok * 1e6), "vsol": int(vsol * 1e9),
                                           "rtok": int(max(0.0, vtok - 279.9e6) * 1e6), "rsol": int(rsol * 1e9),
                                           "complete": done, "slot": slot, "src": "poll", "t": t})


def live_run(data_dir, minutes, seed, jup, over=None, clock=None, tag=None, extra=None, memory=None, checked=True):
    """bot.run() on the stream with config.json (live channel off, the recorder on; over: settings changed for this
    run) -> the live run's trades (this run's day). clock: go on from another run's clock; tag: a letter for this
    stream's mints (two runs on one data dir need different coins); extra: more coins, extra(t_start) -> [SimCoin];
    memory: the LEARNER's memory.json before the run (learner.py's dict). checked: the stream is sane, so the run must
    count no odd curve reading (newpairs.curve_sane's false positives on standard curves: none) and every trade it
    booked must pass memebot/fix_book.py's void rules (its proceeds, exit price, mcap and "since the create" all from
    one standard curve state); AssertionError otherwise."""
    clock = clock or Clock(START)
    coins = make_stream(seed, minutes, clock.epoch())
    if extra is not None:
        coins = sorted(coins + list(extra(clock.epoch())), key=lambda c: c.t0)
    if memory is not None:
        bot.learner.save(os.path.join(data_dir, "memory.json"), memory)
    if tag:
        for c in coins:
            old, c.mint = c.mint, tag + c.mint[1:]
            if c.report is not None:
                c.report = json.loads(json.dumps(c.report).replace(old, c.mint))
            c.sym = tag.upper() + c.sym[1:]
    feeds = StreamFeeds(coins, clock, seed)
    market = Market(coins, clock, feeds, jup)
    rng = random.Random(seed + 7)

    class LiveNight(bot.Night):
        def __init__(self, *a, **kw):
            super(LiveNight, self).__init__(*a, **kw)
            self.rug_pool = SimPool(feeds, rng)
            self.sol_pool = SimPool(feeds, rng)

    cfg = bot.load_config(bot.CONFIG)
    # the momentum entry this stream was made for (np_snipe off; over={"np_snipe": True}: config.json's snipe mode,
    # every coin bought on its first reading: tests/test_snipe.py)
    cfg.update(live_enabled=False, rec_on=True, np_snipe=False)
    cfg.update(copy.deepcopy(over or {}))
    env = dict(os.environ)
    env.pop("MEMEBOT_REC", None)
    with unittest.mock.patch.dict(os.environ, env, clear=True), R.engine(LiveNight):
        bot.run(data_dir, cfg, minutes=minutes, anytime=False, market=market, now_fn=clock.now, sleep=clock.advance,
                out=lambda *a: None, live=None, feeds=feeds)
    st = bot.read_json(os.path.join(data_dir, "state.json"), {})
    trades = list((st.get("night") or {}).get("trades") or [])
    if checked:
        odd = (st.get("night") or {}).get("odd")
        if odd is not None:
            raise AssertionError("odd curve readings on a sane stream: %s" % {k: odd.get(k) for k in odd if k != "mints"})
        void = [(t.get("symbol"), w) for t in trades for w in fix_book().void_reasons(t)]
        if void:
            raise AssertionError("trades fix_book would void: %s" % void[:5])
    return trades, cfg


_FB = []


def fix_book():
    """memebot/fix_book.py, loaded once."""
    if not _FB:
        _FB.append(load("memebot", "fix_book"))
    return _FB[0]


def clean_report(mint, holders, top, empty=None):
    """A clean report: the curve's account and the top holders' %, totalHolders `holders` (accounts it does not all
    list); with `empty`, a complete list as the real API gives under 20 accounts: `holders` wallets holding the coin
    (the top ones, the rest at 0.3 %), `empty` accounts of wallets that sold out, the curve's, all counted."""
    th = [{"address": "curve-ata-" + mint, "owner": "curve-" + mint, "pct": 80.0, "insider": False}]
    pcts = list(top) if empty is None else list(top)[:holders] + [0.3] * max(0, holders - len(top))
    th += [{"address": "h%d-%s" % (k, mint), "owner": "o%d-%s" % (k, mint), "pct": p, "insider": False}
           for k, p in enumerate(pcts)]
    if empty is not None:
        th += [{"address": "e%d-%s" % (k, mint), "owner": "eo%d-%s" % (k, mint), "pct": 0, "amount": 0, "insider": False}
               for k in range(empty)]
    return {"mint": mint, "rugged": False, "risks": [], "topHolders": th,
            "totalHolders": holders if empty is None else len(th),
            "graphInsidersDetected": 0, "markets": [], "knownAccounts": {}, "score_normalised": 5, "fileMeta": {}}


def planted(t_start):
    """Three coins planted in the stream (made up, shaped by the live morning of 2026-11-26):
      BIGFEW  six big buys (0.9 SOL each) from 40 s on, then one wallet sells 3.0 SOL about 2 s after the trigger
              (STOREB's kind: -25 % live); RugCheck lists 10 wallets holding it and 8 empty accounts of early flippers
              (19 accounts with the curve's): 5.4 SOL over 10 wallets at the trigger, over 0.5 a holder: it waits
      CROWD   many small buys (0.12-0.28 SOL), 25 holders, then a run
      SKIPME  a crowd pump whose creator bought 6.4 % of the supply ("np:dev:5-8"), 20 holders"""
    rng = random.Random(5)
    out = []
    t0 = t_start + 300.0
    c = SimCoin("pbigfew".ljust(40, "x") + "pump", t0, "BIGFEW", 0.3, "dev-bigfew", kind="planted/dump")
    c.links, c.meta_s = (False, True, False), 0.09
    for k in range(6):
        c.trades.append((t0 + 40.2 + k, 0.9))
    c.trades.append((t0 + 47.1, -3.0))
    flow(rng, c, t0 + 52, t0 + 200, 0.3, 0.35, 0.05, 0.3)
    c.report = clean_report(c.mint, 10, (4.0, 3.0, 2.5), empty=8)
    out.append(c)
    for name, at, dev, links, holders, top, run in (("CROWD", 600.0, 0.2, (True, True, False), 25, (3.0, 2.5, 2.0, 1.5),
                                                     (240, 1.6, 0.8)),
                                                    ("SKIPME", 900.0, 1.9, (False, True, True), 20, (3.5, 2.0, 1.5),
                                                     (200, 1.8, 0.85))):
        t0 = t_start + at
        c = SimCoin(("p" + name.lower()).ljust(40, "x") + "pump", t0, name, dev, "dev-" + name.lower(),
                    kind="planted/" + name.lower())
        c.links, c.meta_s = links, 0.08
        t = t0 + 12.0
        while t < t0 + 58.0:
            c.trades.append((t, rng.uniform(0.12, 0.28)))
            t += rng.uniform(1.3, 2.2)
        flow(rng, c, t0 + 58, t0 + run[0], run[1], run[2], 0.1, 0.5)
        flow(rng, c, t0 + run[0], t0 + 500, 0.5, 0.33, 0.05, 0.4)
        c.report = clean_report(c.mint, holders, top)
        out.append(c)
    for c in out:
        c.finish()
    return out


class LiveAgainstReplay(unittest.TestCase):
    def run_live(self, minutes, seed, jup):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        trades, cfg = live_run(d, minutes, seed, jup)
        return d, trades, cfg

    def test_the_replay_of_the_tapes_makes_the_live_runs_trades_to_the_cent(self):
        # Jupiter = the curve maths on the last reading (live: "curve maths 0.0% off Jupiter"): the same trades
        d, live, cfg = self.run_live(45, 11, "reading")
        # 22 before the entry-quality rules (np_min_holders: the stream's reports see 4-39 wallets; the SOL a holder):
        # fewer with them, and the replay must make those very trades, waits included
        self.assertGreaterEqual(len(live), 18)
        tape = R.read_tape(d)
        s = tape.summary()
        self.assertEqual((s["bad_lines"], s["unknown_kinds"], len(tape.runs)), (0, {}, 1))
        lines = s["lines"]
        for kind in ("start", "hb", "sol", "create", "c", "meta", "rug", "dec", "fire", "buy", "sell", "follow", "end",
                     "stop"):
            self.assertGreater(lines.get(kind, 0), 0, kind)
        # the paths that once made the replay drift are on this tape: RugCheck's links after a failed uri, a
        # failed or empty RugCheck answer, SOL moving
        self.assertTrue(any(f.get("source") == "rugcheck" and any(f.get(k) for k in ("twitter", "telegram", "website"))
                            for ms in tape.metas.values() for _, f in ms))
        self.assertTrue(any(rep is None for v in tape.rugs.values() for _, rep, _ in v))
        self.assertGreater(len(set(tape.sol[1])), 5)
        start = tape.live_start()
        self.assertEqual((start["bal"], start["cfg"]["np_min_age_s"], start["cfg"]["hold_tiers"]),
                         (100.0, 45, cfg["hold_tiers"]))
        res = R.replay(tape, cfg)
        key = lambda t: (t["mint"], t["opened"], t["closed"], t["why"], round(t["pnl"], 2), t.get("tier"))
        self.assertEqual(sorted(key(t) for t in res["trades"]), sorted(key(t) for t in live))
        self.assertGreater(res["stats"]["rug_same_ask"], 0)
        # backtest.py's live check says the same, and the tape's own buy / sell lines are the live book
        chk = R.live_check(tape, bot.load_config(bot.CONFIG))
        self.assertEqual((chk["live"], chk["same_entry"], chk["same_exit"], chk["only_live"], chk["only_replay"]),
                         (len(live), len(live), len(live), [], []))
        self.assertLessEqual(abs(chk["pnl_pct_gap_max"]), 0.05)                # the tape rounds % to 2 places
        self.assertEqual(sorted((t["mint"], round(t["pnl"], 2)) for t in tape.live_trades()),
                         sorted((t["mint"], round(t["pnl"], 2)) for t in live))

    def test_with_jupiter_ahead_of_the_readings_the_entries_still_match(self):
        # Jupiter = the TRUE curve at the quote (trades after the last poll in it): the replay has only the
        # readings, so its fills, and with them a stop's or a step's level, can differ; the entries cannot
        d, live, cfg = self.run_live(30, 13, "true")
        self.assertGreaterEqual(len(live), 10)                          # fewer with the entry-quality rules on
        chk = R.live_check(R.read_tape(d), bot.load_config(bot.CONFIG))
        self.assertEqual((chk["live"], chk["replay"], chk["same_entry"]), (len(live), len(live), len(live)))
        self.assertGreaterEqual(chk["same_exit"], len(live) // 2)
        self.assertLess(abs(chk["pnl_pct_gap_mean"]), 2.0)

    def test_jupiters_fills_of_the_true_market_are_booked_as_quoted(self):
        # Jupiter quotes the chain at the quote's time, as live: a sale a moment after its reading fills above or under
        # the reading's state, and is booked so (the sale cap, np_sale_cap, is only for curve-filled entries far beyond
        # their state). The code before the odd-reading gate (bb498d0) booked these very 48 trades, $13.58, on this
        # stream; the first version of the gate booked $11.60 (its cap turned two stops' fills down to the curve maths)
        d, live, cfg = self.run_live(60, 13, "true")
        self.assertEqual((len(live), round(sum(t["pnl"] for t in live), 2)), (48, 13.58))
        self.assertFalse([t for t in live if t.get("fill") == "curve"])
        st = bot.read_json(os.path.join(d, "state.json"), {})
        self.assertFalse([r for r in st["night"]["decisions"] if "curve state that decided the sale" in r["text"]])

    def test_a_full_hours_tape_is_unknown_in_the_replay_never_a_flat_curve(self):
        # the review's case: at rec_max_mb_hour 1.5 the recorder left curve readings out and only a heartbeat counter
        # said so; the replay held coins at their last reading ("the curve did not move") and sold them "quiet for
        # 3 min" at a made-up price (E62: live "stop -9%" -12.0 %, replay -7.2 %), never counted blind
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        live, cfg = live_run(d, 45, 11, "reading", over={"rec_max_mb_hour": 1.5})
        tape = R.read_tape(d)
        s = tape.summary()
        self.assertGreater(s["capped_coins"], 20)                       # the coins' "gap" lines are on the tape
        self.assertEqual((s["capped_hours"], s["lost_lines"]), (["2026-11-26T09"], 0))
        res = R.replay(tape, cfg)
        key = lambda t: (t["mint"], t["opened"], t["closed"], t["why"], round(t["pnl"], 2))
        same = {key(t) for t in live}
        for t in res["trades"]:
            if key(t) not in same:                                      # decided on a curve the tape misses part of
                self.assertTrue(t["blind"], t)
            else:
                self.assertFalse(t["blind"], t)
        # most still the same (13 of 19 with the entry-quality rules and the SHADOW lines: which coins the 1.5 MB cap
        # cuts moves with both; on a whole tape the replay makes all 19, test_the_replay_of_the_tapes_...)
        self.assertGreaterEqual(sum(1 for t in res["trades"] if key(t) in same), len(live) * 2 // 3)
        self.assertFalse([t for t in res["trades"] if not t["blind"] and not tape.covered(t["mint"], R.ts_epoch(t["closed"]) - 1)])
        rep = bt.backtest(tape=tape, grid={"combos": []}, cfg=bot.load_config(bot.CONFIG), jobs=1, min_trades=3,
                          live_check=False)
        self.assertTrue(any(x.startswith("tape gaps: 0 lines lost") and "coins' readings left out" in x for x in rep["lines"]))

    def test_the_live_check_replays_each_run_with_its_own_settings(self):
        # the review's case: two jobs, a config.json change between them (+7 % first step, then +10 %); the live check
        # replayed both with the first run's settings, found 12 of 31 exits "different" and blamed the fills
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        old = copy.deepcopy(bot.load_config(bot.CONFIG)["hold_tiers"])
        for t in old:
            t["ladder"][0][0] = 7.0
        clock = Clock(START)
        a, _ = live_run(d, 25, 11, "reading", over={"hold_tiers": old, "tp1_pct": 7.0}, clock=clock, tag="g")
        clock.advance(120)
        b, _ = live_run(d, 25, 12, "reading", clock=clock, tag="h")
        tape = R.read_tape(d)
        self.assertEqual(len(tape.runs), 2)
        chk = R.live_check(tape, bot.load_config(bot.CONFIG))
        n = len(tape.live_trades())
        self.assertGreaterEqual(n, 20)
        self.assertEqual((chk["live"], chk["same_entry"], chk["same_exit"], chk["only_live"], chk["only_replay"]),
                         (n, n, n, [], []))
        self.assertIn("hold_tiers", chk["settings_changed"])
        self.assertIn("the settings changed between runs", R.live_check_text(chk))
        self.assertTrue(any(t["why"] == "TP +7%" for t in tape.live_trades()))
        # the base: config.json as both runs traded it (live_run: the momentum entry, np_snipe off)
        rep = bt.backtest(d, grid={"combos": []}, cfg=dict(bot.load_config(bot.CONFIG), np_snipe=False), jobs=1,
                          min_trades=3)
        self.assertEqual(sorted(rep["live"]["live_config_differs"]), ["hold_tiers", "tp1_pct"])     # the FIRST run's
        self.assertEqual([sorted(x["differs"]) for x in rep["live"]["settings_by_run"]], [["hold_tiers", "tp1_pct"], []])

    def test_a_range_that_starts_inside_a_run_is_replayed_from_the_runs_start(self):
        # the review's case: --since 30 minutes into a run took that run's start line (its cash and LEARNER memory of
        # 30 minutes earlier) for the range's start: live +26.49 $ against replay +19.25 $
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        live, cfg = live_run(d, 60, 11, "reading")
        t0 = R.read_tape(d).span()[0]
        since = t0 + 1800
        self.assertAlmostEqual(R.lookback_for(d, since), 1801.0, delta=1.0)         # back to the run's start line
        self.assertEqual(R.lookback_for(d, None), R.LOOKBACK_S)
        short = R.read_tape(d, since)                                   # 10 minutes before since: no start line
        self.assertIsNone(short.live_start(since))
        chk = R.live_check(short, bot.load_config(bot.CONFIG), since)
        self.assertTrue(chk["no_start"])
        self.assertIn("whose start line is not on the tape", R.live_check_text(chk))
        tape = R.read_tape(d, since, lookback_s=R.lookback_for(d, since))
        chk = R.live_check(tape, bot.load_config(bot.CONFIG), since)
        n = len(tape.live_trades(since))
        self.assertGreaterEqual(n, 7)                                   # fewer with the entry-quality rules on
        self.assertEqual((chk["live"], chk["same_entry"], chk["same_exit"]), (n, n, n))
        self.assertAlmostEqual(chk["replay_pnl"], chk["live_pnl"], delta=0.05)
        self.assertEqual(chk["warmup_from"], R.iso(t0))

    def test_entry_quality_and_shadows_end_to_end(self):
        """The new rules on the live engine, the tape, the replay and the backtest at once: BIGFEW (5.4 SOL over 10
        wallets at its trigger, its 8 empty accounts no holders) waits for RugCheck, is asked about at its fire (that
        report is on the tape), is rejected by its dump and gets no SHADOW; CROWD is bought; SKIPME, which a primed
        LEARNER memory skips (np:dev:5-8 -20 % over 12), is followed as a SHADOW whose result teaches the LEARNER at half
        weight and never enters the book; the replay of the tape makes the same trades and the same LEARNER memory,
        and a replay with the concentration rule off buys BIGFEW into its dump (judged on the report the live bot
        asked for at the fire: the adversarial review found such a coin once had none)."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        mem0 = bot.learner.fresh()
        for _ in range(12):
            bot.learner.update(mem0, ["np:dev:5-8"], -20.0)
        live, cfg = live_run(d, 30, 11, "reading", extra=planted, memory=mem0)
        st = bot.read_json(os.path.join(d, "state.json"), {})
        log = st["night"]["decisions"]
        mints = {c.sym: c.mint for c in planted(0.0)}
        said = lambda sym: [(r["agent"], r["action"]) for r in log if r.get("coin") == sym]     # noqa: E731
        self.assertEqual(said("BIGFEW"), [("SCANNER", "flag"), ("RUG CHECK", "wait")])
        self.assertRegex([r["text"] for r in log if r.get("coin") == "BIGFEW"][1],
                         r"^waiting: 10 holders for \d\.\d SOL in \(0\.\d\d SOL a holder, max 0\.5\) · no entry")
        self.assertIn(("EXECUTOR", "enter"), said("CROWD"))
        self.assertEqual(said("SKIPME")[:4], [("SCANNER", "flag"), ("RUG CHECK", "pass"), ("LEARNER", "skip"),
                                              ("LEARNER", "shadow")])
        # the real book: the cash adds up, each trade is its proceeds less its cost, no shadow in it
        self.assertGreaterEqual(len(live), 8)
        self.assertEqual(st["positions"], {})                           # the job's last minute sold everything
        for t in live:
            self.assertAlmostEqual(t["pnl"], t["proceeds"] - t["cost"], delta=0.0101)
        self.assertAlmostEqual(st["balance"], 100.0 + sum(t["pnl"] for t in live), delta=0.005 * len(live))
        traded = {t["mint"] for t in live}
        self.assertIn(mints["CROWD"], traded)
        self.assertFalse({mints["BIGFEW"], mints["SKIPME"]} & traded)
        sh = st["shadow"]
        self.assertEqual((sh["n"], sh["by"]["learner"]["n"], [r["symbol"] for r in sh["closed"]]), (1, 1, ["SKIPME"]))
        mem = bot.learner.load(os.path.join(d, "memory.json"))
        self.assertEqual((mem["samples"], mem["shadows"]), (12 + len(live), 1))
        self.assertAlmostEqual(mem["labels"]["np:dev:5-8"]["n"], 12.5)
        self.assertAlmostEqual(mem["labels"]["np:dev:5-8"]["sum"], -240 + 0.5 * sh["closed"][0]["pnl_pct"], places=6)
        self.assertTrue(any(k.startswith("np:solh:") for k in mem["labels"]))
        self.assertTrue(any(k.startswith("np:holders:") for k in mem["labels"]))
        # the tape, the replay, the live check
        tape = R.read_tape(d)
        s = tape.summary()
        self.assertEqual((s["bad_lines"], s["unknown_kinds"], s["lines"].get("shadow")), (0, {}, 3))   # open, sell, sell
        start = tape.live_start()
        self.assertEqual((start["cfg"]["np_min_holders"], start["cfg"]["np_max_sol_per_holder"], start["cfg"]["shadow_on"]),
                         (10, 0.5, True))
        # BIGFEW's RugCheck report on the tape (the prefetch's, under 4 s old at its fire: the one its wait was judged
        # on), with the accounts by kind: what lets the replay below judge the looser lever
        rugs = [x for _, path in bot.recorder.tape_files(os.path.join(d, "tapes")) for x in bot.recorder.read_lines(path)
                if x.get("k") == "rug" and x.get("m") == mints["BIGFEW"] and x.get("ok")]
        self.assertTrue(rugs)
        self.assertEqual({(x["holders"], x["held"], x["empty"], x["curve"]) for x in rugs}, {(19, 10, 8, 1)})
        res = R.replay(tape, cfg, memory=start["mem"])
        key = lambda t: (t["mint"], t["opened"], t["closed"], t["why"], round(t["pnl"], 2))     # noqa: E731
        self.assertEqual(sorted(key(t) for t in res["trades"]), sorted(key(t) for t in live))
        self.assertEqual(json.dumps(res["memory"]["labels"], sort_keys=True), json.dumps(mem["labels"], sort_keys=True))
        chk = R.live_check(tape, bot.load_config(bot.CONFIG))
        self.assertEqual((chk["live"], chk["same_entry"], chk["same_exit"], chk["only_live"], chk["only_replay"]),
                         (len(live), len(live), len(live), [], []))
        # the levers on the same tape: without the concentration rule BIGFEW is bought and sold into its dump
        loose = R.replay(tape, dict(cfg, np_max_sol_per_holder=0), memory=start["mem"])
        big = [t for t in loose["trades"] if t["mint"] == mints["BIGFEW"]]
        self.assertEqual(len(big), 1)
        self.assertTrue(big[0]["why"].startswith("dump: "), big[0])
        self.assertLess(big[0]["pnl_pct"], -15)
        self.assertEqual(loose["stats"]["rug_missing"], res["stats"]["rug_missing"])     # it had its report
        # the momentum entry these levers belong to (np_snipe off: a snipe never reads them)
        rep = bt.backtest(tape=tape, grid={"combos": [{"np_max_sol_per_holder": 0}, {"np_min_holders": 0}]},
                          cfg=dict(bot.load_config(bot.CONFIG), np_snipe=False), jobs=1, min_trades=3)
        self.assertEqual(rep["grid"]["configs"], 3)
        self.assertEqual((rep["live_check"]["same_entry"], rep["live_check"]["same_exit"]), (len(live), len(live)))

    def test_backtest_on_the_tapes_with_its_live_check(self):
        d, live, cfg = self.run_live(20, 11, "reading")
        rep = bt.backtest(d, grid={"combos": [{"np_min_age_s": 30}, {"scalp_tp": 15.0}, {"stop_pct": -10.0}]},
                          cfg=dict(bot.load_config(bot.CONFIG), np_snipe=False), jobs=1, min_trades=3)
        self.assertIn(rep["decision"]["action"], ("keep", "change"))
        self.assertEqual(rep["grid"]["configs"], 4)
        self.assertEqual(rep["base"]["error"], None)
        chk = rep["live_check"]
        self.assertEqual((chk["live"], chk["same_entry"], chk["same_exit"]), (len(live), len(live), len(live)))
        self.assertTrue(any(line.startswith("live check: %d live trades" % len(live)) for line in rep["lines"]))
        json.dumps(R.json_safe(rep), allow_nan=False)


def live_memory():
    """The live LEARNER of 2026-11-26: it skips about 80 % of the triggers (hour:day and news:0-2, -17 % over 10)."""
    mem = bot.learner.fresh()
    for _ in range(10):
        bot.learner.update(mem, ["hour:day", "news:0-2"], -17.0)
    return mem


class ShadowsLeaveTheRealBookAlone(unittest.TestCase):
    """The adversarial review's findings, on the whole chain: an open SHADOW once switched every curve to the held
    coins' 0.5 s polls (and the entry rules count buy READINGS), and once asked Jupiter on the real book's quotes;
    both changed which real coins were bought. With shadow_weight 0 (shadows open and close but teach nothing) the
    real trades and the SCANNER's rejects must be those of a run without shadows."""

    def run_book(self, over):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        live, _ = live_run(d, 60, 13, "reading", over=over, memory=live_memory())
        st = bot.read_json(os.path.join(d, "state.json"), {})
        rejects = sorted(r["coin"] for r in st["night"]["decisions"] if r["agent"] == "SCANNER" and r["action"] == "reject")
        return [(t["symbol"], t["opened"], t["why"], round(t["pnl"], 4)) for t in live], rejects, st

    def test_shadows_that_teach_nothing_leave_the_real_trades_alone(self):
        off = self.run_book({"shadow_on": False})
        on = self.run_book({"shadow_weight": 0.0, "shadow_on_risk": False})
        self.assertGreater(on[2]["shadow"]["n"], 0)                     # shadows did open and close
        self.assertGreater(len(off[0]), 0)
        self.assertEqual(on[:2], off[:2])


class RugStale(unittest.TestCase):
    """replay.py's "rug_stale" (a coin waiting for its holders that the tape has no fresher report for) once marked every
    coin handed a report twice, ordinary asks after the cache included, and so made the backtest turn levers down as
    "unjudged" for a false reason. With no holder rule nothing can wait: nothing is stale."""

    def test_no_holder_rule_no_stale_coin(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        live, cfg = live_run(d, 45, 11, "reading")
        tape = R.read_tape(d)
        res = R.replay(tape, dict(cfg, np_min_holders=0, np_max_sol_per_holder=0, shadow_on=False),
                       memory=tape.live_start().get("mem"))
        self.assertGreater(res["stats"]["rug_asks"], len({m for m in tape.rugs}))      # coins asked again
        self.assertEqual((res["stats"]["rug_stale"], res["stats"]["unjudged"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
