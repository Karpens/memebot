"""Meme Bot's NEWS hold plan: how far a coin may run before it is sold, decided by its NEWS score.

The base exit is +10 % (the scalp tier: everything is sold there). When the NEWS is strong, part
of the coin is held for +20 / +60 / +100 %:

    score  tier    ladder (gain %, fraction of what is LEFT to sell)       trail   time stop
    0-2    scalp   +10 all                                                   -     5 min
    3-4    swing   +10 50 %, +20 all                                        6 %    10 min
    5-6    runner  +10 34 %, +20 50 %, +60 all                             10 %    15 min
    7+     moon    +10 25 %, +20 34 %, +60 50 %, +100 all                  15 %    20 min

Before the first step: the hard stop (stop_pct, -7 %) and the tier's time stop (out after N
minutes unless +2 %). After the first step: no time stop, the stop for the rest moves to
break-even (entry +breakeven_pct, 1 %; bot.py raises it to where the whole trade is at 0 after
every cost) so a winner never turns into a loser, and a trailing stop trail % under the peak (on
price) also applies. While holding, the NEWS is read again every news_refresh_sec: the score may
move the coin up or down a tier (replan), and a coin whose buyers left while its news faded is
sold when it is in profit (fading).

Honest maths (the engine's own cost model, memebot/bot.py, checked against the first New Pairs
trades on GitHub Actions, 2026-11-26, SOL at $71): pump.fun's fee 1.25 % each way, the 1 % latency
haircut each way, the curve's price impact (about 0.4 % each way for a $10 order on a curve with
35 SOL of virtual reserves) and 0.001 SOL of network and priority fees an order ($0.07, 0.7 % of a
$10 order each way). A round trip at an unchanged price costs about 6.5 %. Gains are counted from
the fill price, which already holds the buy's costs, so a sale breaks even at about +4 %. The
first step used to be +7 %: it booked about +2.7 % (the live HOGZ trade +1.9 %: its sale filled
1.9 % under the reading), and after a partial +7 % sale the rest's break-even-after-costs stop sat
at +2.4 to +4 %, a normal pullback under the step. At +10 % the step books about +5.6 % sold
whole, and the rest's stop is +1 to +3 %, 7 to 9 points under the step. The stop: -7 % books
about -10.8 %, and the live stops gapped past it (readings of -8, -9 and -13 % booked -12, -13 and
-16 %). So a scalp still needs 2 trades in 3 to reach +10 % before the stop (+5.6 against -10.8;
with the live gaps and the HOGZ fill, +3.7 against -13.8: 4 in 5). No exit makes a coin-toss entry
pay: on a random walk every exit plan loses about the 6.5 % round trip. The ladder's later steps
(+20 / +60 / +100), the entry rules and the LEARNER are what can tilt it.

Pure functions on small dicts (the plan lives on the position in state.json), standard library
only, no I/O; garbage in (None, NaN, negative, text) never raises.

    info = info_from_facts(fx, trending, holder_signal)        # a DexScreener pair (bot.pair_facts)
    info = {"twitter": url, "telegram": url, "website": url,   # a new pair: metadata socials and
            "sol_in": 7.2, "buys": 14, "sells": 3}             # the curve's momentum since launch
    score, parts = news_score(info, cfg)
    plan = new_plan(score, parts, tiers_from_cfg(cfg), cfg)    # at entry, kept on the position
    plan_text(plan)        # "hold plan · 5/10 (X, Telegram, trending, buys 70%) · runner: +10 → +20 → +60"
    # every price reading of a held coin, same loop turn:
    track(plan, gain_pct, cfg)                                 # the peak and the stop for the panel
    d = decide_exit(plan, gain_pct, peak_gain_pct, held_min, cfg)
    if d and <sold d["frac"] of what is left>: mark_step(plan, d, cfg)   # a no-op for stops
    # every news_refresh_sec:
    plan, note = replan(plan, score_now, parts_now, gain_pct, cfg)    # then decide_exit again
    d = fade_exit(entry_info, now_info, gain_pct, cfg)
"""

import math

# The tiers, lowest first. A ladder step is [gain %, fraction of what is LEFT to sell]: runner
# sells 34 % at +10, then 50 % of the rest at +20, then all of it at +60. hold-spec.md had the first
# step at +7 %; it moved to +10 % for the costs (the module's docstring has the arithmetic): the
# shares and the later steps are the user's. config.json's hold_tiers are the same (a test checks).
# trail_pct 0 means no trailing stop (the scalp sells everything at its only step anyway).
DEFAULT_TIERS = [
    {"name": "scalp", "min_score": 0, "ladder": [[10.0, 1.0]], "trail_pct": 0.0, "time_stop_min": 5.0},
    {"name": "swing", "min_score": 3, "ladder": [[10.0, 0.5], [20.0, 1.0]], "trail_pct": 6.0, "time_stop_min": 10.0},
    {"name": "runner", "min_score": 5, "ladder": [[10.0, 0.34], [20.0, 0.5], [60.0, 1.0]], "trail_pct": 10.0,
     "time_stop_min": 15.0},
    {"name": "moon", "min_score": 7, "ladder": [[10.0, 0.25], [20.0, 0.34], [60.0, 0.5], [100.0, 1.0]],
     "trail_pct": 15.0, "time_stop_min": 20.0},
]

# config keys this module reads, with their defaults (cfg may leave any of them out)
CFG = {
    "stop_pct": -7.0,              # hard stop before the first step, % from entry
    "breakeven_pct": 1.0,          # after the first step the rest is sold at entry +1 % at worst
    "time_stop_below_pct": 2.0,    # the time stop fires only while the gain is under this
    "fade_buy_ratio": 0.45,        # fading: buys under 45 % of the last 5 minutes' trades ...
    "fade_score_drop": 2.0,        # ... and the score 2 or more under the score at entry
    "fade_min_gain_pct": 0.0,      # ... sells only above this gain (in profit)
    # NEWS score thresholds (hold-spec.md)
    "news_spike_x": 3.0,           # 5-minute volume against the hour's average
    "news_buy_ratio": 0.65,        # buys as a share of 5-minute trades (DexScreener pairs)
    "news_boosts_big": 50.0,       # this many DexScreener boosts score a second point
    "news_min_trades": 5.0,        # a buy share from fewer trades than this is unknown
    # the curve's momentum for new pairs (they have no DexScreener pair yet)
    "news_inflow_sol": 6.0,        # real SOL gained in the curve: 1 point ...
    "news_inflow_big_sol": 12.0,   # ... 2 points
    "news_curve_buy_ratio": 0.80,  # buy share of curve readings (the entry already needs 0.70)
}
MAX_SCORE = 10
EPS = 1e-9                         # boundaries are inclusive (+10.0 % is +10 %), floats or not
FULL = 1 - 1e-6                    # a fraction this close to 1 sells everything

# what a lost score part means, for the log ("downgrade runner → swing (boosts gone)")
LOST = {"trending": "no longer trending", "buys": "fewer buys", "volume": "volume cooled",
        "inflow": "inflow slowed", "holders": "holder growth stopped"}


# ---------------------------------------------------------------- guards

def _num(x):
    """A finite float, or None for None, NaN, inf, bools, huge ints and anything not a number."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def f(x, default=0.0):
    v = _num(x)
    return default if v is None else v


def _c(cfg, key):
    """A config number, or this module's default when it is missing or broken."""
    return f(cfg.get(key) if isinstance(cfg, dict) else None, CFG[key])


def _tidy(v):
    """5 instead of 5.0 in the log and the panel; one decimal for half points."""
    v = round(v, 1)
    return int(v) if v == int(v) else v


def _flag(x):
    """True for True or a positive number; text ("false" is truthy in Python) never counts."""
    if isinstance(x, bool):
        return x
    v = _num(x)
    return v is not None and v > 0


def _link(x):
    """A social link is there: a real text (metadata JSON often holds "", "null" or "none"), or True
    (bot.pair_facts gives the website as a bool)."""
    if x is True:
        return True
    if not isinstance(x, str):
        return False
    s = x.strip().lower()
    return len(s) >= 2 and s not in ("none", "null", "undefined", "n/a", "false", "nan")


def _score(x):
    return _tidy(min(MAX_SCORE, max(0.0, f(x))))


def _parts(x):
    if not isinstance(x, (list, tuple)):
        return []
    return [p[:40] for p in x if isinstance(p, str) and p][:16]


def _name(x, n=24):
    """A short text (a tier name, a log bit), or "" for anything that is not text."""
    return x.strip()[:n] if isinstance(x, str) else ""


def _key(part):
    """A part's family: "boosts 62" and "boosts 12" are both "boosts"."""
    return part.split(" ")[0]


# ---------------------------------------------------------------- the NEWS score

def twitter_kind(url):
    """"profile" for an account or community link, "status" for a post or a search (someone else's
    words about the coin: weak), None when there is no link."""
    if not isinstance(url, str) or not _link(url):
        return None
    s = url.strip().lower()
    if "/status/" in s or "/statuses/" in s or "/search" in s or "?q=" in s:
        return "status"
    return "profile"


def buy_share(info, min_trades=CFG["news_min_trades"]):
    """Buys as a share of trades (0-1): info["buy_ratio"] when it is a number in 0-1, else
    buys / (buys + sells) when there are at least min_trades of them, else None (unknown: two
    buys out of two trades say nothing)."""
    if not isinstance(info, dict):
        return None
    r = _num(info.get("buy_ratio"))
    if r is not None and 0.0 <= r <= 1.0:
        return r
    b, s = max(0.0, f(info.get("buys"))), max(0.0, f(info.get("sells")))
    if b + s >= max(1.0, f(min_trades, 1.0)):
        return b / (b + s)
    return None


def news_score(info, cfg=None):
    """(score 0-10, parts): the short names that scored, for the log.

    Socials: X account +1 (a status link to someone else's post +0.5), Telegram +1, website +1.
    Promotion: an active DexScreener boost +1, 50 or more +2; in a trending list +2.
    Momentum, from DexScreener for a migrated pair: 5-minute volume spike >= 3x +1, buys >= 65 % of
    5-minute trades +1. From the bonding curve for a new pair (info has "sol_in"; DexScreener has
    no pair for it yet): real SOL gained >= 6 +1, >= 12 +2, buys >= 80 % of the curve readings +1
    (the entry trigger already demands 70 %, so 65 % would score for every entry). Holder growth +1.
    The curve can score 11 in theory: the score is capped at 10."""
    if not isinstance(info, dict):
        return 0, []
    got = []                                    # (points, part)
    tw = info.get("twitter") or info.get("x")
    if isinstance(tw, str) and _link(tw):
        kind = info.get("twitter_kind")
        kind = kind if kind in ("profile", "status") else twitter_kind(tw)
        got.append((0.5, "X post") if kind == "status" else (1.0, "X"))
    if _link(info.get("telegram")):
        got.append((1.0, "Telegram"))
    if _link(info.get("website")) or _link(info.get("web")):
        got.append((1.0, "web"))
    boosts = int(min(1e9, max(0.0, f(info.get("boosts")))))
    if boosts >= _c(cfg, "news_boosts_big"):
        got.append((2.0, "boosts %d" % boosts))
    elif boosts >= 1:
        got.append((1.0, "boosts %d" % boosts))
    if _flag(info.get("trending")):
        got.append((2.0, "trending"))
    share = buy_share(info, _c(cfg, "news_min_trades"))
    if info.get("sol_in") is not None:          # a new pair: momentum from the curve
        sol = max(0.0, f(info.get("sol_in")))
        if sol >= _c(cfg, "news_inflow_big_sol") - EPS:
            got.append((2.0, "inflow %.1f SOL" % sol))
        elif sol >= _c(cfg, "news_inflow_sol") - EPS:
            got.append((1.0, "inflow %.1f SOL" % sol))
        need = _c(cfg, "news_curve_buy_ratio")
    else:                                       # a DexScreener pair
        spike = f(info.get("spike"))
        if spike >= _c(cfg, "news_spike_x") - EPS:
            got.append((1.0, "volume %.1fx" % spike))
        need = _c(cfg, "news_buy_ratio")
    if share is not None and share >= need - EPS:
        got.append((1.0, "buys %.0f%%" % (share * 100)))
    if _flag(info.get("holder_signal")):
        got.append((1.0, "holders"))
    return _score(sum(p for p, _ in got)), [name for _, name in got]


def info_from_facts(fx, trending=(), holder_signal=False):
    """news_score's input from what bot.pair_facts() reads off a DexScreener pair. The buy share
    comes from the 5-minute trade counts, so a pair with no trades reads "unknown", not 0 %."""
    if not isinstance(fx, dict):
        return {}
    # trending is a set (or list) of mints. Text is not one: "" or a short mint would match as a
    # substring and score +2; bytes would raise ValueError on an int mint.
    mint, tr = fx.get("mint"), False
    if isinstance(trending, bool):
        tr = trending
    elif isinstance(trending, (set, frozenset, list, tuple, dict)) and isinstance(mint, str) and mint:
        try:
            tr = mint in trending
        except Exception:                       # noqa: BLE001 - an odd element's == must not stop the scan
            tr = False
    info = {"twitter": fx.get("x") or "", "telegram": fx.get("telegram") or "", "website": fx.get("web"),
            "boosts": fx.get("boosts"), "trending": bool(tr), "spike": fx.get("spike"),
            "holder_signal": _flag(holder_signal)}
    if "buys5" in fx or "sells5" in fx:
        info["buys"], info["sells"] = fx.get("buys5"), fx.get("sells5")
    else:
        info["buy_ratio"] = fx.get("buy_ratio")
    return info


def news_bucket(score):
    """The LEARNER's label for a NEWS score."""
    s = f(score)
    return "news:0-2" if s < 3 else "news:3-4" if s < 5 else "news:5-6" if s < 7 else "news:7+"


# ---------------------------------------------------------------- tiers

def _ladder(x):
    """A clean ladder: [[gain %, frac]] with gain > 0 and frac in (0, 1], lowest gain first; broken
    steps are left out."""
    out = []
    for step in x if isinstance(x, (list, tuple)) else []:
        if not isinstance(step, (list, tuple)) or len(step) < 2:
            continue
        g, fr = _num(step[0]), _num(step[1])
        if g is None or fr is None or g <= 0 or fr <= 0:
            continue
        out.append([g, 1.0 if fr >= FULL else fr])
    out.sort(key=lambda s: s[0])
    return out


def _tier(t):
    """A clean copy of one tier, or None when it has no name or no usable ladder."""
    if not isinstance(t, dict):
        return None
    name, lad = t.get("name"), _ladder(t.get("ladder"))
    if not isinstance(name, str) or not name.strip() or not lad:
        return None
    return {"name": name.strip()[:24], "min_score": max(0.0, f(t.get("min_score"))), "ladder": lad,
            "trail_pct": min(99.0, max(0.0, f(t.get("trail_pct")))),
            "time_stop_min": max(0.0, f(t.get("time_stop_min")))}


def _clean_tiers(tiers):
    """The usable tiers, lowest min_score first; [] when none is usable."""
    out = [t for t in (_tier(x) for x in (tiers if isinstance(tiers, (list, tuple)) else [])) if t]
    return sorted(out, key=lambda t: t["min_score"])


def tiers_from_cfg(cfg):
    """The tiers the bot plays: cfg["hold_tiers"]; without it one "base" tier from the old exit
    keys (tp1_pct, tp1_sell_frac, trail_pct, time_stop_min), so a config without hold_tiers exits
    as before; without those either, DEFAULT_TIERS."""
    cfg = cfg if isinstance(cfg, dict) else {}
    got = _clean_tiers(cfg.get("hold_tiers"))
    if got:
        return got
    if _num(cfg.get("tp1_pct")) is not None:
        step = [cfg.get("tp1_pct"), f(cfg.get("tp1_sell_frac"), 1.0)]
        base = _tier({"name": "base", "min_score": 0, "ladder": [step], "trail_pct": cfg.get("trail_pct"),
                      "time_stop_min": cfg.get("time_stop_min")})
        if base:
            return [base]
    return _clean_tiers(DEFAULT_TIERS)


def pick_tier(score, tiers=None):
    """The first tier from the top whose min_score <= score (a fresh copy); the lowest tier when the
    score is under all of them; DEFAULT_TIERS when tiers is missing or broken."""
    ts = _clean_tiers(tiers) or _clean_tiers(DEFAULT_TIERS)
    s = max(0.0, f(score))
    for t in reversed(ts):
        if t["min_score"] <= s + EPS:
            return t
    return ts[0]


def ladder_text(ladder):
    """"+10 → +20 → +60"."""
    return " → ".join("+%g" % g for g, _ in _ladder(ladder)) or "-"


# ---------------------------------------------------------------- the plan

def new_plan(score, parts, tiers=None, cfg=None):
    """The hold plan at entry. "step" counts the ladder steps sold, "stop_pct" is the stop now as a
    % from entry (the hard stop until the first step), "peak_gain" the best gain seen. Extra keys:
    "entry_score" and "entry_tier" (for fading and the LEARNER: a replan changes "score" and
    "tier", not these), "done_gain" (the highest step gain sold: steps already sold stay sold when
    the tier changes) and "sold" (ladder steps sold, for the trade)."""
    s = _score(score)
    t = pick_tier(s, tiers if tiers is not None else tiers_from_cfg(cfg))
    return {"tier": t["name"], "score": s, "parts": _parts(parts), "ladder": t["ladder"], "step": 0,
            "stop_pct": _c(cfg, "stop_pct"), "trail_pct": t["trail_pct"], "time_stop_min": t["time_stop_min"],
            "peak_gain": 0.0, "entry_score": s, "entry_tier": t["name"], "done_gain": 0.0, "sold": 0}


def _read(plan):
    """A plan's numbers, repaired (a plan comes back from state.json and may be old or hand-edited)."""
    lad = _ladder(plan.get("ladder"))
    step = int(min(len(lad), max(0.0, f(plan.get("step")))))
    done = max(0.0, f(plan.get("done_gain")), lad[step - 1][0] if step else 0.0)
    return {"ladder": lad, "step": step, "done": done, "sold": step > 0 or done > 0,
            "trail": min(99.0, max(0.0, f(plan.get("trail_pct")))),
            "time_stop": max(0.0, f(plan.get("time_stop_min"))), "peak": f(plan.get("peak_gain"))}


def _trail_level(peak, trail):
    """The trailing stop as a gain %: trail % under the peak PRICE, so a +50 % peak with a 10 % trail
    stops at +35 % (1.5 x 0.9), not at +40 %."""
    return ((1 + peak / 100.0) * (1 - trail / 100.0) - 1) * 100.0


def stop_now(plan, cfg=None, peak_gain_pct=None):
    """The stop now as a % from entry: the hard stop until the first step is sold; after it the
    higher of break-even and the trailing stop under the peak."""
    if not isinstance(plan, dict):
        return _c(cfg, "stop_pct")
    p = _read(plan)
    hard = _c(cfg, "stop_pct")
    if not p["sold"]:
        return hard
    peak = max(p["peak"], f(peak_gain_pct, p["peak"]), p["done"])
    floor = max(hard, _c(cfg, "breakeven_pct"))
    if p["trail"] > 0:
        floor = max(floor, _trail_level(peak, p["trail"]))
    return round(floor, 2)


def track(plan, gain_pct, cfg=None):
    """On every reading: raise the plan's peak gain and refresh its stop for the panel. In place;
    returns the plan."""
    g = _num(gain_pct)
    if isinstance(plan, dict) and g is not None:
        plan["peak_gain"] = round(max(f(plan.get("peak_gain")), g), 4)
        plan["stop_pct"] = stop_now(plan, cfg)
    return plan


def _out(kind, why):
    return {"frac": 1.0, "why": why, "final": True, "step": None, "kind": kind}


def _take(p, gain):
    """The ladder steps the gain has reached, sold as one order: the fractions chain on what is
    left, so a jump from +5 to +25 on a runner sells 1 - 0.66 x 0.5 = 67 % at once (first out)."""
    lad, i, left = p["ladder"], p["step"], 1.0
    while i < len(lad) and gain >= lad[i][0] - EPS:
        left *= 1 - lad[i][1]
        i += 1
    if i == p["step"]:
        return None
    frac = 1.0 if 1 - left >= FULL else round(1 - left, 6)
    top = lad[i - 1][0]
    after = "stop to break-even" if not p["sold"] and frac < 1 else ""
    return {"frac": frac, "why": "TP +%g%%" % top, "final": frac >= 1, "step": i, "kind": "take", "gain": top,
            "steps": i - p["step"], "after": after}


def decide_exit(plan, gain_pct, peak_gain_pct, held_min, cfg=None):
    """None, or {"frac": fraction of what is LEFT to sell, "why": short English reason, "final": the
    last sale, "step": the step count to mark for a ladder sale (None for a stop), "kind": "stop" |
    "take" | "time" | "trail" | "breakeven"}; a ladder sale also carries "gain", "steps" (how many
    steps it sells at once) and "after" ("stop to break-even" on the first one).

    Pure: the plan is not changed; after the sale went through, mark_step(plan, d). A sale that
    failed (no route) is simply decided again on the next reading.

    Order, before the first step: the hard stop, the ladder, the tier's time stop (held_min >=
    time_stop_min and the gain under time_stop_below_pct). After it: the ladder, then the trailing
    stop (trail % under the peak price) when it is above break-even, then break-even. The peak is
    the highest of peak_gain_pct, the plan's peak_gain, the highest step sold and this gain."""
    if not isinstance(plan, dict):
        return None
    gain = _num(gain_pct)
    if gain is None:                    # no price, no decision
        return None
    p = _read(plan)
    peak = max(gain, f(peak_gain_pct, gain), p["peak"], p["done"])     # a step sold at +20 means a +20 peak
    held = max(0.0, f(held_min))
    hard = _c(cfg, "stop_pct")
    if not p["sold"] and gain <= hard + EPS:
        return _out("stop", "stop %+.0f%%" % gain)
    take = _take(p, gain)
    if take:
        return take
    if not p["sold"]:
        ts = p["time_stop"]
        if ts > 0 and held >= ts - EPS and gain < _c(cfg, "time_stop_below_pct") - EPS:
            return _out("time", "%d min, only %+.0f%%" % (int(held), gain))
        return None
    be = max(hard, _c(cfg, "breakeven_pct"))
    trail = p["trail"]
    if trail > 0 and _trail_level(peak, trail) > be and \
            1 + gain / 100.0 <= (1 + peak / 100.0) * (1 - trail / 100.0) + EPS:
        return _out("trail", "trailing stop %g%% off the top (peak %+.0f%%)" % (trail, peak))
    if gain <= be + EPS:
        return _out("breakeven", "break-even stop (peak %+.0f%%)" % peak)
    return None


def mark_step(plan, step=None, cfg=None):
    """After a ladder sale went through: step is decide_exit's decision (or its "step" count); None
    advances one step. A decision without a step (a stop) changes nothing. Never goes back. In
    place; returns the plan."""
    if not isinstance(plan, dict):
        return plan
    if isinstance(step, dict):
        if step.get("step") is None:
            return plan
        step = step.get("step")
    p = _read(plan)
    lad, cur = p["ladder"], p["step"]
    want = cur + 1 if _num(step) is None else int(min(1e6, max(0.0, f(step))))
    new = max(cur, min(len(lad), want))
    plan["step"] = new
    if new > cur:
        plan["sold"] = int(min(1e6, max(0.0, f(plan.get("sold"))))) + new - cur
        plan["done_gain"] = max(p["done"], lad[new - 1][0])
    plan["stop_pct"] = stop_now(plan, cfg)
    return plan


def take_text(d):
    """The EXECUTOR line for a ladder sale: "TP +10% · sold 34% · stop to break-even"."""
    if not isinstance(d, dict):
        return ""
    sold = "sold %.0f%%" % (min(1.0, max(0.0, f(d.get("frac")))) * 100)
    if f(d.get("steps"), 1) > 1:
        sold += " (%d steps)" % int(min(1e6, f(d.get("steps"))))
    why, after = _name(d.get("why"), 80) or "TP", _name(d.get("after"), 80)
    return " · ".join([why, sold] + ([after] if after else []))


def plan_text(plan):
    """The NEWS line at entry: "hold plan · 5/10 (X, Telegram, trending, buys 70%) · runner: +10 → +20 → +60"."""
    if not isinstance(plan, dict):
        return "hold plan · none"
    parts = _parts(plan.get("parts"))
    return "hold plan · %s/10 (%s) · %s: %s" % (_score(plan.get("score")), ", ".join(parts) or "no news",
                                                plan.get("tier") or "?", ladder_text(plan.get("ladder")))


def labels(plan):
    """The LEARNER's two labels at entry: "tier:<name>" and the NEWS bucket, both of the entry
    (entry_tier / entry_score), so a call after a replan still gives the pair the entry had, never
    "tier:runner" with "news:3-4". A plan without them (an old one) uses tier / score."""
    if not isinstance(plan, dict):
        return []
    score = plan.get("entry_score") if _num(plan.get("entry_score")) is not None else plan.get("score")
    tier = _name(plan.get("entry_tier")) or _name(plan.get("tier")) or "?"
    return ["tier:%s" % tier, news_bucket(score)]


def panel_plan(plan):
    """The plan as the panel shows it on a position (small: the tick message carries it)."""
    if not isinstance(plan, dict):
        return None
    p = _read(plan)
    # the tier as text: a NaN from a hand-edited state.json would write NaN into panel.json (bot.py
    # dumps it without allow_nan=False), which the page's JSON.parse rejects
    return {"tier": _name(plan.get("tier")) or "?", "score": _score(plan.get("score")),
            "parts": _parts(plan.get("parts")),
            "ladder": p["ladder"], "step": p["step"], "stop_pct": round(f(plan.get("stop_pct"), CFG["stop_pct"]), 2),
            "trail_pct": p["trail"]}


# ---------------------------------------------------------------- NEWS refresh while holding

def _change(old_parts, new_parts, up, old_score, new_score):
    """Why the tier changed, in a few words: the parts gained (upgrade) or lost (downgrade)."""
    old_k = {_key(p): p for p in old_parts}
    new_k = {_key(p): p for p in new_parts}
    if up:
        bits = [p for k, p in new_k.items() if k not in old_k]
    else:
        bits = [LOST.get(k, "%s gone" % k) for k in old_k if k not in new_k]
    # nothing came or went: a part that moved ("boosts 12" → "boosts 62") says it
    bits = bits or [p for k, p in new_k.items() if k in old_k and old_k[k] != p]
    return ", ".join(bits) or "score %s → %s" % (old_score, new_score)


def replan(plan, new_score, new_parts, gain_pct, cfg=None):
    """(plan, note or None) after a NEWS refresh. The score and parts are always updated; the tier
    changes when the score now picks another one:
    - steps already sold stay sold: on the new ladder every step at or below the highest gain sold
      counts as done, except a step that sells everything (the coin is still held). Upgrade swing
      (sold at +10) → runner: next +20 sells 50 %. Downgrade moon (sold +10 and +20) → swing: next
      is swing's +20 that sells the rest;
    - a downgrade whose next step the gain has already passed says "sell now" in the note: call
      decide_exit on the same reading and it sells that step;
    - the stop regime does not go back: once a step was sold, break-even holds on any tier.
    An unknown score (None, NaN, text) keeps the plan as it is. No plan yet (an old position): a
    new plan. In place; returns the plan."""
    tiers = tiers_from_cfg(cfg)
    s = _num(new_score)
    if not isinstance(plan, dict):
        return new_plan(s, new_parts, tiers, cfg), None
    if s is None:
        return plan, None
    s, parts = _score(s), _parts(new_parts)
    old_score, old_parts, old_name = _score(plan.get("score")), _parts(plan.get("parts")), plan.get("tier")
    plan["score"], plan["parts"] = s, parts
    t = pick_tier(s, tiers)
    if t["name"] == old_name:
        return plan, None
    old_t = next((x for x in tiers if x["name"] == old_name), None)
    up = t["min_score"] > old_t["min_score"] if old_t else s > old_score
    p = _read(plan)
    lad = t["ladder"]
    step = sum(1 for g, _ in lad if g <= p["done"] + EPS) if p["sold"] else 0
    while step and lad[step - 1][1] >= 1:      # a sell-everything step can't be done while we hold
        step -= 1
    plan.update({"tier": t["name"], "ladder": lad, "step": step, "trail_pct": t["trail_pct"],
                 "time_stop_min": t["time_stop_min"], "done_gain": p["done"]})
    plan["stop_pct"] = stop_now(plan, cfg)
    note = "%s %s → %s (%s)" % ("upgrade" if up else "downgrade", old_name, t["name"],
                                _change(old_parts, parts, up, old_score, s))
    g = _num(gain_pct)
    if not up and g is not None and step < len(lad) and g >= lad[step][0] - EPS:
        note += " · %+.0f%% is past +%g%%: sell now" % (g, lad[step][0])
    return plan, note


def _entry(entry_info, cfg):
    """(score, parts) at entry: from the info at entry, or a number (the plan's entry_score)."""
    v = _num(entry_info)
    return (_score(v), []) if v is not None else news_score(entry_info, cfg)


def fading(entry_info, now_info, cfg=None):
    """The buyers left and the news faded: buys under fade_buy_ratio (0.45) of the last 5 minutes'
    trades AND the NEWS score fell by fade_score_drop (2) or more since entry. An unknown buy share
    (too few trades) is not fading. now_info's momentum (buys/sells or buy_ratio, sol_in) should
    cover the last 5 minutes; entry_info is the info at entry, or the plan's entry_score."""
    share = buy_share(now_info, _c(cfg, "news_min_trades"))
    if share is None or share >= _c(cfg, "fade_buy_ratio") - EPS:
        return False
    drop = f(_entry(entry_info, cfg)[0]) - f(news_score(now_info, cfg)[0])
    return drop >= _c(cfg, "fade_score_drop") - EPS


def fade_exit(entry_info, now_info, gain_pct, cfg=None):
    """fading() as an exit: sell the rest, only when the gain is above fade_min_gain_pct (in profit);
    at a loss the stops keep the coin. "fading: buys 40%, no longer trending → out"."""
    g = _num(gain_pct)
    if g is None or g <= _c(cfg, "fade_min_gain_pct") or not fading(entry_info, now_info, cfg):
        return None
    share = buy_share(now_info, _c(cfg, "news_min_trades"))
    old_k = {_key(p) for p in _entry(entry_info, cfg)[1]}
    new_k = {_key(p) for p in news_score(now_info, cfg)[1]}
    lost = [LOST.get(k, "%s gone" % k) for k in sorted(old_k - new_k) if k != "buys"]
    why = "fading: buys %.0f%%%s → out" % (share * 100, "".join(", " + x for x in lost))
    return _out("fade", why)
