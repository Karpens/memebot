"""Meme Bot's TAPE RECORDER: what New Pairs saw and did, written down for a replay backtester.

Paper money and no promise: a tape only lets the rules be tried again on what really happened (the
curve readings the bot got, when it got them, what it decided), so a change can be judged on data
instead of hope. A backtest on these tapes is still not a real trade: no sandwich, no failed
transaction, no other bot reacting to ours.

What goes on the tape, for every PumpPortal create the bot sees (memebot/bot.py calls in, one guarded
call per hook; nothing here ever raises into the trading loop or waits for the disk):
  the create's fields; every curve reading while the coin is watched (change-only, as feeds.py sends
  them); the metadata's socials; the RugCheck summary of each report fetched; the trigger fires; every
  decision (skipped at its create, rejected and why, the LEARNER's skip, RISK's "no entry now"), the
  fills of the entry and of each sale, and the SOL/USD price used; and when its watch ended and why.

Followed coins: a coin that came near a trigger (half of np_min_sol and half of np_min_buys within
np_max_age_s of its launch), fired one, or was bought, is KEPT on the curve poller after the bot's
np_watch_s window, until rec_follow_s after its create (a bought coin also rec_follow_exit_s after its
last sale), so exits can be replayed on what the price did next. At most rec_max_follow such coins, and
never more than rec_max_keys curves polled in all (one getMultipleAccounts call of 100 accounts): the
bot's watch list, and with it every coin the bot holds, always counts first; among the followed coins a
bought one goes before a fired one before a near one, the newest first. Coins far from any trigger are
on the tape only for the bot's own np_watch_s.

Files: <data>/tapes/YYYY-MM-DDTHH.jsonl, one per UTC hour (the hour of each line's "t"). They are NOT git data:
the workflow's 30 s saver leaves tapes/ out of the meme-data commits (each save would have stored the growing
hour again, about 2 MB of history per tape hour, forever) and uploads the job's tapes as a GitHub Actions
artifact when the job ends ("tapes-<run id>-<attempt>", kept 14 days). A backtest reads a folder of those
artifacts, one subfolder per job (the loaders look into subfolders; an hour two jobs shared comes in two parts,
and the lines are put back in time order). Lines are buffered on the loop's thread (a dict and a json.dumps, a
few microseconds) and handed every rec_flush_s (5 s) to ONE writer thread that does every file operation (the
appends, the size of an hour already on disk, deleting tapes older than rec_keep_days at start). A slow or full
disk only delays that thread; its queue is bounded (rec_max_queue_mb), what does not fit is dropped and counted
as LOST (hb "lost"). An hour's file has a size cap, rec_max_mb_hour, and when it fills, what goes first is ranked
(SHARE): the followed coins' curve readings stop at 60 % of the cap, the watched coins' at 80 %, the held coins'
(and the other coin lines: creates, RugCheck, decisions, fills) at 100 %; the lines that say what the tape
covers (start, hb, follow, end, gap, stop) have room up to 110 %. A coin's first missing reading leaves a "gap"
line, so a replay knows its curve is unknown from then on (until its next reading), never "flat"; a line lost
without such a mark (the queue, or a coin line at 100 %) is counted in hb "lost", and a replay treats the whole
stretch between two heartbeats where "lost" grew as a gap for every coin. Said once an hour in the log.

THE TAPE FORMAT (version 2; the backtester's contract; version 1 had no "gap" line and no hb "lost" / "capped"). One JSON object per line, "k" its kind, "t" epoch
seconds (UTC, to the microsecond: a replay's ages and windows come out as the live run's), "m" the coin's mint
on every coin line:
  {"k":"create","m","t","symbol","name","creator","initialBuy","solAmount","bondingCurveKey",
   "vSolInBondingCurve","vTokensInBondingCurve","marketCapSol","is_mayhem_mode","pool","_rx"}
      PumpPortal's own fields (creator = traderPublicKey); t is "_rx", when the feed received it.
  {"k":"c","m","t","s","vs","vt","rs"[,"x":1]}
      a curve reading as feeds.py decoded it: s the slot, vs / rs the virtual / real SOL in LAMPORTS, vt
      the virtual tokens in BASE UNITS (1e6 per token), x 1 when the curve is complete (left out when
      not). t is when the poll answered. Change-only (the first reading of a watch, then each change):
      between two readings the curve did not move, as long as the coin was watched (see "follow" and
      "end") and the poller was answering (see "hb").
  {"k":"meta","m","t","ok","x","tg","web","xk","src","ms"}   the coin's X / Telegram / website ("" when
      none), xk the X link's kind ("profile", "status", ...); ok false: the uri could not be read; src
      the host that answered, or "rugcheck" when RugCheck's copy of the metadata gave the links.
  {"k":"rug","m","t","ok","why","ms","holders","top1","top10","top","risks","insiders","rugged","score"}
      a RugCheck report fetched: why "prefetch" | "fire" | "meta" (what asked), ms from the ask to the
      loop taking it; holders = totalHolders; top = the top holders' % (desc, at most 10) with the curve
      and pool accounts left out, top1 / top10 from them; risks [[name, level], ...]; insiders =
      graphInsidersDetected; score = score_normalised. ok means a report CAME, not that it passed; ok
      false: no report (only t, why, ms then). The verdict is not on this line: the bot's own (by the
      run's thresholds) shows as a "dec" or a "buy".
  {"k":"fire","m","t","tt","why","sol_in","buys","sells","rise","mcap","age","price"}
      the momentum trigger fired (once per trigger): tt when its rules started to hold, why the
      trigger's text, the coin's state then (SOL, %, SOL, s, SOL per token).
  {"k":"dec","m","t","d","why"}   a decision: d "skip" (not watched at its create: mayhem mode, no
      symbol, not a pump.fun curve, list full), "reject" (cheap check, RugCheck, sell simulation, ...),
      "learner" (the LEARNER's skip), "risk" (fired, RISK said no entry now; once per reason in a row).
      skip / reject / learner come from the coin's status, seen at the end of that loop turn (t).
  {"k":"buy","m","t","usd","raw","tok","px","px_sol","sol","impact","fee_sol","tier","score","size_pct",
   "check","tt","why","learn"}   the paper entry: usd paid (fees in), raw / tok the tokens got (after the
      latency haircut), px dollars per token, px_sol the curve's price at the fill, sol the SOL/USD used,
      impact Jupiter's price impact %, fee_sol network + priority fee, tier / score the NEWS hold plan,
      check the curve maths against the quotes ({"buy_pct","sell_pct"}), tt the trigger's time.
  {"k":"sell","m","t","frac","raw","usd","px","px_sol","sol","why","fill","final","pnl","pnl_pct","lat_ms"}
      a paper sale: frac of what was left, usd it brought (fees out), px the price that decided it, fill
      "quote" (Jupiter) or "curve" (the curve maths fallback); final true on the last sale, with the
      trade's pnl / pnl_pct; lat_ms from the reading that decided it to the fill (null when none did).
  {"k":"follow","m","t","until","why"}   the bot's watch ended, the recorder keeps the curve polled until
      "until"; why "traded" | "fired" | "near".
  {"k":"end","m","t","why"}   the curve is no longer polled: why "watch" (the bot's np_watch_s, not
      followed), "follow" (the follow time is over), "cap" (made room), "gone" (the account closed),
      "complete" (migrated), "stop" (the run ended), "off" (the recorder stopped after an error).
  {"k":"gap","m","t"}   the hour's tape was full (rec_max_mb_hour, SHARE): this coin's curve readings from t on are
      NOT on the tape until its next "c" line (it may have moved: a replay treats it as not covered, never flat).
  {"k":"gone","m","t"}  the curve account read as null 3 times;  {"k":"migrate","m","t","pool"}
Lines without "m": {"k":"start","t","v":2,"cfg":{...}[,"bal","held","mem"]} (a run started: the settings it traded
with, the book's cash, the positions it carried over, the LEARNER's memory then),
{"k":"sol","t","usd"} the SOL/USD the bot prices with, each time it changes (the replay prices with the same),
{"k":"hb","t","sol","poll_ok","poll_calls","poll_ms","watched","held","coins","follow","drop","lost","capped"}
every rec_hb_s: sol the SOL/USD the bot prices with, poll_ok / poll_calls the curve poller's good / all calls so
far (poll_ok standing still is a gap in the data), poll_ms its last call, watched the curves polled, held the
coins held, coins the watch list's, follow the followed ones; this run's lines dropped so far: drop all of them,
lost those without a mark (the writer's queue was full, or a coin line at the cap: "lost" growing between two
heartbeats is a gap for every coin), capped the curve readings left out behind a coin's "gap" line;
{"k":"stop","t"} (the run ended: the curves still polled got their "end" just before).

    tapes = load_tapes("memedata/tapes", since=None, until=None)    # {mint: tape}, by create time
    tape = {"mint", "t0", "create", "meta", "rug", "readings": [...], "events": [...], "end", "followed"}
        readings: {"t", "slot", "vsol", "vtok", "rsol", "complete", "price"} in SOL and WHOLE tokens
        (price = vsol / vtok, SOL per token; None for a zeroed curve), sorted by t; events: every other
        line as written, sorted by t; meta: the first readable one (else the last); rug: the last report
        that came (else the last failed ask); end: the t of the last "end" line, None while still watched
        (or the run died: the last "hb" then tells when the data stops).
    hb = load_system("memedata/tapes")    # the start / hb / stop lines, sorted by t
    report = rug_report(tape["rug"])        # a RugCheck-shaped report newpairs.rug_check_np() judges the same way

Standard library only.
"""

import collections
import datetime
import json
import math
import os
import re
import threading
import time

TAPE_VERSION = 2
TD = 6                              # the decimals of a time on the tape: microseconds, as the bot's clock (a datetime)

DEFAULTS = {
    "rec_on": True,                 # MEMEBOT_REC=0 in the environment turns it off too
    "rec_follow_s": 900,            # a coin near a trigger is polled until this long after its create ...
    "rec_follow_exit_s": 300,       # ... a bought one also until this long after its last sale
    "rec_max_follow": 60,           # coins followed after the bot's own watch at most ...
    "rec_max_keys": 100,            # ... and never more curves polled in all than one getMultipleAccounts call takes
    "rec_max_mb_hour": 20.0,        # an hour's tape takes no more curve readings past this
    "rec_keep_days": 7,             # tapes older than this are deleted when the recorder starts
    "rec_flush_s": 5.0,             # lines go to the writer thread this often (the files on disk lag this much)
    "rec_hb_s": 10.0,               # a heartbeat line: SOL/USD, the poller's health, the counts
    "rec_max_queue_mb": 16.0,       # lines waiting for a slow disk at most; more is dropped and counted
    "rec_close_wait_s": 3.0,        # at the end of a run the writer gets this long to finish
}

NAME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})\.jsonl$")
# an hour's tape at its cap (rec_max_mb_hour): what still goes on it, as shares of the cap. The followed coins'
# readings stop first, then the watched coins', the held coins' last, with the other coin lines (creates,
# RugCheck, decisions, fills); the lines that say what the tape covers have room past it (a few per coin and a
# heartbeat every 10 s: bounded)
SHARE = {"follow": 0.6, "watch": 0.8, "held": 1.0, "line": 1.0, "mark": 1.1}
MARKS = ("start", "hb", "follow", "end", "gap", "stop")
EPS = 1e-9
MB = 1e6
# the settings a backtest needs to know a run traded with ("start" line)
CFG_KEYS = ("curve_fee_pct", "latency_slip_pct", "network_fee_sol", "priority_fee_sol", "slippage_bps", "position_pct",
            "min_order_usd", "max_open", "stop_pct", "breakeven_pct", "time_stop_below_pct", "time_stop_late_x",
            "hold_tiers", "tp1_pct", "tp1_sell_frac", "trail_pct", "time_stop_min", "max_roundtrip_loss_pct",
            "news_refresh_sec", "fade_buy_ratio", "fade_window_s", "curve_poll_sec", "curve_idle_sec", "start_balance",
            # RISK and the job's end: a replay of this run needs them as much as the entry and exit rules
            "night_stop_pct", "dump_loss_pct", "loss_streak", "cooldown_min", "reenter_min", "max_trades_per_night", "no_entries_last_min",
            "flat_before_end_min", "job_no_entries_min", "job_flat_min", "watch_sec", "feed_wait_sec", "sol_price_sec",
            "quote_timeout_s", "strategies", "session_start", "session_end", "timezone")
CFG_PREFIX = ("np_", "rec_", "news_", "learn_")


# ---------------------------------------------------------------- small helpers (garbage in never raises)

def _num(x):
    """A finite number (ints stay ints), or None for None, bools, NaN, inf and anything else."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x if abs(x) < 2 ** 62 else None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def _f(x, default=0.0):
    v = _num(x)
    return default if v is None else v


def _r(x, nd=6):
    """A rounded float (or the int as it is), or None."""
    v = _num(x)
    if v is None or isinstance(v, int):
        return v
    return round(v, nd)


def _s(x, n):
    """Printable text, at most n characters ("" for anything else): names and links come from strangers."""
    if not isinstance(x, str):
        return ""
    return "".join(ch for ch in x if ch.isprintable()).strip()[:n]


def _yes(x):
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        return x.strip().lower() == "true"
    v = _num(x)
    return v is not None and v > 0


def _clean(x, depth=0):
    """JSON-safe copy of plain data: finite numbers, printable short strings, lists and dicts of them."""
    if depth > 4:
        return None
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, (int, float)):
        return _r(x, 8)
    if isinstance(x, str):
        return _s(x, 200)
    if isinstance(x, (list, tuple)):
        return [_clean(v, depth + 1) for v in list(x)[:40]]
    if isinstance(x, dict):
        return {str(k)[:40]: _clean(v, depth + 1) for k, v in list(x.items())[:60]}
    return None


def _plain(x, most):
    """A JSON copy of the bot's own data (the LEARNER's memory), or None when it is not JSON or over `most` bytes."""
    if x is None:
        return None
    try:
        s = json.dumps(x, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None
    return json.loads(s) if len(s) <= most else None


def hour_key(t):
    """The tape file's hour for an epoch time: "2026-11-26T09"."""
    return time.strftime("%Y-%m-%dT%H", time.gmtime(int(t // 3600) * 3600))


def hour_of(name):
    """A tape file name -> the epoch of its hour, or None when it is not one."""
    mo = NAME.match(name)
    if not mo:
        return None
    try:
        d = datetime.datetime(*[int(g) for g in mo.groups()])
    except ValueError:
        return None
    return (d - datetime.datetime(1970, 1, 1)).total_seconds()


def tape_files(folder, deep=False):
    """[(hour epoch, path)] of the tapes in folder, oldest first ([] when there is no folder). deep: in its
    subfolders too (a folder of downloaded artifacts, one per job: an hour two jobs shared is in both)."""
    out = []
    if deep:
        for root, dirs, names in os.walk(folder):
            dirs.sort()
            for name in names:
                h = hour_of(name)
                if h is not None:
                    out.append((h, os.path.join(root, name)))
        out.sort()
        return out
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for name in names:
        h = hour_of(name)
        if h is not None:
            out.append((h, os.path.join(folder, name)))
    out.sort()
    return out


def append(path, data):
    """The writer thread's append (the tests swap in a slow one)."""
    with open(path, "ab") as fh:
        fh.write(data)


def rug_summary(rep, curve=None, skip=()):
    """A RugCheck report -> the tape's summary (None when it is no report): the holders, the top holders'
    % with the curve and pool accounts (skip) left out, the risks' names and levels, insiders, rugged."""
    if not isinstance(rep, dict) or not rep.get("mint"):
        return None
    skip = set(s for s in (skip or ()) if isinstance(s, str))
    if isinstance(curve, str) and curve:
        skip.add(curve)
    pcts = []
    th = rep.get("topHolders")
    for h in th if isinstance(th, list) else []:
        if not isinstance(h, dict) or h.get("address") in skip or h.get("owner") in skip:
            continue
        p = _num(h.get("pct"))
        if p is not None and p > 0:
            pcts.append(min(float(p), 100.0))
    pcts.sort(reverse=True)
    risks = []
    rs = rep.get("risks")
    for r in rs if isinstance(rs, list) else []:
        if isinstance(r, dict):
            risks.append([_s(r.get("name"), 80) or "unnamed", _s(r.get("level"), 16)])
    return {"holders": int(max(0.0, min(1e9, _f(rep.get("totalHolders"))))), "top1": round(pcts[0], 2) if pcts else 0.0,
            "top10": round(sum(pcts[:10]), 2), "top": [round(p, 2) for p in pcts[:10]], "risks": risks[:12],
            "insiders": int(max(0.0, min(1e6, _f(rep.get("graphInsidersDetected"))))),
            "rugged": _yes(rep.get("rugged")), "score": _r(rep.get("score_normalised"), 2)}


def rug_report(line, mint=None):
    """A "rug" tape line -> a dict shaped like RugCheck's report that newpairs.rug_check_np() judges as it judged
    the real one (the curve and pool accounts are out of "top" already), or None for a failed ask. For replays."""
    if not isinstance(line, dict) or not line.get("ok"):
        return None
    risks = [r for r in line.get("risks") or [] if isinstance(r, (list, tuple)) and len(r) == 2]
    top = [p for p in line.get("top") or [] if _num(p) is not None]
    return {"mint": mint or line.get("m"), "rugged": bool(line.get("rugged")), "totalHolders": _f(line.get("holders")),
            "graphInsidersDetected": _f(line.get("insiders")), "score_normalised": line.get("score"),
            "risks": [{"name": r[0], "level": r[1]} for r in risks], "markets": [], "knownAccounts": {},
            "topHolders": [{"address": "holder-%d" % i, "owner": "owner-%d" % i, "pct": p} for i, p in enumerate(top)]}


class _Coin(object):
    """What the recorder knows of one mint."""

    __slots__ = ("mint", "t0", "key", "coin", "near", "fired", "traded", "exit_t", "gone", "complete", "status",
                 "dec", "following", "ended", "last", "gap")

    def __init__(self, mint, t0, key=""):
        self.mint, self.t0, self.key = mint, t0, key
        self.coin = None                # the watch list's Coin while it has one (its numbers, its status)
        self.near = self.fired = self.traded = False
        self.exit_t = None
        self.gone = self.complete = False
        self.status = "watching"        # the last status put on the tape
        self.dec = None                 # the last (d, why) put on the tape: a decision once per reason
        self.following = False
        self.ended = False
        self.last = t0
        self.gap = False                # its readings are being left out (the hour's tape is full): "gap" written

    def prio(self):
        return 3 if self.traded else 2 if self.fired else 1 if self.near else 0

    def until(self, cfg):
        u = self.t0 + _f(cfg.get("rec_follow_s"), 900)
        if self.traded and self.exit_t is not None:
            u = max(u, self.exit_t + _f(cfg.get("rec_follow_exit_s"), 300))
        return u


class Recorder(object):
    """The tape recorder of one run. Every method but the writer thread's runs on the bot's loop thread.

    rec = Recorder(folder, cfg, wl=watchlist, clock=epoch_fn, log=fn(text), stats=fn() -> dict,
                   pool_accounts=fn(report, coin) -> accounts)
    rec.event(ev)                       # every feed event (create, curve, meta, migrate, curve_gone)
    rec.rug(mint, report, why, asked)   # a RugCheck call that finished (report None: it failed)
    rec.fire(coin); rec.decide(mint, d, why); rec.enter(mint, pos, coin, sol_usd)
    rec.sell(mint, pos, frac, why, usd, raw, sol_usd, lat_s, note)
    keep = rec.keep(mint)               # the bot lets a coin go: True when the recorder keeps it polled
    for m in rec.tick(now): unwatch(m)  # after each batch of events and each 1 s step
    rec.close()                         # the run ends: everything still polled gets its "end", then the disk
    """

    def __init__(self, folder, cfg=None, wl=None, clock=None, log=None, stats=None, pool_accounts=None, write=None,
                 start=None):
        self.dir = folder
        self.cfg = dict(DEFAULTS)
        self.cfg.update(cfg if isinstance(cfg, dict) else {})
        self.wl = wl
        self.clock = clock or time.time
        self.say = log
        self.stats = stats
        self.pool_accounts = pool_accounts
        self.write = write or append
        self.coins = {}                 # mint -> _Coin
        self.follow = {}                # mint -> True: kept polled after the bot let it go
        self.release = []               # mints to unwatch (the next tick hands them to the bot)
        # the loop thread's buffer, by hour file
        self.pending = {}
        self.pending_bytes = 0
        self.handed = {}                # hour -> bytes handed to the writer by this run
        self.base = {}                  # hour -> bytes the file had before (the writer thread sets it)
        self.full = set()               # the hours whose cap was said in the log
        self.hour = None                # the hour (of the clock) the counts below are for
        self.counts = collections.Counter()
        self.total = collections.Counter()
        self._hid, self._hkey = None, None
        now = self._now()
        self.flushed_at = now
        self.hb_at = float("-inf")
        self.pruned_at = now
        self.said = set()
        # the writer thread
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.q = collections.deque()
        self.q_bytes = 0
        self.thread = None
        self.writing = False            # the writer is in a write (drain() waits for it)
        self.stopping = False
        self.cleaned = False
        self.cutoff = now - _f(self.cfg.get("rec_keep_days"), 7) * 86400.0
        self.werr = None                # the writer's last error (said once from the loop thread)
        self.werrs = 0
        self.written = 0
        self.closed = False
        self.sol_last = None            # the last SOL/USD put on the tape
        # every setting by itself (_clean caps a dict at 60 keys: the whole dict at once lost hold_tiers and more)
        cfg_doc = {str(k)[:40]: _clean(v) for k, v in self.cfg.items() if k in CFG_KEYS or k.startswith(CFG_PREFIX)}
        doc = {"k": "start", "t": round(now, TD), "v": TAPE_VERSION, "cfg": cfg_doc}
        st = start if isinstance(start, dict) else {}
        if _num(st.get("bal")) is not None:
            doc["bal"] = _r(st["bal"], 6)               # the book's cash at the start: a replay of this run sizes alike
        if isinstance(st.get("held"), int) and not isinstance(st.get("held"), bool):
            doc["held"] = st["held"]                    # positions carried over from the last job (adopted)
        mem = _plain(st.get("mem"), 65536)
        if mem is not None:
            doc["mem"] = mem                            # the LEARNER's memory.json at the start (a few KB)
        self._line(doc)

    # ---- time and the log
    def _now(self):
        try:
            v = _num(self.clock())
        except Exception:                               # noqa: BLE001 - a broken clock: the machine's
            v = None
        return v if v is not None else time.time()

    def _t(self, x, now):
        """An event's own time when it is a sane one (within a day of now), else now."""
        v = _num(x)
        return round(float(v), TD) if v is not None and abs(v - now) < 86400 else round(now, TD)

    def _log(self, text):
        if self.say is not None:
            try:
                self.say(text)
            except Exception:                           # noqa: BLE001 - the log is the bot's
                pass

    def _once(self, key, text):
        if key not in self.said:
            self.said.add(key)
            self._log(text)

    # ---- the buffer
    def _line(self, obj, share=None, reading=False):
        """One line into the loop thread's buffer, under its share of its hour's size cap (SHARE; by its kind when
        share is None). -> True when it went in. A line that does not fit is counted in "drop" and, unless it is a
        curve reading (the caller marks those with a "gap" line), in "lost"."""
        t = obj["t"]
        hid = int(t // 3600)
        if hid != self._hid:
            self._hid, self._hkey = hid, hour_key(t)
        key = self._hkey
        try:
            s = json.dumps(obj, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            self.total["bad"] += 1
            return False
        size = len(s) + 1
        cap = max(0.0, _f(self.cfg.get("rec_max_mb_hour"), 20.0)) * MB
        used = self.handed.get(key, 0) + self.base.get(key, 0) + size
        if share is None:
            share = SHARE["mark"] if obj.get("k") in MARKS else SHARE["line"]
        if used > cap * share:
            self.total["drop"] += 1
            if not reading:
                self.total["lost"] += 1
            if key not in self.full:
                self.full.add(key)
                self._log("tape %s is filling its %g MB: curve readings left out this hour, the followed coins' "
                          "first, the held coins' last; each coin's first missing reading leaves a \"gap\" line (a "
                          "replay counts that coin's curve as unknown, never flat)" % (
                              key, _f(self.cfg.get("rec_max_mb_hour"), 20.0)))
            return False
        self.pending.setdefault(key, []).append(s)
        self.handed[key] = self.handed.get(key, 0) + size
        self.pending_bytes += size
        self.counts["lines"] += 1
        self.total["lines"] += 1
        self.counts["bytes"] += size
        if reading:
            self.counts["readings"] += 1
        if self.pending_bytes >= MB:                    # a burst: no need to sit on a megabyte
            self._handoff()
        return True

    def _coin_line(self, r, k, now, **kw):
        r.last = now
        obj = {"k": k, "m": r.mint, "t": round(now, TD)}
        obj.update(kw)
        if k != "c":
            return self._line(obj)
        if self._line(obj, SHARE[self._rank(r)], reading=True):
            r.gap = False                               # on the tape again: covered from this reading on
            return True
        self.total["capped"] += 1
        if not r.gap:                                   # the first one left out: the replay must know
            r.gap = True
            self._line({"k": "gap", "m": r.mint, "t": round(now, TD)})
        return False

    def _rank(self, r):
        """Whose reading it is, for the cap (SHARE): a coin the bot holds, one it watches, or one only followed."""
        c = self.wl.get(r.mint) if self.wl is not None else None
        if c is not None:
            return "held" if getattr(c, "status", None) == "entered" else "watch"
        return "follow" if r.following else "watch"

    def _get(self, mint):
        """The mint's record; a coin the watch list has but the recorder never saw created (a held coin a
        restart adopted) is taken on here."""
        r = self.coins.get(mint)
        if r is None and self.wl is not None and isinstance(mint, str):
            c = self.wl.get(mint)
            if c is not None:
                r = self._adopt(c)
        return r

    def _adopt(self, c):
        r = _Coin(c.mint, _f(getattr(c, "t0", None), self._now()), _s(getattr(c, "curve", ""), 64))
        r.coin = c
        if getattr(c, "status", "") == "entered":
            r.traded = True
            r.status = "entered"
        self.coins[c.mint] = r
        return r

    # ---- the hooks
    def event(self, ev):
        """A feed event, as the bot took it off the queue."""
        if not isinstance(ev, dict):
            return
        kind = ev.get("kind") or ev.get("txType")
        mint = ev.get("mint")
        if not isinstance(mint, str) or not mint or len(mint) > 64:
            return
        now = self._now()
        if kind == "curve":
            r = self._get(mint)
            if r is None or r.ended:                    # a reading still in the queue after its watch ended
                return
            t = self._t(ev.get("t"), now)
            kw = {"s": _num(ev.get("slot")), "vs": _num(ev.get("vsol")), "vt": _num(ev.get("vtok")),
                  "rs": _num(ev.get("rsol"))}
            if _yes(ev.get("complete")):
                kw["x"] = 1
                r.complete = True
            self._coin_line(r, "c", t, **kw)
        elif kind == "create":
            if mint in self.coins:                      # PumpPortal sends a create twice now and then
                return
            t = self._t(ev.get("_rx"), now)
            r = self.coins[mint] = _Coin(mint, t, _s(ev.get("bondingCurveKey"), 64))
            self.counts["coins"] += 1
            self._coin_line(r, "create", t, symbol=_s(ev.get("symbol"), 32), name=_s(ev.get("name"), 64),
                            creator=_s(ev.get("traderPublicKey"), 64), initialBuy=_r(ev.get("initialBuy")),
                            solAmount=_r(ev.get("solAmount"), 9), bondingCurveKey=r.key,
                            vSolInBondingCurve=_r(ev.get("vSolInBondingCurve"), 9),
                            vTokensInBondingCurve=_r(ev.get("vTokensInBondingCurve")),
                            marketCapSol=_r(ev.get("marketCapSol")), is_mayhem_mode=_yes(ev.get("is_mayhem_mode")),
                            pool=_s(ev.get("pool"), 20), _rx=_r(ev.get("_rx"), TD))
        elif kind == "meta":
            r = self._get(mint)
            if r is None:
                return
            self._coin_line(r, "meta", self._t(ev.get("t"), now), ok=bool(ev.get("ok", True)),
                            x=_s(ev.get("twitter") or ev.get("x"), 200), tg=_s(ev.get("telegram") or ev.get("tg"), 200),
                            web=_s(ev.get("website") or ev.get("web"), 200), xk=_s(ev.get("x_kind"), 20),
                            src=_s(ev.get("source"), 60), ms=_r(ev.get("ms"), 0))
        elif kind == "curve_gone":
            r = self.coins.get(mint)
            if r is not None:
                r.gone = True
                self._coin_line(r, "gone", self._t(ev.get("t"), now))
        elif kind == "migrate":
            r = self.coins.get(mint)
            if r is not None:
                r.complete = True
                self._coin_line(r, "migrate", self._t(ev.get("_rx"), now), pool=_s(ev.get("pool"), 20))

    def rug(self, mint, report, why=None, asked=None):
        """A RugCheck call that finished (report None: it failed). asked: when it was asked (epoch s)."""
        r = self._get(mint)
        if r is None:
            return
        now = self._now()
        a = _num(asked)
        ms = int(max(0.0, now - a) * 1000) if a is not None and abs(now - a) < 3600 else None
        skip = ()
        if self.pool_accounts is not None and r.coin is not None and isinstance(report, dict):
            try:
                skip = self.pool_accounts(report, r.coin)
            except Exception:                           # noqa: BLE001 - the curve key alone then
                skip = ()
        doc = rug_summary(report, r.key, skip)
        if doc is None:
            self._coin_line(r, "rug", now, ok=False, why=_s(why, 20), ms=ms)
        else:
            self._coin_line(r, "rug", now, ok=True, why=_s(why, 20), ms=ms, **doc)

    def fire(self, coin):
        """The momentum trigger fired (the bot calls it once per trigger)."""
        r = self._get(getattr(coin, "mint", None))
        if r is None:
            return
        r.coin = coin
        r.fired = True
        g = lambda k: getattr(coin, k, None)            # noqa: E731
        self._coin_line(r, "fire", self._now(), tt=_r(g("trigger_t"), TD), why=_s(g("why"), 200), sol_in=_r(g("sol_in"), 4),
                        buys=_num(g("buys")), sells=_num(g("sells")), rise=_r(g("rise_pct"), 2),
                        mcap=_r(g("mcap_sol"), 2), age=_r(g("age_s"), 2), price=_r(g("price"), 15))

    def decide(self, mint, d, why):
        """A decision about a coin ("skip", "reject", "learner", "risk"), once per (d, why) in a row."""
        r = self._get(mint)
        if r is None:
            return
        why = _s(why, 200)
        if r.dec == (d, why):
            return
        r.dec = (d, why)
        self._coin_line(r, "dec", self._now(), d=_s(d, 20), why=why)

    def enter(self, mint, pos, coin=None, sol_usd=None):
        """The paper entry (pos: the bot's new position)."""
        r = self._get(mint)
        if r is None or not isinstance(pos, dict):
            return
        r.traded = True
        r.status = "entered"
        if coin is not None:
            r.coin = coin
        plan = pos.get("plan") if isinstance(pos.get("plan"), dict) else {}
        raw = _num(pos.get("raw"))
        self._coin_line(r, "buy", self._now(), usd=_r(pos.get("cost"), 6), raw=raw, tok=_r(pos.get("tokens_ui"), 6),
                        px=_r(pos.get("entry_price"), 15), px_sol=_r(getattr(coin, "price", None), 15),
                        sol=_r(sol_usd, 4), impact=_r(pos.get("impact_pct"), 3), fee_sol=_r(pos.get("fee_sol"), 9),
                        tier=_s(plan.get("entry_tier") or plan.get("tier"), 20),
                        score=_num(plan.get("entry_score", plan.get("score"))), size_pct=_r(pos.get("size_pct"), 2),
                        check=_clean(pos.get("curve_check")), tt=_r(getattr(coin, "trigger_t", None), TD),
                        why=_s(pos.get("reason"), 200), learn=_clean(pos.get("learn")))

    def sell(self, mint, pos, frac, why, usd, raw=None, sol_usd=None, lat=None, note=""):
        """A paper sale that went through (pos: the position, its sold_usd already counting usd)."""
        r = self._get(mint)
        if r is None or not isinstance(pos, dict):
            return
        now = self._now()
        final = _f(frac) >= 0.999
        kw = {}
        if final:
            r.exit_t = now
            cost = _f(pos.get("cost"))
            pnl = _f(pos.get("sold_usd")) - cost
            kw = {"pnl": round(pnl, 4), "pnl_pct": round(pnl / cost * 100, 2) if cost > 0 else None}
        lat_v = _num(lat)
        self._coin_line(r, "sell", now, frac=_r(frac, 4), raw=_num(raw), usd=_r(usd, 6), px=_r(pos.get("last"), 15),
                        px_sol=_r(getattr(r.coin, "price", None), 15), sol=_r(sol_usd, 4), why=_s(why, 200),
                        fill="curve" if "curve maths" in str(note or "") else "quote", final=final,
                        lat_ms=int(lat_v * 1000) if lat_v is not None else None, **kw)

    def sol(self, usd):
        """The SOL/USD the bot prices with: a "sol" line each time it changes (every sol_price_sec at most)."""
        v = _num(usd)
        if v is None or v <= 0 or v == self.sol_last:
            return
        self.sol_last = v
        self._line({"k": "sol", "t": round(self._now(), TD), "usd": _r(v, 6)})

    # ---- following
    def _near(self, c):
        """Half of np_min_sol and half of np_min_buys within np_max_age_s of the launch."""
        try:
            cfg = self.cfg
            return (_f(c.sol_in) >= _f(cfg.get("np_min_sol"), 3.0) / 2 - EPS
                    and _f(c.buys) >= _f(cfg.get("np_min_buys"), 8) / 2 - EPS
                    and _f(c.age_s) <= _f(cfg.get("np_max_age_s"), 180) + EPS)
        except Exception:                               # noqa: BLE001 - an odd coin is not near
            return False

    def _room(self):
        n = len(self.wl.coins) if self.wl is not None else 0
        return max(0, min(int(_f(self.cfg.get("rec_max_follow"), 60)), int(_f(self.cfg.get("rec_max_keys"), 100)) - n))

    def _end(self, r, why, now=None):
        if r.ended:
            return
        r.ended = True
        self._coin_line(r, "end", self._now() if now is None else now, why=why)

    def _why_not(self, r, now):
        if r.gone:
            return "gone"
        if r.complete:
            return "complete"
        if r.prio() == 0:
            return "watch"
        if now >= r.until(self.cfg):
            return "follow" if r.following else "watch"
        return None

    def keep(self, mint):
        """The bot lets a coin's curve go (its watch window ended, it was evicted, or dropped). -> True when
        the recorder keeps it polled (the bot then leaves it on the poller), False: unwatch it."""
        r = self.coins.get(mint)
        if r is None:
            return False
        now = self._now()
        if r.coin is not None and not r.near and self._near(r.coin):
            r.near = True
        if r.following:
            return True
        why = self._why_not(r, now)
        if why is not None:
            self._end(r, why, now)
            return False
        if len(self.follow) >= self._room():
            victim = self._victim()
            if victim is None or (victim.prio(), victim.t0) >= (r.prio(), r.t0):
                self._end(r, "cap", now)
                return False
            self._unfollow(victim, "cap", now)
        self.follow[mint] = True
        r.following = True
        self._coin_line(r, "follow", now, until=round(r.until(self.cfg), TD),
                        why="traded" if r.traded else "fired" if r.fired else "near")
        return True

    def _victim(self):
        """The followed coin to drop first: the lowest priority, then the oldest."""
        if not self.follow:
            return None
        return min((self.coins[m] for m in self.follow), key=lambda x: (x.prio(), x.t0))

    def _unfollow(self, r, why, now):
        self.follow.pop(r.mint, None)
        r.following = False
        self._end(r, why, now)
        self.release.append(r.mint)

    # ---- the step
    def _sync(self, now):
        """The watch list's coins: near a trigger yet, and the decisions that show as a status (rejected, and
        the coins skipped at their create)."""
        wl = self.wl
        if wl is None:
            return
        for c in list(wl.coins.values()):
            r = self.coins.get(c.mint) or self._adopt(c)
            r.coin = c
            if not r.near and self._near(c):
                r.near = True
            st = c.status
            if st != r.status:
                r.status = st
                if isinstance(st, str) and st.startswith("rejected"):
                    why = _s(c.why, 200)
                    self.decide(c.mint, "learner" if why.startswith("learner") else "reject", why)
        for c in list(wl.skipped):                     # at most 20, the last np_watch_s
            r = self.coins.get(c.mint)
            if r is not None and r.status != c.status and r.coin is None:
                r.status = c.status
                self.decide(c.mint, "skip", c.why)
                self._end(r, "watch", now)              # never polled

    def tick(self, now=None):
        """After each batch of events and each 1 s step: decisions, the follow list, the heartbeat, the
        hand-off to the writer. -> mints the bot should unwatch (the recorder let them go)."""
        now = _num(now)
        now = self._now() if now is None else float(now)
        self._once("on", "tape on · every create, curve reading, RugCheck summary, decision and fill to tapes/<UTC hour>"
                         ".jsonl for the backtester · coins near a trigger followed %gs after launch (at most %d)" % (
                             _f(self.cfg.get("rec_follow_s"), 900), int(_f(self.cfg.get("rec_max_follow"), 60))))
        self._sync(now)
        room = self._room()
        for m in list(self.follow):
            r = self.coins[m]
            why = self._why_not(r, now)
            if why is not None:
                self._unfollow(r, why, now)
        while len(self.follow) > room:
            self._unfollow(self._victim(), "cap", now)
        self._hour(now)
        if now - self.hb_at >= _f(self.cfg.get("rec_hb_s"), 10.0):
            self.hb_at = now
            self._hb(now)
        if now - self.pruned_at >= 60:
            self.pruned_at = now
            self._prune(now)
        if self.werr is not None:
            self._once("werr", "tape writer: %s · the trading goes on (lines may be missing)" % self.werr)
        if self.pending and now - self.flushed_at >= _f(self.cfg.get("rec_flush_s"), 5.0):
            self._handoff()
        out, self.release = self.release, []
        return out

    def _hb(self, now):
        doc = {"k": "hb", "t": round(now, TD)}
        if self.stats is not None:
            try:
                got = self.stats()
                if isinstance(got, dict):
                    doc.update({k: _clean(v) for k, v in got.items() if isinstance(k, str) and k not in ("k", "t", "m")})
            except Exception:                           # noqa: BLE001 - no numbers from the bot this time
                pass
        doc.update(coins=len(self.wl.coins) if self.wl is not None else None, follow=len(self.follow),
                   drop=self.total["drop"], lost=self.total["lost"], capped=self.total["capped"])
        self._line(doc)

    def _hour(self, now):
        """A line in the log for each hour's tape when the hour is over."""
        key = hour_key(now)
        if self.hour is None:
            self.hour = key
        elif key != self.hour:
            c = self.counts
            self._log("tape %s · %d lines (%d curve readings) · %.1f MB · %d coins" % (
                self.hour, c["lines"], c["readings"], c["bytes"] / MB, c["coins"]))
            self.hour = key
            self.counts = collections.Counter()
            old = hour_key(now - 3 * 3600)
            for k in [k for k in self.handed if k < old]:
                self.handed.pop(k, None)
                self.base.pop(k, None)
            self.full = set(k for k in self.full if k >= old)

    def _prune(self, now):
        """Coins the recorder is done with (not watched, nothing for 30 min) are forgotten."""
        wl = self.wl
        for m in [m for m, r in self.coins.items() if not r.following and now - r.last > 1800
                  and (wl is None or wl.get(m) is None)]:
            del self.coins[m]

    # ---- the writer
    def _handoff(self):
        """The buffer to the writer thread (it never waits for the disk)."""
        now = self._now()
        self.flushed_at = now
        if not self.pending:
            return
        batches = [(k, ("\n".join(v) + "\n").encode("utf-8")) for k, v in self.pending.items()]
        self.pending, self.pending_bytes = {}, 0
        most = _f(self.cfg.get("rec_max_queue_mb"), 16.0) * MB
        with self.lock:
            for k, data in batches:
                if self.q_bytes + len(data) > most:     # the disk is far behind: dropped, counted, said once
                    n = data.count(b"\n")
                    self.total["drop"] += n
                    self.total["lost"] += n                 # no mark for these: a gap for every coin (hb "lost")
                    self.total["qdrop"] += 1
                    continue
                self.q.append((k, data))
                self.q_bytes += len(data)
            if self.thread is None and self.q:
                self.thread = threading.Thread(target=self._run, name="tape-writer", daemon=True)
                self.thread.start()
        if self.total["qdrop"]:
            self._once("qdrop", "tape: the disk is behind, lines dropped (%d MB waiting at most) · the trading "
                                "goes on" % int(most / MB))
        self.wake.set()

    def _run(self):
        """The writer thread: every file operation of the recorder. It ends when its queue is empty and the
        recorder is closing, or after a minute without work (the next hand-off starts another)."""
        idle = time.monotonic()
        while True:
            self.wake.wait(1.0)
            self.wake.clear()
            while True:
                with self.lock:
                    if not self.q:
                        break
                    key, data = self.q.popleft()
                    self.q_bytes -= len(data)
                    self.writing = True
                self._write(key, data)
                self.writing = False
                idle = time.monotonic()
            with self.lock:
                if not self.q and (self.stopping or time.monotonic() - idle > 60):
                    self.thread = None
                    return

    def _write(self, key, data):
        try:
            if not self.cleaned:
                self.cleaned = True
                os.makedirs(self.dir, exist_ok=True)
                for h, path in tape_files(self.dir):    # by the name's hour: a git checkout resets the mtimes
                    if h + 3600 <= self.cutoff:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
            path = os.path.join(self.dir, key + ".jsonl")
            if key not in self.base:
                size = 0
                try:
                    size = os.path.getsize(path)
                    if size:
                        with open(path, "rb") as fh:    # a line cut by a killed run: the next one starts clean
                            fh.seek(-1, os.SEEK_END)
                            if fh.read(1) != b"\n":
                                data = b"\n" + data
                except OSError:
                    size = 0
                self.base[key] = size
            self.write(path, data)
            self.written += len(data)
        except Exception as e:                          # noqa: BLE001 - a full disk is no reason to stop anything
            self.werrs += 1
            self.werr = ("%s: %s" % (type(e).__name__, e))[:200]

    def close(self, wait=None, why="stop"):
        """The run ends: every curve still polled gets its "end" (why), the buffer goes to the writer, which
        gets rec_close_wait_s (or wait) to finish. Called again later (an end-of-test sale), it writes what
        came since. -> True when everything handed over is on disk."""
        now = self._now()
        if not self.closed:
            self.closed = True
            for r in list(self.coins.values()):         # still polled (watched or followed): its data stops here
                self._end(r, why, now)
            self.follow = {}
            self._line({"k": "stop", "t": round(now, TD)})
        with self.lock:
            self.stopping = True
        self._handoff()
        self.wake.set()
        t = self.thread
        if t is not None:
            t.join(max(0.0, _f(self.cfg.get("rec_close_wait_s"), 3.0) if wait is None else _f(wait)))
        with self.lock:
            return not self.q and (self.thread is None or not self.thread.is_alive())

    def drain(self, wait=None):
        """Everything so far to disk, waiting at most rec_close_wait_s (or wait): for tests and tools, never the
        loop. -> True when it is all written."""
        self._handoff()
        end = time.monotonic() + (_f(self.cfg.get("rec_close_wait_s"), 3.0) if wait is None else _f(wait))
        while True:
            with self.lock:
                if not self.q and not self.writing:
                    return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.005)

    def info(self):
        """The counts, for the tests and the heartbeat."""
        return {"coins": len(self.coins), "follow": len(self.follow), "lines": self.total["lines"],
                "drop": self.total["drop"], "lost": self.total["lost"], "capped": self.total["capped"],
                "written": self.written, "queue": self.q_bytes, "write_errors": self.werrs,
                "error": self.werr}


# ---------------------------------------------------------------- the loader (the backtester's side)

def _epoch(x):
    """since / until: epoch seconds, a datetime (naive = UTC) or an ISO text; None stays None."""
    if x is None:
        return None
    if isinstance(x, datetime.datetime):
        if x.tzinfo is not None:
            x = x.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return (x - datetime.datetime(1970, 1, 1)).total_seconds()
    if isinstance(x, str):
        s = x.strip().rstrip("Z")
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H", "%Y-%m-%d"):
            try:
                return _epoch(datetime.datetime.strptime(s, fmt))
            except ValueError:
                continue
        return None
    return _num(x)


def read_lines(path):
    """The JSON objects of one tape file, in file order; a line that is not one (a truncated last line, a
    line merged with a cut one) is left out."""
    out = []
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return out
    for ln in raw.split(b"\n"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("k"), str) and _num(obj.get("t")) is not None:
            out.append(obj)
    return out


def _files(folder, since, until, tail_s):
    out = []
    for h, path in tape_files(folder, deep=True):
        if since is not None and h + 3600 <= since - 3600:
            continue
        if until is not None and h >= until + tail_s:
            continue
        out.append(path)
    return out


def reading(obj):
    """A "c" line -> {"t", "slot", "vsol", "vtok", "rsol", "complete", "price"} in SOL and whole tokens (units
    told apart by size like newpairs.read_curve: a curve has 30+ SOL virtual, 2.8e14+ token base units)."""
    vs, vt, rs = _num(obj.get("vs")), _num(obj.get("vt")), _num(obj.get("rs"))
    lamports = (vs is not None and vs > 1e4) or (vs is None and rs is not None and rs > 1e4)
    vsol = vs / 1e9 if vs is not None and lamports else vs
    rsol = rs / 1e9 if rs is not None and lamports else rs
    vtok = vt / 1e6 if vt is not None and vt > 1e12 else vt
    price = vsol / vtok if vsol is not None and vtok and vsol > 0 and vtok > 0 else None
    slot = _num(obj.get("s"))
    return {"t": float(obj["t"]), "slot": int(slot) if slot is not None else None,
            "vsol": float(vsol) if vsol is not None else None, "vtok": float(vtok) if vtok is not None else None,
            "rsol": float(rsol) if rsol is not None else None, "complete": bool(obj.get("x")), "price": price}


def load_tapes(folder, since=None, until=None, tail_s=7200):
    """{mint: tape} of the coins first seen in [since, until) (all when None), sorted by that time. A coin's
    lines after `until` are read from the files up to tail_s later (its follow-up readings and exits)."""
    since, until = _epoch(since), _epoch(until)
    tapes = {}
    for path in _files(folder, since, until, _f(tail_s, 7200)):
        for obj in read_lines(path):
            m = obj.get("m")
            if not isinstance(m, str) or not m:
                continue
            tp = tapes.get(m)
            if tp is None:
                tp = tapes[m] = {"mint": m, "t0": None, "create": None, "meta": None, "rug": None, "readings": [],
                                 "events": [], "end": None, "followed": False}
            k = obj["k"]
            if k == "c":
                tp["readings"].append(reading(obj))
            elif k == "create":
                if tp["create"] is None:
                    tp["create"] = obj
            else:
                tp["events"].append(obj)
    out = []
    for tp in tapes.values():
        tp["readings"].sort(key=lambda x: x["t"])
        tp["events"].sort(key=lambda x: float(x["t"]))
        firsts = [float(tp["create"]["t"])] if tp["create"] is not None else \
            [x["t"] for x in tp["readings"][:1]] + [float(x["t"]) for x in tp["events"][:1]]
        tp["t0"] = min(firsts) if firsts else None
        metas = [e for e in tp["events"] if e["k"] == "meta"]
        tp["meta"] = next((e for e in metas if e.get("ok")), metas[-1] if metas else None)
        rugs = [e for e in tp["events"] if e["k"] == "rug"]
        tp["rug"] = next((e for e in reversed(rugs) if e.get("ok")), rugs[-1] if rugs else None)
        last_end = last_follow = None
        for e in tp["events"]:
            if e["k"] == "end":
                last_end = float(e["t"])
            elif e["k"] == "follow":
                tp["followed"] = True
                last_follow = float(e["t"])
        tp["end"] = last_end if last_end is not None and (last_follow is None or last_end >= last_follow) else None
        if tp["t0"] is None or (since is not None and tp["t0"] < since) or (until is not None and tp["t0"] >= until):
            continue
        out.append(tp)
    out.sort(key=lambda x: x["t0"])
    return {tp["mint"]: tp for tp in out}


def load_system(folder, since=None, until=None):
    """The lines without a coin (start, hb, stop) in [since, until), sorted by t."""
    since, until = _epoch(since), _epoch(until)
    out = []
    for path in _files(folder, since, until, 3600):
        for obj in read_lines(path):
            if obj.get("m") is not None:
                continue
            t = float(obj["t"])
            if (since is None or t >= since) and (until is None or t < until):
                out.append(obj)
    out.sort(key=lambda x: float(x["t"]))
    return out
