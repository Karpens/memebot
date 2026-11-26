"""Meme Bot's LEARNER: it learns from its own closed trades which kinds of coins lose.

A candidate is described by a few coarse buckets ("liq:<20k", "age:<30m", "sig:trending",
"hour:01-03" ...; a New Pair by "np:dev:0-2", "np:sol:3-6", "news:3-4", "tier:swing" ...).
When a trade closes, its P&L % goes into the stats of the buckets it had at entry. Before a buy, the learner scores the candidate: each bucket's mean P&L, shrunk towards 0
by k imaginary flat trades (sum / (n + k)), averaged over the buckets with at least min_n trades.
A clearly bad score skips the coin, but every 5th such skip is still bought at half size, so a
bucket that once went bad can show that it got better. Any other score scales the size.

Pure functions on a small dict (memory.json in the data dir, kept for months), standard library
only. Never rename a label: the memory would silently forget what it learned under the old name
(the test that lists every label is there to stop that).

    labels = features(fx, holders, slip_pct, signals, hour)   # at entry, kept on the position
    d = decide(mem, labels, cfg)                              # buy / skip / explore, and a size mult
    update(mem, labels, pnl_pct)                              # when the trade closes
    summary(mem)                                              # what the page shows
"""

import datetime
import json
import math
import os

try:
    from zoneinfo import ZoneInfo
except ImportError:                     # pragma: no cover - Python 3.8
    ZoneInfo = None

# the buckets: (upper bound, exclusive; None closes the table, label). Few buckets per family,
# so 5-40 trades a night fill them: a bucket counts in the score only from min_n trades on.
LIQ = ((20000.0, "liq:<20k"), (50000.0, "liq:20-50k"), (None, "liq:50k+"))
VOL5 = ((5000.0, "vol5:<5k"), (20000.0, "vol5:5-20k"), (None, "vol5:20k+"))
BUYS = ((0.65, "buys:<65"), (0.80, "buys:65-80"), (None, "buys:80+"))
AGE = ((30.0, "age:<30m"), (120.0, "age:30m-2h"), (None, "age:2h+"))
HOLDERS = ((300.0, "holders:<300"), (1000.0, "holders:300-1k"), (None, "holders:1k+"))
SLIP = ((1.0, "slip:<1"), (2.0, "slip:1-2"), (None, "slip:2+"))
# DexScreener's dexId by prefix ("meteoradbc" is meteora too); anything else is dex:other
DEXES = (("pumpfun", "dex:pumpfun"), ("pumpswap", "dex:pumpswap"), ("raydium", "dex:raydium"),
         ("meteora", "dex:meteora"))
OTHER_DEX = "dex:other"
# the scanner's signal texts by prefix ("volume 2.3x", "holders +7%"); a coin can have several
SIGNALS = (("trending", "sig:trending"), ("boosted", "sig:boosted"), ("volume", "sig:volume"),
           ("holders", "sig:holders"))
SOCIAL = ("social:x", "social:no-x", "social:none")
# two-hour blocks of the night: one bucket per hour would take a week of nights to fill
NIGHT_HOURS = {23: "hour:23-01", 0: "hour:23-01", 1: "hour:01-03", 2: "hour:01-03",
               3: "hour:03-05", 4: "hour:03-05", 5: "hour:05-07", 6: "hour:05-07"}
DAY = "hour:day"                        # --anytime test runs, kept apart from the night

# New Pairs (memebot/newpairs.py labels(), on the pump.fun curve) and the NEWS hold plan (memebot/plan.py):
# the creator's initial buy, the real SOL in since launch, the buy share of the curve readings, the age at
# entry, the coin's links, the NEWS score bucket and the hold plan's tier at entry. Fixed sets like the
# others, so the memory stays bounded (a tier renamed in config.json's hold_tiers is not learned).
NP_DEV = ("np:dev:0-2", "np:dev:2-5", "np:dev:5-8", "np:dev:8+")
NP_SOL = ("np:sol:<3", "np:sol:3-6", "np:sol:6-12", "np:sol:12+")
NP_BUYS = ("np:buys:<70", "np:buys:70-80", "np:buys:80-90", "np:buys:90+")
NP_AGE = ("np:age:<30s", "np:age:30-90s", "np:age:90s+")
NP_SOCIAL = ("np:social:x", "np:social:tg", "np:social:web", "np:social:none")
NEWS = ("news:0-2", "news:3-4", "news:5-6", "news:7+")
TIERS = ("tier:scalp", "tier:swing", "tier:runner", "tier:moon", "tier:base")

KNOWN = frozenset([lab for table in (LIQ, VOL5, BUYS, AGE, HOLDERS, SLIP) for _, lab in table]
                  + [lab for _, lab in DEXES + SIGNALS] + [OTHER_DEX] + list(SOCIAL)
                  + list(NIGHT_HOURS.values()) + [DAY]
                  + list(NP_DEV + NP_SOL + NP_BUYS + NP_AGE + NP_SOCIAL + NEWS + TIERS))

K, MIN_N = 10, 8
CFG = {"learn_skip_pct": -6.0, "learn_explore_every": 5, "learn_k": 10, "learn_min_n": 8,
       "learn_mult_min": 0.5, "learn_mult_max": 1.5}
MULT_PER = 20.0                         # size x (1 + score / 20): buckets that made +10 % a trade buy 1.5x
EXPLORE_MULT = 0.5
PNL_CAP = 100.0                         # one +900 % moon shot must not bless its buckets for weeks
# a bucket's n stops growing here, so old nights fade (about 10 nights for the busiest buckets:
# the meme market of March is not the one of November)
FORGET_AT = 300.0


def fresh():
    return {"v": 1, "labels": {}, "samples": 0, "skipped": 0, "explored": 0, "skip_run": 0}


# ---------------------------------------------------------------- guards

def _num(x):
    """A finite float, or None for None, NaN, inf, bools and anything that is not a number."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def _count(x):
    v = _num(x)
    return int(v) if v is not None and v > 0 else 0


def _tidy(v):
    """Short numbers in the file: 8 instead of 8.0, four decimals at most."""
    v = round(v, 4)
    return int(v) if v == int(v) else v


def _on(v):
    """A social link is there: a non-empty text or a true value (NaN is truthy in Python: not here)."""
    if isinstance(v, float):
        return math.isfinite(v) and v != 0
    if isinstance(v, str):
        return bool(v.strip())
    return bool(v)


def _labels(labels):
    """Known labels only, each once: whatever comes in, the memory holds at most len(KNOWN) labels."""
    if isinstance(labels, str):
        labels = [labels]
    if not isinstance(labels, (list, tuple, set, frozenset)):
        return []
    return [lab for lab in dict.fromkeys(x for x in labels if isinstance(x, str)) if lab in KNOWN]


def _table(mem):
    labels = mem.get("labels") if isinstance(mem, dict) else None
    return labels if isinstance(labels, dict) else {}


def _clean_stat(st):
    """A label's stats as floats, or None when they are missing or broken."""
    if not isinstance(st, dict):
        return None
    n, s = _num(st.get("n")), _num(st.get("sum"))
    if n is None or s is None or n <= 0:
        return None
    sq, w = _num(st.get("sumsq")), _num(st.get("wins"))
    return {"n": n, "sum": s, "sumsq": max(sq or 0.0, 0.0), "wins": min(max(w or 0.0, 0.0), n)}


def _ensure(mem):
    """Repairs a memory in place: an old or hand-edited file must not break a trade."""
    if not isinstance(mem.get("labels"), dict):
        mem["labels"] = {}
    for key in ("samples", "skipped", "explored", "skip_run"):
        mem[key] = _count(mem.get(key))
    mem.setdefault("v", 1)


def _gate(k, min_n):
    """(k >= 0, min_n >= 1 as an int); the defaults for anything broken."""
    k, min_n = _num(k), _num(min_n)
    k = K if k is None else max(0.0, k)
    min_n = MIN_N if min_n is None else max(1, int(math.ceil(min_n)))
    return _tidy(k), min_n


# ---------------------------------------------------------------- features

def _bucket(v, table):
    for bound, label in table:
        if bound is None or v < bound:
            return label


def _dex(dex):
    if not isinstance(dex, str) or not dex.strip():
        return []
    d = dex.strip().lower()
    return [next((lab for prefix, lab in DEXES if d.startswith(prefix)), OTHER_DEX)]


def _social(fx):
    if not any(k in fx for k in ("x", "telegram", "web")):
        return []
    if _on(fx.get("x")):
        return ["social:x"]
    return ["social:no-x" if _on(fx.get("telegram")) or _on(fx.get("web")) else "social:none"]


def _signals(signals):
    if isinstance(signals, str):                # a reason text: "buys 80% · volume 6.0x"
        signals = signals.replace(",", "·").split("·")
    if not isinstance(signals, (list, tuple, set, frozenset)):
        return []
    said = [s.strip().lower() for s in signals if isinstance(s, str)]
    return [lab for prefix, lab in SIGNALS if any(s.startswith(prefix) for s in said)]


def _hour(hour):
    h = _num(getattr(hour, "hour", hour))       # an int, or a datetime
    if h is None:
        return []
    return [NIGHT_HOURS.get(int(h) % 24, DAY)]


def features(fx, holders, slip_pct, signals, hour):
    """The candidate's labels, in a fixed family order. fx is bot.pair_facts(), holders RugCheck's
    count, slip_pct the entry quote's price impact in %, signals the scanner's signal texts, hour
    the local hour (0-23). A missing or broken input leaves its family out (no label beats a wrong
    one), so features(None, None, None, None, 23) is just ["hour:23-01"]: the backfill of old trades."""
    fx = fx if isinstance(fx, dict) else {}
    out = []
    for key, table in (("liquidity", LIQ), ("vol5", VOL5), ("buy_ratio", BUYS), ("age_min", AGE)):
        v = _num(fx.get(key))
        if key == "buy_ratio" and v is not None and v > 1:
            v /= 100.0                          # a percent instead of pair_facts' fraction
        if v is not None:
            out.append(_bucket(v, table))
    n = _num(holders)
    if n is not None and n > 0:                 # 0 is "RugCheck gave no count", not an empty coin
        out.append(_bucket(n, HOLDERS))
    out += _dex(fx.get("dex")) + _social(fx) + _signals(signals)
    slip = _num(slip_pct)
    if slip is not None:
        out.append(_bucket(slip, SLIP))
    return out + _hour(hour)


# ---------------------------------------------------------------- learning and scoring

def update(mem, labels, pnl_pct):
    """A closed trade: its P&L % (fees included) goes into every label it had at entry."""
    pnl, labels = _num(pnl_pct), _labels(labels)
    if not isinstance(mem, dict) or pnl is None or not labels:
        return
    _ensure(mem)
    pnl = max(-PNL_CAP, min(PNL_CAP, pnl))
    mem["samples"] += 1
    for lab in labels:
        st = _clean_stat(mem["labels"].get(lab)) or {"n": 0.0, "sum": 0.0, "sumsq": 0.0, "wins": 0.0}
        st["n"] += 1
        st["sum"] += pnl
        st["sumsq"] += pnl * pnl
        st["wins"] += 1 if pnl > 0 else 0
        if st["n"] > FORGET_AT:                 # the mean stays, the weight of old trades fades
            fade = FORGET_AT / st["n"]
            st = {k: v * fade for k, v in st.items()}
        mem["labels"][lab] = {k: _tidy(v) for k, v in st.items()}


def _rows(mem, labels, k, min_n):
    """(label, n, mean, shrunk mean) of the labels with at least min_n trades."""
    table, out = _table(mem), []
    for lab in labels:
        st = _clean_stat(table.get(lab))
        if st and st["n"] >= min_n:
            out.append((lab, st["n"], st["sum"] / st["n"], st["sum"] / (st["n"] + k)))
    return out


def _why(rows, s):
    """The two labels that pull the score its way: the worst two of a bad score, the best two of a good one."""
    rows = sorted(rows, key=lambda r: (r[3], r[0]) if s < 0 else (-r[3], r[0]))[:2]
    return ", ".join("%s %+.1f%% over %d" % (lab, mean, round(n)) for lab, n, mean, _ in rows)


def score(mem, labels, k=K, min_n=MIN_N):
    """(score %, why): the mean of the shrunk means sum / (n + k) of the candidate's labels with
    min_n trades or more, and the two labels that weigh most; (0.0, "") when none has enough.
    The k imaginary flat trades keep a small sample modest: 8 trades at -10 % score -4.4 %,
    40 of them -8 %."""
    k, min_n = _gate(k, min_n)
    rows = _rows(mem, _labels(labels), k, min_n)
    if not rows:
        return 0.0, ""
    s = sum(r[3] for r in rows) / len(rows)
    return s, _why(rows, s)


def _cfg(cfg):
    """The learner's settings from the bot's config, a default for anything missing or broken."""
    c = cfg if isinstance(cfg, dict) else {}

    def get(key):
        v = _num(c.get(key))
        return CFG[key] if v is None else v

    k, min_n = _gate(get("learn_k"), get("learn_min_n"))
    lo, hi = sorted((max(0.0, get("learn_mult_min")), max(0.0, get("learn_mult_max"))))
    return {"skip": get("learn_skip_pct"), "every": int(get("learn_explore_every")), "k": k,
            "min_n": min_n, "lo": lo, "hi": hi}


def decide(mem, labels, cfg):
    """Before a buy -> {"action": "buy" | "skip" | "explore", "mult": the size multiplier, "score",
    "why": text for the LEARNER log line after "<action> · "}. A score at or under learn_skip_pct
    skips the coin, except that every learn_explore_every-th such skip buys it at half size
    (without those trades a bucket that went bad once could never show it got better). Any other
    score sizes the buy by clamp(1 + score / 20, learn_mult_min, learn_mult_max). The skip count
    lives in mem: save mem after a skip or an explore too."""
    c = _cfg(cfg)
    if isinstance(mem, dict):
        _ensure(mem)
        mem["k"], mem["min_n"] = c["k"], c["min_n"]         # summary() shows the same gate
    rows = _rows(mem, _labels(labels), c["k"], c["min_n"])
    if not rows:                                # nothing to go on yet: a plain buy
        return {"action": "buy", "mult": 1.0, "score": 0.0, "why": "no bucket has %d trades yet" % c["min_n"]}
    s = sum(r[3] for r in rows) / len(rows)
    why = _why(rows, s)
    past = "past trades like this %s" % ("lost avg %.1f%%" % -s if s < 0 else "made avg only %+.1f%%" % s)
    if s <= c["skip"]:
        mem["skip_run"] += 1
        if c["every"] > 0 and mem["skip_run"] >= c["every"]:
            mem["skip_run"] = 0
            mem["explored"] += 1
            return {"action": "explore", "mult": EXPLORE_MULT, "score": round(s, 2),
                    "why": "1 in %d skips is bought at half size · %s · %s" % (c["every"], past, why)}
        mem["skipped"] += 1
        return {"action": "skip", "mult": 0.0, "score": round(s, 2), "why": "%s · %s" % (past, why)}
    mult = min(c["hi"], max(c["lo"], 1 + s / MULT_PER))
    return {"action": "buy", "mult": round(mult, 3), "score": round(s, 2),
            "why": "score %+.1f%% · size x%.2f · %s" % (s, mult, why)}


def learned(mem, labels):
    """For the close log line: the trade's label the memory now rates most strongly (furthest from
    0 after shrinking), as "age:<30m now avg -9.1% over 13"; "" when none has data."""
    k, _ = _gate(mem.get("k") if isinstance(mem, dict) else None, 1)
    rows = _rows(mem, _labels(labels), k, 1)
    if not rows:
        return ""
    lab, n, mean, _ = max(rows, key=lambda r: (abs(r[3]), r[0]))
    return "%s now avg %+.1f%% over %d" % (lab, mean, round(n))


def _row_doc(row):
    return {"label": row[0], "n": int(round(row[1])), "avg": round(row[2], 1)}


def summary(mem, k=None, min_n=None):
    """What the page shows: counts, and the best and worst labels by shrunk mean (only labels with
    min_n trades; avg is the plain mean). k and min_n default to what the last decide() used.
    Best and worst never show the same label: with fewer than 6 labels they share them out."""
    m = mem if isinstance(mem, dict) else {}
    k, min_n = _gate(m.get("k") if k is None else k, m.get("min_n") if min_n is None else min_n)
    rows = sorted(_rows(m, [lab for lab in _table(m) if lab in KNOWN], k, min_n), key=lambda r: (-r[3], r[0]))
    half = (len(rows) + 1) // 2
    return {"samples": _count(m.get("samples")), "skipped": _count(m.get("skipped")),
            "explored": _count(m.get("explored")), "min_samples": min_n,
            "best": [_row_doc(r) for r in rows[:min(3, half)]],
            "worst": [_row_doc(r) for r in rows[half:][::-1][:3]]}


# ---------------------------------------------------------------- the memory file

def _repair(doc):
    """A clean copy of a memory: known labels with sound stats and counters as counts, else a fresh one."""
    if not isinstance(doc, dict) or doc.get("v", 1) != 1:
        return fresh()
    mem = fresh()
    for key in ("samples", "skipped", "explored", "skip_run"):
        mem[key] = _count(doc.get(key))
    for lab, st in _table(doc).items():
        st = _clean_stat(st)
        if lab in KNOWN and st:
            mem["labels"][lab] = {k: _tidy(v) for k, v in st.items()}
    if "k" in doc or "min_n" in doc:
        mem["k"], mem["min_n"] = _gate(doc.get("k"), doc.get("min_n"))
    return mem


def load(path):
    """The memory in path; a fresh one when the file is missing, unreadable or not a memory."""
    if not isinstance(path, (str, os.PathLike)):        # open(3) would read file descriptor 3
        return fresh()
    try:
        with open(path, encoding="utf-8") as fh:
            return _repair(json.load(fh))
    except (OSError, ValueError, RecursionError):
        return fresh()


def save(path, mem):
    """Writes the memory atomically: a temp file, then os.replace, so a crash mid-write leaves the
    old file whole. -> True, or False when it could not (the night goes on without it). Only the
    learner's own keys are written."""
    if not isinstance(path, (str, os.PathLike)) or not str(path) or not isinstance(mem, dict):
        return False
    tmp = "%s.tmp" % os.fspath(path)
    try:
        folder = os.path.dirname(tmp)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_repair(mem), fh, ensure_ascii=False, indent=1, sort_keys=True, allow_nan=False)
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _opened_hour(iso, tz):
    """The local hour of a trade's "opened" (UTC, "2026-11-25T22:30:00Z"), or None."""
    try:
        t = datetime.datetime.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    if ZoneInfo is None:                        # pragma: no cover - as bot.local() without zoneinfo
        return (t.hour + 1) % 24
    try:
        return t.replace(tzinfo=datetime.timezone.utc).astimezone(ZoneInfo(tz)).hour
    except (KeyError, TypeError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        return None


def backfill(mem, trades, tz="Europe/Bratislava"):
    """Seeds a new memory from the closed trades of past nights -> how many it learned. A trade
    from before the learner has no buckets, only the hour it opened, so only its hour label learns
    (a trade that carries its own "learn" labels uses those)."""
    if not isinstance(mem, dict) or not isinstance(trades, (list, tuple)):
        return 0
    _ensure(mem)
    before = mem["samples"]
    for t in trades:
        if isinstance(t, dict):
            labels = _labels(t.get("learn")) or features(None, None, None, None, _opened_hour(t.get("opened"), tz))
            update(mem, labels, t.get("pnl_pct"))
    return mem["samples"] - before
