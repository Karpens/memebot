#!/usr/bin/env python3
"""Meme Bot FIX BOOK: a one-off correction of a night the bot booked on curve readings no pump.fun curve can be in.

Why: the night of 2026-11-27 SNIPE mode filled coins by the curve maths on readings that were not a standard pump.fun
curve's (feeds.decode_curve() read any bytes as reserves, nothing judged them; newpairs.curve_sane() does now, and the
bot never prices, fills or exits on such a reading). 530A was bought for $6.38 as 80 % of the supply (virtual SOL
0.026, "-100 % since the create") and Jupiter's quote of the real market sold those tokens for $1416.50; Coke-Cola
(virtual SOL 3.5: "-88 %") booked +$233 the same way. The book went from $82.30 to $361.97, and every later order was
sized on that ($36 at the end, not $5-8). This tool repairs the paper book from the trade records alone:

  1. VOID: a closed New Pairs trade whose own record shows a state no standard curve can be in (void_reasons(), every
     rule from pump.fun's curve: newpairs.curve_sane()'s constants): its entry mcap under the launch mcap (27.96 SOL)
     or over the completion mcap (~411 SOL); its "% since the create" under what the creator's own buy allows (a
     curve is never under its launch price) or over what a curve can rise before it completes; a curve fill whose
     price impact is more than a 30+ SOL curve can give for that order even at SOL $20 (--sol-min); more tokens
     bought than the 793.1 M a curve ever sells; a single-sale trade whose proceeds are under 0.5x or over 1.25x what
     its tokens at its own exit price give (the sale's costs and a moment's move explain 0.87-1.12x on the 2026-11-27
     book). --void MINT (or MINT@OPENED, SYMBOL@OPENED) voids one trade by hand; a mark that names more than one trade
     of the night (a symbol: Thanksgiving was two coins that night) or none is refused with the candidates. Every void
     is printed with its reasons. A night with nothing to void is left as it is (no rescaling, nothing written).
  2. REPLAY: the night's other trades again, in time order (exits before entries in the same second, a trade's own
     entry before its exit), with the bot's own sizing and RISK as far as the records allow: a SNIPE sized as
     np_snipe_fire sizes (position_pct of the equity, at least min_order_usd, never more than the cash: nothing under
     a $50 book), a momentum trade at its own recorded share of the equity (size_pct: its LEARNER size in it), and
     bot.Night.why_no_entries() itself on the replayed book (max_open, max_trades_per_night, the night stop and its
     dump guard, the pause after loss_streak losses); each trade's cost from that size, its proceeds and P&L by its
     own pnl_pct. A trade the corrected book would not have taken is dropped, with why. APPROXIMATIONS (the records
     have no more): open positions are valued at their cost (the bot marks them at their last price; the night stop
     then sees them at that value less the sale's costs), the time windows (the session's and a job's last minutes,
     re-entry) are as the night had them (the replay only removes trades, it cannot move one), a trade the real book
     did not make (its 250 cap, a pause or a full book) cannot be added, and each trade keeps the % it made at its
     original size (fixed fees weigh more on a smaller order, the curve's impact less).
  3. WRITE: ONE corrected balance (the start plus the replayed trades' P&L when the night started and ended flat,
     the old balance less the start less the old P&L being only the old trades' cents; else the old balance less the
     old P&L plus the new), the night's trades (the voided and dropped ones moved to night["fix"], with the old
     figures kept on each rescaled trade as "fix_was"), its streak and pause, its equity curve (cash plus open
     positions at cost at each point), its nights summary when the night is closed (end = the corrected balance, as
     close_night() takes the equity at the close) and the next night's start and flat curve, a LOGGER line, the
     panel's balance / equity / trades / night / curve / nights / LEARNER as bot.panel() shows them (the current
     night, else the last one), and the LEARNER's memories CORRECTED, not rebuilt (relearn): memory_snipe.json (and
     memory.json for momentum trades) with each voided trade's lesson unlearned (the exact inverse of learner.update()
     while its labels never reached FORGET_AT, said when not), each dropped trade's re-weighted from a real trade's 1
     to shadow_weight (the RISK shadow the corrected book would have followed: shadow_on_risk), and every other lesson
     kept, the old shadow lessons with them (a shadow filled on an odd reading is not in state.json and stays); a
     memory that does not exist is made as the bot would make it (learner.backfill of the kept trades) plus the
     dropped ones at shadow_weight.
  4. ARCHIVE: every file it changes is copied first to DIR/archive/<name>-<night>-before-fix.json (never over an
     older archive: -2, -3, ...). Every body is written to a temp file first (a failure changes nothing), then the
     originals archived, then the files replaced, state.json last: night["fix"] marks a night fixed (not fixed
     again), so a run that stopped half way is simply run again.
  --dry-run prints the report and writes nothing.

    python3 memebot/fix_book.py --data DIR [--night ID] [--void MINT[@OPENED]] [--sol-min 20] [--dry-run]

It refuses (exit 2) while positions are open (they were sized on the book being corrected): run it on a flat book,
with the bot stopped, and before a later night trades on the book (a closed night is fixed only while the night after
it has no trade: run it before the bot rolls the night). The night: --night ID, else the one of the current and the last
night that has trades (both: the one with something to void, else it asks). Nothing to void or fixed already: exit 0.
Paper on paper: the corrected book is what these rules would have booked on these trades.
"""

import argparse
import copy
import datetime
import importlib.util
import json
import math
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bot = _load("memebot_fix_bot", os.path.join(HERE, "bot.py"))
newpairs = bot.newpairs
learner = bot.learner

# the standard curve (newpairs.py: what curve_sane() checks every reading against)
LAUNCH_MCAP = newpairs.LAUNCH_PRICE * newpairs.SUPPLY          # 27.96 SOL
END_MCAP = newpairs.END_PRICE * newpairs.SUPPLY                # ~410.9 SOL
REAL_TOKENS = newpairs.STD_RTOK0                               # 793.1 M tokens a curve ever sells
MCAP_ROUND = 0.15           # a record's mcap_sol is rounded to 0.1 SOL: 27.96 can read 28.0, never 27.8
MCAP_END_TOL = 0.01         # newpairs.SANE_END_TOL
RISE_TOL = 1.0              # points: the record's "% since the create" is rounded to a whole %
PROCEEDS_LO, PROCEEDS_HI = 0.5, 1.25    # proceeds / (tokens x exit price) a single sale can show (see the docstring)
SOL_MIN = 20.0              # SOL/USD at least this when the order's SOL is bounded (the impact rule)
RISE_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)% since (?:the create|launch)")


def num(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def epoch(iso):
    """A record's "2026-11-27T02:03:33Z" -> epoch seconds, or None."""
    try:
        t = datetime.datetime.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return (t - datetime.datetime(1970, 1, 1)).total_seconds()


def iso(t):
    return (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(t))).isoformat() + "Z"


def rise_of(trade):
    """The "% since the create" (a snipe) or "% since launch" (a momentum trigger) in the trade's reason, or None."""
    m = RISE_RE.search(str(trade.get("reason") or ""))
    return float(m.group(1)) if m else None


def rise_bounds(dev_pct):
    """(lowest, highest) % a standard curve's price can be over its create's own price, when the creator bought dev_pct
    % of the supply in the create: never under the launch price, never over the completion price."""
    dev = max(0.0, min(num(dev_pct) or 0.0, 70.0))
    vtok0 = newpairs.START_VTOK - dev / 100.0 * newpairs.SUPPLY
    return ((vtok0 / newpairs.START_VTOK) ** 2 - 1) * 100.0, ((vtok0 / newpairs.END_VTOK) ** 2 - 1) * 100.0


def single_sale(trade):
    """The trade went out in one sale (no ladder step sold before it): its proceeds are its tokens at one price."""
    plan = trade.get("plan") if isinstance(trade.get("plan"), dict) else {}
    if int(num(plan.get("step")) or 0) > 0:
        return False
    steps = int(num(trade.get("steps")) or 0)
    ladder = plan.get("ladder") if isinstance(plan.get("ladder"), list) else []
    first = ladder[0] if ladder and isinstance(ladder[0], (list, tuple)) and len(ladder[0]) > 1 else None
    return steps == 0 or (steps == 1 and first is not None and (num(first[1]) or 0) >= 0.999)


def void_reasons(trade, sol_min=SOL_MIN):
    """Why a closed trade's own record shows a state no standard pump.fun curve can be in: [] when none (or when it
    is no New Pairs trade: the migrated strategy's coins are not on a curve)."""
    if not isinstance(trade, dict) or trade.get("strategy") != "new_pairs":
        return []
    out = []
    mcap, cost = num(trade.get("mcap_sol")), num(trade.get("cost"))
    if mcap is not None and mcap < LAUNCH_MCAP - MCAP_ROUND:
        out.append("entry mcap %.1f SOL: under a standard curve's launch mcap %.2f SOL" % (mcap, LAUNCH_MCAP))
    if mcap is not None and mcap > END_MCAP * (1 + MCAP_END_TOL):
        out.append("entry mcap %.1f SOL: over a standard curve's completion mcap %.0f SOL" % (mcap, END_MCAP))
    rise = rise_of(trade)
    if rise is not None:
        lo, hi = rise_bounds(trade.get("dev_pct"))
        if rise < lo - RISE_TOL:
            out.append("%+.0f%% since the create: a curve is never under its launch price (%+.1f%% with the "
                       "creator's %.1f%% buy)" % (rise, lo, num(trade.get("dev_pct")) or 0.0))
        if rise > hi + RISE_TOL:
            out.append("%+.0f%% since the create: more than a curve rises before it completes (%+.0f%%)" % (rise, hi))
    slip = num(trade.get("slip"))
    if trade.get("entry_fill") == "curve" and slip is not None and cost is not None and cost > 0 and sol_min > 0:
        net = cost / sol_min * (1 - 0.0125)
        top = net / (newpairs.START_VSOL + net) * 100.0
        if slip > top + 0.5:
            out.append("a curve fill with %.1f%% impact for a $%.2f order: a 30+ SOL curve moves at most %.1f%% for it "
                       "(even at SOL $%g)" % (slip, cost, top, sol_min))
    entry, exit_px, proceeds = num(trade.get("entry_price")), num(trade.get("exit_price")), num(trade.get("proceeds"))
    if entry and entry > 0 and cost and cost > 0:
        tokens = cost / entry                           # the order's fee in it: a hair more than it got
        if tokens > REAL_TOKENS * 1.001:
            out.append("%.0f M tokens bought: more than the %.1f M a curve ever sells" % (tokens / 1e6, REAL_TOKENS / 1e6))
        if exit_px and exit_px > 0 and proceeds is not None and single_sale(trade):
            ratio = proceeds / (tokens * exit_px)
            if ratio > PROCEEDS_HI or ratio < PROCEEDS_LO:
                out.append("proceeds $%.2f are %.3gx what its tokens give at its own exit price (a sale's costs and "
                           "a moment's move: %gx-%gx)" % (proceeds, ratio, PROCEEDS_LO, PROCEEDS_HI))
    return out


def by_hand(trade, marks):
    """--void MINT, MINT@OPENED, SYMBOL or SYMBOL@OPENED names this trade (resolve_marks() refuses one that names more
    than one trade of the night, or none)."""
    for m in marks or ():
        who, _, when = str(m).partition("@")
        if who in (trade.get("mint"), trade.get("symbol")) and (not when or when == trade.get("opened")):
            return True
    return False


def resolve_marks(trades, marks):
    """The trades --void names, each mark exactly one: -> (set of indexes, None) or (None, why). A mark that names no
    trade of the night, or more than one (a symbol: 24 of them repeat in the night of 2026-11-27, Thanksgiving twice;
    a mint traded twice), is refused with the candidates as MINT@OPENED: a hand void is one trade, never a guess."""
    out = set()
    for m in marks or ():
        hit = [i for i, t in enumerate(trades) if by_hand(t, [m])]
        if not hit:
            return None, "--void %s names no trade of this night" % m
        if len(hit) > 1:
            return None, "--void %s names %d trades: name one as MINT@OPENED (%s)" % (
                m, len(hit), ", ".join("%s@%s" % (trades[i].get("mint"), trades[i].get("opened")) for i in hit))
        out.add(hit[0])
    return out, None


# ---------------------------------------------------------------- the replay

class Book(object):
    """The replayed book, with bot.Night's own RISK and equity code on it (why_no_entries, equity, exit_value, fee_usd):
    state["balance"], state["positions"] (each valued at its cost: tokens_ui = cost, entry_price 1), the night's
    trades so far, its streak, its pause, and the replay's clock."""

    why_no_entries = bot.Night.why_no_entries
    equity = bot.Night.equity
    exit_value = bot.Night.exit_value
    fee_usd = bot.Night.fee_usd

    def __init__(self, cfg, start_equity, cash):
        self.cfg = cfg
        self.now = 0.0
        self.sol_px = None                              # no fee in exit_value: the cost already has it
        self.state = {"balance": float(cash), "positions": {},
                      "night": {"trades": [], "start_equity": float(start_equity), "streak": 0, "cooldown_until": 0}}

    def epoch(self):
        return self.now


def replay(trades, keep, cfg, start_equity, cash):
    """The kept trades (indexes into trades) again with the bot's sizing and RISK. -> {"taken": {i: new cost}, "dropped":
    {i: why}, "eq": {i: the equity it was sized on}, "cash", "streak", "cooldown_until", "timeline": [(t, equity at
    cost) after each event], "stopped": (t, why) or None}."""
    b = Book(cfg, start_equity, cash)
    n, st = b.state["night"], b.state
    events = []
    for k, i in enumerate(sorted(keep)):
        t = trades[i]
        a, z = epoch(t.get("opened")), epoch(t.get("closed"))
        a = z if a is None else a
        z = a if z is None else max(z, a)
        if a is None:
            a = z = float(k)                            # no times at all: the record's order
        events.append((a, 1, k, i, "open"))
        events.append((z, 2 if z <= a else 0, k, i, "close"))     # exits first in a second, but never before its entry
    events.sort()
    taken, dropped, timeline, stopped, eqs = {}, {}, [], None, {}
    pp, least = float(cfg["position_pct"]), float(cfg["min_order_usd"])
    for t_ev, _, _, i, kind in events:
        b.now = t_ev
        tr = trades[i]
        if kind == "close":
            cost = taken.get(i)
            if cost is None:
                continue
            pct = num(tr.get("pnl_pct")) or 0.0
            st["balance"] += cost * (1 + pct / 100.0)
            del st["positions"][str(i)]
            n["trades"].append(i)
            step = int(num(((tr.get("plan") or {}) if isinstance(tr.get("plan"), dict) else {}).get("step")) or 0)
            n["streak"] = n["streak"] + 1 if pct < 0 and step == 0 else 0
            if n["streak"] >= cfg["loss_streak"]:
                n["cooldown_until"] = b.now + cfg["cooldown_min"] * 60
        else:
            why = b.why_no_entries(bot.ALWAYS_LEFT, {})
            if why is None:
                cash_now = max(0.0, math.floor(st["balance"] * 1e4) / 1e4)
                eq = b.equity({})
                size = min(cash_now, eq * pp / 100.0)
                if size < least:
                    why = "only $%.2f to size" % size
                else:
                    if tr.get("snipe"):
                        share = size
                    else:                               # a momentum trade: its own share of the equity (its LEARNER size)
                        sp = num(tr.get("size_pct"))
                        share = eq * sp / 100.0 if sp is not None and sp > 0 else size
                    cost = min(cash_now, max(least, share))
                    taken[i], eqs[i] = cost, eq
                    st["balance"] = max(0.0, st["balance"] - cost)
                    st["positions"][str(i)] = {"tokens_ui": cost, "entry_price": 1.0, "last": 1.0,
                                               "strategy": tr.get("strategy") or "migrated", "cost": cost}
            if why is not None:
                dropped[i] = why
                if stopped is None and "stopped for the night" in why:
                    stopped = (t_ev, why)
        timeline.append((t_ev, round(b.equity({}), 2)))
    return {"taken": taken, "dropped": dropped, "cash": st["balance"], "streak": n["streak"], "eq": eqs,
            "cooldown_until": n["cooldown_until"], "timeline": timeline, "stopped": stopped}


def equity_at(timeline, t, before):
    """The replayed equity (cash and open positions at cost) at t: the last point at or before t; `before` until the
    first one."""
    out = before
    for tt, eq in timeline:
        if tt > t:
            break
        out = eq
    return out


# ---------------------------------------------------------------- the LEARNER's memories

def unlearn(mem, labels, pnl_pct, weight=1.0, shadow=False):
    """The inverse of learner.update(mem, labels, pnl_pct, weight, shadow) for one trade. -> True when exact (no label
    of it had reached learner.FORGET_AT, where update() fades every count: then it is an approximation)."""
    pnl = num(pnl_pct)
    labels = learner._labels(labels)
    w = float(weight)
    if not isinstance(mem, dict) or pnl is None or not labels or w <= 0:
        return True
    learner._ensure(mem)
    pnl = max(-learner.PNL_CAP, min(learner.PNL_CAP, pnl))
    if shadow:
        mem["shadows"] = max(0, int(mem.get("shadows") or 0) - 1)
    else:
        mem["samples"] = max(0, int(mem.get("samples") or 0) - 1)
    exact = True
    for lab in labels:
        st = learner._clean_stat(mem["labels"].get(lab))
        if st is None:
            continue
        if st["n"] >= learner.FORGET_AT - 1e-6:
            exact = False
        st["n"] -= w
        st["sum"] -= w * pnl
        st["sumsq"] -= w * pnl * pnl
        st["wins"] -= w if pnl > 0 else 0.0
        if st["n"] <= 1e-9:
            del mem["labels"][lab]
        else:
            mem["labels"][lab] = {k: learner._tidy(max(0.0, v) if k != "sum" else v) for k, v in st.items()}
    return exact


def shadow_weight(cfg):
    """What a trade the corrected book would not have taken counts for in the LEARNER: the bot follows a coin RISK
    said no to as a SHADOW (shadow_on, shadow_on_risk) and learns it at shadow_weight; 0 when it would not have."""
    if not cfg.get("shadow_on", True) or not cfg.get("shadow_on_risk", True):
        return 0.0
    return min(1.0, max(0.0, num(cfg.get("shadow_weight")) if num(cfg.get("shadow_weight")) is not None else 0.5))


def relearn(mem, voided, dropped, kept, w, existed, tz="Europe/Bratislava"):
    """One memory corrected for the fix: a voided trade's lesson unlearned (it never happened), a dropped one's
    re-weighted from a real trade's 1 to w as the RISK shadow the corrected book would have followed (unlearned, then
    learner.update(weight=w, shadow=True)), the kept ones and every older lesson (the shadows among them) as they were.
    A memory that did not exist (existed False) is made as the bot would make it (learner.backfill of the kept trades)
    plus the dropped ones at w. -> (exact, counts)."""
    exact = True
    if not existed:
        learner.backfill(mem, kept, tz)
    for t in voided:
        if existed and t.get("learn"):
            exact = unlearn(mem, t["learn"], t.get("pnl_pct")) and exact
    for t in dropped:
        if not t.get("learn"):
            continue
        if existed:
            exact = unlearn(mem, t["learn"], t.get("pnl_pct")) and exact
        if w > 0:
            learner.update(mem, t["learn"], t.get("pnl_pct"), w, True)
    return exact


def learner_summary(mem, cfg):
    """The page's LEARNER block as Night.learn_summary() makes it (SNIPE mode: the snipes' memory, marked)."""
    s = learner.summary(mem, cfg.get("learn_k"), cfg.get("learn_min_n"))
    if s is not None and bot.snipe_on(cfg):
        s = dict(s, memory="snipe")
        for k in ("best", "worst"):
            s[k] = [dict(r, label="snipe · %s" % r.get("label")) if isinstance(r, dict) else r for r in s.get(k) or []]
    return s


# ---------------------------------------------------------------- the fix

def usd(x):
    return ("-$%.2f" % -x) if x < 0 else "$%.2f" % x


def auto_voids(trades, sol_min=SOL_MIN):
    """{index: reasons} of the trades whose own record shows an impossible curve state (void_reasons)."""
    out = {}
    for i, t in enumerate(trades):
        why = void_reasons(t, sol_min)
        if why:
            out[i] = why
    return out


ROUNDING = 0.005            # a trade's pnl is kept to the cent: each one rounds by at most half a cent


def plan_fix(data, night_id=None, marks=(), sol_min=SOL_MIN, cfg=None):
    """Everything the fix would do, nothing written. -> (report lines, the plan doc or None when it does nothing, why).
    The night: --night ID, else the one of the current and the last night that has trades; when both have trades, the
    one with something to void (a void rule or a --void mark), else it asks for --night. A night with nothing to void
    is left as it is (why "nothing to void ...")."""
    cfg = cfg or bot.load_config()
    st = bot.read_json(os.path.join(data, "state.json"), None)
    if not isinstance(st, dict):
        return [], None, "no state.json in %s" % data
    found = [(k, st[k]) for k in ("night", "last_night") if isinstance(st.get(k), dict)
             and isinstance(st[k].get("trades"), list) and (night_id is None or st[k].get("id") == night_id)]
    busy = [(k, n) for k, n in found if n["trades"]]
    if night_id is None and len(busy) == 2:
        hits = [(k, n) for k, n in busy if auto_voids(n["trades"], sol_min) or any(by_hand(t, marks) for t in n["trades"])]
        if len(hits) != 1:
            return [], None, "the current night %s and the last one %s both have trades%s: name one with --night" % (
                busy[0][1].get("id"), busy[1][1].get("id"), "" if hits else " and neither has one to void")
        where, night = hits[0]
    elif busy or found:
        where, night = (busy or found)[0]
    else:
        return [], None, "no night %s in state.json" % (night_id or "")
    if where == "last_night" and isinstance(st.get("night"), dict) and (st["night"].get("trades") or []):
        return [], None, "night %s is closed and a later night has trades (sized on its book): not fixed" % night["id"]
    if night.get("fix"):
        return [], None, "night %s was fixed already (%s): nothing to do" % (night["id"], night["fix"].get("at"))
    if st.get("positions"):
        return [], None, "%d position(s) open: they were sized on the book being corrected; run it on a flat book" % (
            len(st["positions"]))
    trades = list(night.get("trades") or [])
    hand, bad = resolve_marks(trades, marks)
    if bad:
        return [], None, bad
    voids = auto_voids(trades, sol_min)
    for i in hand:
        voids[i] = voids.get(i, []) + ["voided by hand (--void)"]
    start = num(night.get("start_equity")) or 0.0
    old_bal = num(st.get("balance")) or 0.0
    head = ["FIX BOOK · night %s (%s) · %d closed trades · start %s · balance now %s" % (
        night["id"], where, len(trades), usd(start), usd(old_bal)), ""]
    if not voids:
        return head + ["VOID 0: every trade reads as a standard curve's"], None, \
            "nothing to void in night %s: nothing written" % night["id"]
    keep = [i for i in range(len(trades)) if i not in voids]
    r = replay(trades, keep, cfg, start, start)
    old_pnl = sum(num(t.get("pnl")) or 0.0 for t in trades)
    new_trades, fixed = [], []
    for i, t in enumerate(trades):
        if i not in r["taken"]:
            continue
        cost = r["taken"][i]
        pct = num(t.get("pnl_pct")) or 0.0
        proceeds = cost * (1 + pct / 100.0)
        nt = dict(t, cost=round(cost, 2), proceeds=round(proceeds, 2), pnl=round(proceeds - cost, 2))
        if abs(cost - (num(t.get("cost")) or 0.0)) >= 0.005:
            nt["fix_was"] = {k: t.get(k) for k in ("cost", "proceeds", "pnl", "size_pct")}
        eq_at = r["eq"].get(i)
        if eq_at:
            nt["size_pct"] = round(cost / eq_at * 100, 1)
        new_trades.append(nt)
        fixed.append(proceeds - cost)
    new_pnl = sum(fixed)
    # ONE corrected balance, for the balance, the night's summary and the report. The old balance less the start less
    # the old trades' pnl (each kept to the cent) is what the cents dropped, when the night started and ended flat: then
    # the corrected book is the replay's own cash (the start plus the new trades' P&L, unrounded), and the old cents go
    # with the old trades. More than rounding explains (a night that started with positions held from the night
    # before): the old balance less the old P&L plus the new.
    residue = old_bal - start - old_pnl
    flat = abs(residue) <= ROUNDING * len(trades) + 0.01
    new_bal = start + new_pnl if flat else old_bal - old_pnl + new_pnl
    lines = list(head)
    lines.append("VOID %d (a state no standard pump.fun curve can be in, from the record itself):" % len(voids))
    for i in sorted(voids):
        t = trades[i]
        lines.append("  %s %s %s · booked %s (%+.1f%%) on %s · %s" % (
            str(t.get("opened"))[11:19], t.get("symbol"), str(t.get("mint"))[:8], usd(num(t.get("pnl")) or 0.0),
            num(t.get("pnl_pct")) or 0.0, usd(num(t.get("cost")) or 0.0), " · ".join(voids[i])))
    kept_pct = [num(trades[i].get("pnl_pct")) or 0.0 for i in keep]
    lines += ["", "KEPT %d: %d wins (%.0f%%), mean %+.2f%%, median %+.2f%% (as booked, at their own size)" % (
        len(keep), sum(1 for x in kept_pct if x > 0), 100.0 * sum(1 for x in kept_pct if x > 0) / max(1, len(keep)),
        sum(kept_pct) / max(1, len(keep)), bot.median(kept_pct) or 0.0)]
    took = sorted(r["taken"])
    why_n = {}
    for i, why in r["dropped"].items():
        w = re.sub(r"[-+]?\d+(\.\d+)?", "N", why)
        why_n[w] = why_n.get(w, 0) + 1
    lines.append("REPLAY (the bot's sizing and RISK on the kept trades): %d taken, %d dropped (%s)" % (
        len(took), len(r["dropped"]), ", ".join("%s: %d" % kv for kv in sorted(why_n.items(), key=lambda kv: -kv[1]))
        or "none"))
    if took:
        last = trades[took[-1]]
        lines.append("  first %s %s · last %s %s · orders %s to %s" % (
            str(trades[took[0]].get("opened"))[11:19], trades[took[0]].get("symbol"), str(last.get("opened"))[11:19],
            last.get("symbol"), usd(min(r["taken"].values())), usd(max(r["taken"].values()))))
    if r["stopped"]:
        lines.append("  night stop at %s: %s" % (iso(r["stopped"][0])[11:19], r["stopped"][1]))
    wins = [x for x in fixed if x > 0]
    lines += ["", "BALANCE %s -> %s (the night: %s booked -> %s; %s -> %s at the end, %+.1f%%)" % (
        usd(old_bal), usd(new_bal), usd(old_pnl), usd(new_pnl), usd(start), usd(new_bal),
        ((new_bal - start) / start * 100) if start else 0.0),
        "  %d trades, %d wins, best %s, worst %s" % (len(new_trades), len(wins), usd(max(fixed, default=0.0)),
                                                  usd(min(fixed, default=0.0))),
        ("  the old balance less the start less the old trades' P&L: %s, the old trades' cents (the night started and "
         "ended flat): dropped with them" % usd(residue)) if flat else
        ("  the old balance less the start less the old trades' P&L: %s, more than the cents explain (positions held "
         "into the night?): kept, the balance is the old one less the old P&L plus the new" % usd(residue))]
    doc = {"state": st, "where": where, "night": night, "trades": trades, "voids": voids, "replay": r,
           "new_trades": new_trades, "old_balance": old_bal, "new_balance": new_bal, "old_pnl": old_pnl,
           "new_pnl": new_pnl, "start": start, "cfg": cfg, "residue": residue, "flat": flat}
    return lines, doc, None


def archive_path(data, name, night_id):
    folder = os.path.join(data, "archive")
    base = "%s-%s-before-fix" % (os.path.splitext(name)[0], night_id)
    path, k = os.path.join(folder, base + ".json"), 2
    while os.path.exists(path):
        path, k = os.path.join(folder, "%s-%d.json" % (base, k)), k + 1
    return path


def apply_fix(data, doc, now=None, dry_run=False):
    """Writes the plan (unless dry_run). -> report lines about the files. Every body is written to a temp file first
    (one that fails: nothing is changed, OSError), then each original is archived, then the temp files replace them,
    state.json LAST: its night["fix"] is the mark that the fix is done, so a run that stopped half way is run again."""
    st, night, cfg = copy.deepcopy(doc["state"]), None, doc["cfg"]
    night = st[doc["where"]]
    r, trades = doc["replay"], doc["trades"]
    now = (now or bot.utc_now()).replace(microsecond=0)
    out = []
    # the state
    night["trades"] = doc["new_trades"]
    night["streak"] = int(r["streak"])
    night["cooldown_until"] = r["cooldown_until"]
    curve = night.get("curve")
    if isinstance(curve, list):
        night["curve"] = [[p[0], equity_at(r["timeline"], num(p[0]) or 0.0, doc["start"])]
                          for p in curve if isinstance(p, (list, tuple)) and len(p) >= 2]
    st["balance"] = doc["new_balance"]
    night["fix"] = {"at": now.isoformat() + "Z", "tool": "memebot/fix_book.py",
                    "balance_was": round(doc["old_balance"], 2), "balance": round(doc["new_balance"], 2),
                    "pnl_was": round(doc["old_pnl"], 2), "pnl": round(doc["new_pnl"], 2),
                    "residue": round(doc.get("residue") or 0.0, 4),
                    "void": [dict(trades[i], fix_void=doc["voids"][i]) for i in sorted(doc["voids"])],
                    "dropped": [dict(trades[i], fix_dropped=r["dropped"][i]) for i in sorted(r["dropped"])]}
    text = "book fixed (memebot/fix_book.py): %d trade%s voided (curve states no pump.fun curve can be in), %d not " \
           "taken by the corrected book · balance %s -> %s" % (
               len(doc["voids"]), "" if len(doc["voids"]) == 1 else "s", len(r["dropped"]), usd(doc["old_balance"]),
               usd(doc["new_balance"]))

    def logger(n, words):
        n["lines"] = int(num(n.get("lines")) or 0) + 1
        n.setdefault("decisions", []).append({
            "t": now.isoformat() + "Z", "time": bot.local(now, cfg["timezone"]).strftime("%H:%M:%S"), "agent": "LOGGER",
            "coin": None, "action": "fix", "text": words, "n": n["lines"]})
        del n["decisions"][:-600]
    logger(night, text)
    end = round(st["balance"], 2)
    if doc["where"] == "last_night":                   # a closed night: its summary too, and the night after it (no
        nxt = st.get("night")                           # trade yet: plan_fix refuses otherwise) starts from the book
        if isinstance(nxt, dict):
            nxt["start_equity"] = end
            # it has traded nothing: its equity was its start all along (its curve's points, its start line, inflated)
            if isinstance(nxt.get("curve"), list):
                nxt["curve"] = [[p[0], end] for p in nxt["curve"] if isinstance(p, (list, tuple)) and len(p) >= 2]
            logger(nxt, "start corrected to %s: night %s was fixed (memebot/fix_book.py)" % (usd(end), night["id"]))
            out.append("night %s (no trades yet) now starts at %s, its equity curve flat there" % (nxt.get("id"),
                                                                                                    usd(end)))
        for s in st.get("nights") or []:
            if isinstance(s, dict) and s.get("id") == night["id"]:
                pnl = [num(t.get("pnl")) or 0.0 for t in doc["new_trades"]]
                start = num(s.get("start"))             # close_night's own sums: end = the equity at the close (the
                start = doc["start"] if start is None else start     # corrected balance: flat), pnl = end - start
                s.update(end=end, pnl=round(end - start, 2), trades=len(pnl), wins=sum(1 for x in pnl if x > 0),
                         best=max(pnl, default=0), worst=min(pnl, default=0))
    # the LEARNER (relearn): the voided trades' lessons unlearned, the dropped ones' re-weighted to the RISK shadow
    # the corrected book would have followed, everything else as it was (the old shadows too)
    w = shadow_weight(cfg)
    voided = [trades[i] for i in sorted(doc["voids"])]
    dropped = [trades[i] for i in sorted(r["dropped"])]
    kept = [t for t in doc["new_trades"]]
    mems = {}
    for name, snipe in (("memory_snipe.json", True), ("memory.json", False)):
        mine = lambda ts: [t for t in ts if isinstance(t, dict) and bool(t.get("snipe")) == snipe]   # noqa: E731
        v, d, k = mine(voided), mine(dropped), mine(kept)
        path = os.path.join(data, name)
        existed = os.path.exists(path)
        if not [t for t in v + d if t.get("learn")] and (existed or not snipe or not k):
            out.append("%s: unchanged (%s)" % (name, "no %s trade voided or dropped with labels" % (
                "snipe" if snipe else "momentum") if existed else "none, and none to make"))
            continue
        mem = learner.load(path) if existed else learner.fresh()
        before = (int(num(mem.get("samples")) or 0), int(num(mem.get("shadows")) or 0))
        exact = relearn(mem, v, d, k, w, existed, cfg["timezone"])
        after = (int(num(mem.get("samples")) or 0), int(num(mem.get("shadows")) or 0))
        mems[name] = mem
        if existed:
            out.append("%s: %d voided unlearned, %d dropped re-weighted to x%g (the RISK shadows the corrected book would "
                       "have followed), the rest kept · samples %d -> %d, shadow lessons %d -> %d%s · a shadow filled on "
                       "an odd reading is not in state.json and stays" % (
                           name, len([t for t in v if t.get("learn")]), len([t for t in d if t.get("learn")]), w,
                           before[0], after[0], before[1], after[1], "" if exact else
                           " · approximately: a label had reached FORGET_AT, where the counts fade"))
        else:
            out.append("%s: there is none: made as the bot would (learner.backfill of the %d kept trades) plus the %d "
                       "dropped at x%g · %d samples, %d shadow lessons" % (name, len(k), len(d), w, after[0], after[1]))
    mem_now = mems.get("memory_snipe.json" if bot.snipe_on(cfg) else "memory.json")
    if mem_now is None:
        mem_now = learner.load(os.path.join(data, "memory_snipe.json" if bot.snipe_on(cfg) else "memory.json"))
    summ = learner_summary(mem_now, cfg)
    if summ is not None:
        st["learner_summary"] = summ
    # the panel: what bot.panel() shows (the current night, else the last one)
    panel = bot.read_json(os.path.join(data, "panel.json"), None)
    if isinstance(panel, dict):
        pn = st.get("night") or st.get("last_night") or {}
        pos, eq = bot.positions_view(st, {})
        panel.update(balance=round(st["balance"], 2), equity=round(eq, 2), positions=pos,
                     trades=bot.trades_view(pn.get("trades")), nights=(st.get("nights") or [])[-30:],
                     curve=(pn.get("curve") or [])[-600:], night={k: pn.get(k) for k in bot.NIGHT_KEYS},
                     log=(pn.get("decisions") or [])[-200:])
        if summ is not None:
            panel["learner"] = summ
    files = [(name, mem, "learner") for name, mem in sorted(mems.items())]
    if isinstance(panel, dict):
        files.append(("panel.json", panel, "bot"))
    files.append(("state.json", st, "bot"))            # last: the fix's mark
    plan = []
    for name, body, how in files:
        src = os.path.join(data, name)
        arch = archive_path(data, name, night["id"]) if os.path.exists(src) else None
        plan.append((name, body, how, src, arch))
        out.append("%s %s%s" % ("would write" if dry_run else "wrote", name,
                                (", the original archived to %s" % os.path.relpath(arch, data)) if arch else
                                " (no original)"))
    if dry_run:
        return out
    tmps = []
    try:                                                # 1. every body to a temp file: a failure changes nothing
        for name, body, how, src, arch in plan:
            tmp = src + ".fix.tmp"
            tmps.append(tmp)
            if how == "learner":
                if not learner.save(tmp, body):
                    raise OSError("could not write %s" % tmp)
            else:
                bot.write_json(tmp, body)
    except Exception:
        for tmp in tmps:
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    for name, body, how, src, arch in plan:             # 2. the originals archived
        if arch:
            os.makedirs(os.path.dirname(arch), exist_ok=True)
            shutil.copy2(src, arch)
    for name, body, how, src, arch in plan:             # 3. in place, state.json last
        os.replace(src + ".fix.tmp", src)
    return out


def main(argv=None, out=print):
    ap = argparse.ArgumentParser(description="Meme Bot: void the trades booked on impossible curve readings and "
                                             "rebuild the night's book (paper).")
    ap.add_argument("--data", required=True, help="the bot's data folder (state.json, panel.json, memory*.json)")
    ap.add_argument("--night", default=None, help="the night's id (default: the one of the current and the last night "
                                                  "that has trades; both: the one with something to void)")
    ap.add_argument("--void", action="append", default=[], metavar="MINT[@OPENED]",
                    help="void this trade by hand too: its mint (or symbol), @ its opened time when the night has "
                         "more than one trade of it (one that names several, or none, is refused)")
    ap.add_argument("--sol-min", type=float, default=SOL_MIN, help="SOL/USD at least this, for the impact rule")
    ap.add_argument("--config", default=bot.CONFIG, help="the config the night traded with (config.json)")
    ap.add_argument("--dry-run", action="store_true", help="print the report, write nothing")
    a = ap.parse_args(argv)
    cfg = bot.load_config(a.config)
    lines, doc, why = plan_fix(a.data, a.night, a.void, a.sol_min, cfg)
    for x in lines:
        out(x)
    if doc is None:
        out("NOT FIXED: %s" % why)
        return 0 if why and ("fixed already" in why or why.startswith("nothing to void")) else 2
    out("")
    for x in apply_fix(a.data, doc, dry_run=a.dry_run):
        out(x)
    out("DRY RUN: nothing written" if a.dry_run else "FIXED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
