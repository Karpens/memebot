#!/usr/bin/env python3
"""Meme Bot BACKTEST: the New Pairs rules tried again on the recorded tapes, walk-forward.

    python3 memebot/backtest.py --data DIR [--since ISO] [--until ISO] [--grid FILE|default] [--split 0.7]
                                [--out report.json] [--config FILE] [--jobs N] [--max-combos 200] [--budget-min M]
                                [--min-trades 30] [--margin-pct 2] [--top 5] [--seed 7]
                                [--job-min 340] [--job-gap-s 120] [--align auto|tape|chain]
                                [--rug-latency 0.4] [--fill-delay 0]

What it does:
  1. reads <DIR>/tapes (memebot/replay.py: the recorder's lines, field aliases, unknown kinds ignored);
  2. builds the grid: the base config (memebot/config.json, or --config) and variations of the main levers (the
     entry trigger, the creator's buy, the size, the book, the take-profit ladders, the stop, the trailing stops,
     the time stops), one lever at a time first, then random mixes of a few, at most --max-combos (200) in all;
  3. WALK-FORWARD: every config is replayed through the real engine (memebot/replay.py) on the FIRST part of the
     time range (--split 0.7: the first 70 %), in parallel processes, each in a fresh temp dir; they are ranked
     by their total P&L after every cost there (configs with fewer than --min-trades trades are not ranked);
  4. the top 5 and the base are replayed on the HELD-OUT rest, each with the LEARNER memory its first part left
     (a fresh $ balance, so their dollars compare);
  5. a change is recommended only when a top config, taken in its first-part rank order, on the held-out part
     has at least --min-trades (30) trades, a mean P&L per trade after costs above 0, and a total P&L at least
     --margin-pct (2) % of the start balance above the base's. Otherwise: "keep".

The report (JSON, --out) has per config: trades, win rate, total and mean P&L after costs ($ and %), max drawdown,
profit factor, by hold tier and by exit, "blind" exits (priced on a stale reading), and the decision with the
config.json diff to apply. It also checks the replay itself ("live_check", when the tape has the live bot's own
trades): the whole range replayed with the settings, cash and LEARNER memory the live run started with (its
"start" line), trade by trade against the tape's buy / sell lines: the same entries, the same exits, the P&L gap.
Where they differ, the replay's fills (the curve maths on the last reading) are not what Jupiter gave live, and
every number above is that much less sure.

Honest limits (said in the report too): it is paper on paper. Picking the best of up to 200 configs on the first
part flatters them; only the held-out part counts, and even a pass there is a hint, not a promise. The replay can
only use what the live bot recorded (memebot/replay.py says what that means for looser rules and longer holds).
Standard library only.
"""

import argparse
import copy
import datetime
import json
import multiprocessing
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import replay  # noqa: E402 - by name, so worker processes can unpickle replay.run_task

bot = replay.bot

# ---------------------------------------------------------------- the levers

# plain config keys and the alternatives the default grid tries (the base value is the config's own). Chosen
# around the live settings of 2026-11-26: entries looser and stricter, the size and the book, the stop.
DEFAULT_GRID = {
    "np_min_sol": [2.0, 3.0, 4.0],
    "np_min_buys": [4, 8, 10],
    "np_min_buy_share": [0.55, 0.75, 0.85],
    "np_min_rise_pct": [8.0, 18.0, 25.0],
    "np_max_age_s": [60, 120],
    "np_max_dev_pct": [4.0, 12.0],
    "position_pct": [5.0, 15.0, 20.0],
    "max_open": [4, 12],
    "stop_pct": [-5.0, -10.0, -15.0],
    "np_quiet_min": [1.5, 6.0],
    # pseudo-levers on the NEWS hold plan's tiers (see LEVERS below)
    "tp_x": [0.7, 1.5, 2.0],
    "scalp_tp": [5.0, 10.0, 15.0],
    "scalp_frac": [0.5],
    "swing_tp": [10.0, 15.0],
    "swing_frac": [0.25, 0.75],
    "trail_x": [0.5, 1.5, 2.0],
    "time_stop_x": [0.5, 2.0],
}

# pseudo-levers: (what it means, the function that applies it to the tiers). The tiers are config.json's
# hold_tiers (plan.py: [{"name", "min_score", "ladder": [[gain %, fraction of what is left], ...], "trail_pct",
# "time_stop_min"}]); a recommendation writes the whole new hold_tiers.
PSEUDO_ORDER = ("tp_x", "scalp_tp", "scalp_frac", "swing_tp", "swing_frac", "trail_x", "time_stop_x")
PSEUDO_HELP = {
    "tp_x": "every take-profit step of every tier times x (7 / 20 / 60 / 100 → 7x / 20x / ...)",
    "scalp_tp": "the scalp tier's take-profit gain % (it sells everything there)",
    "scalp_frac": "the scalp tier sells this fraction at its take-profit and trails the rest (the swing tier's "
                  "trail, a last step at 3x the gain)",
    "swing_tp": "the swing tier's first step gain % (its second step at least 2.5x that)",
    "swing_frac": "the fraction the swing tier sells at its first step",
    "trail_x": "every tier's trailing stop % times x",
    "time_stop_x": "every tier's time stop (minutes) times x",
}


def tiers(cfg):
    """A deep copy of the tiers the engine plays (plan.tiers_from_cfg: hold_tiers, or the old tp1_* keys)."""
    return copy.deepcopy(bot.holdplan.tiers_from_cfg(cfg))


def _tier(ts, name):
    return next((t for t in ts if t["name"] == name), None)


def _p_tp_x(ts, x):
    for t in ts:
        t["ladder"] = [[round(g * x, 3), fr] for g, fr in t["ladder"]]


def _p_scalp_tp(ts, v):
    t = _tier(ts, "scalp")
    if t:
        t["ladder"][0][0] = v
        t["ladder"] = [s for s in t["ladder"] if s is t["ladder"][0] or s[0] > v]


def _p_scalp_frac(ts, v):
    t = _tier(ts, "scalp")
    if t and v < 1.0:
        tp = t["ladder"][0][0]
        t["ladder"] = [[tp, v], [round(max(20.0, 3 * tp), 3), 1.0]]
        sw = _tier(ts, "swing")
        t["trail_pct"] = t["trail_pct"] or (sw["trail_pct"] if sw and sw["trail_pct"] else 6.0)


def _p_swing_tp(ts, v):
    t = _tier(ts, "swing")
    if t:
        t["ladder"][0][0] = v
        for s in t["ladder"][1:]:
            s[0] = max(s[0], round(2.5 * v, 3))


def _p_swing_frac(ts, v):
    t = _tier(ts, "swing")
    if t and len(t["ladder"]) > 1:
        t["ladder"][0][1] = v


def _p_trail_x(ts, x):
    for t in ts:
        t["trail_pct"] = round(min(99.0, t["trail_pct"] * x), 3)


def _p_time_stop_x(ts, x):
    for t in ts:
        t["time_stop_min"] = round(t["time_stop_min"] * x, 3)


PSEUDO = {"tp_x": _p_tp_x, "scalp_tp": _p_scalp_tp, "scalp_frac": _p_scalp_frac, "swing_tp": _p_swing_tp,
          "swing_frac": _p_swing_frac, "trail_x": _p_trail_x, "time_stop_x": _p_time_stop_x}


def apply(cfg, levers):
    """The base config with the levers set: plain config keys as they are (any key, hold_tiers too), then the
    pseudo-levers on the tiers in PSEUDO_ORDER. -> a new config."""
    c = copy.deepcopy(cfg)
    for k, v in (levers or {}).items():
        if k not in PSEUDO:
            c[k] = copy.deepcopy(v)
    pseudo = [(k, levers[k]) for k in PSEUDO_ORDER if k in (levers or {})]
    if pseudo:
        ts = tiers(c)
        for k, v in pseudo:
            PSEUDO[k](ts, float(v))
        for t in ts:
            t["ladder"] = sorted([[g, fr] for g, fr in t["ladder"] if g > 0 and fr > 0], key=lambda s: s[0])
        c["hold_tiers"] = ts
    return c


def config_diff(base, new):
    """The config.json keys a config changes, with their new values (hold_tiers whole)."""
    out = {}
    for k, v in new.items():
        if k.startswith("_"):
            continue
        if k == "hold_tiers":
            if tiers(new) != tiers(base):
                out[k] = tiers(new)
        elif base.get(k) != v:
            out[k] = v
    return out


def lever_text(levers):
    return ", ".join("%s=%s" % (k, json.dumps(v)) for k, v in sorted((levers or {}).items())) or "base"


# ---------------------------------------------------------------- the grid

def load_grid(spec):
    """"default" (None), a JSON file, or a dict -> {"levers": {key: [alternatives]}} or {"combos": [levers, ...]},
    with optional "max_combos", "seed", "random" (false: one lever at a time only)."""
    if spec in (None, "", "default"):
        return {"levers": copy.deepcopy(DEFAULT_GRID)}
    if isinstance(spec, dict):
        return copy.deepcopy(spec)
    with open(spec, encoding="utf-8") as fh:
        doc = json.load(fh)
    if isinstance(doc, list):
        return {"combos": doc}
    if not isinstance(doc, dict) or not ("levers" in doc or "combos" in doc):
        raise ValueError("a grid is {\"levers\": {key: [values]}} or {\"combos\": [{key: value}, ...]}")
    return doc


def _key(levers):
    return json.dumps(levers, sort_keys=True)


def build_grid(grid, base_cfg, max_combos=200, seed=7):
    """[levers dict] to replay, the base ({}) first, at most max_combos in all: explicit "combos" as given;
    else every lever's alternatives one at a time (values equal to the base's left out), then random mixes of 2
    to 4 levers (seeded: the same grid every run) until max_combos."""
    grid = load_grid(grid) if not isinstance(grid, dict) or not ("levers" in grid or "combos" in grid) else grid
    cap = max(1, int(grid.get("max_combos") or max_combos))
    out, seen = [{}], {_key({})}

    def add(lv):
        k = _key(lv)
        if k not in seen and len(out) < cap:
            seen.add(k)
            out.append(lv)

    if grid.get("combos") is not None:
        for lv in grid["combos"]:
            if isinstance(lv, dict):
                add(lv)
        return out
    levers = {}
    for k, vals in (grid.get("levers") or {}).items():
        vals = vals if isinstance(vals, list) else [vals]
        if k not in PSEUDO:
            vals = [v for v in vals if v != base_cfg.get(k)]
        if vals:
            levers[k] = vals
    for k, vals in levers.items():
        for v in vals:
            add({k: v})
    if grid.get("random", True) and levers:
        rng = random.Random(grid.get("seed", seed))
        names = sorted(levers)
        tries = 0
        while len(out) < cap and tries < cap * 50:
            tries += 1
            pick = rng.sample(names, min(len(names), rng.randint(2, 4)))
            add({k: rng.choice(levers[k]) for k in sorted(pick)})
    return out


# ---------------------------------------------------------------- running many replays

def run_many(tape, tasks, jobs=None, budget_s=None, progress=None):
    """{key: result} of replay.run_task over tasks [(key, cfg, since, until, memory, opts)], in `jobs` processes
    (fork where the OS has it: the tape is shared, not copied), each replay in its own temp dir. A wall-clock
    budget stops handing out new ones (what finished is kept)."""
    jobs = max(1, int(jobs or os.cpu_count() or 1))
    out, t0, n = {}, time.monotonic(), len(tasks)

    def took(k, r):
        out[k] = r
        if progress is not None:
            progress(len(out), n)
        return budget_s is not None and time.monotonic() - t0 > budget_s

    if jobs == 1 or n <= 1:
        replay.init_worker(tape)
        for task in tasks:
            if took(*replay.run_task(task)):
                break
        return out
    methods = multiprocessing.get_all_start_methods()
    ctx = multiprocessing.get_context("fork" if "fork" in methods else None)
    pool = ctx.Pool(min(jobs, n), initializer=replay.init_worker, initargs=(tape,))
    try:
        for k, r in pool.imap_unordered(replay.run_task, tasks):
            if took(k, r):
                break
    finally:
        pool.terminate()
        pool.join()
    return out


# ---------------------------------------------------------------- walk-forward

SMALL = ("trades", "win_rate", "total_pnl", "return_pct", "mean_pnl", "mean_pnl_pct", "max_drawdown",
         "max_drawdown_pct", "profit_factor", "blind")


def small(m):
    return {k: m.get(k) for k in SMALL} if isinstance(m, dict) else None


def judge(cand, base_test, min_trades, margin_usd):
    """Why a candidate does NOT pass on the held-out part ([] when it passes)."""
    m, b = cand.get("test") or {}, base_test or {}
    why = []
    if (m.get("trades") or 0) < min_trades:
        why.append("only %d held-out trades (needs %d)" % (m.get("trades") or 0, min_trades))
    if not (m.get("mean_pnl") or 0) > 0:
        why.append("held-out mean %+.3f $ a trade after costs (needs above 0)" % (m.get("mean_pnl") or 0))
    need = (b.get("total_pnl") or 0) + margin_usd
    if not (m.get("total_pnl") or 0) >= need:
        why.append("held-out total %+.2f $ against the base's %+.2f $ (needs %+.2f $ or more)" % (
            m.get("total_pnl") or 0, b.get("total_pnl") or 0, need))
    return why


def backtest(data=None, since=None, until=None, grid="default", split=0.7, cfg=None, jobs=None, max_combos=200,
             budget_min=None, min_trades=30, margin_pct=2.0, top=5, seed=7, replay_opts=None, tape=None, out=None,
             live_check=True):
    """The walk-forward backtest -> the report dict (see the module's doc). tape: a replay.Tape already read
    (else read from data)."""
    say = out or (lambda *a: None)
    wall = time.monotonic()
    base_cfg = copy.deepcopy(cfg) if cfg is not None else bot.load_config(bot.CONFIG)
    tape = tape if tape is not None else replay.read_tape(data, since, until)
    t0, t1 = tape.span()
    since = t0 if since is None else since
    until = t1 if until is None else until
    opts = dict(replay_opts or {})
    start_bal = float(base_cfg.get("start_balance") or 100.0)
    margin_usd = start_bal * float(margin_pct) / 100.0
    report = {"generated": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z",
              "data": data, "tape": tape.summary(), "assumptions": ASSUMPTIONS,
              "costs": costs(base_cfg, replay.median(tape.sol[1])),
              "settings": {"split": split, "min_trades": min_trades, "margin_pct": margin_pct, "margin_usd": margin_usd,
                           "top": top, "max_combos": max_combos, "budget_min": budget_min, "seed": seed,
                           "replay": opts}}
    if since is None or until is None or until - since < 60:
        report.update(decision={"action": "keep", "why": "no tape to replay (%s)" % (data or "no data")}, top=[],
                      ranking=[], base=None, lines=["keep: no tape to replay"])
        return report
    split_t = round(since + float(split) * (until - since), 3)
    report["range"] = {"since": replay.iso(since), "until": replay.iso(until), "split_at": replay.iso(split_t),
                       "first_part_h": round((split_t - since) / 3600, 2), "held_out_h": round((until - split_t) / 3600, 2)}
    combos = build_grid(grid, base_cfg, max_combos, seed)
    cfgs = [apply(base_cfg, lv) for lv in combos]
    say("backtest · %s → %s · split at %s · %d configs · %s processes" % (
        replay.iso(since), replay.iso(until), replay.iso(split_t), len(combos), jobs or os.cpu_count()))

    def progress(done, n):
        if done == n or done % max(1, n // 10) == 0:
            say("  first part: %d/%d replays done (%.0f s)" % (done, n, time.monotonic() - wall))

    train = run_many(tape, [(i, cfgs[i], since, split_t, None, opts) for i in range(len(combos))], jobs,
                     budget_s=budget_min * 60 if budget_min else None, progress=progress)
    errors = {i: r["error"] for i, r in train.items() if r.get("error")}
    ranked = sorted([i for i, r in train.items() if i != 0 and not r.get("error")
                     and (r["metrics"]["trades"] or 0) >= min_trades],
                    key=lambda i: (-(train[i]["metrics"]["total_pnl"] or 0), -(train[i]["metrics"]["mean_pnl_pct"] or 0)))
    thin = sum(1 for i, r in train.items() if i != 0 and not r.get("error") and (r["metrics"]["trades"] or 0) < min_trades)
    picks = ranked[:max(0, int(top))]
    say("  ranked %d configs on the first part (%d had under %d trades, %d failed); held-out: base + %d" % (
        len(ranked), thin, min_trades, len(errors), len(picks)))
    test = run_many(tape, [(i, cfgs[i], split_t, until, (train.get(i) or {}).get("memory"), opts)
                           for i in [0] + picks], jobs)
    base_train = (train.get(0) or {}).get("metrics")
    base_test = (test.get(0) or {}).get("metrics")
    report["base"] = {"levers": {}, "first_part": base_train, "held_out": base_test,
                      "error": (train.get(0) or {}).get("error") or (test.get(0) or {}).get("error")}
    cands = []
    for rank, i in enumerate(picks, 1):
        c = {"rank": rank, "id": i, "levers": combos[i], "text": lever_text(combos[i]),
             "config_diff": config_diff(base_cfg, cfgs[i]), "first_part": train[i]["metrics"],
             "held_out": (test.get(i) or {}).get("metrics"), "error": (test.get(i) or {}).get("error")}
        c["test"] = c["held_out"] or {}
        c["why_not"] = judge(c, base_test, min_trades, margin_usd) if c["held_out"] else ["not replayed (budget)"]
        c["passes"] = not c["why_not"]
        cands.append(c)
    report["ranking"] = [{"rank": k + 1, "id": i, "levers": combos[i], "first_part": small(train[i]["metrics"])}
                         for k, i in enumerate(ranked[:20])]
    report["grid"] = {"configs": len(combos), "replayed": len(train), "ranked": len(ranked), "thin": thin,
                      "errors": {str(k): v for k, v in list(errors.items())[:10]},
                      "levers": sorted({k for lv in combos for k in lv}), "pseudo_levers": PSEUDO_HELP}
    win = next((c for c in cands if c["passes"]), None)
    if win is not None:
        report["decision"] = {"action": "change", "id": win["id"], "levers": win["levers"],
                              "config_diff": win["config_diff"],
                              "why": "rank %d on the first part; on the held-out part %d trades, mean %+.3f $ (%+.2f %%) a "
                                     "trade after costs, total %+.2f $ against the base's %+.2f $" % (
                                         win["rank"], win["test"]["trades"], win["test"]["mean_pnl"],
                                         win["test"]["mean_pnl_pct"], win["test"]["total_pnl"],
                                         (base_test or {}).get("total_pnl") or 0)}
    else:
        if not ranked:
            why = "no config made %d trades on the first part: too little data to judge (%s)" % (
                min_trades, "%d coins on the tape" % tape.summary()["coins"])
        else:
            why = "no top config passed on the held-out part: " + "; ".join(
                "#%d %s: %s" % (c["rank"], c["text"], ", ".join(c["why_not"])) for c in cands[:3])
        report["decision"] = {"action": "keep", "why": why}
    for c in cands:
        c.pop("test", None)
    report["top"] = cands
    lt = tape.live_trades(since, until)
    report["live"] = {"note": "the live bot's own trades in this range (the tape's buy / sell lines), for comparison "
                              "with the base config's replay (first part + held-out)",
                      "trades": replay.metrics(lt, start_bal) if lt else None,
                      "replay_base_trades": ((base_train or {}).get("trades") or 0) + ((base_test or {}).get("trades") or 0),
                      "live_config_differs": config_diff(base_cfg, dict(base_cfg, **(tape.live_cfg(since, until) or {})))
                      if tape.live_cfg(since, until) else None}
    if lt and live_check:
        # the replay itself checked: the whole range with the live run's own settings, cash and LEARNER memory,
        # trade by trade against what the live bot did (memebot/replay.py live_check)
        say("  live check: the live run's trades against their replay")
        report["live_check"] = replay.live_check(tape, base_cfg, since, until, **opts)
    report["wall_s"] = round(time.monotonic() - wall, 1)
    report["lines"] = text_lines(report)
    return report


def costs(cfg, sol=None):
    """The costs the engine charges, and what they come to on one order of the start balance's size."""
    sol = sol or replay.DEFAULT_SOL_USD
    order = float(cfg.get("start_balance") or 100) * float(cfg.get("position_pct") or 10) / 100.0
    fixed = (float(cfg.get("network_fee_sol") or 0) + float(cfg.get("priority_fee_sol") or 0)) * sol
    each = float(cfg.get("curve_fee_pct") or 0) + float(cfg.get("latency_slip_pct") or 0) + (fixed / order * 100 if order else 0)
    return {"curve_fee_pct_each_way": cfg.get("curve_fee_pct"), "latency_haircut_pct_each_way": cfg.get("latency_slip_pct"),
            "network_plus_priority_sol_per_order": round(float(cfg.get("network_fee_sol") or 0) +
                                                         float(cfg.get("priority_fee_sol") or 0), 6),
            "example": "a %.0f $ order at SOL %.0f $: about %.1f %% each way, %.1f %% a round trip (plus price impact)" % (
                order, sol, each, 2 * each)}


ASSUMPTIONS = [
    "Paper on paper: fills are the pump.fun curve maths on the last recorded reading at or before the fill (the live "
    "log showed it 0.0 % off Jupiter), less the engine's own latency haircut and network + priority fees. No failed "
    "transaction, no sandwich, no other bot reacting to ours.",
    "No look-ahead: every event is seen at or after its recorded time; RugCheck reports only from their recorded "
    "time; the held-out part is never used to rank.",
    "RugCheck: only reports the live bot fetched are known; a coin without one is not bought (like live with no "
    "report). Looser entry rules than the live ones can therefore look worse than they would be.",
    "A coin held past what the tape recorded of it gets no readings: it is sold on the clock's exits at its last "
    "recorded state and counted as a blind exit (its price is a guess).",
    "The jobs follow the recorded runs (start / stop lines) when the tape has them, else 340-minute jobs 2 minutes "
    "apart: no entries in a job's last 5 minutes, everything sold in its last one, like the live chain.",
    "The split is a job end for both parts (no entries in the 5 minutes before it, everything sold in its last "
    "minute, a fresh balance after it): a few trades fewer than the live run made around it.",
    "Selection bias: the best of many configs on the first part looks better than it is; a change is recommended "
    "only when it also wins on the held-out part. Even then it is a hint, not a promise. Not financial advice.",
]


def text_lines(r):
    """The report in a few lines for the log and the terminal."""
    out = []
    tp = r.get("tape") or {}
    rg = r.get("range") or {}
    out.append("tape %s → %s · %s coins · %s with RugCheck · split at %s" % (
        tp.get("from"), tp.get("to"), tp.get("coins"), tp.get("coins_with_rugcheck"), rg.get("split_at")))

    def fmt(m):
        if not m:
            return "no result"
        return "%d trades · win %s · total %+.2f $ · mean %+.2f %% · max DD %.2f $ · PF %s%s" % (
            m["trades"], "%.0f%%" % (m["win_rate"] * 100) if m.get("win_rate") is not None else "-", m["total_pnl"],
            m["mean_pnl_pct"] or 0, m["max_drawdown"], m["profit_factor"],
            " · %d blind" % m["blind"] if m.get("blind") else "")
    b = r.get("base") or {}
    out.append("base       first part: %s" % fmt(b.get("first_part")))
    out.append("           held-out:   %s" % fmt(b.get("held_out")))
    for c in r.get("top") or []:
        out.append("#%d %s" % (c["rank"], c["text"]))
        out.append("           first part: %s" % fmt(c.get("first_part")))
        out.append("           held-out:   %s%s" % (fmt(c.get("held_out")), "" if c.get("passes") else
                                                     " · no: " + "; ".join(c.get("why_not") or [])))
    lv = (r.get("live") or {}).get("trades")
    if lv:
        out.append("live bot   same range: %s (replay of the base: %d trades)" % (fmt(lv), r["live"]["replay_base_trades"]))
    if r.get("live_check"):
        out.append(replay.live_check_text(r["live_check"]))
    d = r.get("decision") or {}
    out.append("DECISION: %s · %s" % (d.get("action", "keep").upper(), d.get("why", "")))
    if d.get("action") == "change":
        out.append("config.json: %s" % json.dumps(d.get("config_diff"), separators=(",", ":")))
    out.append("(paper backtest on recorded data: a hint, not a promise)")
    return out


# ---------------------------------------------------------------- command line

def main(argv=None, out=print):
    ap = argparse.ArgumentParser(description="Meme Bot backtest: walk-forward replays of the New Pairs rules (paper).")
    ap.add_argument("--data", required=True, help="the bot's data dir (tapes in DATA/tapes/*.jsonl)")
    ap.add_argument("--since", help="UTC ISO time (default: the tape's start)")
    ap.add_argument("--until", help="UTC ISO time (default: the tape's end)")
    ap.add_argument("--grid", default="default", help="'default' or a JSON file ({\"levers\": ...} or {\"combos\": ...})")
    ap.add_argument("--split", type=float, default=0.7, help="the first part's share of the time range (ranking)")
    ap.add_argument("--out", help="write the report (JSON) here")
    ap.add_argument("--config", default=bot.CONFIG, help="the base config (default memebot/config.json)")
    ap.add_argument("--jobs", type=int, default=None, help="processes (default: the CPU count)")
    ap.add_argument("--max-combos", type=int, default=200)
    ap.add_argument("--budget-min", type=float, default=None, help="stop starting first-part replays after this")
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--margin-pct", type=float, default=2.0, help="of the start balance, over the base, held-out")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--job-min", type=float, default=340.0)
    ap.add_argument("--job-gap-s", type=float, default=120.0)
    ap.add_argument("--align", choices=("auto", "tape", "chain"), default="auto",
                    help="jobs: the recorded runs (tape), or job-min chains (chain); auto: tape when it has them")
    ap.add_argument("--rug-latency", type=float, default=0.4)
    ap.add_argument("--fill-delay", type=float, default=0.0, help="seconds between a decision and its fill's price")
    a = ap.parse_args(argv)
    if not 0.1 <= a.split <= 0.95:
        ap.error("--split between 0.1 and 0.95")
    os.environ["MEMEBOT_LIVE"] = "0"
    os.environ["MEMEBOT_FEEDS"] = "0"
    os.environ["MEMEBOT_REC"] = "0"
    since, until = replay.parse_time(a.since), replay.parse_time(a.until)
    t = time.monotonic()
    tape = replay.read_tape(a.data, since, until)
    out("tape read: %s (%.1f s)" % (json.dumps(tape.summary()["lines"]), time.monotonic() - t))
    rep = backtest(a.data, since, until, a.grid, a.split, bot.load_config(a.config), a.jobs, a.max_combos, a.budget_min,
                   a.min_trades, a.margin_pct, a.top, a.seed,
                   {"job_min": a.job_min, "job_gap_s": a.job_gap_s, "align": a.align, "rug_latency_s": a.rug_latency,
                    "fill_delay_s": a.fill_delay}, tape=tape, out=out)
    for line in rep["lines"]:
        out(line)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(replay.json_safe(rep), fh, indent=1, allow_nan=False, ensure_ascii=False)
        out("report: %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
