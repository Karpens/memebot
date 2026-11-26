#!/usr/bin/env python3
"""Meme Bot REPLAY: recorded tapes played back through the real New Pairs engine on a simulated clock.

The live bot's recorder (memebot/recorder.py, "THE TAPE FORMAT", version 1) writes <data>/tapes/YYYY-MM-DDTHH.jsonl,
one JSON object a line: "k" (the kind), "t" (epoch seconds), "m" (the mint) and the line's own fields. Read here:

    create            PumpPortal's create fields (creator, bondingCurveKey, initialBuy, solAmount, vSol/vTokens...)
    c                 a curve reading, change-only: s (slot), vs / rs (virtual / real SOL, lamports), vt (virtual
                      tokens, base units), x 1 when complete
    meta              ok, x, tg, web, xk (the coin's links; ok false: the uri could not be read; src "rugcheck":
                      the links the bot took from RugCheck's copy of the metadata, put back into that report)
    rug               ok, holders, top1, top10, top (the top holders' %, the curve left out), risks [[name, level]],
                      insiders, rugged: rebuilt into the report fields the engine's own RUG CHECK reads; ms (from
                      the ask): an ask the replay makes at the same moment gets this very answer, when it came
    follow / end      until when the coin's curve was polled (the bot's watch, then the recorder's follow-up)
    gap               the hour's tape was full: this coin's readings are missing from t until its next "c" line
    start / hb / stop the recorded runs (jobs), the poller's health (hb "poll_ok"), the lines the recorder lost
                      (hb "lost"; version 1 tapes: "drop")
    sol               the SOL/USD the live bot priced with, at each change (older tapes: hb "sol"); the replay's
                      engine prices with the same at the same moment
    fire / dec / buy / sell    the live bot's own decisions and fills (a live-vs-replay check; never replayed)
    shadow            the live bot's SHADOW trades (coins not bought, followed for its LEARNER): kept with the live
                      lines, never a trade (the engine makes its own shadows from the readings)
    gone / migrate    passed through to the engine as feed events

The generic names work too (curve with slot / vsol / vtok / rsol / complete, meta twitter / telegram / website, rug
with a whole "report", sol with usd, trigger / decision / fill). Unknown kinds are counted and ignored, a line that is
not JSON is counted, never fatal; the one place to align field names is Tape.add() and rug_report_of().

What the replay does (no network, no threads that matter, deterministic):

  * bot.run() itself runs, one job per recorded run (start ... stop; else 340-minute jobs 2 minutes apart, like
    the GitHub Actions chain), with the job-end rules (no entries in a job's last 5 minutes, everything sold in its
    last one), on one temp data dir (state.json and the LEARNER's memory.json carry from job to job, as live), with
      - ReplayFeeds: feeds.Feeds' API (get / drain / watch / unwatch / fetch_meta / set_holding / status / text).
        It hands out the recorded events in time order, never before their time, and a curve reading only while
        the engine watches that mint (like the real poller). The engine's queue wait moves the simulated clock.
      - ReplayMarket: Jupiter's quotes are the pump.fun curve maths (constant product, curve_fee_pct each way) on
        the latest recorded reading at or before the quote's time (the live log showed the curve maths 0.0 % off
        Jupiter); the engine itself takes its latency haircut and network + priority fees on top, as live.
        RugCheck answers with the recorded report (or "no report" when none was recorded: never invented), after
        rug_latency_s (the probe: 280-460 ms), never before the report's recorded time. SOL's price: the tape's.
      - a Night subclass (night_class) that changes no rule: its RugCheck and SOL-price pools run on the simulated
        clock (the same trades every time), it keeps each day's trades before close_night() drops them, and it
        skips the per-trade files and the speed panel (display and disk only).
  * No look-ahead: an event is seen at or after its recorded time; a fill at time t is priced on the last reading
    at or before t (plus fill_delay_s when asked, 0 by default); a RugCheck report recorded at T is never handed
    out before T. The same tape cut at any time T gives the same trades up to T (tests/test_backtest.py), and a
    tape the real engine recorded replays into the very same trades (the round-trip test).
  * A coin the engine holds beyond what the tape recorded of it (its "end" line, its "gap" line until its next
    reading, while the live poller was failing, or between two heartbeats where the recorder lost lines) gets no
    readings and no "fresh" touches: it goes stale like live and is sold on the clock's exits (quiet curve, time
    stop) at its last recorded state. Such trades are counted as "blind": their exit price is a guess. Jupiter's
    price v3 (live's stale fallback) is not on the tape, so a stale coin keeps its last price. Tape.summary() says
    how many lines were lost and in which hours.

Honest limits: the replay can only judge rules on what the live bot recorded, and what it did not record is
missing, not neutral. A looser entry rule finds coins the live bot let go at once (no readings: they never
trigger here) or never asked RugCheck about ("no report": no entry, like live): the extra trades the rule would
make are simply not in the replay, winners and losers alike, so such a rule looks like the base, not better or
worse ("unseen" and "rug_missing" in the stats count those coins: "rug_missing" a coin whose trigger fired and
that no ask ever got a recorded report for, never a prefetch that only came too early; backtest.py will not recommend
a config with many of them, and leaves them out of its comparison with the base). The same for a RugCheck recheck (newpairs: a report that leaves the coin waiting, too few wallets
holding it for np_min_holders or np_max_sol_per_holder, is asked for again after np_rug_recheck_s): when the replay
waits longer than the live bot did, the tape has no fresher report, the coin goes unbought, and "rug_stale" counts it
(in "unjudged" too); a report handed out again that would not leave it waiting is not stale. A hold longer than the recorder's
follow-up (rec_follow_s, 15 minutes after launch) finds no readings (blind exits). Paper money, no guarantee: a
replay that made money is a hint, not a promise.

    python3 memebot/replay.py --data DIR [--since ISO] [--until ISO] [--config FILE] [--set key=json ...] [--out FILE]
                              [--live-check]
    python3 memebot/replay.py --synth DIR [--coins 100] [--seed 1]      # a made-up tape (tests and demos only)

    tape = read_tape(DIR, since, until)                     # epoch seconds or None
    res = replay(tape, cfg, since, until)                   # {"trades", "open", "end_equity", "memory", "stats"}
    m = metrics(res["trades"], cfg["start_balance"])       # trades, win rate, P&L, drawdown, profit factor, tiers
    chk = live_check(tape, cfg, since, until)               # the live run's own trades against their replay

The live check (live_check; --live-check, and backtest.py's report): the range replayed run by run, each recorded
run with the settings on its own "start" line (a config.json change between two jobs is replayed as it was
traded), from the cash and LEARNER memory on the first run's start line (a range that starts inside a run is
replayed from that run's start, and only the trades opened in the range are compared), its trades against the
tape's buy / sell lines. On a realistic
synthetic stream (tests/test_endtoend.py) the replay makes the live run's trades to the cent when Jupiter quoted
the last reading (what the live log showed: "curve maths 0.0% off Jupiter"); when Jupiter's quote ran ahead of
the readings the entries still match, but the fills, and with them where a stop or a step sits, can differ.
"""

import argparse
import bisect
import collections
import concurrent.futures
import contextlib
import copy
import datetime
import glob
import gzip
import heapq
import importlib.util
import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
EPOCH = datetime.datetime(1970, 1, 1)
EPS = 1e-6                              # times are kept to the microsecond (the engine's datetimes are)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# the real engine, loaded by its path like bot.py loads its parts (a script, the tests' loader, a worker process)
bot = _load("memebot_replay_bot", os.path.join(HERE, "bot.py"))
SOL = bot.SOL
read_curve = bot.newpairs.read_curve if bot.newpairs is not None else None

LOOKBACK_S = 600.0                      # tape read this long before `since`: SOL's price and the market's last state
DEFAULT_SOL_USD = 200.0                 # only when the tape has no "sol" line at all (said in the result)
TAPE_WATCH_S = 180.0                    # the live bot followed every new coin this long (config np_watch_s at recording)

# what every replay changes in the config (the rest is the config under test):
#   one strategy (the migrated scanner would call the network), no live channel, 24/7 (each day its own night, as
#   the live config), the loop waits on the feeds a whole step (the replay's queue wakes it at each event's time
#   anyway, so this only saves idle turns), state.json written after trades only, no tape recorder (a replay
#   must not write tapes of its own).
REPLAY_OVERRIDES = {"strategies": ["new_pairs"], "live_enabled": False, "session_start": "00:00",
                    "session_end": "00:00", "save_every_sec": 10 ** 9, "curve_ws": False, "rec_on": False}


def replay_cfg(cfg):
    c = copy.deepcopy(cfg)
    c.update(copy.deepcopy(REPLAY_OVERRIDES))
    c["feed_wait_sec"] = max(0.25, float(bot.watch_sec(c)))
    return c


def num(x):
    """A finite float, or None (None, NaN, inf, bools, text)."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def iso(t):
    return (EPOCH + datetime.timedelta(seconds=t)).replace(microsecond=0).isoformat() + "Z" if t is not None else None


def parse_time(s):
    """'2026-11-26', '2026-11-26T08:00', '2026-11-26T08:00:00Z' (UTC) or epoch seconds -> epoch seconds."""
    if s is None or s == "":
        return None
    v = num(s)
    if v is not None and v > 1e8:
        return v
    s = str(s).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1]
    d = datetime.datetime.fromisoformat(s)
    if d.tzinfo is not None:
        d = d.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return (d - EPOCH).total_seconds()


def ts_epoch(s):
    """The engine's "2026-11-26T08:00:05Z" -> epoch seconds, or None."""
    try:
        return (datetime.datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ") - EPOCH).total_seconds()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- the tape

CREATE, CURVE, MIGRATE, GONE = "create", "curve", "migrate", "curve_gone"
# the recorder's short kinds (memebot/recorder.py, tape format version 1) and the generic names
KINDS = {"c": CURVE, "curve": CURVE, "create": CREATE, "migrate": MIGRATE, "gone": GONE, "curve_gone": GONE,
         "meta": "meta", "rug": "rug", "sol": "sol", "fire": "trigger", "trigger": "trigger", "dec": "decision",
         "decision": "decision", "buy": "fill", "sell": "fill", "fill": "fill", "follow": "follow", "end": "end",
         "gap": "gap", "start": "start", "hb": "hb", "stop": "stop", "shadow": "shadow"}
SYSTEM = ("start", "hb", "stop")
DROP = ("k", "kind", "t", "ts", "m", "mint")
# per kind: the recorder's short field -> the feed event's field
CURVE_FIELDS = {"s": "slot", "vs": "vsol", "vt": "vtok", "rs": "rsol", "x": "complete"}
META_FIELDS = {"x": "twitter", "tg": "telegram", "web": "website", "xk": "x_kind", "src": "source"}
HB_S = 10.0                             # the recorder's heartbeat (rec_hb_s): a run without "stop" ended about then
SAME_ASK_S = 0.05                       # a replay's RugCheck ask this close to a recorded one is the same ask


class Tape(object):
    """Recorded lines, sorted, with lookups for the market (curve states by time), the metadata, the RugCheck
    reports, SOL's price, how long each coin's curve was recorded, the recorded runs (start / hb / stop) and the
    live bot's own trades (buy / sell)."""

    def __init__(self):
        self.events = []            # [t, seq, kind, mint, fields]: what the feeds hand out (create, curve, ...)
        self.states = {}            # mint -> ([t], [(vsol SOL, vtok tokens)]) usable curve states for quotes
        self.keys = {}              # mint -> bonding curve key
        self.metas = {}             # mint -> [(t, fields)]
        self.rugs = {}              # mint -> [(t, report or None: a failed ask, the ask's time or None)]
        self.sol = ([], [])         # ([t], [usd])
        self.last = {}              # mint -> its last line's time
        self.ends = {}              # mint -> [(t, "end" | "follow", until)]
        self.gaps = {}              # mint -> [t] of its "gap" lines (the hour's tape was full: readings left out)
        self.created = {}           # mint -> create time
        self.live = []              # (t, kind as written, mint, fields) of the live bot's fire / dec / buy / sell
        self.system = []            # (t, "start" | "hb" | "stop", fields)
        self.n = collections.Counter()
        self.unknown = collections.Counter()
        self.bad = 0
        self.rug_failed = 0
        self.rug_old = 0            # reports from "rug" lines recorded before the holder count of wallets ("held")
        self.files = []
        self._seq = 0
        self.t0 = self.t1 = None
        self.watch_s = TAPE_WATCH_S

    # building
    def add(self, rec):
        """One tape line (a dict). Unknown kinds are counted and ignored; nothing raises."""
        if not isinstance(rec, dict):
            self.bad += 1
            return False
        raw = rec.get("k") or rec.get("kind")
        t = num(rec.get("t") if rec.get("t") is not None else rec.get("ts"))
        mint = rec.get("m") or rec.get("mint")
        if not isinstance(raw, str) or t is None or (mint is not None and not isinstance(mint, str)):
            self.bad += 1
            return False
        kind = KINDS.get(raw)
        if kind is None:
            self.unknown[raw] += 1
            return False
        t = round(t, 6)
        self._seq += 1
        fields = {k: v for k, v in rec.items() if k not in DROP}
        if kind in SYSTEM:
            self.system.append((t, kind, fields))
            self._sol(t, fields.get("sol"))
            cfg = fields.get("cfg")
            if kind == "start" and isinstance(cfg, dict) and num(cfg.get("np_watch_s")) is not None:
                self.watch_s = num(cfg["np_watch_s"])
        elif kind == "sol":
            if not self._sol(t, rec.get("usd") if rec.get("usd") is not None else rec.get("price")):
                self.bad += 1
                return False
        elif not mint:
            self.bad += 1
            return False
        elif kind == CREATE:
            if "traderPublicKey" not in fields and isinstance(fields.get("creator"), str):
                fields["traderPublicKey"] = fields["creator"]       # the serial-launcher check reads it
            fields.pop("_rx", None)                                 # the feed sets _rx when it hands the create out
            if mint in self.created:                                # PumpPortal sends a create twice now and then
                return False
            self.created[mint] = t
            key = fields.get("bondingCurveKey") or fields.get("key")
            if isinstance(key, str) and key:
                self.keys.setdefault(mint, key)
            self._state(mint, t, fields)
            self.events.append([t, self._seq, CREATE, mint, fields])
        elif kind == CURVE:
            for short, name in CURVE_FIELDS.items():
                if short in fields and name not in fields:
                    fields[name] = fields.pop(short)
            fields["complete"] = bool(fields.get("complete"))
            if isinstance(fields.get("key"), str) and fields["key"]:
                self.keys.setdefault(mint, fields["key"])
            self._state(mint, t, fields)
            self.events.append([t, self._seq, CURVE, mint, fields])
        elif kind == "meta":
            for short, name in META_FIELDS.items():
                if short in fields and name not in fields:
                    fields[name] = fields.pop(short)
            self.metas.setdefault(mint, []).append((t, fields))
        elif kind == "rug":
            rep = rug_report_of(fields, mint)
            if rep is None:
                self.rug_failed += 1                                # a failed ask: no report then
            elif rug_line_old(fields):
                self.rug_old += 1                                   # its holders: a lower bound (rug_report_of)
            ms = num(fields.get("ms"))                              # v1: from the ask to the loop taking it
            ask = round(t - ms / 1000.0, 6) if ms is not None and 0 <= ms < 3.6e6 else None
            if rep is not None or ask is not None:                  # a failed ask is kept when its ask is known
                self.rugs.setdefault(mint, []).append((t, rep, ask))
        elif kind in (MIGRATE, GONE):
            self.events.append([t, self._seq, kind, mint, fields])
        elif kind in ("follow", "end"):
            self.ends.setdefault(mint, []).append((t, kind, num(fields.get("until"))))
        elif kind == "gap":
            self.gaps.setdefault(mint, []).append(t)
        else:                                                       # the live bot's fire / dec / buy / sell
            self.live.append((t, raw, mint, fields))
            if raw in ("buy", "sell"):
                self._sol(t, fields.get("sol"))
        self.n[raw] += 1
        if mint:
            self.last[mint] = max(self.last.get(mint, t), t)
        self.t0 = t if self.t0 is None else min(self.t0, t)
        self.t1 = t if self.t1 is None else max(self.t1, t)
        return True

    def _sol(self, t, usd):
        v = num(usd)
        if v is None or v <= 0:
            return False
        self.sol[0].append(t)
        self.sol[1].append(v)
        return True

    def _state(self, mint, t, fields):
        r = read_curve(fields) if read_curve is not None else None
        if r is not None and r["vsol"] > 0 and r["vtok"] > 0:
            st = self.states.setdefault(mint, ([], []))
            st[0].append(t)
            st[1].append((r["vsol"], r["vtok"]))

    def finish(self):
        """Sort everything by time (the file order breaks ties), work out the recorded runs and each coin's
        recorded span."""
        self.events.sort(key=lambda e: (e[0], e[1]))
        self.times = [e[0] for e in self.events]
        self.curves = {}                    # mint -> ([t], [fields]) of its curve lines (a re-watch's first reading)
        for t, _, kind, mint, fields in self.events:
            if kind == CURVE:
                c = self.curves.setdefault(mint, ([], []))
                c[0].append(t)
                c[1].append(fields)
        for mint, (ts, xs) in list(self.states.items()):
            order = sorted(range(len(ts)), key=lambda i: ts[i])
            self.states[mint] = ([ts[i] for i in order], [xs[i] for i in order])
        for d in (self.metas, self.rugs, self.ends):
            for mint in d:
                d[mint].sort(key=lambda x: x[0])
        self._rug_links()
        order = sorted(range(len(self.sol[0])), key=lambda i: self.sol[0][i])
        self.sol = ([self.sol[0][i] for i in order], [self.sol[1][i] for i in order])
        self.system.sort(key=lambda x: x[0])
        self.live.sort(key=lambda x: x[0])
        self.runs = self._runs()
        self.cov = {m: self._cov(m) for m in set(self.created) | set(self.last)}
        self.down = self._poll_down()
        self.down_t = [a for a, _ in self.down]
        # coins the live bot let go at once (rejected at their create: no reading on the tape, coverage over within
        # 2 s): a config that watches them has nothing to judge them on (ReplayFeeds counts them: "unseen")
        self.unpolled = {m for m, t in self.created.items() if m not in self.curves and self.cov.get(m, t) - t <= 2.0}
        self.holes = {}                     # mint -> [(from, to)]: its "gap" line to its next reading (or for good)
        for mint, ts in self.gaps.items():
            reads = (self.curves.get(mint) or ([], []))[0]
            spans = []
            for a in sorted(ts):
                i = bisect.bisect_right(reads, a + EPS)
                spans.append((a, reads[i] - EPS if i < len(reads) else float("inf")))
            self.holes[mint] = spans
        return self

    def _rug_links(self):
        """The uri failed and the live bot took the coin's links from RugCheck's copy of its metadata (a "meta" line
        with src "rugcheck", written on the loop turn that took the report): that report gets the links back as its
        fileMeta, so the replay's engine reads them from it on the same path (np_rug_meta). {} links: RugCheck read
        the metadata and it had none."""
        for mint, metas in self.metas.items():
            reps = self.rugs.get(mint) or []
            for t, fields in metas:
                if fields.get("source") != "rugcheck":
                    continue
                for rt, rep, _ in reps:
                    if rep is not None and abs(rt - t) <= 0.002:
                        rep["fileMeta"] = {"name": "(replay: RugCheck's copy)", "twitter": fields.get("twitter") or "",
                                           "telegram": fields.get("telegram") or "", "website": fields.get("website") or ""}
                        break

    def _poll_down(self):
        """[(from, to)] between two heartbeats of a run where the tape cannot say what any coin did: the live curve
        poller made no good call (hb "poll_ok" did not grow), or the recorder lost lines without a mark (hb "lost"
        grew: its writer's queue was full, or a coin line did not fit the hour's cap; a version 1 tape has no
        "lost", so any growth of its "drop" counts, readings capped or not). [] when the tape has no such numbers.
        Also counts what was lost for summary(): self.lost (lines), self.lost_hours (the hours of those gaps)."""
        out, prev, prev_lost = [], None, None
        self.lost, self.lost_hours, run_lost = 0, set(), 0
        for t, kind, f in self.system:
            if kind == "start":
                prev, prev_lost = None, None
                self.lost += run_lost
                run_lost = 0
            if kind != "hb":
                continue
            ok = num(f.get("poll_ok"))
            lost = num(f["lost"] if "lost" in f else f.get("drop"))
            if ok is not None:
                if prev is not None and ok <= prev[1] and t > prev[0]:
                    out.append((prev[0], t))
                prev = (t, ok)
            if lost is not None:
                if prev_lost is not None and lost > prev_lost[1]:
                    out.append((prev_lost[0], t))
                    for h in range(int(prev_lost[0] // 3600), int(t // 3600) + 1):
                        self.lost_hours.add(h * 3600)
                elif prev_lost is None and lost > 0:        # lost before this run's first heartbeat on the tape
                    run_start = max((a for a, _ in self.runs if a <= t + EPS), default=t)
                    out.append((run_start, t))
                    self.lost_hours.add(int(t // 3600) * 3600)
                run_lost = max(run_lost, lost)
                prev_lost = (t, lost)
        self.lost += run_lost
        out.sort()
        merged = []
        for a, b in out:                                    # overlapping gaps as one (polled() looks up by start)
            if merged and a <= merged[-1][1] + EPS:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    def polled(self, t):
        """False while the live poller was failing (see _poll_down)."""
        i = bisect.bisect_right(self.down_t, t + EPS) - 1
        return i < 0 or t > self.down[i][1] + EPS

    def _runs(self):
        """[(start, end)] of the live runs the recorder saw: a "start" to its "stop" (or the last heartbeat when the
        run died); a tape cut in the middle of a run starts at its first line. [] without system lines. Each run's
        "start" line's fields go to self.run_starts (None for a run whose start is not on the tape)."""
        runs, cur = [], None
        self.run_starts = []
        for t, kind, f in self.system:
            if kind == "start":
                if cur is not None:
                    runs.append((cur[0], cur[1] + HB_S))
                    self.run_starts.append(cur[2])
                cur = [t, t, f]
            elif cur is None:
                cur = [t, t, None]
            else:
                cur[1] = t
            if kind == "stop":
                runs.append((cur[0], t))
                self.run_starts.append(cur[2])
                cur = None
        if cur is not None:
            runs.append((cur[0], cur[1] + HB_S))
            self.run_starts.append(cur[2])
        return runs

    def _run_end(self, t):
        for a, b in self.runs:
            if a - EPS <= t <= b + EPS:
                return b
        return None

    def _cov(self, mint):
        """Until when the live bot (or its recorder) polled this coin's curve: its last "end" line (after its last
        "follow"); without one, the end of the recorded run it was in (the run died); without system lines (a
        generic tape), its last line or the bot's watch window after its create, whichever is later."""
        ends = self.ends.get(mint) or []
        last_end = max((t for t, k, _ in ends if k == "end"), default=None)
        last_follow = max((t for t, k, _ in ends if k == "follow"), default=None)
        if last_end is not None and (last_follow is None or last_end >= last_follow):
            return last_end
        t0 = self.created.get(mint, self.last.get(mint))
        if self.runs:
            end = self._run_end(self.last.get(mint, t0))
            if end is not None:
                return end
        until = max([u for _, k, u in ends if k == "follow" and u is not None] or [float("-inf")])
        return max(self.last.get(mint, t0), (t0 if t0 is not None else float("-inf")) + self.watch_s, until)

    # lookups (all "at or before t": no look-ahead)
    def state_at(self, mint, t):
        """(t of the reading, vsol SOL, vtok tokens) of the latest usable curve state at or before t, or None."""
        st = self.states.get(mint)
        if not st:
            return None
        i = bisect.bisect_right(st[0], t + EPS) - 1
        if i < 0:
            return None
        return (st[0][i],) + st[1][i]

    def sol_at(self, t):
        """SOL's price at or before t; before the first recorded one, the first one (it only turns SOL into
        dollars); None when the tape has none."""
        ts, xs = self.sol
        if not ts:
            return None
        i = bisect.bisect_right(ts, t + EPS) - 1
        return xs[max(0, i)]

    def covered(self, mint, t):
        """The live bot (or its recorder) was still polling this coin's curve at t, the poller was answering, and the
        recorder wrote what it got (no "gap" of this coin's, no lost lines then): its readings are on the tape
        (change-only: no line means no change)."""
        if not (t <= self.cov.get(mint, float("-inf")) + EPS and self.polled(t)):
            return False
        for a, b in self.holes.get(mint) or ():
            if a - EPS <= t <= b:
                return False
        return True

    def gap_in(self, mint, a, b):
        """True when the tape misses part of this coin's curve somewhere in [a, b]: one of its "gap" spans, or a
        stretch unknown for every coin (the poller down, lines lost). A trade over such a stretch was decided on an
        incomplete curve (a late trigger, a stop not seen): its result is a guess like a blind exit's."""
        for x, y in self.holes.get(mint) or ():
            if x <= b + EPS and y >= a - EPS:
                return True
        i = bisect.bisect_right(self.down_t, b + EPS) - 1
        return i >= 0 and self.down[i][1] >= a - EPS

    def first_index(self, t):
        return bisect.bisect_left(self.times, t - EPS)

    def span(self):
        return self.t0, self.t1

    def live_trades(self, since=None, until=None):
        """The live bot's own closed trades from its "buy" and final "sell" lines (opened in [since, until)), in the
        replay's trade form, for a live-vs-replay check."""
        opened, out = {}, []
        for t, raw, mint, f in self.live:
            if raw == "fill":                                      # a generic fill line: its side says which
                side = str(f.get("side") or f.get("kind") or "").lower()
                raw = "buy" if side in ("buy", "enter", "entry") else "sell" if side in ("sell", "exit") else raw
            if raw == "buy":
                opened[mint] = (t, f)
            elif raw == "sell" and f.get("final") and mint in opened:
                t0, b = opened.pop(mint)
                if (since is not None and t0 < since) or (until is not None and t0 >= until):
                    continue
                out.append({"mint": mint, "opened": iso(t0), "closed": iso(t), "cost": num(b.get("usd")),
                            "pnl": num(f.get("pnl")), "pnl_pct": num(f.get("pnl_pct")), "why": f.get("why"),
                            "tier": b.get("tier"), "fill": f.get("fill")})
        return out

    def run_settings(self, since=None, until=None):
        """The recorded runs that overlap [since, until), oldest first, each with what its own "start" line says:
        [{"from", "to", "t" (the start line's time; None when the tape starts inside the run), "cfg", "bal", "held",
        "mem"} (each None when the line has none)]."""
        out = []
        for (a, b), f in zip(self.runs, self.run_starts):
            if (until is not None and a >= until) or (since is not None and b <= since):
                continue
            g = f if isinstance(f, dict) else {}
            out.append({"from": a, "to": b, "t": a if f is not None else None,
                        "cfg": g.get("cfg") if isinstance(g.get("cfg"), dict) else None, "bal": num(g.get("bal")),
                        "held": g.get("held"), "mem": g.get("mem") if isinstance(g.get("mem"), dict) else None})
        return out

    def live_start(self, since=None, until=None):
        """What the live bot started the range with: the start line of the first run in [since, until) whose start
        is on the tape (the run the range starts in, when its start was read: it may be before since): {"t", "cfg",
        "bal", "held", "mem"} (each None when the line has none), or None."""
        got = next((r for r in self.run_settings(since, until) if r["t"] is not None), None)
        if got is None:
            return None
        return {k: got[k] for k in ("t", "cfg", "bal", "held", "mem")}

    def live_cfg(self, since=None, until=None):
        """The config the live bot traded with at the start of the range (live_start's line: the same one), or None."""
        st = self.live_start(since, until)
        return st["cfg"] if st else None

    def summary(self):
        down_s = sum(b - a for a, b in self.down)
        return {"files": len(self.files), "from": iso(self.t0), "to": iso(self.t1),
                "hours": round((self.t1 - self.t0) / 3600.0, 2) if self.t0 is not None else 0.0,
                "lines": dict(self.n), "bad_lines": self.bad, "unknown_kinds": dict(self.unknown),
                "coins": len(self.created), "coins_with_readings": sum(1 for m in self.created if m in self.states and
                                                                       len(self.states[m][0]) > 1),
                "coins_with_rugcheck": sum(1 for v in self.rugs.values() if any(r is not None for _, r, _ in v)),
                "rugcheck_failed_asks": self.rug_failed,
                # RugCheck reports recorded before the tape counted the wallets holding a coin (f16ee6e): the holder
                # rules judge them on the wallets their top lists, a lower bound (rug_report_of)
                "rug_lines_old": self.rug_old,
                "coins_with_meta": len(self.metas), "sol_prices": len(self.sol[0]),
                "runs": [[iso(a), iso(b)] for a, b in self.runs][:50], "live_lines": len(self.live),
                # what the tape does not cover: lines the recorder lost (a gap for every coin between two heartbeats),
                # coins whose readings a full hour's cap left out ("gap" lines), the seconds treated as unknown for
                # every coin (those gaps and the poller's)
                "lost_lines": int(self.lost), "lost_hours": [iso(h)[:13] for h in sorted(self.lost_hours)][:48],
                "capped_coins": len(self.gaps),
                "capped_hours": sorted({iso(t)[:13] for ts in self.gaps.values() for t in ts})[:48],
                "unknown_s": round(down_s, 1)}


def rug_report_of(fields, mint):
    """A RugCheck report the engine reads (newpairs.rug_check_np) from a tape "rug" line, or None (a failed ask):
      - the whole report under "report", or its kept fields inline (topHolders, risks, ...): as it is;
      - the recorder's summary (version 1: "ok", "holders", "top1", "top10", "top" = the top holders' % with the
        curve and pools left out, "risks" = [[name, level], ...], "insiders", "rugged"; since the holder count of
        wallets: "held", "empty", "curve" = the accounts it listed by kind): rebuilt into the few fields
        rug_check_np() reads (a holder per % in "top", then as many holding, empty and curve accounts as the line
        counted: recorder.holder_list, the risks with their levels), so the engine's own rules (np_max_holder_pct,
        np_max_top10_pct, the bad risk names, insiders, the holders: newpairs.rug_holders) judge it again. A line
        recorded before "held" (rug_line_old: f16ee6e's, whose "holders" is RugCheck's totalHolders, every open
        token account, the empty ones and the curve's included) counts the wallets its "top" lists and no more: all
        the wallets holding the coin when it lists fewer than 10 (RugCheck lists the biggest accounts first, so the
        ones it left out held nothing), else at least 10, a lower bound. A coin whose holder rules need more waits;
        with no fresher report on the tape the replay counts it "rug_stale" (unjudged), never a PASS live would not
        give (live's count: newpairs.rug_holders on the whole report);
      - a short {"ok", "reasons", "top1_pct", "top10_pct"} summary: one holder at top1_pct, the rest of the top
        10 in nine equal parts, a "danger" risk per reason when it said FAIL."""
    if not isinstance(fields, dict) or fields.get("ok") is False and not any(
            k in fields for k in ("report", "topHolders", "top", "top1", "top1_pct")):
        return None
    if isinstance(fields.get("report"), dict):
        rep = dict(fields["report"])
    elif "topHolders" in fields or "graphInsidersDetected" in fields or "totalHolders" in fields:
        rep = dict(fields)
    elif _summary_line(fields):
        pcts = [num(p) for p in fields.get("top") or [] if num(p) is not None and num(p) > 0]
        if not pcts and num(fields.get("top1")):
            top1 = num(fields.get("top1"))
            rest = max(0.0, (num(fields.get("top10")) or top1) - top1) / 9.0
            pcts = [top1] + ([min(rest, top1)] * 9 if rest > 0 else [])
        risks = []
        for r in fields.get("risks") or []:
            if isinstance(r, (list, tuple)) and r:
                risks.append({"name": str(r[0]), "level": str(r[1]) if len(r) > 1 else ""})
            elif isinstance(r, dict):
                risks.append(r)
            elif isinstance(r, str):
                risks.append({"name": r, "level": ""})
        rep = {"rugged": bool(fields.get("rugged")), "risks": risks,
               "topHolders": bot.recorder.holder_list(pcts, fields.get("held"), fields.get("empty") or 0,
                                                  fields.get("curve") or 0),
               # an old line: the wallets its top lists (its totalHolders counted the empty accounts too)
               "totalHolders": len(pcts) if fields.get("held") is None else int(num(fields.get("holders")) or 0),
               "graphInsidersDetected": int(num(fields.get("insiders")) or 0),
               "markets": bot.recorder.rebuilt_markets(fields), "knownAccounts": {}}
    elif any(k in fields for k in ("ok", "top1_pct", "top10_pct", "reasons")):
        top1 = max(0.0, num(fields.get("top1_pct")) or 0.0)
        top10 = max(top1, num(fields.get("top10_pct")) or top1)
        holders = [{"address": "replay-top1", "owner": "replay-top1", "pct": top1}]
        rest = (top10 - top1) / 9.0
        if rest > 0:
            holders += [{"address": "replay-h%d" % i, "owner": "replay-h%d" % i, "pct": min(rest, top1 or rest)}
                        for i in range(9)]
        reasons = [str(r) for r in fields.get("reasons") or [] if isinstance(r, str)]
        bad = fields.get("ok") is False
        rep = {"rugged": bool(fields.get("rugged")), "topHolders": holders,
               "totalHolders": int(num(fields.get("holders")) or 0),
               "graphInsidersDetected": int(num(fields.get("insiders")) or 0),
               "risks": [{"name": r, "level": "danger"} for r in (reasons or ["RugCheck: fail"])] if bad else [],
               "markets": [], "knownAccounts": {}}
    else:
        return None
    rep.setdefault("mint", mint)
    return rep


def _summary_line(fields):
    """Whether a "rug" line is the recorder's summary (rug_report_of's second form), not a whole report."""
    return not isinstance(fields.get("report"), dict) and not any(
        k in fields for k in ("topHolders", "graphInsidersDetected", "totalHolders")) and (
        any(k in fields for k in ("top", "top1", "top10")) or (
            any(k in fields for k in ("risks", "insiders", "holders")) and
            not any(k in fields for k in ("top1_pct", "top10_pct", "reasons"))))


def rug_line_old(fields):
    """Whether a "rug" line is a recorder summary from before the holder count of wallets ("held", "empty",
    "curve": c8d6617): its "holders" is RugCheck's totalHolders, so rug_report_of counts the wallets its top lists."""
    return isinstance(fields, dict) and _summary_line(fields) and fields.get("held") is None


HOUR_RE = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2})")


def tape_files(data):
    """The tape files under <data>/tapes (or data itself when it holds them), in its subfolders too (the workflow's
    artifacts, one folder per job: an hour two jobs shared comes in two parts; Tape sorts the lines by time)."""
    root = os.path.join(data, "tapes") if os.path.isdir(os.path.join(data, "tapes")) else data
    if os.path.isfile(root):
        return [root]
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True) + \
        glob.glob(os.path.join(root, "**", "*.jsonl.gz"), recursive=True)
    return sorted(files, key=lambda p: (os.path.basename(p), p))


def _file_hour(path):
    m = HOUR_RE.search(os.path.basename(path))
    if not m:
        return None
    try:
        return parse_time("%sT%s:00" % (m.group(1), m.group(2)))
    except ValueError:
        return None


def read_tape(data, since=None, until=None, lookback_s=LOOKBACK_S):
    """The tape lines of [since - lookback_s, until] from <data>/tapes/*.jsonl (whole hours by their file names;
    a file whose name has no hour is read whole). since / until: epoch seconds or None (everything). -> Tape."""
    tape = Tape()
    lo = None if since is None else since - lookback_s
    for path in tape_files(data):
        h = _file_hour(path)
        if h is not None and ((lo is not None and h + 3600 <= lo) or (until is not None and h > until)):
            continue
        tape.files.append(path)
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        tape.bad += 1
                        continue
                    t = num(rec.get("t") if isinstance(rec, dict) else None)
                    if t is not None and ((lo is not None and t < lo) or (until is not None and t > until + EPS)):
                        continue
                    tape.add(rec)
        except OSError:
            tape.bad += 1
    return tape.finish()


def lookback_for(data, since, most_h=8.0):
    """How far before `since` to read the tape so that the run `since` falls in starts on it (its "start" line: the
    settings, cash and LEARNER memory a live check warms up from; a run is at most 340 minutes): seconds, at least
    LOOKBACK_S; LOOKBACK_S when there is no since or no start line within most_h hours before it."""
    if since is None:
        return LOOKBACK_S
    best = None
    for path in sorted(tape_files(data), key=lambda p: _file_hour(p) or 0.0, reverse=True):
        h = _file_hour(path)
        if h is None or h > since or h + 3600 < since - most_h * 3600:
            continue
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"start"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    t = num(rec.get("t")) if isinstance(rec, dict) and rec.get("k") == "start" else None
                    if t is not None and t <= since and (best is None or t > best):
                        best = t
        except OSError:
            continue
        if best is not None and best >= h:
            break                           # a start in this hour: an older file cannot hold a later one
    return max(LOOKBACK_S, since - best + 1.0) if best is not None else LOOKBACK_S


def tape_of(records):
    """A Tape from records in memory (the tests, the synthetic generator)."""
    tape = Tape()
    for r in records:
        tape.add(r)
    return tape.finish()


# ---------------------------------------------------------------- the simulated clock and worker pools

class SimClock(object):
    """Epoch seconds that only move forward; now() is the engine's naive UTC datetime."""

    def __init__(self, t):
        self.t = float(t)

    def now(self):
        return EPOCH + datetime.timedelta(seconds=self.t)

    def set(self, t):
        if t > self.t:
            self.t = float(t)

    def advance(self, sec):                 # bot.run()'s sleep (only used when the events fail)
        self.t += max(0.0, num(sec) or 0.0)


class SimFuture(object):
    """concurrent.futures.Future's face (done, result, add_done_callback, cancel) for a call that is answered at a
    time of the simulated clock."""

    def __init__(self, ctx, ready, value=None, exc=None):
        self.ctx, self.ready, self.value, self.exc = ctx, ready, value, exc
        self.cbs, self._cancelled, self.fired = [], False, False

    def done(self):
        return self._cancelled or self.ctx.clock.t >= self.ready - EPS

    def cancelled(self):
        return self._cancelled

    def cancel(self):
        if self.done():
            return False
        self._cancelled = True
        return True

    def result(self, timeout=None):
        if self._cancelled:
            raise concurrent.futures.CancelledError()
        if not self.done():
            raise concurrent.futures.TimeoutError()
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
        if self.fired or self._cancelled:
            return
        self.fired = True
        for fn in self.cbs:
            try:
                fn(self)
            except Exception:               # noqa: BLE001 - like a real executor: a callback's error is not ours
                pass


class SimPool(object):
    """The Night's RugCheck and SOL-price pools on the simulated clock: RugCheck answers rug_latency_s later (or
    when the recorded report exists), everything else at once. Deterministic, unlike threads."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.futs = []

    def submit(self, fn, *args):
        ctx, now = self.ctx, self.ctx.clock.t
        if getattr(fn, "__self__", None) is ctx.market and getattr(fn, "__name__", "") in ("rug_report_np", "rug_report"):
            ready, value = ctx.market.rug_answer(args[0] if args else None, now)
            fut = SimFuture(ctx, ready, value)
        else:
            try:
                fut = SimFuture(ctx, now, fn(*args))
            except Exception as e:          # noqa: BLE001
                fut = SimFuture(ctx, now, exc=e)
        if not fut.done():
            ctx.wait(fut)
        self.futs.append(fut)
        del self.futs[:-500]
        return fut

    def shutdown(self, wait=True, cancel_futures=False):
        if cancel_futures:
            for fut in self.futs:
                fut.cancel()


# ---------------------------------------------------------------- the market: Jupiter = the curve maths

class ReplayMarket(object):
    """Market's face for the engine. A quote at time t: the pump.fun curve maths on the latest recorded reading at
    or before min(t + fill_delay_s, horizon) with curve_fee_pct (bot.curve_buy_tokens / curve_sell_sol: the very
    functions the engine checks Jupiter against). The engine applies its own latency haircut and fees after."""

    def __init__(self, tape, clock, horizon, cfg, rug_latency_s=0.4, rug_wait_s=6.0, fill_delay_s=0.0,
                 sol_default=DEFAULT_SOL_USD):
        self.tape, self.clock, self.horizon = tape, clock, horizon
        self.fee = num(cfg.get("curve_fee_pct"))
        self.fee = 1.25 if self.fee is None else self.fee
        self.rug_latency, self.rug_wait = max(0.0, rug_latency_s), max(0.0, rug_wait_s)
        self.fill_delay = max(0.0, fill_delay_s)
        self.sol_default = sol_default
        self.errors = []
        self.quote_why = None
        self.source = "replay"
        self.stats = collections.Counter()
        # mints RugCheck was asked about with no report on the tape (any ask: a prefetch's too); replay() counts
        # those whose ENTRY needed one (Context.wanted) and never got a recorded report ("rug_missing")
        self.rug_missing = set()
        # mints the engine asked again while that report left them WAITING (too few holders for the entry: the
        # recheck of newpairs.rug_cache_s) and the tape could only answer with that same report: the live bot never
        # asked then. Unless the coin was bought in the end, its result is unknown ("unjudged"). A report handed out
        # again that would not leave the coin waiting (an ordinary ask after the 30 s cache, a RISK retry: the
        # latest report by now, the replay's policy) is no reason to doubt the result
        self.rug_stale = set()
        self.rug_given = {}                 # mint -> recorded times of the reports handed out
        self.waits = None                   # fn(mint, report) -> True when that report leaves the coin waiting now

    def _handed(self, mint, t, rep=None):
        seen = self.rug_given.setdefault(mint, set())
        if t in seen and self._waits(mint, rep):
            self.rug_stale.add(mint)
            self.stats["rug_stale"] += 1
        seen.add(t)

    def _waits(self, mint, rep):
        """Whether the report leaves the coin waiting for its holders now (replay() wires the engine's judgement;
        without it, never: no coin is doubted on a guess)."""
        if self.waits is None or not isinstance(rep, dict):
            return False
        try:
            return bool(self.waits(mint, rep))
        except Exception:                   # noqa: BLE001 - a doubt, never a crash
            return False

    def at(self):
        return min(self.clock.t, self.horizon)

    # Jupiter
    def can_quote(self, n=1):
        return True                         # no rate limits in a replay (live: 100 quotes per 10 s, enough)

    def quote(self, input_mint, output_mint, amount, slippage_bps=300):
        self.quote_why = None
        mint = output_mint if input_mint == SOL else input_mint
        a = num(amount)
        if a is None or a <= 0:
            self.quote_why = "nothing to sell"
            return None
        st = self.tape.state_at(mint, min(self.at() + self.fill_delay, self.horizon))
        if st is None:
            self.quote_why = "no Jupiter route"
            self.stats["no_route"] += 1
            return None
        _, vsol, vtok = st
        if input_mint == SOL:
            sol_in = a / 1e9
            tokens = bot.curve_buy_tokens(vsol, vtok, sol_in, self.fee)
            spot = sol_in * (1 - self.fee / 100.0) / (vsol / vtok)
            out = int(tokens * 1e6)
            impact = 1 - tokens / spot if spot > 0 else 0.0
            self.stats["buy_quotes"] += 1
        else:
            tokens = a / 1e6
            sol = bot.curve_sell_sol(vsol, vtok, tokens, self.fee)
            spot = tokens * (vsol / vtok) * (1 - self.fee / 100.0)
            out = int(sol * 1e9)
            impact = 1 - sol / spot if spot > 0 else 0.0
            self.stats["sell_quotes"] += 1
        if out <= 0:
            self.quote_why = "no Jupiter route"
            return None
        return {"outAmount": str(out), "priceImpactPct": "%.6f" % max(0.0, impact), "routePlan": [{"label": "replay"}]}

    def prices(self, mints, keep=0):
        """SOL only: Jupiter's price v3 of a coin (live's stale fallback) is not on the tape, so a coin without a
        fresh reading keeps its last price in the engine ("no price: the last one stands")."""
        sol = self.sol_price()
        return {SOL: {"usd": sol}} if sol else {}

    def live_prices(self, mints):
        return self.prices(mints)

    def sol_price(self):
        return self.sol_at(self.at())

    def sol_at(self, t):
        px = self.tape.sol_at(min(t, self.horizon))
        return px if px is not None else self.sol_default

    # RugCheck
    def rug_answer(self, mint, now):
        """(when the answer lands, the report or None). The live bot's own ask at this moment (a "rug" line whose
        t - ms is within SAME_ASK_S of now): its answer as it came, the report or the failure, at its recorded time
        (RugCheck's real latency; a replay in step with the live run asks when live did). Any other moment: the
        latest report recorded by now + rug_latency_s, landing then; else the first one recorded within rug_wait_s
        (RugCheck's own timeout: it lands then, never before the report existed); else None ("no report", never
        invented). A report handed out to this coin before, handed out again, marks it rug_stale (see __init__)."""
        self.stats["rug_asks"] += 1
        reps = self.tape.rugs.get(mint) or []
        for t, rep, ask in reps:
            if ask is not None and abs(ask - now) <= SAME_ASK_S and t <= self.horizon + EPS:
                self.stats["rug_same_ask"] += 1
                if rep is None:
                    self.stats["rug_failed"] += 1
                else:
                    self._handed(mint, t, rep)
                return max(t, now), copy.deepcopy(rep)
        land = min(now + self.rug_latency, self.horizon)
        best = None
        for t, rep, _ in reps:
            if rep is None:
                continue
            if t <= land + EPS:
                best = (land, rep, t)
            elif best is None and t <= now + self.rug_wait + EPS and t <= self.horizon:
                best = (t, rep, t)
                break
            else:
                break
        if best is None:
            self.rug_missing.add(mint)
            self.stats["rug_none"] += 1
            return land, None
        self._handed(mint, best[2], best[1])
        return best[0], copy.deepcopy(best[1])

    def rug_report(self, mint, timeout=20):
        """A synchronous call (nothing in New Pairs makes one; here for Market's face): what has landed by now."""
        land, rep = self.rug_answer(mint, self.at())
        return rep if land <= self.at() + EPS else None

    def rug_report_np(self, mint):
        return self.rug_report(mint)


# ---------------------------------------------------------------- the feeds

class ReplayFeeds(object):
    """feeds.Feeds' API on a tape. get(wait) hands out the next due event, moving the simulated clock up to `wait`
    seconds (to the next event's time when it comes sooner); a curve reading goes out only while its mint is
    watched; nothing goes out before its recorded time or after `end`. The coin's metadata comes when fetch_meta()
    asks for it, at its recorded time (or now when that was earlier)."""

    def __init__(self, ctx, start, end):
        self.ctx, self.tape, self.clock = ctx, ctx.tape, ctx.clock
        self.end = end
        self.i = self.tape.first_index(start)
        self.q = collections.deque()        # the engine's own wake-ups (np_wake appends a "rug" event)
        self.later = []                     # heap (t, seq, event): metadata answers, first readings after a watch
        self.seq = 0
        self.watching = {}
        self.w0 = {}                        # mint -> when the engine started watching it
        self.cand = {}                      # unpolled mint watched -> still an entry candidate (not rejected) when seen
        self.poller = types.SimpleNamespace(ok=0)     # never moves: the replay touches the covered held coins itself
        self.night = None
        self.holding = False
        self.handed = 0
        self.started = self.stopped = 0

    def bind(self, night):
        self.night = night

    # feeds.Feeds' API
    def start(self):
        self.started += 1
        return self

    def stop(self, timeout=3.0):
        self.stopped += 1
        return True

    def set_holding(self, holding):
        self.holding = bool(holding)

    def watch(self, mint, key):
        if not (isinstance(mint, str) and mint):
            return False
        new = mint not in self.watching
        self.watching[mint] = key
        if new:
            self.w0[mint] = self.clock.t
            if mint in self.tape.unpolled:
                self.cand[mint] = True
            # a new watch starts with a first reading (feeds.py: CurveState.forget): the latest recorded state at
            # or before now, if there is one (a brand-new coin has none yet: its first reading is on the tape)
            ev = self._latest_curve(mint)
            if ev is not None:
                self._later(self.clock.t, ev)
        return True

    def unwatch(self, mint):
        self.seen(mint)
        return self.watching.pop(mint, None) is not None

    def seen(self, mint):
        """A watch ends: a coin the engine watched 2 s or more as an entry candidate (never rejected) that the live
        bot let go at once (Tape.unpolled) had no readings to judge it on: counted (ctx.unseen), so a looser entry
        rule's missing trades are not taken for "no trade"."""
        w0 = self.w0.pop(mint, None)
        cand = self.cand.pop(mint, False)
        if w0 is not None and cand and self.clock.t - w0 >= 2.0:
            self.ctx.unseen.add(mint)

    def watched(self):
        return dict(self.watching)

    def fetch_meta(self, mint, uri):
        """The uri's answer as the live bot got it, when it got it: its links, or its failure (the engine then asks
        RugCheck for its copy of the metadata, like live, and finds the links in the report live took them from:
        Tape._rug_links). A tape without the uri's own line: the first readable one (RugCheck's copy), else the
        first failed one; none on the tape: a failed read 2 s later (every gateway failed)."""
        recs = self.tape.metas.get(mint) or []
        own = [r for r in recs if r[1].get("source") != "rugcheck"]
        recs = own or [r for r in recs if r[1].get("ok", True)] or recs
        if recs:
            t, fields = recs[0]
            at = max(self.clock.t, t)
            ev = dict(fields, kind="meta", mint=mint, t=at)
            ev.setdefault("ok", True)
            self._later(at, ev)
        else:                               # live: every gateway failed after ~2 s; the engine then asks RugCheck
            at = self.clock.t + 2.0
            self._later(at, {"kind": "meta", "mint": mint, "ok": False, "twitter": "", "telegram": "", "website": "",
                             "error": "no metadata on the tape", "t": at})
        return True

    def status(self):
        return [{"name": "pumpportal", "connected": True, "events": self.handed, "last_event_age_s": 0.0, "errors": 0,
                 "last_error": None},
                {"name": "curve-poll", "connected": True, "events": self.handed, "last_event_age_s": 0.0, "errors": 0,
                 "last_error": None}]

    def text(self):
        return "replay · %d watched" % len(self.watching)

    def get(self, timeout=0.25):
        deadline = self.clock.t + max(0.0, num(timeout) or 0.0)
        while True:
            self._tick()
            ev = self._next_due()
            if ev is not None:
                return ev
            nxt = self._next_time()
            if nxt is None or nxt > deadline + EPS:
                self.clock.set(deadline)
                self._tick()
                return self._next_due()
            self.clock.set(nxt)

    def drain(self, limit=1000):
        out = []
        self._tick()
        while len(out) < limit:
            ev = self._next_due()
            if ev is None:
                break
            out.append(ev)
        return out

    # inside
    def _later(self, t, ev):
        self.seq += 1
        heapq.heappush(self.later, (t, self.seq, ev))

    def _tick(self):
        """At the clock's time: RugCheck answers that landed wake the loop; the held coins the tape still covers are
        fresh (the live poller read them: feeds.py sends changes only, the engine's np_touch marks them fresh)."""
        self.ctx.fire(self.clock.t)
        night = self.night
        wl = getattr(night, "wl", None)
        if wl is not None:
            held = [m for m in wl.held() if self.tape.covered(m, self.clock.t)]
            if held:
                wl.touch(self.clock.t, held)
            for m in self.cand:             # an unpolled coin the engine rejected is no missing trade
                c = wl.get(m)
                if c is not None and getattr(c, "status", "watching") != "watching":
                    self.cand[m] = False

    def _next_due(self):
        now = self.clock.t
        if self.q:
            return self._out(self.q.popleft())
        if self.later and self.later[0][0] <= now + EPS:
            return self._out(heapq.heappop(self.later)[2])
        ev = self.tape.events
        while self.i < len(ev) and ev[self.i][0] <= now + EPS:
            t, _, kind, mint, fields = ev[self.i]
            self.i += 1
            if t > self.end + EPS:
                self.i = len(ev)
                break
            if kind in (CURVE, GONE) and mint not in self.watching:
                continue                    # the poller reads watched curves only
            return self._out(self._event(t, kind, mint, fields))
        return None

    def _next_time(self):
        ts = []
        if self.later:
            ts.append(self.later[0][0])
        if self.i < len(self.tape.events) and self.tape.events[self.i][0] <= self.end + EPS:
            ts.append(self.tape.events[self.i][0])
        w = self.ctx.next_ready()
        if w is not None:
            ts.append(w)
        return min(ts) if ts else None

    def _out(self, ev):
        self.handed += 1
        self.ctx.delivered += 1
        t = ev.get("t") if isinstance(ev, dict) else None
        if isinstance(t, (int, float)) and t > self.ctx.last_event_t:
            self.ctx.last_event_t = t
        return ev

    def _event(self, t, kind, mint, fields):
        if kind == CREATE:
            self.ctx.creates += 1
            ev = dict(fields)
            ev.update(kind="create", txType="create", mint=mint, _rx=t, t=t)
            return ev
        if kind == CURVE:
            return curve_event(mint, self.watching.get(mint) or self.tape.keys.get(mint) or "", t, fields)
        ev = dict(fields)
        ev.update(kind=kind, mint=mint, t=t)
        if kind == MIGRATE:
            ev.setdefault("txType", "migrate")
            ev["_rx"] = t
        return ev

    def _latest_curve(self, mint):
        c = self.tape.curves.get(mint)
        if not c:
            return None
        i = bisect.bisect_right(c[0], min(self.clock.t, self.end) + EPS) - 1
        return curve_event(mint, self.watching.get(mint) or "", self.clock.t, c[1][i]) if i >= 0 else None


def curve_event(mint, key, t, fields):
    """feeds.py's curve event from a tape line: the raw reserves as recorded, price_sol worked out when missing (0.0
    for a zeroed curve, like feeds.py)."""
    ev = dict(fields)
    ev.update(kind="curve", mint=mint, key=key or fields.get("key") or "", t=t, src=fields.get("src") or "tape")
    if num(ev.get("price_sol")) is None:
        r = read_curve(ev) if read_curve is not None else None
        ev["price_sol"] = r["price"] if r is not None else 0.0
    return ev


# ---------------------------------------------------------------- one replay

class Context(object):
    """What one replay's parts share: the tape, the clock, the market, the futures waiting on the clock, the Nights
    made (one a job) and each day's trades taken before close_night() drops them."""

    def __init__(self, tape, clock, market):
        self.tape, self.clock, self.market = tape, clock, market
        self.pending = []                   # heap (ready, seq, future)
        self.seq = 0
        self.night = None
        self.closed = []                    # [(night id, trades)] of each day the engine closed
        self.delivered = 0
        self.creates = 0
        self.last_event_t = float("-inf")
        self.unseen = set()                 # coins watched with no readings on the tape (ReplayFeeds.seen)
        self.wanted = set()                 # coins whose trigger fired and wanted their RugCheck report (np_rug_get)

    def wait(self, fut):
        self.seq += 1
        heapq.heappush(self.pending, (fut.ready, self.seq, fut))

    def next_ready(self):
        while self.pending and self.pending[0][2].cancelled():
            heapq.heappop(self.pending)
        return self.pending[0][0] if self.pending else None

    def fire(self, now):
        while self.pending and self.pending[0][0] <= now + EPS:
            heapq.heappop(self.pending)[2].fire()


def night_class(ctx):
    """bot.Night with deterministic worker pools, bound to the replay's feeds, keeping each day's trades. It changes
    no rule: every decision is the engine's own. Two things a replay does not need are skipped: the files and the
    live panel after each trade (publish(); a job's last, forced publish still writes state.json, which the next job
    reads, as live) and the speed panel's medians every second (np_speed_view: display only). They were two thirds
    of a replay's time."""

    class ReplayNight(bot.Night):
        def __init__(self, *a, **kw):
            super(ReplayNight, self).__init__(*a, **kw)
            self.rug_pool = SimPool(ctx)
            self.sol_pool = SimPool(ctx)
            ctx.night = self

        def attach(self, feeds, why=None):
            if hasattr(feeds, "bind"):
                feeds.bind(self)
            return super(ReplayNight, self).attach(feeds, why)

        def publish(self, prices, live=None, seq=0, memo=None, force=False):
            if force:
                return super(ReplayNight, self).publish(prices, live, seq, memo, force)
            self.traded = False                 # what publish() does first; the files wait for the job's end
            return None

        def np_speed_view(self, now):
            return None

        def np_rug_get(self, mint):
            # the report a fire (or RISK's shadow) judges the coin's entry on: a coin whose entry never got one is
            # unjudged; a prefetch that found nothing yet is not (it asks before the trigger, maybe never fires)
            ctx.wanted.add(mint)
            return super(ReplayNight, self).np_rug_get(mint)

        def sol_usd(self, prices=None, fetch=False):
            # SOL/USD as the live bot priced with it at this moment (the tape's "sol" lines: each change; older
            # tapes: the heartbeat's), not re-fetched on the engine's own 30 s schedule: a fetch only lands on the
            # tape after it, so the replay would lag the live run's price by a fetch
            px = ctx.market.sol_at(self.epoch())
            self.sol_px = (self.epoch(), px)
            return px

        def close_night(self):
            n = self.state.get("night")
            if n:
                ctx.closed.append((n.get("id"), copy.deepcopy(n.get("trades") or [])))
            return super(ReplayNight, self).close_night()

    return ReplayNight


_SWAP = threading.Lock()


@contextlib.contextmanager
def engine(cls):
    """bot.run() builds its Night by name: the replay's subclass for the length of one run (a lock: one at a time)."""
    with _SWAP:
        real = bot.Night
        bot.Night = cls
        try:
            yield
        finally:
            bot.Night = real


def rug_waits(night, mint, rep):
    """Whether RugCheck's report leaves the engine's coin waiting for its holders now (newpairs.rug_check_np's
    "waiting: ..." on a copy of the coin: the real one keeps its own verdict). False when the coin is not watched."""
    wl = getattr(night, "wl", None)
    coin = wl.get(mint) if wl is not None else None
    if coin is None:
        return False
    np = bot.newpairs
    c = np.Coin.from_dict(coin.to_dict())
    ok = np.rug_check_np(rep, c, night.cfg)[0]
    return ok is None and np.rug_waiting(c)


TRADE_KEYS = ("symbol", "mint", "opened", "closed", "cost", "proceeds", "pnl", "pnl_pct", "why", "tier", "score",
              "steps", "age_s", "mcap_sol", "dev_pct", "sol_in", "fill", "size_pct", "exit_ms", "slip")


def jobs_for(tape, since, until, job_min=340.0, job_gap_s=120.0, align="auto"):
    """[(start, end)] of the jobs a replay of [since, until) runs, each one bot.run() with its job-end rules (no
    entries in its last job_no_entries_min, everything sold in its last job_flat_min):
      align "tape" (or "auto" when the tape has start / hb / stop lines): the live runs as recorded, cut to the
      range: the replay starts and stops where the live bot did, and sees nothing in between (nor did live);
      else jobs of job_min minutes, job_gap_s apart (the GitHub Actions chain: 340 min, then about 2 minutes
      until the next job runs). job_min 0: one job for the whole range."""
    if align in ("tape", "auto") and getattr(tape, "runs", None):
        out = [(max(a, since), min(b, until)) for a, b in tape.runs if b > since and a < until]
        return [(a, b) for a, b in out if b - a >= 1.0]
    if align == "tape":
        return []
    step = (num(job_min) or 0.0) * 60.0
    gap = max(0.0, num(job_gap_s) or 0.0)
    out, t = [], since
    while t < until - 1.0:
        end = min(until, t + step) if step > 0 else until
        out.append((t, end))
        t = end + gap
    return out


def replay(tape, cfg, since=None, until=None, job_min=340.0, job_gap_s=120.0, rug_latency_s=0.4, fill_delay_s=0.0,
           memory=None, out=None, data_dir=None, keep_log=False, align="auto", cfg_at=None):
    """One config over [since, until) of the tape, through bot.run() job after job (jobs_for). -> a result dict:
    {"trades": [...], "open": [...], "start_equity", "end_equity", "memory": the LEARNER's memory at the end,
     "stats": {...}, "log": [...] (keep_log)}. memory: a LEARNER memory to start from (else a fresh one).
    cfg_at: fn(job start) -> the config that job trades with (None: cfg), for a range whose recorded runs traded
    with different settings (live_check); the book and the LEARNER's memory carry from job to job as always."""
    t0, t1 = tape.span()
    since = t0 if since is None else since
    until = t1 if until is None else until
    if since is None or until is None or until <= since:
        return {"trades": [], "open": [], "start_equity": None, "end_equity": None, "memory": memory,
                "stats": {"sim_s": 0.0, "wall_s": 0.0, "jobs": 0, "note": "nothing to replay"}, "log": []}
    raw_cfg, cfg = cfg, replay_cfg(cfg)
    clock = SimClock(since)
    market = ReplayMarket(tape, clock, until, cfg, rug_latency_s=rug_latency_s, fill_delay_s=fill_delay_s)
    ctx = Context(tape, clock, market)
    market.waits = lambda mint, rep: rug_waits(ctx.night, mint, rep)
    cls = night_class(ctx)
    own = data_dir is None
    data_dir = data_dir or tempfile.mkdtemp(prefix="memebot-replay-")
    log = []
    sink = (lambda line: log.append(line)) if keep_log else (out or (lambda *a: None))
    wall = time.monotonic()
    jobs = 0
    try:
        if memory is not None:
            with open(os.path.join(data_dir, "memory.json"), "w", encoding="utf-8") as fh:
                json.dump(memory, fh)
        for t, end in jobs_for(tape, since, until, job_min, job_gap_s, align):
            clock.set(t)
            feeds = ReplayFeeds(ctx, t, end)
            jc = cfg
            if cfg_at is not None:
                jc = replay_cfg(cfg_at(t) or raw_cfg)
                fee = num(jc.get("curve_fee_pct"))
                market.fee = 1.25 if fee is None else fee
            with engine(cls):
                bot.run(data_dir, jc, minutes=(end - t) / 60.0, anytime=False, market=market, now_fn=clock.now,
                        sleep=clock.advance, out=sink, live=None, feeds=feeds)
            for m in list(feeds.watching):      # still watched at the job's end
                feeds.seen(m)
            jobs += 1
        night = ctx.night
        state = night.state if night is not None else {}
        trades = [tr for _, ts in ctx.closed for tr in ts] + list((state.get("night") or {}).get("trades") or [])
        mem = bot.read_json(os.path.join(data_dir, "memory.json"), None)
        start_eq = num(cfg.get("start_balance")) or 0.0
        open_ = []
        end_eq = num(state.get("balance")) or 0.0
        for mint, pos in (state.get("positions") or {}).items():
            st = tape.state_at(mint, until)
            value = None
            if st is not None and pos.get("raw"):
                sol = bot.curve_sell_sol(st[1], st[2], pos["raw"] / 1e6, market.fee)
                value = sol * market.sol_price() * (1 - (num(cfg.get("latency_slip_pct")) or 0.0) / 100.0)
            end_eq += value or 0.0
            open_.append({"symbol": pos.get("symbol"), "mint": mint, "opened": pos.get("opened"), "cost": pos.get("cost"),
                          "value": round(value, 4) if value is not None else None})
        rows = []
        for tr in trades:
            row = {k: tr.get(k) for k in TRADE_KEYS if k in tr}
            te, to, mint = ts_epoch(tr.get("closed")), ts_epoch(tr.get("opened")), tr.get("mint")
            # blind: sold on a stale price (the tape had stopped covering the coin), or decided on a curve the tape
            # misses part of between the coin's launch and the sale (a "gap" line, the poller down, lines lost)
            row["blind"] = bool(te is not None and (
                not tape.covered(mint, te - num(cfg.get("np_stale_s") or 5.0))
                or tape.gap_in(mint, tape.created.get(mint, to if to is not None else te), te + 1.0)))
            rows.append(row)
        stats = dict(market.stats)
        bought = {tr.get("mint") for tr in trades} | set(state.get("positions") or {})
        stale = market.rug_stale - bought               # asked again, nothing newer on the tape, never bought
        # its trigger fired and no ask ever got a recorded report (a prefetch that came too early for one is no
        # reason: the coin may never fire, or its fire finds the live bot's own report)
        missing = (market.rug_missing & ctx.wanted) - set(market.rug_given) - bought
        stats.update(sim_s=round(until - since, 1), wall_s=round(time.monotonic() - wall, 2), jobs=jobs,
                     events=ctx.delivered, rug_missing=len(missing), unseen=len(ctx.unseen),
                     rug_stale=len(stale),
                     # coins this config would have judged on data the tape does not have (no readings, no
                     # RugCheck report, no fresher report for a recheck): its result on them is unknown, not "no trade"
                     unjudged=len(ctx.unseen | missing | stale),
                     sol_from_tape=bool(tape.sol[0]), max_event_t=ctx.last_event_t if ctx.delivered else None)
        stats["creates"] = ctx.creates
        return {"trades": rows, "open": open_, "start_equity": start_eq, "end_equity": round(end_eq, 4),
                "memory": mem, "stats": stats, "log": log if keep_log else [],
                # which coins, by cause (backtest.py leaves them out of its comparison with the base)
                "unjudged_coins": {"unseen": sorted(ctx.unseen), "missing": sorted(missing - ctx.unseen),
                                   "stale": sorted(stale - ctx.unseen - missing)}}
    finally:
        if own:
            shutil.rmtree(data_dir, ignore_errors=True)


# ---------------------------------------------------------------- live against replay

LIVE_SKIP = ("rec_", "live_")           # recorded settings a live check does not take over
# rules a run's start line does not name did not exist in the code that run traded with: a live check replays such a
# run with them off (the SHADOW trades; the entry-quality rules of newpairs.py), so a tape recorded before them is
# replayed as it was traded. bot.DEFAULTS names every one, so every start line since carries its value
LIVE_OFF = {"shadow_on": False, "np_min_holders": 0, "np_max_sol_per_holder": 0.0, "np_max_buy_sol": 0.0}


def live_check(tape, cfg, since=None, until=None, **opts):
    """Does the replay reproduce what the live bot did? The range replayed run by run: each recorded run with the
    settings on its own "start" line (over cfg; rec_* and live_* left out), so a config.json change between two jobs
    is replayed as it was traded; the cash and the LEARNER memory on the first run's start line, then the book and
    the memory the replay itself carries from run to run (as live). A range that starts inside a run is replayed
    from that run's start (when the tape has it: backtest.py reads the tape from there), and only the trades opened
    in [since, until) are compared. Then the live run's own trades (the tape's buy / sell lines) against the
    replay's: compare(). None when the tape has no live trade."""
    t0, t1 = tape.span()
    since = t0 if since is None else since
    until = t1 if until is None else until
    live = tape.live_trades(since, until)
    if not live or since is None or until is None:
        return None
    runs = tape.run_settings(since, until)

    def own(line):
        c = copy.deepcopy(cfg)
        c.update({k: copy.deepcopy(v) for k, v in (line or {}).items() if not k.startswith(LIVE_SKIP)})
        if line is not None:                        # a run from before a rule: replayed without it (LIVE_OFF)
            c.update({k: off for k, off in LIVE_OFF.items() if k not in line})
        return c

    first = runs[0] if runs and runs[0]["t"] is not None else None
    begin = first["t"] if first is not None and first["t"] < since else since
    c = own(first["cfg"] if first is not None else None)
    if first is not None and first["bal"] is not None and first["bal"] > 0:
        c["start_balance"] = first["bal"]
    per = [(r["from"], r["to"], own(r["cfg"])) for r in runs if r["cfg"] is not None]

    def cfg_at(t):
        for a, b, rc in per:
            if a - EPS <= t <= b + EPS:
                return dict(rc, start_balance=c["start_balance"])     # the cash only counts for a fresh book
        return c

    res = replay(tape, c, begin, until, memory=first["mem"] if first is not None else None, cfg_at=cfg_at, **opts)
    out = compare(live, [t for t in res["trades"] if since <= (ts_epoch(t.get("opened")) or since) < until])
    lines = [r["cfg"] for r in runs if r["cfg"] is not None]
    keys = sorted({k for x in lines for k in x if not k.startswith(LIVE_SKIP)})
    changed = [k for k in keys if len({json.dumps(x.get(k), sort_keys=True) for x in lines}) > 1]
    out.update(settings=("each run's own start line (%d runs)" % len(lines)) if lines else
               "the base config (no start line on the tape)",
               settings_changed=changed[:30], runs=len(runs),
               warmup_from=iso(begin) if begin < since else None,
               no_start=bool(runs) and first is None,
               memory="the first run's LEARNER memory" if first is not None and first["mem"] else "a fresh LEARNER memory",
               start_balance=c.get("start_balance"), unknown=tape.summary()["lost_lines"] + len(tape.gaps))
    return out


def compare(live, rep, entry_s=1.0, exit_s=2.0):
    """Live trades against replayed ones: matched by coin, in order, opened within entry_s seconds. -> counts (same
    entry; same exit: the same reason within exit_s), the gaps (exit time, P&L % after costs: replay - live), the
    unmatched trades and the matched pairs (a few fields each)."""
    def by_mint(ts):
        out = collections.defaultdict(list)
        for t in sorted(ts, key=lambda t: str(t.get("opened") or "")):
            out[t.get("mint")].append(t)
        return out

    lv, rp = by_mint(live), by_mint(rep)
    pairs, only_live, only_replay = [], [], []
    for mint in sorted(set(lv) | set(rp), key=str):
        left = list(rp.get(mint, []))
        for x in lv.get(mint, []):
            tx = ts_epoch(x.get("opened"))
            j = next((i for i, y in enumerate(left) if tx is not None and ts_epoch(y.get("opened")) is not None
                      and abs(ts_epoch(y.get("opened")) - tx) <= entry_s + EPS), None)
            if j is None:
                only_live.append(x)
            else:
                pairs.append((x, left.pop(j)))
        only_replay += left
    exit_gap, pnl_gap, same_exit = [], [], 0
    rows = []
    for x, y in pairs:
        a, b = ts_epoch(x.get("closed")), ts_epoch(y.get("closed"))
        g = abs(b - a) if a is not None and b is not None else None
        d = (num(y.get("pnl_pct")) - num(x.get("pnl_pct"))) if num(y.get("pnl_pct")) is not None and \
            num(x.get("pnl_pct")) is not None else None
        if g is not None:
            exit_gap.append(g)
        if d is not None:
            pnl_gap.append(d)
        if g is not None and g <= exit_s + EPS and x.get("why") == y.get("why"):
            same_exit += 1
        rows.append({"mint": x.get("mint"), "opened": x.get("opened"), "live": [x.get("closed"), x.get("why"), x.get("pnl_pct")],
                     "replay": [y.get("closed"), y.get("why"), y.get("pnl_pct")]})
    short = lambda t: {k: t.get(k) for k in ("mint", "symbol", "opened", "closed", "why", "pnl_pct") if k in t}
    n = len(pairs)
    return {"live": len(live), "replay": len(rep), "same_entry": n, "same_exit": same_exit,
            "exit_gap_s_max": round(max(exit_gap), 1) if exit_gap else None,
            "exit_gap_s_median": round(median(exit_gap), 1) if exit_gap else None,
            "pnl_pct_gap_mean": round(sum(pnl_gap) / len(pnl_gap), 2) if pnl_gap else None,
            "pnl_pct_gap_max": round(max(pnl_gap, key=abs), 2) if pnl_gap else None,
            "live_pnl": round(sum(num(t.get("pnl")) or 0.0 for t in live), 2),
            "replay_pnl": round(sum(num(t.get("pnl")) or 0.0 for t in rep), 2),
            "only_live": [short(t) for t in only_live[:20]], "only_replay": [short(t) for t in only_replay[:20]],
            "pairs": rows[:60]}


def live_check_text(c):
    if not c:
        return "live check: no live trade on the tape in this range"
    text = ("live check: %d live trades, %d replayed; same entry %d, same exit (reason, within 2 s) %d; exit gap max %s s; "
            "P&L gap a trade (replay - live) mean %s %%, worst %s %%; live %+.2f $ against replay %+.2f $" % (
                c["live"], c["replay"], c["same_entry"], c["same_exit"], c["exit_gap_s_max"], c["pnl_pct_gap_mean"],
                c["pnl_pct_gap_max"], c["live_pnl"], c["replay_pnl"]))
    if c.get("settings_changed"):
        text += " · the settings changed between runs (%s): each run replayed with its own" % ", ".join(
            c["settings_changed"][:6])
    if c.get("warmup_from"):
        text += " · replayed from the run's start (%s), the trades opened in the range compared" % c["warmup_from"]
    if c.get("no_start"):
        text += (" · the range starts inside a run whose start line is not on the tape: its first trades were replayed "
                 "on the base config's cash and a fresh LEARNER memory")
    if c.get("same_exit") is not None and (c["same_exit"] < c["live"] or c["replay"] != c["live"]):
        text += (" · where they differ, look first for a change the start lines do not show (the code between two "
                 "jobs)%s, then the fills (the curve maths on the last reading against what Jupiter gave live)" % (
                     ", the tape's gaps (%d lost lines and capped coins)" % c["unknown"] if c.get("unknown") else ""))
    return text


# ---------------------------------------------------------------- the numbers

def median(xs):
    xs = sorted(x for x in xs if num(x) is not None)
    if not xs:
        return None
    k = len(xs) // 2
    return xs[k] if len(xs) % 2 else (xs[k - 1] + xs[k]) / 2.0


def exit_kind(why):
    """A trade's exit reason in a few families for the report."""
    w = str(why or "").lower()
    for prefix, name in (("stop ", "stop"), ("tp ", "take profit"), ("trailing", "trailing stop"),
                         ("break-even", "break-even"), ("dump", "dump"), ("migrat", "migration"),
                         ("curve quiet", "quiet curve"), ("no curve reading", "no reading (blind)"),
                         ("fading", "fading"), ("the job ends", "job end"), ("end of", "end of night")):
        if w.startswith(prefix):
            return name
    if re.match(r"^\d+ min, only", w):
        return "time stop"
    if re.match(r"^\d+ min, no \+", w):
        return "late time stop"
    return "other"


def metrics(trades, start_equity=100.0):
    """Trades (closed, after every cost) -> the numbers per config: count, win rate, total and mean P&L ($ and %),
    max drawdown of the realized equity, profit factor, by tier, by exit."""
    rows = sorted([t for t in trades if num(t.get("pnl")) is not None], key=lambda t: str(t.get("closed") or ""))
    n = len(rows)
    pnl = [num(t["pnl"]) for t in rows]
    pct = [num(t.get("pnl_pct")) or 0.0 for t in rows]
    wins = sum(1 for x in pnl if x > 0)
    gain = sum(x for x in pnl if x > 0)
    loss = -sum(x for x in pnl if x < 0)
    eq = peak = float(start_equity or 0.0)
    dd = dd_pct = 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        if peak - eq > dd:
            dd = peak - eq
        if peak > 0:
            dd_pct = max(dd_pct, (peak - eq) / peak * 100.0)
    tiers, exits = {}, {}
    for t, x, p in zip(rows, pnl, pct):
        for table, key in ((tiers, t.get("tier") or "?"), (exits, exit_kind(t.get("why")))):
            r = table.setdefault(key, {"trades": 0, "wins": 0, "pnl": 0.0, "pnl_pct_sum": 0.0})
            r["trades"] += 1
            r["wins"] += 1 if x > 0 else 0
            r["pnl"] += x
            r["pnl_pct_sum"] += p
    for table in (tiers, exits):
        for r in table.values():
            r["pnl"] = round(r["pnl"], 2)
            r["mean_pct"] = round(r.pop("pnl_pct_sum") / r["trades"], 2)
    holds = [te - to for te, to in ((ts_epoch(t.get("closed")), ts_epoch(t.get("opened"))) for t in rows)
             if te is not None and to is not None]
    total = sum(pnl)
    mean = sum(pct) / n if n else 0.0
    sd = (sum((p - mean) ** 2 for p in pct) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {"trades": n, "wins": wins, "win_rate": round(wins / n, 3) if n else None,
            "total_pnl": round(total, 2), "return_pct": round(total / start_equity * 100, 2) if start_equity else None,
            "mean_pnl": round(total / n, 3) if n else None, "mean_pnl_pct": round(sum(pct) / n, 2) if n else None,
            "sum_pnl_pct": round(sum(pct), 2),          # the trades' returns added up: what backtest.py ranks by
            "median_pnl_pct": round(sorted(pct)[n // 2], 2) if n else None,
            "t_stat": round(mean / (sd / n ** 0.5), 2) if n > 1 and sd > 0 else None,
            "profit_factor": round(gain / loss, 2) if loss > 0 else (None if gain == 0 else float("inf")),
            "max_drawdown": round(dd, 2), "max_drawdown_pct": round(dd_pct, 2),
            "best": round(max(pnl), 2) if pnl else None, "worst": round(min(pnl), 2) if pnl else None,
            "avg_hold_s": round(sum(holds) / len(holds), 1) if holds else None,
            "blind": sum(1 for t in rows if t.get("blind")), "by_tier": tiers, "by_exit": exits}


def json_safe(x):
    """inf -> "inf" (json.dumps with allow_nan=False refuses it), recursively."""
    if isinstance(x, float) and not math.isfinite(x):
        return "inf" if x > 0 else ("-inf" if x < 0 else None)
    if isinstance(x, dict):
        return {k: json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [json_safe(v) for v in x]
    return x


# ---------------------------------------------------------------- worker processes (memebot/backtest.py)

_TAPE = None


def init_worker(tape):
    """A worker process's tape (inherited for free under fork; pickled once per worker otherwise)."""
    global _TAPE
    _TAPE = tape
    os.environ["MEMEBOT_LIVE"] = "0"
    os.environ["MEMEBOT_FEEDS"] = "0"


def run_task(task):
    """(key, cfg, since, until, memory, opts) -> (key, result without the log), in a fresh temp dir."""
    key, cfg, since, until, memory, opts = task
    try:
        res = replay(_TAPE, cfg, since, until, memory=memory, **(opts or {}))
        res.pop("log", None)
        res["metrics"] = metrics(res["trades"], res["start_equity"] or cfg.get("start_balance", 100.0))
        return key, res
    except Exception as e:                  # noqa: BLE001 - one broken config never stops the grid
        return key, {"error": "%s: %s" % (type(e).__name__, e), "trades": [], "metrics": metrics([]), "memory": None,
                     "stats": {}}


# ---------------------------------------------------------------- a synthetic tape (tests, demos)

K = 30.0 * 1.073e9


def vsol_at_gain(entry_vsol, gain_pct):
    """The virtual SOL at which the curve price is gain_pct over the price at entry_vsol (price = vsol^2 / K)."""
    return entry_vsol * math.sqrt(1 + gain_pct / 100.0)


def path_to(rsol_from, rsol_to, step):
    """Real SOL from one level to another in steps of `step` SOL (the last step shorter), the start left out."""
    out, r = [], rsol_from
    step = abs(step) if rsol_to >= rsol_from else -abs(step)
    while (rsol_to - r) * (1 if step > 0 else -1) > 1e-9:
        r = r + step if abs(rsol_to - r) > abs(step) else rsol_to
        out.append(round(r, 6))
    return out


def pump_path(peak_gain=60.0, trigger_rsol=3.3, dev_sol=0.3, up=0.5, down=0.4, floor_rsol=0.5, rise=0.5):
    """A coin that reaches the default trigger (6 buys, 3 SOL in, +20 %), climbs to peak_gain % over the trigger
    price, then bleeds down in sells of `down` SOL (under the 2 SOL dump size) to floor_rsol."""
    path = path_to(dev_sol, trigger_rsol, rise)
    peak = vsol_at_gain(30.0 + trigger_rsol, peak_gain) - 30.0
    path += path_to(trigger_rsol, peak, up)
    path += path_to(peak, floor_rsol, down)
    return path


def synth_coin(mint, t0, path, symbol=None, dev_sol=0.3, social=(False, False, False), rug="clean", meta=True,
               step_s=0.7, slot0=None, creator=None, mayhem=False, fmt="v1"):
    """The tape lines of one coin: its create at t0, its metadata 0.4 s later, its RugCheck report 2 s later
    ("clean", "rugged", or None for none), and a curve reading every step_s seconds along path (real SOL).
    fmt "v1": the recorder's lines (memebot/recorder.py: "c" with s/vs/vt/rs, meta x/tg/web, the RugCheck
    summary); "generic": curve/vsol/vtok/rsol, twitter/telegram/website, the whole report."""
    sym = symbol or mint[:6].upper()
    vsol = 30.0 + dev_sol
    vtok = K / vsol
    v1 = fmt == "v1"
    create = {"k": "create", "t": round(t0, 3), "m": mint, "symbol": sym, "name": sym + " coin",
              "initialBuy": 1.073e9 - vtok, "solAmount": dev_sol, "bondingCurveKey": "curve-" + mint,
              "vSolInBondingCurve": vsol, "vTokensInBondingCurve": vtok, "marketCapSol": vsol / vtok * 1e9,
              "is_mayhem_mode": mayhem, "pool": "pump"}
    if v1:
        create.update(creator=creator or "dev-" + mint, _rx=round(t0, 3))
    else:
        create.update(mint=mint, signature="sig-" + mint, traderPublicKey=creator or "dev-" + mint, txType="create",
                      uri="https://meta.example/" + mint)
    recs = [create]
    if meta:
        x, tg, web = social
        links = ("https://x.com/" + mint if x else "", "https://t.me/" + mint if tg else "",
                 "https://%s.fun" % mint if web else "")
        names = ("x", "tg", "web") if v1 else ("twitter", "telegram", "website")
        m = {"k": "meta", "t": round(t0 + 0.4, 3), "m": mint, "ok": True}
        m.update(zip(names, links))
        if v1:
            m.update(xk="profile" if x else "", src="meta.example", ms=90)
        recs.append(m)
    if rug is not None:
        risks = [["Creator history of rugged tokens", "danger"]] if rug == "rugged" else []
        top = [3.0, 2.0, 1.5]
        if v1:
            # as the recorder writes it now: 14 token accounts, the curve's and 13 wallets holding it (3 in "top")
            recs.append({"k": "rug", "t": round(t0 + 2.0, 3), "m": mint, "ok": True, "why": "prefetch", "ms": 350,
                         "holders": 14, "top1": top[0], "top10": sum(top), "top": top, "held": 3, "empty": 0,
                         "curve": 1, "risks": risks, "insiders": 0, "rugged": False, "score": 1})
        else:
            th = [{"address": "curve-ata-" + mint, "owner": "curve-" + mint, "pct": 80.0}] + \
                 [{"address": "h%d-%s" % (i, mint), "owner": "o%d-%s" % (i, mint), "pct": p} for i, p in enumerate(top)]
            recs.append({"k": "rug", "t": round(t0 + 2.0, 3), "m": mint,
                         "report": {"mint": mint, "rugged": False, "risks": [{"name": n, "level": l} for n, l in risks],
                                    "topHolders": th, "totalHolders": 14, "graphInsidersDetected": 0, "markets": [],
                                    "knownAccounts": {}}})
    slot = slot0 if slot0 is not None else int(t0 * 2.5)
    for k, rsol in enumerate(path):
        v = 30.0 + rsol
        t = round(t0 + 1.0 + k * step_s, 3)
        if v1:
            recs.append({"k": "c", "t": t, "m": mint, "s": slot + k + 1, "vs": int(v * 1e9), "vt": int(K / v * 1e6),
                         "rs": int(rsol * 1e9)})
        else:
            recs.append({"k": "curve", "t": t, "m": mint, "slot": slot + k + 1, "vsol": int(v * 1e9),
                         "vtok": int(K / v * 1e6), "rsol": int(rsol * 1e9), "complete": False})
    return recs


def synth_tape(start, coins, sol_usd=200.0, minutes=None, sol_every_s=10.0, fmt="v1"):
    """Tape lines for coins = [(seconds after start, mint, path, kwargs for synth_coin)] plus SOL's price every
    sol_every_s (v1: the recorder's heartbeat "hb" lines; generic: "sol" lines), sorted by time. No start / stop
    lines: a replay of it runs chained jobs, not recorded runs."""
    recs = []
    end = start
    for at, mint, path, kw in coins:
        c = synth_coin(mint, start + at, path, fmt=fmt, **(kw or {}))
        recs += c
        end = max(end, max(r["t"] for r in c))
    if minutes is not None:
        end = max(end, start + minutes * 60.0)
    t = start
    while t <= end + 1e-9:
        recs.append({"k": "hb", "t": round(t, 3), "sol": sol_usd} if fmt == "v1" else
                    {"k": "sol", "t": round(t, 3), "usd": sol_usd})
        t += sol_every_s
    recs.sort(key=lambda r: r["t"])
    return recs


def write_tape(data_dir, recs):
    """Tape lines into <data_dir>/tapes/YYYY-MM-DDTHH.jsonl by their hour (UTC), as the recorder writes them."""
    root = os.path.join(data_dir, "tapes")
    os.makedirs(root, exist_ok=True)
    by_hour = collections.defaultdict(list)
    for r in recs:
        h = (EPOCH + datetime.timedelta(seconds=r["t"])).strftime("%Y-%m-%dT%H")
        by_hour[h].append(r)
    paths = []
    for h, rs in sorted(by_hour.items()):
        path = os.path.join(root, h + ".jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            for r in rs:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        paths.append(path)
    return paths


def demo_coins(n=100, every_s=60.0, seed=1):
    """A mixed synthetic market: pumps that run to +20..+120 %, coins that stall and bleed to the stop, coins that
    never trigger, and a few with a RugCheck FAIL. Made up: for tests and demos, never a claim about real coins."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        mint = "syn%04d" % i + "x" * 32 + "pump"
        roll = rng.random()
        social = (rng.random() < 0.4, rng.random() < 0.5, rng.random() < 0.3)
        if roll < 0.35:
            path = pump_path(peak_gain=rng.choice([20, 40, 60, 90, 120]))
            kw = {"social": social}
        elif roll < 0.75:
            path = pump_path(peak_gain=rng.choice([3, 5, 6]), down=0.3)
            kw = {"social": social}
        elif roll < 0.9:
            path = path_to(0.3, rng.choice([1.0, 1.5, 2.0]), 0.3) + path_to(2.0, 0.4, 0.3)
            kw = {}
        else:
            path = pump_path(peak_gain=40)
            kw = {"rug": "rugged"}
        kw["symbol"] = "SYN%d" % i
        out.append((i * every_s + rng.random() * 10, mint, path, kw))
    return out


# ---------------------------------------------------------------- command line

def set_values(cfg, pairs):
    """--set key=value (JSON values; plain text when not JSON)."""
    for item in pairs or []:
        k, _, v = item.partition("=")
        try:
            cfg[k.strip()] = json.loads(v)
        except ValueError:
            cfg[k.strip()] = v
    return cfg


def main(argv=None, out=print):
    ap = argparse.ArgumentParser(description="Meme Bot replay: recorded tapes through the real New Pairs engine (paper).")
    ap.add_argument("--data", help="the bot's data dir (tapes in DATA/tapes/*.jsonl)")
    ap.add_argument("--since", help="UTC ISO time (default: the tape's start)")
    ap.add_argument("--until", help="UTC ISO time (default: the tape's end)")
    ap.add_argument("--config", default=bot.CONFIG)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=JSON", help="change one config value")
    ap.add_argument("--job-min", type=float, default=340.0, help="minutes a job (the GitHub Actions chain); 0: one job")
    ap.add_argument("--job-gap-s", type=float, default=120.0, help="seconds between two jobs (nothing is watched)")
    ap.add_argument("--rug-latency", type=float, default=0.4, help="RugCheck's answer time in seconds")
    ap.add_argument("--fill-delay", type=float, default=0.0, help="price fills this many seconds after the decision")
    ap.add_argument("--out", help="write the result (trades and numbers) as JSON here")
    ap.add_argument("-v", "--verbose", action="store_true", help="print the engine's log lines")
    ap.add_argument("--live-check", action="store_true",
                    help="also replay with the live run's own settings, cash and memory, and compare with its trades")
    ap.add_argument("--synth", metavar="DIR", help="write a synthetic tape into DIR/tapes and stop")
    ap.add_argument("--coins", type=int, default=100)
    ap.add_argument("--start", default="2026-11-26T08:00:00Z")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    for k in ("MEMEBOT_LIVE", "MEMEBOT_FEEDS", "MEMEBOT_REC"):    # this process never goes online nor records
        os.environ[k] = "0"
    if a.synth:
        recs = synth_tape(parse_time(a.start), demo_coins(a.coins, seed=a.seed))
        paths = write_tape(a.synth, recs)
        out("synthetic tape: %d lines in %d files under %s (made-up coins: for tests and demos only)" % (
            len(recs), len(paths), os.path.join(a.synth, "tapes")))
        return 0
    if not a.data:
        ap.error("--data or --synth")
    since, until = parse_time(a.since), parse_time(a.until)
    tape = read_tape(a.data, since, until, lookback_s=lookback_for(a.data, since) if a.live_check else LOOKBACK_S)
    cfg = set_values(bot.load_config(a.config), a.set)
    res = replay(tape, cfg, since, until, job_min=a.job_min, job_gap_s=a.job_gap_s, rug_latency_s=a.rug_latency,
                 fill_delay_s=a.fill_delay, out=out if a.verbose else None)
    res["metrics"] = metrics(res["trades"], res["start_equity"] or cfg["start_balance"])
    res["tape"] = tape.summary()
    m = res["metrics"]
    out("tape %s → %s · %s" % (res["tape"]["from"], res["tape"]["to"], json.dumps(res["tape"]["lines"])))
    out("replay: %d trades · win rate %s · total %+.2f $ · mean %s %% a trade (after every cost) · max drawdown %.2f $ · "
        "%d blind exits · %.1f s" % (m["trades"], m["win_rate"], m["total_pnl"], m["mean_pnl_pct"], m["max_drawdown"],
                                     m["blind"], res["stats"]["wall_s"]))
    if a.live_check:
        res["live_check"] = live_check(tape, cfg, since, until, job_min=a.job_min, job_gap_s=a.job_gap_s,
                                       rug_latency_s=a.rug_latency, fill_delay_s=a.fill_delay)
        out(live_check_text(res["live_check"]))
    if a.out:
        res.pop("memory", None)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(json_safe(res), fh, indent=1, allow_nan=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
