#!/usr/bin/env python3
"""Meme Bot EDGE STUDY: does ANY long-only rule on brand-new pump.fun coins' first 15 minutes make money after the
real costs, and does it still do so on held-out time? Judged on the live bot's recorded tapes, never on hope.

    python3 memebot/study.py --data DIR [--since ISO] [--until ISO] [--split 0.7] [--out study.json] [--md SUMMARY.md]
                             [--max-rules 300] [--config FILE] [--balance 85] [--latency S] [--fill next|at]
                             [--min-trades 15] [--top 15] [--seed 7] [--boot 2000] [--embargo-s 900]

Why it exists: on 2026-11-26 the live bot made 14 trades and won 1 ($100 -> $84.53); the replay backtest over 4 hours
of tape made 3 trades, all losers. The current strategy has no demonstrated edge, and tuning its levers on 3 trades
says nothing. This study asks the wider question with every rule family three designers proposed (entry timing and
selection, exit policy, and a skeptic's base rates), each tried on the same tape the same way.

WHAT IT READS: a folder of tapes (memebot/recorder.py's lines; a folder of the workflow's artifacts, one subfolder per
job, works too) through recorder.load_tapes() and recorder.load_system(). A coin is built the live engine's way: its
create through newpairs.coin_from_create(), every curve reading through newpairs.apply_reading() (so a buy reading,
a sell reading and the biggest sell are counted exactly as live), and the live trigger itself is newpairs.trigger()
with the config's own numbers. Nothing here writes anywhere but --out and --md.

CAUSALITY. Every entry decision happens on a curve reading (or, for the base rows, at a clock time; SNIPE create on
the create line itself) and reads only lines with t at or before it: the readings so far, the create, the meta and
RugCheck lines from their own t, the coin's "follow" / "end" / "gap" lines so far, the heartbeats so far. A coin is
"polled" at t when its first reading came by t (the poller writes the first reading of every watch: before it the
curve is unknown), no "end" line came for it by t, no "gap" line of it is still open (no reading since), its run is
alive, and, past the bot's own watch window (np_watch_s + 5 s), its "follow" line came at or before t (or the live
bot held it then): an entry after 180 s can only be taken on a coin the recorder had chosen to follow by then. A
window feature ("SOL in over the last 120 s") also needs the coin polled over the whole window, with every hole the
heartbeats had shown by t. The rules never read a coin's future peak, its end, its completion or how long the tape
followed it. The no-look-ahead test (tests/test_study.py) cuts the tape after a decision and checks that every
decision before the cut is the same.

FILLS. An order decided at t is sent at t + L and fills against the curve as the next poll saw it: the first reading
at or after t + L when one came within a poll (--poll-s, 1 s), else the last reading at or before t + L (readings
are change-only: no reading means the curve had not moved). L is calibrated on the tape: entries from the buy
lines (their t against the moment the buy was free to go: its trigger's reading, "tt", or the RugCheck report it
waited for, whichever came later), exits from the sell lines' lat_ms (from the reading that decided the sale to its
fill); 1 s each when the tape has fewer than 3 of them. The live bot's RugCheck wait (from the trigger's reading to
that report: the holder re-asks, a late report) is the current rule's, not a fill's: only the REF rows' entries pay
it on top (its median). --fill at prices every order on the last reading at or before t + L instead.

COSTS, the engine's own maths (bot.py): an order of position_pct of a ~$85 book ($8.50; --balance, --order-usd),
its network + priority fee inside the order as np_fire sizes it; the tokens are bot.curve_buy_tokens() on the fill
state (pump.fun's curve_fee_pct and the constant-product impact) less the latency haircut (latency_slip_pct); each
sale is bot.curve_sell_sol() on its fill state, less the haircut, less the order's fee (every partial sale pays
its own). SOL/USD is the tape's own ("sol" lines, heartbeats, the fills' "sol"). P&L is % of the order after every
cost. The report breaks a round trip at an unchanged price into its parts (about -6.5 % at $8.50 and SOL $71).

COVERAGE. When the tape stops knowing the coin while a trade is open (its "end" line: cap, stop, gone, the follow
time; a "gap" line; a heartbeat stretch where the poller was silent 3 s or more (Market: by the heartbeats' own
poll_gap_ms / poll_since_ms where they have them, from the call counts on older tapes) or the recorder lost lines;
the run's end), the trade is sold BLIND at the last state the tape knew, with costs, and counted. A rule with more
than 10 % blind trades is flagged and never passes. A curve that completes (migrates) while held is sold into its
last curve state before the completion (the bot's own "migrating: take it"), at the completing reading itself (the
tape has no price after it), counted apart: a pass must still hold with every completion at x0.85, and the summary
shows each finalist's completed share.

CALIBRATION. The cost model is checked on every live trade (buy -> sells) and every shadow trade (open -> sells) on
the tape: the study's P&L of the same trade, with its fills at the lines' own times, against the P&L the bot
booked (mean absolute error, in points). Also with the study's own fill model (decided when the buy was free to go
and at each sale's deciding reading, plus L): that one carries the latency's cost too; and the bot's scalp exit as the
study approximates it (X1), replayed from each live fill (the bot's tiers, NEWS and fading are not in it). The live
stop table shows the gain the deciding reading had against what was booked. Over 1 point off: said with the verdict.

THE RULES (rule x parameter combinations, capped by --max-rules; every one tried is printed). Families from the
three designs, merged where two proposed the same thing (the report's "rules" lists the merges and the drops):
  SURV   survivors still bought at 3-10 min (designers 1 F1 and 3 F1, merged)
  DIP    the first dump absorbed, a rebound with net buying (1 F2 and 3 F2, merged)
  ORG    organic, spread-out net buying, no whale reading (1 F3)
  GRAD   the last third of the curve toward completion (1 F4 and 3 F3, merged)
  BRK    a new high after a held base (1 F5)
  MOM    the live trigger recomputed (newpairs.trigger, config.json's numbers, 45-180 s), a looser one, the skeptic's
         steelman (5 SOL in, a 10 % pullback), each under the exit families of design 2: take-profit / stop / time,
         a scale-out ladder, a trailing stop, a time-only hold, the dump response, the stall exit; and the bot's own
         scalp exit (X1)
  Q1-Q5  quality overlays (socials, the dev's buy, RugCheck, holders per SOL, the snipers' overhang) on the two best
         first-part rules of the older families (design 1 F6; never a SNIPE row: at a create line or a first reading
         Q1, Q3, Q4 and Q5 are unknowable and Q2 repeats SNIPE's own dev filter), counted like every other combination
  SNIPE  buy a new coin in its first second. SNIPE create: at its create line itself (t0, when the feed received it:
         the first moment the bot can see the coin), on that line alone: every coin the bot's checks at the create
         let through (mayhem mode, a symbol, a pump.fun curve; one it then skipped for a full watch list has no
         reading to fill on). Its order, sent at t0 + L, fills on a reading only (the tape knows the curve from the
         first reading on; the create's own state is PumpPortal's): the first reading at or after t0 + L when one
         came within the next poll, else the last one before t0 + L; none when the first reading came later ("no
         reading by the fill": the tape cannot price it) or the tape lost lines between the send and that reading.
         The other four at its first curve reading, a poll after the create (the poller writes the first reading of
         every watch; before it the curve is unknown), on a coin the live bot watched (not skipped at its create),
         not complete, polled then, the tape's first reading being the bot's first sight (no "gap" line of the coin
         by then, no tape hole over [create, it] the heartbeats had shown by then) and at most SNIPE_MAX_DELAY_S
         (10 s) after the create (never a later reading instead: no snipe). They read the create line and that
         reading only: any coin; the price at most R % over the create's own state (R 10, 30: early, not bought after
         a bundle already pumped it); at most +10 % and the dev's initial buy at most 3 % of the supply. Their
         orders fill like every other (sent at t + L: the first reading in the next poll, or, when the curve did not
         change, the last state at or before t + L, which can be the deciding reading's own). Every SNIPE entry under
         a hold of 5 / 10 / 30 s (X3), the bot's scalp out by 175 s of age (X1@175, as the BASE-A rows have it: a
         coin the recorder did not follow leaves the tape at 181 s, and the plain X1 would hold it past that, blind)
         and a sniper's exit S1 (+25 % take, -15 % stop, both on the bot's own gain on the cost basis like X1, out
         30 s after the fill whatever): 5 x 5, few on purpose (each costs Bonferroni width), last in the grid (a
         smaller --max-rules keeps every older row as it was), never the overlays' source and shown apart from the
         older rows (they keep every slot they had). The trades before the first reading (the launch block's own
         snipers, the wallets that make +90 % in 5 s) are invisible to the tape: these rules buy after them, at the
         earliest the bot can. Fills in a coin's first seconds are the most contested of all (other snipers in the
         same slots, which paper fills never meet): a pass here is the weakest hint of any.
Base rows (never selectable): BASE-A every watched coin at 30 / 60 / 120 s and at its first reading (BASE-A first,
what SNIPE is read against), BASE-B every coin near a trigger at 60-480 s, C0 a random time per near coin (one
seeded uniform draw in [max(45 s, near), 600 s]; left out when not polled then), under fixed exits. Reference rows:
the tape's own fire lines under the bot's scalp exit (the CURRENT rule, RugCheck and the LEARNER left out), the same
under time-only holds, and the live and shadow trades as booked. A SNIPE run's fire lines (its start line has np_snipe,
or the line says "entry": "snipe") are first readings bought, not momentum triggers: never a REF fire, never checked
against the recomputed trigger (the Tape checks count them apart).

STATISTICS. The coins are split by create time: the first --split (0.7) of the range ranks, the rest is held out
(coins created in the last --embargo-s before the split are left out of both: a coin lives 900 s on the tape).
Ranking: selectable combinations with at least --min-trades (15) first-part trades, by the lower one-sided 95 %
bound of their mean (mean - 1.645 sd / sqrt(n)): a few lucky trades do not rank first. EVERY ranked combination is
then judged once on the held-out coins at the same Bonferroni z (which already pays for all M: judging them all
costs no error rate, and a family that ranked low on the first part is not left untested). The top --top (15) of the
older families by first-part rank are shown, the best SNIPE_SHOWN (5) SNIPE rows apart (SNIPE takes no older row's
slot), and every one that passes: n, win rate, mean, median, 5 % trimmed mean, mean without the top 3, the total in
$ for the typical order, a bootstrap 95 % CI of the mean (trade-level percentile, fixed seed), the mean by UTC hour,
the blind and completed shares, a stress table and two matched nulls (the same coin at a random time; another coin
of the same hour at the same age).
A rule PASSES only when, on the held-out part: n >= --min-trades; mean > 0; the Bonferroni lower bound > 0; its
first-part mean > 0; at most 10 % blind trades; its mean is still >= 0 with 2 s more latency each way than the
tape's own; and still >= 0 with every completion sold at x0.85 of the curve's last state.
The Bonferroni bound, exactly: M = the number of selectable combinations tried on the first part (overlays
included); z = NormalDist().inv_cdf(1 - 0.025 / M), the lower end of a two-sided (1 - 0.05 / M) interval;
SE = the larger of the trade-level and the hour-cluster bootstrap standard errors of the held-out mean (--boot
resamples each, seed --seed); bound = mean - z * SE. The verdict names the rules that pass, or says "no rule beats
costs on held-out data".

OUTPUT: --out a JSON with everything (settings, tape, coverage, costs, calibration, the first readings, every
combination's first-part numbers, every ranked one's held-out numbers and gates, the shown ones in full, the base and
reference rows, the verdict); --md a
Markdown summary appended to the file ($GITHUB_STEP_SUMMARY); the same summary on stdout. Its "First reading" line
(first_reading(): the watched coins the first-reading SNIPE rules may decide on; the others counted by why not: no
reading, complete at it, not on the tape, not polled then, later than 10 s): the delay from the create line to the
first reading (median, p90; the share read within L + a poll, where a SNIPE create order is priced), the price's
rise there over the create's state (median, p75, p90, the share at +50 % or more) and the real SOL in the curve then
(median, p90; and how much of it came in since the create): how far the coins had already moved before the bot
could see their curve.

HONEST LIMITS: paper fills on the curve maths (no sandwich, no failed or late transaction, nobody reacting to our
order); the universe is what the live bot watched (its cheap checks at the create already applied: mayhem mode,
no symbol, not a pump.fun curve); after 180 s only coins near a trigger are on the tape, at most 60 at once (busy
hours drop some); holds end by 870 s because the tape does; a pass is a hint for a small live paper test, never a
change to config.json. Standard library only.
"""

import argparse
import bisect
import collections
import datetime
import importlib.util
import json
import math
import os
import random
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# the real engine, loaded by its path like replay.py does: its curve maths, its rules (newpairs), its tape loader
bot = _load("memebot_study_bot", os.path.join(HERE, "bot.py"))
newpairs = bot.newpairs
recorder = bot.recorder

EPS = 1e-6                      # times are kept to the microsecond on the tape
MOVE = 1e-6                     # newpairs.MOVE: a reserve change under 1000 lamports is no trade
INF = float("inf")
WATCH_SLACK_S = 5.0             # the bot lets a coin go a loop turn or two after np_watch_s
HB_S = 10.0                     # the recorder's heartbeat (rec_hb_s): a run without "stop" ended about 2 of them later
AGE_MAX_S = 870.0               # every hold ends by then: the tape follows a coin to 900 s after its create
DEFAULT_SOL_USD = 200.0         # only when the tape has no SOL price at all (said in the report)
DEFAULT_LAT_S = 1.0             # the latency when the tape has too few live fills to measure it
NEAR_TOO_LATE = 180.0           # a coin is "near" only on a reading within np_max_age_s (the recorder's _near)
BLIND_MAX = 0.10                # a rule with more blind trades than this is flagged and never passes
STRESS_LAT_S = 2.0              # the latency a passing rule must survive on top of the tape's own, each way
OVERLAY_ROOM = 15               # combinations kept for the quality overlays (10 selectable, 5 on the reference)
SNIPE_MAX_DELAY_S = 10.0        # a first curve reading later than this after the create is no snipe (the live bot's
                                # first poll of a new coin comes within a second or two: one this late is a poller
                                # that was behind, or a tape that lost the real first reading)
SNIPE_SHOWN = 5                 # the best SNIPE rows shown in full apart from the --top of the older families
DAYS_FOR_VERDICT = 3.0          # under this much tape the verdict is descriptive (said in the summary)
CAL_WARN_PP = 1.0               # the cost model further than this off the bot's own trades: said with the verdict

LIMITS = [
    "Paper on paper: fills are the pump.fun curve maths on the recorded readings (the live log showed it 0.0 % off "
    "Jupiter), with the engine's own fees, impact, latency haircut and network + priority fee. No sandwich, no failed "
    "or late transaction, nobody reacting to our order.",
    "The universe is what the live bot watched: coins it skipped at their create (mayhem mode, no symbol, another "
    "launchpad) have no readings. After 180 s only coins that came near a trigger are on the tape, at most 60 at once: "
    "busy hours drop some (the coverage table counts the cap ends), and a coin that was quiet at 180 s and ran later "
    "is not on it at all (\"late bloomers\").",
    "Every hold ends by 870 s after the create because the tape stops at 900 s: a rule that needs longer is judged "
    "short (conservative for the runners). A completion is sold into the last curve state, as the paper bot does; a "
    "real order would sell on the AMM after the migration at an unknown price (stress x0.85).",
    "The study is portfolio-free: one order per trade, no book, no max_open, no LEARNER, no RISK, no job edges. A rule "
    "that passes here still has to be written as engine levers and replayed (memebot/backtest.py) before anything.",
    "Selection: the best of M combinations on the first part looks better than it is; only the held-out part counts, "
    "with a Bonferroni bound over every combination tried. Even a pass there is a hint for a small live paper test, "
    "never a change to config.json. Not financial advice.",
]


def num(x):
    """A finite float, or None (None, bools, NaN, inf, text)."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def iso(t):
    if t is None or not math.isfinite(t):
        return None
    return (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=float(t))).strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_of(t):
    return int(t // 3600) * 3600


def median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    k = len(xs) // 2
    return xs[k] if len(xs) % 2 else (xs[k - 1] + xs[k]) / 2.0


def json_safe(x):
    """inf / NaN -> None, tuples -> lists, everything else as it is (json.dump with allow_nan=False then works)."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [json_safe(v) for v in x]
    return x


def rnd(x, nd=2):
    return None if x is None or not math.isfinite(x) else round(x, nd)


# ---------------------------------------------------------------- the runs, SOL's price and the tape's holes

class Market(object):
    """The tape's lines without a coin: the recorded runs (start ... stop, or the last heartbeat + 2 when the run died),
    each run's settings (its start line's cfg on config.json's), SOL's dollar price, and the stretches the tape
    says nothing about for any coin. Readings are change-only (no reading = unchanged), which is only true while
    the poller answered, so a heartbeat stretch is a hole where it did not (poller_stretch). On a tape whose
    heartbeats carry the poller's own silences (poll_gap_ms, poll_since_ms): where it stood still with curves
    watched, or a wait between its good answers of POLL_FAIL_S (3 s) or more ended in the stretch or was still
    open at its end (not where nothing was watched at either end); such a hole starts at the last good answer
    before the stretch when one was still awaited at its start (the tape cannot tell whether the silence began
    there or later in the stretch: a bound on the safe side). The run's first stretch, from its start line, is
    judged so too. On older tapes, from the call counts: where it made no good call; made a call that failed (a
    429 pause, a time-out, an error) and its good calls leave POLL_FAIL_S or more of the stretch unaccounted for
    (a silence of that length somewhere in it; the stretch after it too when it is still that short, or after one
    that stood still: the pause ran on), or of this stretch and the one before together, beyond what the run's
    poller usually leaves (a silence over their heartbeat: the hole reaches back as far as the one before's
    count lets the silence have begun); or made under half the good calls its poll interval gives (curve_poll_sec
    while the bot held a coin at both heartbeats, else curve_idle_sec: a poller that slow, failure or not). The
    rule is chosen per pair of heartbeats (both carry the silences: by gaps), never across a start line: a tape
    can hold a run recorded before they came and one after. And where the recorder lost lines without a mark.
    Each hole is known from its own closing heartbeat b on, whatever its start: a decision at t only skips the
    holes that ended by t (back-to-back stretches are merged for scoring only, never for a decision)."""

    POLL_SHORT = 0.5                    # under this share of the calls the poll interval gives: a hole (old tapes)
    POLL_FAIL_S = 3.0                   # a silence this long is a hole: a wait between good answers on a tape that
                                        # records them; on older ones, a failed call (or the stretch after one) with
                                        # the good calls leaving this many seconds unaccounted for. A quick error
                                        # answered by the next round a second later is no silence (the first real
                                        # tape: 97 stretches, one every 2.5 min, were holes for one failed call each)
    POLL_HELD_S = 1.0                   # old tapes, a silence over a heartbeat while a coin is held (0.5 s polls):
                                        # this much more unaccounted for, as answers slower than the interval leave a
                                        # second or so a stretch, and a call on its way at the heartbeat (half of
                                        # them then) looks failed (a never-failing poller, held spells, 24 h
                                        # simulated: 41 holes over two stretches without it, 12 with it)
    POLL_ANSWER_S = 1.0                 # old tapes: the time an answer can take, in how far back a silence over a
                                        # heartbeat can have begun (the stretch's first good answer may be a call
                                        # sent before the stretch began)

    def __init__(self, system, base_cfg):
        self.base_cfg = base_cfg
        self.sol = []
        self.runs = []                  # [(start, end, cfg or None)]
        holes = []
        cur = None
        prev_ok = prev_lost = None
        run_cfg = base_cfg
        self.stops = 0
        self.poll_stretches = collections.Counter()     # heartbeat stretches: n, failed_call, hole; by_gaps / by_counts
                                                        # (the rule that judged them), carried (old-tape holes over two)
        self._usual = {}                                # the run's unaccounted seconds a stretch (poller_stretch)
        for x in system:
            t, k = num(x.get("t")), x.get("k")
            if t is None or not isinstance(k, str):
                continue
            if k == "sol":
                self.add_sol(t, x.get("usd"))
            elif k == "hb":
                self.add_sol(t, x.get("sol"))
            if k == "start":
                if cur is not None:
                    self.runs.append((cur[0], cur[1] + 2 * HB_S, cur[2]))
                cur = [t, t, x.get("cfg") if isinstance(x.get("cfg"), dict) else None]
                run_cfg = dict(base_cfg, **cur[2]) if cur[2] else base_cfg
                prev_ok = self.run_state(t)                 # a stretch never spans a start line (nor a stop): the
                prev_lost = None                            # run's first one is from it, its counters then at 0
                continue
            if cur is None:                             # a tape that starts inside a run: from its first line
                cur = [t, t, None]
            cur[1] = max(cur[1], t)
            if k == "stop":
                self.runs.append((cur[0], t, cur[2]))
                self.stops += 1
                cur, prev_ok, prev_lost = None, None, None
                continue
            if k != "hb":
                continue
            ok = num(x.get("poll_ok"))
            calls = num(x.get("poll_calls"))
            lost = num(x["lost"] if "lost" in x else x.get("drop"))
            if ok is not None:
                if prev_ok is not None and not prev_ok["run"] and t <= prev_ok["t"]:   # the same heartbeat again:
                    prev_ok = dict(prev_ok, ok=ok, calls=calls, hb=x)   # nothing to judge, the stretch before it keeps
                                                                        # its verdict
                else:
                    prev_ok = self.poller_stretch(prev_ok, x, t, ok, calls, run_cfg, cur[0], holes)
            if lost is not None:
                if prev_lost is not None and lost > prev_lost[1]:
                    holes.append((prev_lost[0], t, "lost lines"))
                elif prev_lost is None and lost > 0:
                    holes.append((cur[0], t, "lost lines"))
                prev_lost = (t, lost)
        if cur is not None:
            self.runs.append((cur[0], cur[1] + 2 * HB_S, cur[2]))
        self.runs.sort(key=lambda r: r[0])
        self.run_a = [r[0] for r in self.runs]
        holes.sort()
        # for decisions: every heartbeat stretch apart (each known from its own closing heartbeat), sorted by start,
        # with the running highest end (the stretches may overlap: a poller and a lost-lines hole of one stretch)
        self.parts = holes
        self.part_a = [h[0] for h in holes]
        self.part_b = [h[1] for h in holes]
        self.part_bmax = []
        for b in self.part_b:
            self.part_bmax.append(max(b, self.part_bmax[-1]) if self.part_bmax else b)
        # for scoring a trade already decided (it reads the lines after the decision): back-to-back stretches merged
        merged = []
        for a, b, why in holes:
            if merged and a <= merged[-1][1] + EPS:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b), merged[-1][2])
            else:
                merged.append((a, b, why))
        self.holes = merged
        self.hole_a = [h[0] for h in merged]
        self.hole_b = [h[1] for h in merged]
        self.cfgs = {}

    @staticmethod
    def run_state(t):
        """The poller's state at a run's start line (time t): no call made yet (poller_stretch's p)."""
        return {"t": t, "ok": 0, "calls": 0, "hb": {}, "start": None, "fail": False, "stood": False, "u": None,
                "ulo": None, "seclo": None, "ux": None, "hole": False, "run": True}

    def poller_stretch(self, p, x, t, ok, calls, cfg, run_t0, holes):
        """The heartbeat stretch (p's heartbeat, x] judged for the poller; a hole goes into holes. p: the last
        heartbeat's state in this run (run_state at its start line; None at the first heartbeat of a tape that
        starts inside a run: no stretch yet). -> this heartbeat's state.

        BY GAPS, when both heartbeats carry the poller's own silences (poll_gap_ms and poll_since_ms: tapes recorded
        since they came): a hole when the poller stood still with curves watched at both heartbeats, when a wait
        between good answers that ended in the stretch was POLL_FAIL_S or more, or when one still open at x was;
        with nothing watched at both heartbeats no call went out (a coin watched in between was watched under 10 s,
        too young for an entry at 45 s, and let go by its end) and a pause that outlasted the last curve still
        counts on there: no hole. It starts at the last good answer before the stretch when one was still awaited
        at the heartbeat before (never before the heartbeat before that): that wait may be the silence, or it may
        have ended in time and the silence come later in the stretch, which the tape cannot tell, so the start is
        a bound on the safe side (under POLL_FAIL_S early: a wait that long at the heartbeat before made its own
        stretch a hole). It is still known only from x. The run's first stretch, from its start line (the counters
        at 0, no wait open), is judged so when it is POLL_FAIL_S or longer. No call counts: a slow but answering
        poller is no silence, and poll_calls - poll_ok counts a call still on its way as failed (about 7 % of the
        first tape's healthy stretches) while it can miss a real one. With nothing watched at one end, standing
        still is no silence (no call goes out); the wait before the poller went idle is in poll_gap_ms.
        BY COUNTS, on older tapes (never the stretch from the start line: such a tape's first heartbeat came at the
        run's first step): stood still; under POLL_SHORT of the calls due; or a failed call (poll_calls - poll_ok
        grew) in this stretch or the one before (a stretch that stood still counts as failed for the next: the tail
        of a long 429 pause) with POLL_FAIL_S or more unaccounted for. And a silence over a heartbeat: when the
        stretch before had a failed call and was no hole, and the two together leave POLL_FAIL_S or more
        unaccounted beyond what this run's poller usually leaves at that interval (the median of its last 30
        stretches with nothing amiss so far, from 5 of them on; POLL_HELD_S more while a coin was held at both
        heartbeats: answers slower than the 0.5 s interval leave a second or so a stretch, no silence, and a call
        on its way at a heartbeat looks failed), under it on each side or this one a hole of its own (the silence
        may have begun before its heartbeat): one hole, known only from x, that starts as far back as the stretch
        before's good calls let the silence have begun (its unaccounted seconds, one poll interval and
        POLL_ANSWER_S before its heartbeat, at the faster interval when a coin was held at one end; never before
        that stretch). A stretch with a coin held at one heartbeat only is reckoned at the idle interval, which
        the poller ran at for only part of it: its unaccounted seconds count as 0 at least there, and it never
        sets the usual. With nothing watched at one of the two heartbeats no calls are due and only standing still
        is judged there, as before (a failed call then makes no hole; harmless: a coin watched in such a stretch
        was watched under 10 s at its start, too young for an entry at 45 s, or let go by its end, and the bot
        never lets a held coin go)."""
        st = {"t": t, "ok": ok, "calls": calls, "hb": x, "start": None, "fail": False, "stood": False, "u": None,
              "ulo": None, "seclo": None, "ux": None, "hole": False, "run": False}
        gapped = all(k in x for k in ("poll_gap_ms", "poll_since_ms"))
        if p is None or p["run"]:
            self._usual = {}                            # this run's unaccounted seconds, by poll interval (below)
            if p is None or not gapped or t - p["t"] < self.POLL_FAIL_S - 1e-9:
                return st                               # no stretch to judge (yet)
            p = dict(p, hb={"poll_fail": 0, "poll_gap_ms": None, "poll_since_ms": None})
        ps = self.poll_stretches
        hb0, dt = p["hb"], t - p["t"]
        f0, f1 = num(hb0.get("poll_fail")), num(x.get("poll_fail"))
        if f0 is not None and f1 is not None:
            fail = f1 > f0                              # settled failures (never a call still on its way)
        else:
            fail = calls is not None and p["calls"] is not None and calls - ok > p["calls"] - p["ok"]
        stood = ok <= p["ok"]
        need, sec = self.calls_due(cfg, hb0, x, dt, with_sec=True)
        held0, held1 = ((num(h.get("held")) or 0) > 0 for h in (hb0, x))
        mixed = held0 != held1                          # a coin held at one end only: polled faster for part of it
        good = ok - p["ok"]
        u = dt - good * sec if need is not None else None              # seconds the good calls leave unaccounted
        if u is not None and mixed:
            u = max(0.0, u)
        seclo = min(sec, self.calls_due(cfg, {"held": 1}, {"held": 1}, 1.0, with_sec=True)[1]) if mixed else sec
        ulo = dt - good * seclo if need is not None else None         # the same at the faster interval
        usual = self._usual.setdefault(sec, collections.deque(maxlen=30))
        ux = None if u is None else u - (max(0.0, median(usual)) if len(usual) >= 5 else 0.0)  # beyond the usual
        start = p["t"]
        lim = self.POLL_FAIL_S - 1e-9
        if all(k in d for d in (hb0, x) for k in ("poll_gap_ms", "poll_since_ms")):
            ps["by_gaps"] += 1
            w0, w1 = num(hb0.get("watched")), num(x.get("watched"))
            watched = not ((w0 is not None and w0 <= 0) or (w1 is not None and w1 <= 0))
            quiet = w0 is not None and w0 <= 0 and w1 is not None and w1 <= 0      # nothing watched at both ends
            gap, since = num(x.get("poll_gap_ms")), num(x.get("poll_since_ms"))
            hole = (stood and watched) or (not quiet and ((gap is not None and gap / 1000.0 >= lim) or (
                since is not None and since / 1000.0 >= lim)))
            since0 = num(hb0.get("poll_since_ms"))
            if hole and since0 is not None and since0 > 0:
                start = max(p["start"] if p["start"] is not None else run_t0, p["t"] - since0 / 1000.0)
        else:
            ps["by_counts"] += 1
            if stood:
                hole = True
            else:
                short = need is not None and good < self.POLL_SHORT * need - 1e-9
                hole = short or (need is not None and (fail or p["fail"] or p["stood"]) and u >= lim)
            bar = lim + (self.POLL_HELD_S if held0 and held1 else 0.0)
            if ux is not None and p["ux"] is not None and (p["fail"] or p["stood"]) and not p["hole"] \
                    and (fail or p["fail"]) and ux + p["ux"] >= bar:
                hole = True                             # one silence over two stretches, this one a hole of its
                ps["carried"] += 1                      # own or not (it may have begun before its heartbeat)
                start = max(p["start"], p["t"] - (max(0.0, p["ulo"]) + p["seclo"] + self.POLL_ANSWER_S))
        if hole:
            holes.append((start, t, "poller"))
        ps["n"] += 1
        ps["failed_call"] += 1 if fail else 0
        ps["hole"] += 1 if hole else 0
        if u is not None and not hole and not fail and not stood and not mixed:
            usual.append(u)                             # a stretch with nothing amiss
        st.update(start=p["t"], fail=fail, stood=stood, u=u, ulo=ulo, seclo=seclo, ux=ux, hole=hole)
        return st

    @staticmethod
    def calls_due(cfg, hb0, hb1, dt, with_sec=False):
        """The good calls a healthy poller makes between two heartbeats (one batched call a poll: curve_poll_sec
        apart while a coin was held at both, else curve_idle_sec), or None when none are due (no curve watched).
        with_sec: (that, the poll interval in seconds)."""
        w0, w1 = num(hb0.get("watched")), num(hb1.get("watched"))
        held = (num(hb0.get("held")) or 0) > 0 and (num(hb1.get("held")) or 0) > 0
        sec = num(cfg.get("curve_poll_sec" if held else "curve_idle_sec"))
        sec = min(max(sec if sec is not None and sec > 0 else (0.5 if held else 1.0), 0.1), 60.0)
        need = None if (w0 is not None and w0 <= 0) or (w1 is not None and w1 <= 0) else dt / sec
        return (need, sec) if with_sec else need

    def add_sol(self, t, usd):
        v = num(usd)
        if v is not None and v > 0:
            self.sol.append((t, v))

    def finish(self):
        self.sol.sort()
        self.sol_t = [x[0] for x in self.sol]
        self.sol_v = [x[1] for x in self.sol]
        return self

    def sol_at(self, t):
        """SOL's price at or before t; before the first one, the first one; DEFAULT_SOL_USD on a tape with none."""
        if not self.sol_t:
            return DEFAULT_SOL_USD
        i = bisect.bisect_right(self.sol_t, t + EPS) - 1
        return self.sol_v[max(0, i)]

    def run_at(self, t):
        """The run (start, end, cfg) t falls in, or None (no system lines at all: None too, see alive())."""
        i = bisect.bisect_right(self.run_a, t + EPS) - 1
        if i >= 0 and t <= self.runs[i][1] + EPS:
            return self.runs[i]
        return None

    def alive(self, t):
        """A run was recording at t (always True on a tape without system lines: a generic or test tape)."""
        return not self.runs or self.run_at(t) is not None

    def run_end(self, t):
        """When the run t falls in ended (INF on a tape without system lines; t itself outside any run)."""
        if not self.runs:
            return INF
        r = self.run_at(t)
        return r[1] if r is not None else t

    def cfg_at(self, t):
        """The settings the live bot ran with at t: its run's start line on config.json (or the last start line
        before t; config.json when none)."""
        r = self.run_at(t)
        cfg = r[2] if r is not None else None
        if cfg is None:
            i = bisect.bisect_right(self.run_a, t + EPS) - 1
            while i >= 0 and cfg is None:
                cfg = self.runs[i][2]
                i -= 1
        if not cfg:
            return self.base_cfg
        key = id(cfg)
        got = self.cfgs.get(key)
        if got is None:
            got = dict(self.base_cfg)
            got.update(cfg)
            self.cfgs[key] = got
        return got

    def start_cfg(self, t):
        """The start line's own cfg of the run t falls in (or the last one before t), as the run had it; None."""
        r = self.run_at(t)
        if r is not None and r[2] is not None:
            return r[2]
        i = bisect.bisect_right(self.run_a, t + EPS) - 1
        while i >= 0:
            if self.runs[i][2] is not None:
                return self.runs[i][2]
            i -= 1
        return None

    def hole_known(self, a, b, now):
        """A hole that overlaps [a, b] and that the tape had shown by `now`: one heartbeat stretch, its own closing
        heartbeat at or before `now` (a stretch merged with later ones for scoring is known from its own end)."""
        i = bisect.bisect_right(self.part_a, b) - 1
        while i >= 0 and self.part_bmax[i] > a:
            if a < self.part_b[i] <= now + EPS:
                return True
            i -= 1
        return False

    def hole_from(self, t):
        """The first instant at or after t inside a hole (whenever the tape shows it: this is for scoring), or INF."""
        i = bisect.bisect_right(self.hole_b, t + EPS)
        if i < len(self.holes):
            return max(t, self.hole_a[i])
        return INF

    def hole_seconds(self):
        out = collections.Counter()
        for a, b, _ in self.holes:
            t = a
            while t < b:
                h = hour_of(t)
                nxt = min(b, h + 3600)
                out[h] += nxt - t
                t = nxt
        return out


# ---------------------------------------------------------------- one coin, the engine's way

class CoinView(object):
    """One coin off the tape as arrays the rules read by index. Index 0 is the create's own state at t0; 1..n are
    the curve readings the live engine took (newpairs.apply_reading: an older slot or the same state again is left
    out, a completion keeps the last state before it). Per index: t, vsol, vtok, real SOL, price (SOL a token),
    complete, buy / sell reading counts so far, the biggest single sell so far, this reading's real SOL change, the
    gross SOL bought / sold so far, the highest price so far. And what the tape says about its coverage."""

    __slots__ = ("idx", "mint", "sym", "t0", "hour", "creator", "dev_pct", "skip", "skip0", "cfg", "watch_s", "rsol0",
                 "price0", "ts", "vs", "vt", "rs", "px", "cx", "bu", "se", "smax", "d", "gb", "gs", "pk",
                 "iv", "follow_t", "gaps", "held", "near_t", "mom", "fires", "buys", "sells", "shadows", "metas",
                 "rugs", "ends", "last_t", "decs", "part", "migrate_t", "snipe_fires", "odd")

    def __init__(self):
        for k in self.__slots__:
            setattr(self, k, None)

    # lookups (at or before t: no look-ahead)
    def at(self, t):
        """The index of the state at t (the last reading at or before t; 0 is the create)."""
        return max(0, bisect.bisect_right(self.ts, t + EPS) - 1)

    def complete_by(self, t):
        return self.cx[self.at(t)]

    def held_at(self, t):
        for a, b in self.held:
            if a - EPS <= t < b:
                return True
        return False

    def gap_open(self, t):
        for g, c in self.gaps:
            if g - EPS <= t < c:
                return True
        return False

    def interval(self, t):
        for a, b, why in self.iv:
            if a - EPS <= t < b:
                return a, b, why
        return None

    def polled(self, mk, t):
        """Its curve was polled at t, judged from the lines at or before t: its first reading came, no "end" line
        yet (in a coverage interval), no open "gap", the run alive, and past the watch window a "follow" line by then
        (or the live bot held it)."""
        iv = self.interval(t)
        if iv is None:
            return False
        if t - self.t0 > self.watch_s + WATCH_SLACK_S and not (
                (self.follow_t is not None and self.follow_t <= t + EPS) or self.held_at(t)):
            return False
        if self.gaps and self.gap_open(t):
            return False
        return mk.alive(t)

    def span(self, mk, a, b, now):
        """Polled without a break over [a, b], judged at `now` (>= b): one coverage interval, no gap in it, no hole
        the heartbeats had shown by now, one run."""
        if not self.polled(mk, b):
            return False
        iv = self.interval(b)
        if iv[0] > a + EPS:
            return False
        for g, c in self.gaps:
            if g <= b + EPS and c > a:
                return False
        if mk.holes and mk.hole_known(a, b, now):
            return False
        if mk.runs:
            r = mk.run_at(b)
            if r is None or r[0] > a + EPS:
                return False
        return True

    def unknown_from(self, mk, t):
        """(the first instant at or after t the tape does not know this coin's curve, why). For SCORING a trade
        already decided (it reads the lines after t: an "end", a "gap", a hole, the run's end); never for a decision."""
        iv = self.interval(t)
        if iv is None:
            return t, "not polled"
        best, why = iv[1], "end %s" % iv[2]
        for g, c in self.gaps:
            if c > t and max(g, t) < best:
                best, why = max(g, t), "gap"
        h = mk.hole_from(t)
        if h < best:
            best, why = h, "tape hole"
        r = mk.run_end(t)
        if r < best:
            best, why = r, "run end"
        return best, why

    # windows over the readings at or before reading j
    def win(self, j, w):
        """(first index inside (t_j - w, t_j], net SOL in over the window, gross SOL bought, gross SOL sold)."""
        ts = self.ts
        i0 = bisect.bisect_right(ts, ts[j] - w + EPS)
        if i0 < 1:
            i0 = 1
        if i0 > j:
            return j + 1, 0.0, 0.0, 0.0
        return i0, self.rs[j] - self.rs[i0 - 1], self.gb[j] - self.gb[i0 - 1], self.gs[j] - self.gs[i0 - 1]

    def maxsell(self, i0, j):
        d = self.d
        m = 0.0
        for i in range(i0, j + 1):
            if -d[i] > m:
                m = -d[i]
        return m

    def maxbuy(self, i0, j):
        d = self.d
        m = 0.0
        for i in range(i0, j + 1):
            if d[i] > m:
                m = d[i]
        return m


def _line_t(x):
    return float(x["t"])


def snipe_run(mk, t):
    """The run t falls in (its start line's cfg, or the last one before t) traded SNIPE mode (np_snipe)."""
    try:
        c = mk.start_cfg(t)
        return isinstance(c, dict) and bool(newpairs._on(c, "np_snipe"))
    except Exception:                                   # noqa: BLE001 - an odd start line: a momentum run
        return False


def snipe_line(x, mk):
    """A fire line that is a SNIPE's (the coin's first reading bought), not the momentum trigger's: it says "entry":
    "snipe", or the run it is in started with np_snipe (an older SNIPE run's line may say neither)."""
    return x.get("entry") == "snipe" or snipe_run(mk, _line_t(x))


def creators_index(tapes):
    """creator -> sorted create times of every coin on the tape (the serial-launcher check: Watchlist._launch)."""
    out = collections.defaultdict(list)
    for tp in tapes.values():
        cr = tp.get("create")
        if isinstance(cr, dict) and isinstance(cr.get("creator"), str) and cr["creator"]:
            out[cr["creator"]].append(float(cr["t"]))
    for v in out.values():
        v.sort()
    return out


def serial_at(times, t0, t, win):
    """newpairs' coin.serial at t: the creator's other launches in the window, the way Watchlist._launch counts
    them (at the coin's create, and at each later create of the same creator up to t)."""
    if not times:
        return 0
    best = 0
    for c in times:
        if c < t0 - EPS or c > t + EPS:
            continue
        n = bisect.bisect_right(times, c + EPS) - bisect.bisect_left(times, c - win - EPS)
        best = max(best, n - 1)
    return best


class MomVariant(object):
    """A momentum trigger recomputed with newpairs.trigger(): a key, its config (config.json's rules with a few
    numbers changed), and an optional pullback (the price at least that far under its high so far)."""

    def __init__(self, key, cfg, pullback=0.0):
        self.key, self.cfg, self.pullback = key, cfg, pullback
        self.need_sol = float(cfg.get("np_min_sol", 3.0))
        self.need_buys = int(math.ceil(float(cfg.get("np_min_buys", 8)) - 1e-9))
        self.lo = float(cfg.get("np_min_age_s", 0) or 0)
        self.hi = float(cfg.get("np_max_age_s", 180))
        self.social = bool(cfg.get("np_need_social"))
        self.win = float(cfg.get("np_serial_window_min", 60)) * 60.0


def build_view(tp, mk, creators, variants, fire_check=True):
    """A CoinView from a load_tapes() tape (None when it has no create line), with the momentum variants' first
    fires (newpairs.trigger on the coin as live, reading by reading) and, when fire_check, the trigger with its own
    run's settings (to compare with the tape's fire lines)."""
    cr = tp.get("create")
    if not isinstance(cr, dict):
        return None
    t0 = float(cr["t"])
    cfg = mk.cfg_at(t0)
    ev = dict(cr)
    ev.update(mint=tp["mint"], txType="create")
    if "traderPublicKey" not in ev and isinstance(cr.get("creator"), str):
        ev["traderPublicKey"] = cr["creator"]
    coin, why = newpairs.coin_from_create(ev, t0, cfg)
    if coin is None:
        return None
    cv = CoinView()
    cv.mint, cv.sym, cv.t0, cv.hour = tp["mint"], coin.symbol or tp["mint"][:6], coin.t0, hour_of(coin.t0)
    cv.creator, cv.dev_pct, cv.skip, cv.skip0, cv.cfg = coin.creator, coin.dev_pct, why, why, cfg
    cv.watch_s = float(cfg.get("np_watch_s", 180) or 180)
    cv.rsol0, cv.price0 = coin.rsol0, coin.price0
    ev_lines = tp.get("events") or []
    by = collections.defaultdict(list)
    for x in ev_lines:
        by[x.get("k")].append(x)
    # a fire line of a SNIPE run (its start line's cfg has np_snipe, or the line says "entry": "snipe") is the coin's
    # first reading bought, not a momentum trigger: kept apart (never a REF fire, never checked against the trigger)
    cv.fires, cv.snipe_fires = [], []
    for x in by["fire"]:
        (cv.snipe_fires if snipe_line(x, mk) else cv.fires).append(x)
    cv.buys, cv.sells, cv.shadows = by["buy"], by["sell"], by["shadow"]
    cv.metas, cv.rugs, cv.decs = by["meta"], by["rug"], by["dec"]
    cv.migrate_t = min(float(x["t"]) for x in by["migrate"]) if by["migrate"] else None
    cv.ends = [(float(x["t"]), str(x.get("why") or "")) for x in by["end"]]
    cv.ends.sort()
    follows = sorted(float(x["t"]) for x in by["follow"])
    cv.follow_t = follows[0] if follows else None
    cv.last_t = max([t0] + [float(r["t"]) for r in tp["readings"][-1:]] + [float(x["t"]) for x in ev_lines[-1:]])
    if cv.skip is None:                                 # skipped for a reason only the bot knew (list full): a "dec"
                                                        # line after the create (skip0: the create's own checks only)
        cv.skip = next((x.get("why") or "skip" for x in by["dec"] if x.get("d") == "skip"), None)
    # the live bot's own holds: a real one (buy -> final sell) and a shadow (open -> final sell or drop)
    held, open_t = [], None
    for x in sorted(by["buy"] + by["sell"], key=_line_t):
        if x["k"] == "buy":
            open_t = float(x["t"])
        elif x.get("final") and open_t is not None:
            held.append((open_t, float(x["t"])))
            open_t = None
    if open_t is not None:
        held.append((open_t, INF))
    open_t = None
    for x in by["shadow"]:
        act = x.get("act")
        if act == "open":
            open_t = float(x["t"])
        elif open_t is not None and (act == "drop" or (act == "sell" and x.get("final"))):
            held.append((open_t, float(x["t"])))
            open_t = None
    if open_t is not None:
        held.append((open_t, INF))
    cv.held = held
    # the readings, the engine's way
    ts, vs, vt, rs, px, cx = [coin.t0], [coin.vsol], [coin.vtok], [coin.rsol], [coin.price], [False]
    bu, se, smax, d, gb, gs, pk = [0], [0], [0.0], [0.0], [0.0], [0.0], [coin.price]
    metas = sorted((float(x["t"]), x) for x in cv.metas)
    mi = 0
    times = creators.get(coin.creator) or []
    pending = [v for v in variants]
    mom = {}
    if fire_check and not snipe_run(mk, t0):            # the run's own trigger, as it ran (newpairs' defaults under it);
        pending.append(MomVariant("RUN", mk.start_cfg(t0) or cfg))  # a SNIPE run had none
    cv.odd = 0
    for r in tp["readings"]:
        t = r["t"]
        if t < coin.t0 - EPS:
            continue
        rd = {"vsol": r["vsol"], "vtok": r["vtok"], "rsol": r["rsol"], "complete": r["complete"], "odd": r.get("odd")}
        why = newpairs.curve_sane(rd)                   # the live bot's own verdict when the line has one ("o")
        if why is not None and not newpairs.complete_flag(rd, why):
            cv.odd += 1                                 # no standard pump.fun curve reads so (the Tape checks count it);
                                                        # a migrated curve's withdrawn reserves are its completion
        rr = newpairs.read_curve({"vsol": r["vsol"], "vtok": r["vtok"], "rsol": r["rsol"], "complete": r["complete"],
                                  "slot": r["slot"]}) if r["vsol"] else None
        if rr is None:
            if r["complete"] and not coin.complete:     # a zeroed curve after its migration: the last state stays
                coin.complete = True
                ts.append(t), vs.append(vs[-1]), vt.append(vt[-1]), rs.append(rs[-1]), px.append(px[-1])
                cx.append(True), bu.append(bu[-1]), se.append(se[-1]), smax.append(smax[-1]), d.append(0.0)
                gb.append(gb[-1]), gs.append(gs[-1]), pk.append(pk[-1])
                break
            continue
        before = coin.rsol
        if not newpairs.apply_reading(coin, rr, t):
            continue
        dd = coin.rsol - before
        ts.append(t), vs.append(coin.vsol), vt.append(coin.vtok), rs.append(coin.rsol), px.append(coin.price)
        cx.append(bool(coin.complete)), bu.append(coin.buys), se.append(coin.sells), smax.append(coin.sell_max)
        d.append(dd), gb.append(gb[-1] + (dd if dd > MOVE else 0.0)), gs.append(gs[-1] + (-dd if dd < -MOVE else 0.0))
        pk.append(max(pk[-1], coin.price))
        if coin.complete:
            break
        if not pending:
            continue
        age = t - coin.t0
        while mi < len(metas) and metas[mi][0] <= t + EPS:
            if metas[mi][1].get("ok"):
                coin.meta = newpairs.socials(metas[mi][1])
            mi += 1
        left = []
        for v in pending:
            if age > v.hi + 1.0:
                continue                                # too old for this trigger: never again
            if (age < v.lo - EPS or coin.sol_in < v.need_sol - 1e-9 or coin.buys < v.need_buys
                    or (v.pullback and coin.price > (1.0 - v.pullback) * pk[-1] + 1e-15)):
                left.append(v)
                continue
            coin.serial = serial_at(times, coin.t0, t, v.win)
            coin.status, coin.trigger_t = newpairs.WATCHING, None
            fire, _ = newpairs.trigger(coin, v.cfg, t)
            if fire:
                mom[v.key] = t
            else:
                left.append(v)
        pending = left
    cv.ts, cv.vs, cv.vt, cv.rs, cv.px, cv.cx = ts, vs, vt, rs, px, cx
    cv.bu, cv.se, cv.smax, cv.d, cv.gb, cv.gs, cv.pk = bu, se, smax, d, gb, gs, pk
    # coverage intervals: from its first reading (the poller writes the first reading of every watch: before it the
    # curve is unknown, not the create's) to its "end"; a reading after an end opens another (a re-watch)
    iv, k = [], 0
    a = ts[1] if len(ts) > 1 else None
    ends = cv.ends
    while a is not None:
        while k < len(ends) and ends[k][0] < a - EPS:
            k += 1
        if k >= len(ends):                              # no end line: the run's end (it died), or on a tape
            if mk.runs:                                 # without runs the watch window or the follow-up time
                b = mk.run_end(a)
            else:
                until = max([num(x.get("until")) or 0.0 for x in by["follow"]] or [0.0])
                b = max(cv.last_t, coin.t0 + cv.watch_s, until)
            iv.append((a, b, "run" if mk.runs else "tape"))
            break
        e, why = ends[k]
        iv.append((a, e, why))
        i = bisect.bisect_right(ts, e + EPS)
        if i >= len(ts):
            break
        a, k = ts[i], k + 1
    cv.iv = iv
    gaps = []
    for x in by["gap"]:
        g = float(x["t"])
        i = bisect.bisect_right(ts, g + EPS)
        gaps.append((g, ts[i] if i < len(ts) else INF))
    cv.gaps = gaps
    # near a trigger (the recorder's _near, with the run's own numbers): the first reading within np_max_age_s
    # where half np_min_sol came in and half np_min_buys buy readings were counted
    ns = float(cfg.get("np_min_sol", 3.0)) / 2.0
    nb = float(cfg.get("np_min_buys", 8)) / 2.0
    na = float(cfg.get("np_max_age_s", NEAR_TOO_LATE))
    cv.near_t = None
    for j in range(1, len(ts)):
        if ts[j] - coin.t0 > na + 1e-9:
            break
        if rs[j] - coin.rsol0 >= ns - 1e-9 and bu[j] >= nb - 1e-9:
            cv.near_t = ts[j]
            break
    # a momentum decision counts only on a polled reading (a decision needs the coin on the tape then)
    cv.mom = {k: t for k, t in mom.items() if cv.polled(mk, t)}
    return cv


# ---------------------------------------------------------------- fills and costs

def fill_index(cv, t, poll_s, mode="next"):
    """The index of the curve state an order sent at t fills against: mode "next", the first reading at or after t
    when it came within poll_s (the poll that answered next; a reading, never index 0, the create's own state, even
    for an order sent at the create itself), else the last state at or before t (change-only: the curve had not
    moved); mode "at", always the last state at or before t."""
    ts = cv.ts
    if mode == "next":
        i = max(1, bisect.bisect_left(ts, t - EPS))
        if i < len(ts) and ts[i] <= t + poll_s + EPS:
            return i
    return bisect.bisect_right(ts, t + EPS) - 1


class Costs(object):
    """The engine's costs (bot.py buy / sale_usd / np_fire): pump.fun's fee and the constant-product impact by the
    curve maths, the latency haircut both ways, the network + priority fee of each order."""

    def __init__(self, cfg, order_usd, extra_slip_pct=0.0, extra_fee_sol=0.0):
        self.fee_pct = float(cfg.get("curve_fee_pct", 1.25))
        self.slip = float(cfg.get("latency_slip_pct", 1.0)) + extra_slip_pct
        self.fee_sol = float(cfg.get("network_fee_sol", 0.0005) or 0) + float(cfg.get("priority_fee_sol", 0) or 0) \
            + extra_fee_sol
        self.order = float(order_usd)

    def buy(self, vsol, vtok, sol_usd, order=None, fee_sol=None):
        """(tokens after the haircut, the cost in $, the bot's entry price in $ a token) of an order of `order`
        dollars with its fee inside (np_fire: buy(want - fee)), or None."""
        order = self.order if order is None else order
        fee = self.fee_sol if fee_sol is None else fee_sol
        usd_in = order - fee * sol_usd
        if not (usd_in > 0 and sol_usd > 0):
            return None
        tok = bot.curve_buy_tokens(vsol, vtok, usd_in / sol_usd, self.fee_pct) * (1 - self.slip / 100.0)
        if not tok > 0:
            return None
        return tok, usd_in + fee * sol_usd, usd_in / tok

    def sell(self, vsol, vtok, tokens, sol_usd, haircut=1.0, fee_sol=None):
        """Dollars a sale of `tokens` brings (bot.py sale_usd): the curve maths, the haircut, the order's fee out."""
        fee = self.fee_sol if fee_sol is None else fee_sol
        sol = bot.curve_sell_sol(vsol, vtok, tokens, self.fee_pct) * haircut
        return sol * sol_usd * (1 - self.slip / 100.0) - fee * sol_usd

    def round_trip(self, vsol, sol_usd):
        """A buy and an immediate sale at the same state: the total % and its parts (pump.fun's fee, the impact,
        the haircut, the fixed fee), each the % of the order it costs."""
        vtok = newpairs.K / vsol

        def rt(fee_pct, slip, fee_sol):
            c = Costs({"curve_fee_pct": fee_pct, "latency_slip_pct": slip, "network_fee_sol": fee_sol,
                       "priority_fee_sol": 0}, self.order)
            tok, cost, _ = c.buy(vsol, vtok, sol_usd)
            return (c.sell(vsol, vtok, tok, sol_usd) - cost) / cost * 100

        total = rt(self.fee_pct, self.slip, self.fee_sol)
        no_fee = rt(0.0, self.slip, self.fee_sol)
        no_slip = rt(self.fee_pct, 0.0, self.fee_sol)
        no_fixed = rt(self.fee_pct, self.slip, 0.0)
        bare = rt(0.0, 0.0, 0.0)                        # the impact alone
        return {"order_usd": round(self.order, 2), "sol_usd": round(sol_usd, 2), "vsol": vsol,
                "total_pct": round(total, 2), "pumpfun_fee_pct": round(total - no_fee, 2),
                "haircut_pct": round(total - no_slip, 2), "fixed_fee_pct": round(total - no_fixed, 2),
                "impact_pct": round(bare, 2)}


class Sim(object):
    """How a trade is filled: the costs, the latency of an entry and of an exit, the poll window, the fill mode, the
    completion haircut and the pessimistic factor for a blind exit."""

    def __init__(self, costs, l_in, l_out, poll_s=1.0, mode="next", haircut=1.0, blind_x=0.7, age_max=AGE_MAX_S):
        self.costs, self.l_in, self.l_out, self.poll_s, self.mode = costs, l_in, l_out, poll_s, mode
        self.haircut, self.blind_x, self.age_max = haircut, blind_x, age_max

    def but(self, **kw):
        s = Sim(self.costs, self.l_in, self.l_out, self.poll_s, self.mode, self.haircut, self.blind_x, self.age_max)
        for k, v in kw.items():
            setattr(s, k, v)
        return s


# ---------------------------------------------------------------- exits

class Pos(object):
    """An open paper position: the fill, what is left, and what the exits track (the price, its high since the
    fill, the last reading's time)."""

    __slots__ = ("cv", "t_fill", "fi", "tokens", "left", "cost", "entry_px", "pe", "px", "peak", "t_peak", "last_t",
                 "sol", "j", "sold", "blind_usd", "gross", "x")

    def gain_bot(self):
        """The bot's own gain % (np_exit): the price in dollars over the cost-basis entry price."""
        return (self.px * self.sol / self.entry_px - 1) * 100

    def gain_curve(self):
        """The curve price's move since the fill, %."""
        return (self.px / self.pe - 1) * 100

    def peak_bot(self):
        return (self.peak * self.sol / self.entry_px - 1) * 100

    def peak_curve(self):
        return (self.peak / self.pe - 1) * 100


class Policy(object):
    """An exit rule: decisions on each reading after the fill and at clock times, as (fraction of what is left,
    why) or None. Its per-trade state lives in pos.x."""

    basis = "curve"
    age_max = None

    def gain(self, pos):
        return pos.gain_bot() if self.basis == "bot" else pos.gain_curve()

    def peak(self, pos):
        return pos.peak_bot() if self.basis == "bot" else pos.peak_curve()

    def begin(self, pos):
        pass

    def deadline(self, pos):
        return INF

    def on_clock(self, pos, t):
        return None

    def on_reading(self, pos, j):
        return None


class Hold(Policy):
    """Sell everything H seconds after the fill (X3 / design 2's time-only hold), optionally on a take-profit first."""

    def __init__(self, h, tp=None, age_max=None):
        self.h, self.tp, self.age_max = float(h), tp, age_max

    def deadline(self, pos):
        return pos.t_fill + self.h

    def on_clock(self, pos, t):
        return 1.0, "held %g s" % self.h

    def on_reading(self, pos, j):
        if self.tp is not None and pos.gain_curve() >= self.tp - 1e-9:
            return 1.0, "take +%g%% (curve)" % self.tp
        return None


class Scalp(Policy):
    """Take-profit, stop and time exits (the bot's scalp X1, design 1's E1 / E3, design 2's TP / SL / T): on each
    reading a dump sell, the stop, the take-profit, a real-SOL take (R >= r_take); a time limit, a quiet curve, and
    the "held s seconds and still under level %" stops (np: the tier's time stop, late: the late exit), which stay
    armed after their time."""

    def __init__(self, basis="bot", tp=None, sl=None, t_max=None, np=None, late=None, dump=None, quiet=None,
                 r_take=None, age_max=None):
        self.basis, self.tp, self.sl, self.t_max, self.np, self.late = basis, tp, sl, t_max, np, late
        self.dump, self.quiet, self.r_take, self.age_max = dump, quiet, r_take, age_max

    def begin(self, pos):
        pos.x["np"] = pos.x["late"] = False

    def deadline(self, pos):
        d = INF
        if self.t_max is not None:
            d = min(d, pos.t_fill + self.t_max)
        if self.np is not None and not pos.x["np"]:
            d = min(d, pos.t_fill + self.np[0])
        if self.late is not None and not pos.x["late"]:
            d = min(d, pos.t_fill + self.late[0])
        if self.quiet is not None:
            d = min(d, pos.last_t + self.quiet)
        return d

    def on_clock(self, pos, t):
        if self.t_max is not None and t >= pos.t_fill + self.t_max - EPS:
            return 1.0, "time %g s" % self.t_max
        if self.quiet is not None and t >= pos.last_t + self.quiet - EPS:
            return 1.0, "quiet %g s" % self.quiet
        g = self.gain(pos)
        if self.np is not None and not pos.x["np"] and t >= pos.t_fill + self.np[0] - EPS:
            pos.x["np"] = True
            if g < self.np[1] - 1e-9:
                return 1.0, "time stop %g s under %+g%%" % self.np
        if self.late is not None and not pos.x["late"] and t >= pos.t_fill + self.late[0] - EPS:
            pos.x["late"] = True
            if g < self.late[1] - 1e-9:
                return 1.0, "late %g s under %+g%%" % self.late
        return None

    def on_reading(self, pos, j):
        g = self.gain(pos)
        cv = pos.cv
        if self.dump is not None and cv.d[j] <= -self.dump + 1e-9:
            return 1.0, "dump %.1f SOL sold" % -cv.d[j]
        if self.sl is not None and g <= self.sl + 1e-9:
            return 1.0, "stop %+g%%" % self.sl
        if self.tp is not None and g >= self.tp - 1e-9:
            return 1.0, "take %+g%%" % self.tp
        if self.r_take is not None and cv.rs[j] >= self.r_take - 1e-9:
            return 1.0, "real SOL %g" % self.r_take
        if self.np is not None and pos.x["np"] and g < self.np[1] - 1e-9:
            return 1.0, "time stop %g s under %+g%%" % self.np
        if self.late is not None and pos.x["late"] and g < self.late[1] - 1e-9:
            return 1.0, "late %g s under %+g%%" % self.late
        return None


class Runner(Policy):
    """Let it run (design 1's E2, the skeptic's X2, design 2's trailing stop): a stop before it is armed, armed when
    the peak gain reaches `arm`, then out on a trail off the peak price or under a floor gain; a catastrophe stop at
    all times; a real-SOL take; a no-progress exit when not armed by np_s; a time limit."""

    def __init__(self, basis="bot", pre_stop=None, arm=0.0, trail=0.2, floor=None, cat=None, r_take=None,
                 t_max=None, np_s=None, age_max=None):
        self.basis, self.pre_stop, self.arm, self.trail, self.floor = basis, pre_stop, arm, trail, floor
        self.cat, self.r_take, self.t_max, self.np_s, self.age_max = cat, r_take, t_max, np_s, age_max

    def begin(self, pos):
        pos.x["armed"] = self.arm <= 0
        pos.x["np"] = False

    def deadline(self, pos):
        d = INF
        if self.t_max is not None:
            d = min(d, pos.t_fill + self.t_max)
        if self.np_s is not None and not pos.x["armed"] and not pos.x["np"]:
            d = min(d, pos.t_fill + self.np_s)
        return d

    def on_clock(self, pos, t):
        if self.t_max is not None and t >= pos.t_fill + self.t_max - EPS:
            return 1.0, "time %g s" % self.t_max
        if self.np_s is not None and not pos.x["np"] and t >= pos.t_fill + self.np_s - EPS:
            pos.x["np"] = True
            if not pos.x["armed"]:
                return 1.0, "not armed by %g s" % self.np_s
        return None

    def on_reading(self, pos, j):
        g = self.gain(pos)
        if self.cat is not None and g <= self.cat + 1e-9:
            return 1.0, "catastrophe %+g%%" % self.cat
        if self.r_take is not None and pos.cv.rs[j] >= self.r_take - 1e-9:
            return 1.0, "real SOL %g" % self.r_take
        if not pos.x["armed"] and self.peak(pos) >= self.arm - 1e-9:
            pos.x["armed"] = True
        if not pos.x["armed"]:
            if self.pre_stop is not None and g <= self.pre_stop + 1e-9:
                return 1.0, "stop %+g%%" % self.pre_stop
            return None
        if pos.px <= pos.peak * (1 - self.trail) + 1e-18:
            return 1.0, "trail %g%% off the top" % (self.trail * 100)
        if self.floor is not None and g <= self.floor + 1e-9:
            return 1.0, "floor %+g%%" % self.floor
        return None


class Ladder(Policy):
    """Design 2's scale-out: half at TP1 (curve %), then the rest on a trail off the peak or at the trade's own
    break-even after every cost (bot.np_even_gain's arithmetic); a stop before the step; no step by 240 s: out;
    600 s: out."""

    def __init__(self, tp1, trail, sl=None, frac=0.5):
        self.tp1, self.trail, self.sl, self.frac = tp1, trail, sl, frac

    def begin(self, pos):
        pos.x["step"] = 0

    def deadline(self, pos):
        return pos.t_fill + (240.0 if pos.x["step"] == 0 else 600.0)

    def on_clock(self, pos, t):
        if pos.x["step"] == 0:
            return 1.0, "no step by 240 s"
        return 1.0, "time 600 s"

    def even_gain(self, pos, j):
        """The curve gain % at which selling the rest now leaves the whole trade at 0 after every cost."""
        cv, c = pos.cv, self.costs
        got = bot.curve_sell_sol(cv.vs[j], cv.vt[j], pos.left, c.fee_pct)
        k = got / (pos.left * cv.px[j]) if got > 0 else 1 - c.fee_pct / 100.0
        k *= 1 - c.slip / 100.0
        need = pos.cost - pos.sold + c.fee_sol * pos.sol
        if k <= 0:
            return INF
        return (need / (pos.left * k) / pos.sol / pos.pe - 1) * 100

    def on_reading(self, pos, j):
        g = pos.gain_curve()
        if pos.x["step"] == 0:
            if self.sl is not None and g <= self.sl + 1e-9:
                return 1.0, "stop %+g%% (curve)" % self.sl
            if g >= self.tp1 - 1e-9:
                pos.x["step"] = 1
                return self.frac, "step +%g%% (curve)" % self.tp1
            return None
        if pos.px <= pos.peak * (1 - self.trail) + 1e-18:
            return 1.0, "trail %g%%" % (self.trail * 100)
        if g <= self.even_gain(pos, j) + 1e-9:
            return 1.0, "break-even"
        return None


class DumpResponse(Policy):
    """Design 2's dump response on a frozen base exit (take +20 % curve, 240 s, catastrophe -40 %): when D SOL or
    more went out over the last 2 s, sell now, or wait up to 30 s for half the drop to come back."""

    def __init__(self, d=None, mode="now"):
        self.dsol, self.mode = d, mode

    def begin(self, pos):
        pos.x["wait"] = None

    def deadline(self, pos):
        d = pos.t_fill + 240.0
        if pos.x["wait"] is not None:
            d = min(d, pos.x["wait"][0])
        return d

    def on_clock(self, pos, t):
        if pos.x["wait"] is not None and t >= pos.x["wait"][0] - EPS:
            return 1.0, "dump: no rebound in 30 s"
        return 1.0, "time 240 s"

    def on_reading(self, pos, j):
        g = pos.gain_curve()
        cv = pos.cv
        if g <= -40 + 1e-9:
            return 1.0, "catastrophe -40%"
        if g >= 20 - 1e-9:
            return 1.0, "take +20% (curve)"
        w = pos.x["wait"]
        if w is not None:
            if pos.px >= w[2] + 0.5 * (w[1] - w[2]):
                return 1.0, "dump: half came back"
            return None
        if self.dsol is None:
            return None
        k = cv.at(cv.ts[j] - 2.0)
        out2 = cv.rs[k] - cv.rs[j]
        if out2 >= self.dsol - 1e-9:
            if self.mode == "now":
                return 1.0, "dump %.1f SOL in 2 s: out now" % out2
            pos.x["wait"] = (cv.ts[j] + 30.0, cv.px[k], cv.px[j])
        return None


class Stall(Policy):
    """Design 2's stall exit: out when no new high (1.005x the last) for X seconds; armed variant only after +15 %
    (before: out at 90 s); a catastrophe stop at -35 %; out at 600 s."""

    def __init__(self, x, arm=None):
        self.xs, self.arm = float(x), arm

    def begin(self, pos):
        pos.x["hi"], pos.x["t_hi"] = pos.pe, pos.t_fill
        pos.x["armed"] = self.arm is None
        pos.x["np"] = False

    def deadline(self, pos):
        d = pos.t_fill + 600.0
        if pos.x["armed"]:
            d = min(d, pos.x["t_hi"] + self.xs)
        elif not pos.x["np"]:
            d = min(d, pos.t_fill + 90.0)
        return d

    def on_clock(self, pos, t):
        if t >= pos.t_fill + 600.0 - EPS:
            return 1.0, "time 600 s"
        if pos.x["armed"] and t >= pos.x["t_hi"] + self.xs - EPS:
            return 1.0, "stall %g s" % self.xs
        if not pos.x["armed"] and t >= pos.t_fill + 90.0 - EPS:
            pos.x["np"] = True
            return 1.0, "not armed by 90 s"
        return None

    def on_reading(self, pos, j):
        if pos.gain_curve() <= -35 + 1e-9:
            return 1.0, "catastrophe -35%"
        if pos.px > pos.x["hi"] * 1.005:
            pos.x["hi"], pos.x["t_hi"] = pos.px, pos.cv.ts[j]
        if not pos.x["armed"] and pos.x["hi"] >= pos.pe * (1 + self.arm / 100.0) - 1e-18:
            pos.x["armed"] = True
        return None


Trade = collections.namedtuple("Trade", "cv t0 dec fill exit pnl gross blind complete pess why")


def simulate(cv, mk, dec_t, pol, sim):
    """One paper trade: decided at dec_t, filled at dec_t + L (fill_index), sold by the exit policy on the readings
    and clock times after the fill, every sale at its own decision + L. -> (Trade, None) or (None, why not).
    A decision before the coin's first reading (SNIPE create, at the create line; every other rule decides on a
    polled reading or clock time, never before the first reading) fills only on a reading: the tape knows the curve
    from the first reading on (the create's own state is PumpPortal's, no reading), so the order sent at t + L is
    priced when the first reading came by then or within the next poll (else "no reading by the fill") and nothing
    was lost between the send and that reading (a gap line, a tape hole: the next poll may be missing)."""
    ts = cv.ts
    n = len(ts)
    t_send = dec_t + sim.l_in
    early = n > 1 and dec_t < ts[1] - EPS
    unk, unk_why = cv.unknown_from(mk, ts[1] if early else dec_t)
    if early and ts[1] > t_send + EPS and (mk.hole_from(t_send) < ts[1] - EPS or any(
            g < ts[1] - EPS for g, _ in cv.gaps)):
        unk, unk_why = t_send, "lost before the first reading"
    fi = fill_index(cv, t_send, sim.poll_s, sim.mode)
    if fi < 0:
        return None, "no state"
    if early and fi < 1:
        return None, "no reading by the fill"
    t_fill = max(t_send, ts[fi])
    if unk <= t_fill + EPS:
        return None, "not polled at the fill"
    if cv.cx[fi]:
        return None, "curve complete"
    sol = mk.sol_at(t_fill)
    got = sim.costs.buy(cv.vs[fi], cv.vt[fi], sol)
    if got is None:
        return None, "no fill"
    pos = Pos()
    pos.cv, pos.t_fill, pos.fi = cv, t_fill, fi
    pos.tokens, pos.cost, pos.entry_px = got
    pos.left, pos.pe, pos.px, pos.peak, pos.t_peak = pos.tokens, cv.px[fi], cv.px[fi], cv.px[fi], t_fill
    pos.last_t, pos.sol, pos.j, pos.sold, pos.blind_usd, pos.gross, pos.x = t_fill, sol, fi, 0.0, 0.0, 0.0, {}
    if isinstance(pol, Ladder):
        pol.costs = sim.costs
    pol.begin(pos)
    t_force = cv.t0 + (pol.age_max if pol.age_max is not None else sim.age_max)
    j = fi + 1
    blind = complete = False
    why = None
    exit_t = t_fill
    guard = 0
    while pos.left > 1e-12:
        guard += 1
        if guard > 4 * n + 10000:
            why = "exit loop guard"
            break
        t_read = ts[j] if j < n else INF
        t_clock = min(pol.deadline(pos), t_force)
        t_next = min(t_read, t_clock)
        if unk <= t_next + EPS and (unk < t_read - EPS or t_clock <= t_read):
            k = max(pos.j, bisect.bisect_left(ts, unk - EPS) - 1)
            done = unk_why == "end complete" or (cv.migrate_t is not None and cv.migrate_t <= unk + EPS)
            hc = sim.haircut if done else 1.0           # it migrated: sold into its last curve state (the bot's way)
            usd = sim.costs.sell(cv.vs[k], cv.vt[k], pos.left, mk.sol_at(unk), haircut=hc)
            pos.sold += usd
            pos.gross += pos.left * cv.px[k] * hc
            pos.left = 0.0
            if done:
                complete, why, exit_t = True, "completed: sold into the last curve state", unk
            else:
                pos.blind_usd += usd
                blind, why, exit_t = True, "blind: " + unk_why, unk
            break
        if t_next == INF:
            why = "no exit"
            break
        if t_clock <= t_read:
            t = max(t_clock, pos.last_t)
            dec = (1.0, "age %g s" % (t_force - cv.t0)) if t >= t_force - EPS else pol.on_clock(pos, t)
            if dec is None and pol.deadline(pos) <= t + EPS and t < t_force - EPS:
                dec = (1.0, "exit rule stuck")         # never: every policy moves its clock on
        else:
            t = t_read
            pos.px, pos.last_t, pos.j, pos.sol = cv.px[j], t, j, mk.sol_at(t)
            if pos.px > pos.peak:
                pos.peak, pos.t_peak = pos.px, t
            if cv.cx[j]:
                hc = sim.haircut
                usd = sim.costs.sell(cv.vs[j], cv.vt[j], pos.left, pos.sol, haircut=hc)
                pos.sold += usd
                pos.gross += pos.left * cv.px[j] * hc
                pos.left = 0.0
                complete, why, exit_t = True, "completed: sold into the last curve state", t
                break
            dec = pol.on_reading(pos, j)
            j += 1
        if dec is None:
            continue
        frac, why = dec
        t_out = t + sim.l_out
        fx = fill_index(cv, t_out, sim.poll_s, sim.mode)
        fx = max(fx, pos.j)
        t_fx = max(t_out, ts[fx])
        hc = 1.0
        if t_fx >= unk - EPS:                          # the tape lost the coin before the sale filled
            fx = max(pos.j, bisect.bisect_left(ts, unk - EPS) - 1)
            t_fx, blind = unk, True
            why = "%s (filled blind: %s)" % (why, unk_why)
        if cv.cx[fx]:                                   # it completed while the order was on its way
            complete, hc = True, sim.haircut
            frac = 1.0
        q = pos.left if frac >= 0.999 else pos.left * frac
        usd = sim.costs.sell(cv.vs[fx], cv.vt[fx], q, mk.sol_at(t_fx), haircut=hc)
        pos.sold += usd
        if blind:
            pos.blind_usd += usd
        pos.gross += q * cv.px[fx] * hc
        pos.left -= q
        exit_t = t_fx
        if frac >= 0.999 or blind or complete:
            pos.left = 0.0
            break
    if pos.left > 1e-12:                                # no exit came (a policy without a clock): never in this study
        k = pos.j
        usd = sim.costs.sell(cv.vs[k], cv.vt[k], pos.left, pos.sol)
        pos.sold += usd
        pos.gross += pos.left * cv.px[k]
        blind, why = True, why or "no exit"
    pnl = (pos.sold - pos.cost) / pos.cost * 100
    pess = (pos.sold - pos.blind_usd * (1 - sim.blind_x) - pos.cost) / pos.cost * 100 if blind else pnl
    gross = (pos.gross / (pos.tokens * pos.pe) - 1) * 100
    return Trade(cv.idx, cv.t0, dec_t, t_fill, exit_t, pnl, gross, blind, complete, pess, why), None


# ---------------------------------------------------------------- the entry rules (one scan per family and coin)

def _scan_window(cv, lo, hi):
    """The reading indexes with age in [lo, hi]."""
    ts = cv.ts
    a = max(1, bisect.bisect_left(ts, cv.t0 + lo - EPS))
    b = bisect.bisect_right(ts, cv.t0 + hi + EPS)
    return a, b


def scan_surv(cv, mk, cfgs):
    """SURV: still bought at 3-10 minutes (designers 1 F1 and 3 F1). On a polled reading in the config's age window
    [A, A + 180] (to 600 s at most; past the watch window only a coin whose "follow" line came before the reading, so
    a coin the recorder chose to follow by then), a coin near a trigger by 180 s, polled without a break over the
    last 120 s, not complete: net SOL in over 120 s >= f, SOL bought over 60 s >= 60 % of the SOL moved, no single
    sell over 2 SOL in 120 s nor 1.5 SOL in 60 s, at most 20 % under its high, real SOL under 50, SOL in since the
    launch >= S."""
    out = {}
    if cv.near_t is None:
        return out
    a, b = _scan_window(cv, 180.0, 600.0)
    left = list(cfgs)
    rs, px, pk, ts, t0 = cv.rs, cv.px, cv.pk, cv.ts, cv.t0
    fmin = min(c[1]["f"] for c in left) if left else 0
    for j in range(a, b):
        if not left:
            break
        if cv.cx[j]:
            break
        t = ts[j]
        age = t - t0
        if rs[j] >= 50.0 or px[j] < 0.8 * pk[j] or cv.near_t > t + EPS:
            continue
        i120, f120, _, _ = cv.win(j, 120.0)
        if f120 < fmin - 1e-9:
            continue
        i60, _, gb60, gs60 = cv.win(j, 60.0)
        if gb60 + gs60 <= 0 or gb60 / (gb60 + gs60) < 0.6:
            continue
        if cv.maxsell(i120, j) > 2.0 + 1e-9 or cv.maxsell(i60, j) >= 1.5 - 1e-9:
            continue
        if not cv.polled(mk, t) or not cv.span(mk, t - 120.0, t, t):
            continue
        sol_in = rs[j] - cv.rsol0
        keep = []
        for key, p in left:
            if p["A"] - EPS <= age <= min(p["A"] + 180.0, 600.0) + EPS and f120 >= p["f"] - 1e-9 and sol_in >= p["S"] - 1e-9:
                out[key] = t
            elif age <= min(p["A"] + 180.0, 600.0) + EPS:
                keep.append((key, p))
        left = keep
    return out


def scan_dip(cv, mk, cfgs):
    """DIP: the first dump absorbed (designers 1 F2 and 3 F2). The dump: the first polled reading at 30-300 s where
    one reading sold >= D SOL (or the price is 35 % under its high), after at least 3 SOL had come in, with at least
    40 % of the highest real SOL still in, polled over the 30 s before. The entry: the first polled reading within
    90 s after it, at most 360 s old, with net SOL in since the trough >= C, no sell of 1 SOL or more since the dump,
    SOL in at least half its highest, the price still 10 % or more under its high, not complete. Past the watch
    window only on a coin whose "follow" line came before the reading (polled)."""
    out = {}
    ts, rs, px, pk, d, t0 = cv.ts, cv.rs, cv.px, cv.pk, cv.d, cv.t0
    a, b = _scan_window(cv, 30.0, 300.0)
    if a >= b:
        return out
    trig = collections.defaultdict(list)
    for key, p in cfgs:
        trig[p["trig"]].append((key, p))
    for tk, group in trig.items():
        kind, size = tk
        jd = None
        max_in = 0.0
        max_rs = 0.0
        for j in range(1, b):
            if cv.cx[j]:
                break
            if j >= a:
                hit = (d[j] <= -size + 1e-9) if kind == "sell" else (px[j] <= (1 - size) * pk[j] + 1e-18)
                if hit and max_in >= 3.0 - 1e-9 and rs[j] >= 0.4 * max_rs - 1e-9 and cv.polled(mk, ts[j]) \
                        and cv.span(mk, ts[j] - 30.0, ts[j], ts[j]):
                    jd = j
                    break
            max_in = max(max_in, rs[j] - cv.rsol0)
            max_rs = max(max_rs, rs[j])
        if jd is None:
            continue
        td = ts[jd]
        trough = jd
        sold_big = False
        max_in = max(max_in, max(rs[1:jd + 1]) - cv.rsol0)
        left = list(group)
        for j in range(jd + 1, len(ts)):
            t = ts[j]
            if t > td + 90.0 + EPS or t - t0 > 360.0 + EPS or cv.cx[j] or not left:
                break
            max_in = max(max_in, rs[j] - cv.rsol0)
            if d[j] <= -1.0 + 1e-9:
                sold_big = True
            if sold_big:
                break
            if px[j] < px[trough]:
                trough = j
            if not (rs[j] - cv.rsol0 >= 0.5 * max_in - 1e-9 and px[j] <= 0.9 * pk[j] + 1e-18 and cv.polled(mk, t)):
                continue
            keep = []
            for key, p in left:
                if rs[j] - rs[trough] >= p["C"] - 1e-9:
                    out[key] = t
                else:
                    keep.append((key, p))
            left = keep
    return out


def scan_org(cv, mk, cfgs):
    """ORG: organic flow (designer 1 F3). On a polled reading at 60-240 s, polled over the last 60 s: net SOL in over
    60 s >= f, SOL sold at most 35 % of SOL bought, no single buy reading over m of the SOL bought nor over 1.5 SOL, no
    sell over 0.75 SOL in 60 s nor over 2 SOL since the launch, the price +5..+35 % over 60 s, SOL in at most 25, not
    complete. Past the watch window only on a coin whose "follow" line came before the reading (polled)."""
    out = {}
    a, b = _scan_window(cv, 60.0, 240.0)
    left = list(cfgs)
    rs, px, ts = cv.rs, cv.px, cv.ts
    fmin = min(p["f"] for _, p in left) if left else 0
    for j in range(a, b):
        if not left or cv.cx[j]:
            break
        if cv.smax[j] > 2.0 + 1e-9:
            break
        t = ts[j]
        if rs[j] - cv.rsol0 > 25.0:
            continue
        i60, f60, gb60, gs60 = cv.win(j, 60.0)
        if f60 < fmin - 1e-9 or gs60 > 0.35 * gb60 + 1e-9 or gb60 <= 0:
            continue
        k = cv.at(t - 60.0)
        ch = px[j] / px[k] - 1
        if not (0.05 - 1e-9 <= ch <= 0.35 + 1e-9):
            continue
        mb = cv.maxbuy(i60, j)
        if mb > 1.5 + 1e-9 or cv.maxsell(i60, j) > 0.75 + 1e-9:
            continue
        if not cv.polled(mk, t) or not cv.span(mk, t - 60.0, t, t):
            continue
        keep = []
        for key, p in left:
            if f60 >= p["f"] - 1e-9 and mb <= p["m"] * gb60 + 1e-9:
                out[key] = t
            else:
                keep.append((key, p))
        left = keep
    return out


def scan_grad(cv, mk, cfgs):
    """GRAD: the last third of the curve (designers 1 F4 and 3 F3). On a polled reading at most 780 s old: real SOL
    in the curve >= G, net SOL in over 60 s >= g, no single sell of 3 SOL or more in 60 s, at most 15 % under its
    high, polled over the last 60 s, not complete. Past the watch window only on a coin whose "follow" line came
    before the reading (polled): a coin quiet at 180 s that ran later is not on the tape, so not here either."""
    out = {}
    gmin = min(p["G"] for _, p in cfgs) if cfgs else INF
    if max(cv.rs) < gmin - 1e-9:                        # an optimisation only: it never got there
        return out
    a, b = _scan_window(cv, 0.0, 780.0)
    left = list(cfgs)
    rs, px, pk, ts = cv.rs, cv.px, cv.pk, cv.ts
    for j in range(a, b):
        if not left or cv.cx[j]:
            break
        if rs[j] < gmin - 1e-9 or px[j] < 0.85 * pk[j]:
            continue
        t = ts[j]
        i60, f60, _, _ = cv.win(j, 60.0)
        if cv.maxsell(i60, j) >= 3.0 - 1e-9:
            continue
        if not cv.polled(mk, t) or not cv.span(mk, t - 60.0, t, t):
            continue
        keep = []
        for key, p in left:
            if rs[j] >= p["G"] - 1e-9 and f60 >= p["g"] - 1e-9:
                out[key] = t
            else:
                keep.append((key, p))
        left = keep
    return out


def scan_brk(cv, mk, cfgs):
    """BRK: a new high after a held base (designer 1 F5). The running high P was made at t1; at the reading before,
    a base: P at least +40 % over the launch price, held at least B seconds, never more than x under P since, polled
    over [t1, that reading]. The entry, at 120-600 s: the price at least 1.02 x P, net SOL in over 30 s >= 1, real
    SOL at most 60, polled (past the watch window: its "follow" line before the reading), not complete."""
    out = {}
    ts, px, rs, t0 = cv.ts, cv.px, cv.rs, cv.t0
    left = list(cfgs)
    hi, t1, low = px[0], ts[0], INF                     # the high, when it was made, the lowest price since
    b = _scan_window(cv, 0.0, 600.0)[1]
    for j in range(1, b):
        if not left or cv.cx[j]:
            break
        t = ts[j]
        if t - t0 >= 120.0 - EPS and px[j] >= 1.02 * hi and hi >= 1.4 * cv.price0 and rs[j] <= 60.0 \
                and cv.win(j, 30.0)[1] >= 1.0 - 1e-9 and cv.polled(mk, t):
            prev = ts[j - 1]
            if cv.span(mk, t1, prev, t):
                keep = []
                for key, p in left:
                    if prev - t1 >= p["B"] - EPS and low >= (1 - p["x"]) * hi - 1e-18:
                        out[key] = t
                    else:
                        keep.append((key, p))
                left = keep
        if px[j] > hi:
            hi, t1, low = px[j], t, INF
        else:
            low = min(low, px[j])
    return out


def scan_mom(cv, mk, cfgs):
    """MOM: the momentum triggers recomputed in build_view (newpairs.trigger, reading by reading, on a polled
    reading)."""
    return {key: cv.mom[p["v"]] for key, p in cfgs if p["v"] in cv.mom}


FIRST_LEFT_OUT = ("no reading", "complete", "gap", "tape hole", "not polled", "late")


def first_reading_why(cv, mk):
    """(t, None): the time of the coin's first curve reading (index 1: the first state the engine took after the
    create; the poller writes the first reading of every watch, before it the curve is unknown) when a rule may decide
    on it, or (None, why not), why one of FIRST_LEFT_OUT ("skipped" for a coin not watched). A rule may decide there
    on a coin the live bot watched (not skipped at its create), that reading not complete, and the tape's first
    reading the bot's first sight: no "gap" line of the coin at or before it (the hour cap dropped the real first
    reading) and no tape hole over [create, it] the heartbeats had shown by then (lost lines, a silent poller: the
    real one may be missing); the coin polled then; and at most SNIPE_MAX_DELAY_S after the create (a later first
    reading is no snipe). Never a later reading instead: that is no snipe either. Reads the lines at or before it."""
    if cv.skip is not None:
        return None, "skipped"
    if len(cv.ts) < 2:
        return None, "no reading"
    if cv.cx[1]:
        return None, "complete"
    t = cv.ts[1]
    if any(g <= t + EPS for g, _ in cv.gaps):
        return None, "gap"
    if mk.holes and mk.hole_known(cv.t0, t, t):
        return None, "tape hole"
    if not cv.polled(mk, t):
        return None, "not polled"
    if t - cv.t0 > SNIPE_MAX_DELAY_S + EPS:
        return None, "late"
    return t, None


def first_reading_at(cv, mk):
    """The time of the coin's first curve reading when a rule may decide on it (first_reading_why), else None."""
    return first_reading_why(cv, mk)[0]


def rise_at_first(cv):
    """The price's rise at the first reading over the create's own state (index 0), % (newpairs' rise_pct then)."""
    return (cv.px[1] / cv.px[0] - 1) * 100 if cv.px[0] > 0 else INF


def scan_snipe(cv, mk, cfgs):
    """SNIPE: buy a new coin in its first second. "at" "create": at the create line itself (t0, when the feed received
    it: the first moment the bot can see the coin), on that line alone: a coin the bot's checks at the create let
    through (mayhem mode, a symbol, a pump.fun curve; skip0: the full-list skip is a "dec" line after the create, and
    such a coin has no reading to fill on); simulate() prices it on the first reading. Else at its first curve
    reading (first_reading_at: one poll later), on the create line and that reading only: with R, the price there at
    most R % over the create's own state (early, not after a bundle already pumped it); with D (either way), the
    dev's initial buy (initialBuy) at most D % of the supply."""
    out = {}
    t = first_reading_at(cv, mk)
    rise = rise_at_first(cv) if t is not None else None
    for key, p in cfgs:
        if p.get("D") is not None and cv.dev_pct > p["D"] + 1e-9:
            continue
        if p.get("at") == "create":
            if cv.skip0 is None:
                out[key] = cv.t0
            continue
        if t is None or (p.get("R") is not None and rise > p["R"] + 1e-9):
            continue
        out[key] = t
    return out


def scan_base_a(cv, mk, cfgs):
    """BASE-A: every coin the live bot watched (not skipped at its create), at a fixed age, if still polled; "first":
    at its first curve reading (first_reading_at), what every SNIPE rule is read against."""
    out = {}
    if cv.skip is not None:
        return out
    for key, p in cfgs:
        if p.get("first"):
            t = first_reading_at(cv, mk)
            if t is not None:
                out[key] = t
            continue
        t = cv.t0 + p["a"]
        if cv.polled(mk, t) and not cv.complete_by(t):
            out[key] = t
    return out


def scan_base_b(cv, mk, cfgs):
    """BASE-B: every coin near a trigger by min(a, 180) s, at age a, if still polled (past 180 s: followed)."""
    out = {}
    if cv.near_t is None:
        return out
    for key, p in cfgs:
        t = cv.t0 + p["a"]
        if cv.near_t <= cv.t0 + min(p["a"], NEAR_TOO_LATE) + EPS and cv.polled(mk, t) and not cv.complete_by(t):
            out[key] = t
    return out


def scan_c0(cv, mk, cfgs, seed=7):
    """C0: a random time per near coin (designers 1 C0, 2 E_RAND and 3 BASE-C, merged): ONE seeded uniform draw in
    [max(45 s, when it came near), 600 s]; a coin not polled (or complete) at its draw is left out, never drawn again
    (the earliest of several draws would lean to the early ages, where the returns differ). The draw is judged only
    on lines up to its own time."""
    out = {}
    if cv.near_t is None:
        return out
    lo = max(45.0, cv.near_t - cv.t0)
    if lo >= 600.0:
        return out
    rng = random.Random("%s:%s" % (seed, cv.mint))
    t = cv.t0 + rng.uniform(lo, 600.0)
    if cv.polled(mk, t) and not cv.complete_by(t):
        for key, _ in cfgs:
            out[key] = t
    return out


def scan_fire(cv, mk, cfgs):
    """REF / FIRE: the tape's own first fire line at 45 s or older (the bot's current age floor), decided at the
    line's time (the reading that fired, the same loop turn). A momentum fire line only (cv.fires: a SNIPE run's are
    cv.snipe_fires)."""
    out = {}
    for x in sorted(cv.fires, key=_line_t):
        t = float(x["t"])
        age = num(x.get("age"))
        age = t - cv.t0 if age is None else age
        if age >= 45.0 - 1e-6:
            if cv.polled(mk, t) and not cv.complete_by(t):
                for key, _ in cfgs:
                    out[key] = t
            break
    return out


def decide(views, mk, entries, seed=7):
    """Every entry rule's decisions: {entry key: {coin index: decision time}} for entries = {entry key: (family,
    params)}, one scan per family and coin (each decision reads the lines at or before its own time only)."""
    by_fam = collections.defaultdict(list)
    for ek, (fam, p) in entries.items():
        by_fam[fam].append((ek, p))
    dec = {ek: {} for ek in entries}
    for cv in views:
        for fam, cfgs in by_fam.items():
            got = scan_c0(cv, mk, cfgs, seed) if fam == "C0" else FAMILIES[fam](cv, mk, cfgs)
            for ek, t in got.items():
                dec[ek][cv.idx] = t
    return dec


def views_of(tapes, mk, cfg):
    """The CoinViews of loaded tapes (each coin's readings freed once its arrays hold them), and how many coins had
    no create line; SOL's price from the fills goes to the market too."""
    creators = creators_index(tapes)
    variants = mom_variants(cfg)
    views, no_create = [], 0
    for m in list(tapes):
        cv = build_view(tapes[m], mk, creators, variants)
        tapes[m]["readings"] = []
        if cv is None:
            no_create += 1
            continue
        cv.idx = len(views)
        views.append(cv)
        for x in cv.buys + cv.sells + cv.shadows:
            mk.add_sol(float(x["t"]), x.get("sol"))
    mk.finish()
    return views, no_create


# ---------------------------------------------------------------- the grid

def exit_policies(cfg):
    """{exit key: (policy, words)} for every exit any combination uses."""
    scalp = next((t for t in cfg.get("hold_tiers") or [] if t.get("name") == "scalp"), {})
    first = float(((scalp.get("ladder") or [[10.0, 1.0]])[0] or [10.0])[0])
    ts = float(scalp.get("time_stop_min") or cfg.get("time_stop_min") or 5) * 60.0
    late = ts * float(cfg.get("time_stop_late_x", 2.0) or 0)
    x1 = dict(basis="bot", tp=first, sl=float(cfg.get("stop_pct", -7.0)), dump=float(cfg.get("np_dump_sol", 2.0)),
              np=(ts, float(cfg.get("time_stop_below_pct", 2.0))), late=(late, first) if late > 0 else None,
              quiet=float(cfg.get("np_quiet_min", 3.0)) * 60.0 or None)
    ex = {
        "X1": (Scalp(**x1), "the bot's scalp: %+g%% take, %+g%% stop, dump %g SOL, %g s time stop under %+g%%, quiet "
                            "%g s, %g s late (gain on the cost basis)" % (
                    first, x1["sl"], x1["dump"], ts, x1["np"][1], x1["quiet"] or 0, late)),
        "X1@175": (Scalp(age_max=175.0, **x1), "the bot's scalp, out by 175 s of age"),
        "E1": (Scalp(basis="bot", tp=15.0, sl=-12.0, np=(180.0, 3.0)), "scalp: +15% take, -12% stop, out at 180 s "
                                                                          "under +3% (cost basis)"),
        "E2": (Runner(basis="bot", pre_stop=-20.0, arm=30.0, trail=0.20, floor=5.0, r_take=80.0),
               "runner: -20% stop until +30%, then a 20% trail or +5% floor; out at 80 SOL real (cost basis)"),
        "E3": (Scalp(basis="bot", sl=-15.0, r_take=80.0), "graduation: out at 80 SOL real or -15% (cost basis)"),
    }
    for h in (5, 10, 15, 30, 45, 60, 180, 300, 480):
        ex["X3_%d" % h] = (Hold(h), "hold %d s" % h)
    for h in (15, 45, 60, 180, 300, 480):
        ex["H%d_TP30" % h] = (Hold(h, tp=30.0), "hold %d s, +30%% (curve) take" % h)
    ex["S1"] = (Scalp(basis="bot", tp=25.0, sl=-15.0, t_max=30.0),
                "sniper: +25% take, -15% stop (cost basis), out 30 s after the fill")
    for tp in (10, 20, 40):
        for sl in (-10, None):
            for tm in (60, 240):
                k = "TPSL_%d_%s_%d" % (tp, "off" if sl is None else abs(sl), tm)
                ex[k] = (Scalp(basis="curve", tp=float(tp), sl=None if sl is None else float(sl), t_max=float(tm)),
                         "curve +%d%% take, %s, %d s" % (tp, "no stop" if sl is None else "%d%% stop" % sl, tm))
    for tp1 in (15, 30):
        for tr in (15, 30):
            for sl in (-15, None):
                k = "LAD_%d_%d_%s" % (tp1, tr, "off" if sl is None else abs(sl))
                ex[k] = (Ladder(float(tp1), tr / 100.0, None if sl is None else float(sl)),
                         "half at +%d%% (curve), rest on a %d%% trail or break-even, %s" % (
                             tp1, tr, "no stop" if sl is None else "%d%% stop" % sl))
    for tr in (15, 30, 50):
        for arm in (0, 20):
            ex["TRL_%d_%d" % (tr, arm)] = (Runner(basis="curve", arm=float(arm), trail=tr / 100.0, cat=-30.0,
                                                  t_max=600.0, np_s=120.0 if arm else None),
                                           "trail %d%% %s, -30%% catastrophe, 600 s" % (
                                               tr, "from the fill" if not arm else "once +%d%%" % arm))
    ex["DR_off"] = (DumpResponse(None), "base: +20% (curve) take, 240 s, -40% catastrophe")
    for dsol in (1, 2, 3):
        for mode in ("now", "rebound"):
            ex["DR_%d_%s" % (dsol, mode)] = (DumpResponse(float(dsol), mode), "base + %d SOL out in 2 s: %s" % (
                dsol, "out now" if mode == "now" else "wait 30 s for half back"))
    for x in (10, 30, 60):
        for arm in (None, 15):
            ex["ST_%d_%s" % (x, "x" if arm is None else arm)] = (
                Stall(float(x), arm if arm is None else float(arm)),
                "stall %d s%s, -35%% catastrophe, 600 s" % (x, "" if arm is None else " once +%d%%" % arm))
    return ex


def mom_variants(cfg):
    """The momentum triggers MOM tries (config.json's live rules, a looser one, the skeptic's steelman)."""
    def v(key, pullback=0.0, **kw):
        c = dict(cfg)
        c.update(kw)
        return MomVariant(key, c, pullback)
    return [v("BASE"), v("LOOSE", np_min_sol=1.5, np_min_buys=4, np_min_rise_pct=5.0), v("S5", np_min_sol=5.0),
            v("PB", pullback=0.10), v("PB5", pullback=0.10, np_min_sol=5.0)]


FAMILIES = {
    "SURV": scan_surv, "DIP": scan_dip, "ORG": scan_org, "GRAD": scan_grad, "BRK": scan_brk, "MOM": scan_mom,
    "BASE-A": scan_base_a, "BASE-B": scan_base_b, "C0": scan_c0, "REF": scan_fire, "SNIPE": scan_snipe,
}


def build_grid():
    """Every rule x parameter combination, in the order the cap keeps them: the reference and base rows first (they
    are what every rule is read against), then the families by prior plausibility (designer 1: GRAD > SURV > DIP > ORG
    > BRK; the skeptic: the momentum steelman last), then design 2's exits on the live trigger; SNIPE last (its base
    row first), added after the others so a smaller --max-rules keeps every older row as it was.
    -> ([combo dict], {entry key: (family, params)})"""
    combos, entries = [], {}

    def entry(fam, key, **p):
        entries[key] = (fam, p)
        return key

    def add(kind, fam, ek, xk, text):
        key = "%s | %s" % (ek, xk)
        if any(c["key"] == key for c in combos):
            return
        combos.append({"key": key, "kind": kind, "family": fam, "entry": ek, "exit": xk, "text": text,
                       "selectable": kind == "rule"})

    ref = entry("REF", "REF fire")
    add("ref", "REF", ref, "X1", "the CURRENT rule: the tape's own fire lines (45 s+), the bot's scalp exit")
    for a in (30, 60, 120):
        ek = entry("BASE-A", "BASE-A %ds" % a, a=float(a))
        for xk in ("X3_45", "X1@175"):
            add("base", "BASE-A", ek, xk, "every watched coin at %d s" % a)
    for a in (60, 120, 180, 300, 480):
        ek = entry("BASE-B", "BASE-B %ds" % a, a=float(a))
        for xk in ("X1", "E2", "X3_60", "X3_180", "X3_300"):
            add("base", "BASE-B", ek, xk, "every near coin at %d s" % a)
    c0 = entry("C0", "C0 random")
    for xk in ("X1", "E1", "E2", "X3_15", "X3_60", "X3_180", "X3_480"):
        add("control", "C0", c0, xk, "one uniform random time per near coin (45-600 s), if polled then")
    for h in (15, 60, 180, 480):
        add("ref", "REF", ref, "X3_%d" % h, "the tape's fire lines, held %d s" % h)
    base = entry("MOM", "MOM BASE", v="BASE")
    add("rule", "MOM", base, "X1", "the live trigger recomputed (no RugCheck, no LEARNER), the bot's scalp exit")
    for G in (35, 50, 65):
        for g in (2, 5):
            ek = entry("GRAD", "GRAD G%d g%d" % (G, g), G=float(G), g=float(g))
            for xk in ("E3", "E2", "X3_180"):
                add("rule", "GRAD", ek, xk, "real SOL >= %d, +%d SOL in 60 s" % (G, g))
    for A in (180, 300, 420):
        for f in (1, 3, 6):
            for S in (0, 8):
                ek = entry("SURV", "SURV A%d f%d S%d" % (A, f, S), A=float(A), f=float(f), S=float(S))
                for xk in ("E1", "E2", "X3_180"):
                    add("rule", "SURV", ek, xk, "%d-%d s, +%d SOL in 120 s, %d SOL in" % (A, min(A + 180, 600), f, S))
    for trig in (("sell", 1.5), ("sell", 3.0), ("dd", 0.35)):
        for C in (0.3, 1.0, 2.0):
            tname = "sell%g" % trig[1] if trig[0] == "sell" else "dd35"
            ek = entry("DIP", "DIP %s C%g" % (tname, C), trig=trig, C=float(C))
            for xk in ("E1", "E2", "X3_180"):
                add("rule", "DIP", ek, xk, "after a %s, +%g SOL off the trough" % (
                    "sell of %g SOL" % trig[1] if trig[0] == "sell" else "35%% drawdown", C))
    for f in (1.5, 3):
        for m in (0.25, 0.5):
            ek = entry("ORG", "ORG f%g m%g" % (f, m), f=float(f), m=float(m))
            for xk in ("E1", "E2", "X3_180"):
                add("rule", "ORG", ek, xk, "+%g SOL in 60 s, no buy over %d%% of it" % (f, m * 100))
    for x in (0.25, 0.40):
        for B in (45, 120):
            ek = entry("BRK", "BRK x%d B%d" % (x * 100, B), x=x, B=float(B))
            for xk in ("E1", "E2", "X3_180"):
                add("rule", "BRK", ek, xk, "new high after a %d s base, at most %d%% deep" % (B, x * 100))
    steel = {"BASE": base, "S5": entry("MOM", "MOM S5", v="S5"), "PB": entry("MOM", "MOM PB", v="PB"),
             "PB5": entry("MOM", "MOM PB5", v="PB5")}
    for name, ek in steel.items():
        for xk in ("X1", "E2", "X3_180"):
            add("rule", "MOM", ek, xk, "momentum %s" % name)
    loose = entry("MOM", "MOM LOOSE", v="LOOSE")
    for ek in (base, loose):
        for tp in (10, 20, 40):
            for sl in ("10", "off"):
                for tm in (60, 240):
                    add("rule", "MOM", ek, "TPSL_%d_%s_%d" % (tp, sl, tm), "momentum, take / stop / time")
    for ek in (base, loose):
        for h in (15, 60, 180, 480):
            add("rule", "MOM", ek, "X3_%d" % h, "momentum, time-only hold")
            add("rule", "MOM", ek, "H%d_TP30" % h, "momentum, time-only hold with a +30% take")
    for ek in (base, loose):
        for tr in (15, 30, 50):
            for arm in (0, 20):
                add("rule", "MOM", ek, "TRL_%d_%d" % (tr, arm), "momentum, trailing stop")
    for tp1 in (15, 30):
        for tr in (15, 30):
            for sl in ("15", "off"):
                add("rule", "MOM", base, "LAD_%d_%d_%s" % (tp1, tr, sl), "momentum, scale-out ladder")
    for ek in (base, loose):
        for x in (10, 30, 60):
            for arm in ("x", "15"):
                add("rule", "MOM", ek, "ST_%d_%s" % (x, arm), "momentum, stall exit")
    add("rule", "MOM", base, "DR_off", "momentum, the dump-response base exit")
    for dsol in (1, 2, 3):
        for mode in ("now", "rebound"):
            add("rule", "MOM", base, "DR_%d_%s" % (dsol, mode), "momentum, dump response")
    first = entry("BASE-A", "BASE-A first", first=True)
    for xk in ("X3_5", "X3_30", "X1@175"):                # X1 out by 175 s: an unfollowed coin leaves the tape at 181 s
        add("base", "BASE-A", first, xk, "every watched coin at its first reading")
    snipe = [(entry("SNIPE", "SNIPE any"), "at its first reading: any watched coin")]
    snipe += [(entry("SNIPE", "SNIPE r<=%d" % R, R=float(R)),
               "at its first reading: the price at most +%d%% over the create's" % R) for R in (10, 30)]
    snipe += [(entry("SNIPE", "SNIPE r<=10 d<=%d" % D, R=10.0, D=float(D)), "at its first reading: the price at "
               "most +10%% over the create's, the dev's buy at most %d%% of the supply" % D) for D in (3,)]
    snipe += [(entry("SNIPE", "SNIPE create", at="create"), "at its create line, filled on its first reading")]
    for ek, what in snipe:
        for xk in ("X3_5", "X3_10", "X3_30", "X1@175", "S1"):
            add("rule", "SNIPE", ek, xk, what)
    return combos, entries


# ---------------------------------------------------------------- the quality overlays (design 1 F6)

def _last_before(lines, t):
    best = None
    for x in lines:
        if float(x["t"]) <= t + EPS:
            if best is None or float(x["t"]) >= float(best["t"]):
                best = x
    return best


def overlay(q, cv, t):
    """True / False, or None (unknown: no metadata read, no RugCheck report, no reading in the first 5 s or a decision
    inside them) for quality filter q at decision time t, from lines at or before t only."""
    if q == "Q1":                                       # socials: an X link that is not a post, or a Telegram
        m = _last_before([x for x in cv.metas if x.get("ok")], t)
        if m is None:
            return None
        return bool((m.get("x") and m.get("xk") != "status") or m.get("tg"))
    if q == "Q2":                                       # the creator bought at most 2 % of the supply
        return cv.dev_pct <= 2.0 + 1e-9
    if q in ("Q3", "Q4"):
        r = _last_before([x for x in cv.rugs if x.get("ok")], t)
        if r is None:
            return None
        holders = num(r.get("held")) if r.get("held") is not None else num(r.get("holders"))
        holders = holders or 0.0
        if q == "Q3":
            return bool((num(r.get("top1")) or 0) <= 6.0 and (num(r.get("top10")) or 0) <= 25.0 and holders >= 25
                        and not (num(r.get("insiders")) or 0) and not r.get("rugged"))
        sol_in = cv.rs[cv.at(t)] - cv.rsol0
        return sol_in <= 0.1 or holders / sol_in >= 3.0
    if q == "Q5":                                       # the snipers' SOL has probably come back out
        if t < cv.t0 + 5.0 - EPS:                       # the first 5 s are not over yet: unknown (no look ahead)
            return None
        k = cv.at(cv.t0 + 5.0)
        first_in = cv.rs[k] - cv.rsol0 if k >= 1 else None
        if first_in is None or first_in <= 0.05:
            return None
        return cv.gs[cv.at(t)] / first_in >= 1.0
    return None


OVERLAYS = {"Q1": "socials: an X profile or a Telegram", "Q2": "the dev bought at most 2 %",
            "Q3": "RugCheck: top1 <= 6 %, top10 <= 25 %, 25+ holders, no insiders",
            "Q4": "RugCheck holders >= 3 per SOL in", "Q5": "the snipers' overhang sold (gross sells >= the first 5 s' SOL)"}


# ---------------------------------------------------------------- statistics

def zq(p):
    return statistics.NormalDist().inv_cdf(p)


def stats(trades, order_usd, boot=0, seed=7, z=None, by_hour=False):
    """The numbers of a list of trades: n, win rate, mean / median / 5 % trimmed mean / mean without the top 3 (net %),
    the gross mean, profit factor, the total in $ for the order size, the blind and completion shares; with boot, a
    bootstrap 95 % CI of the mean (trade-level percentile), the trade-level and hour-cluster bootstrap standard
    errors, and with z the Bonferroni lower bound mean - z * the larger SE."""
    pn = [t.pnl for t in trades]
    n = len(pn)
    out = {"n": n}
    if not n:
        return out
    s = sorted(pn)
    mean = math.fsum(pn) / n
    sd = statistics.stdev(pn) if n > 1 else 0.0
    k = int(n * 0.05)
    trim = s[k:n - k] if n - 2 * k > 0 else s
    wins = sum(1 for p in pn if p > 0)
    pos, neg = math.fsum(p for p in pn if p > 0), math.fsum(p for p in pn if p < 0)
    blind = sum(1 for t in trades if t.blind)
    out.update(mean=mean, median=median(pn), sd=sd, win=wins / n, trim5=math.fsum(trim) / len(trim),
               no_top3=math.fsum(s[:-3]) / (n - 3) if n > 3 else None, gross=math.fsum(t.gross for t in trades) / n,
               pf=pos / -neg if neg < 0 else None, usd=math.fsum(pn) / 100.0 * order_usd, blind=blind,
               blind_share=blind / n, complete=sum(1 for t in trades if t.complete),
               pess=math.fsum(t.pess for t in trades) / n,
               score=mean - 1.645 * sd / math.sqrt(n) if n > 1 else None)
    if by_hour or boot:
        hours = collections.OrderedDict()
        for t, p in zip(trades, pn):
            h = hour_of(t.t0)
            g = hours.setdefault(h, [0, 0.0])
            g[0] += 1
            g[1] += p
        out["hours"] = len(hours)
        out["by_hour"] = {iso(h)[:13]: [g[0], g[1] / g[0]] for h, g in sorted(hours.items())}
        out["hours_up"] = sum(1 for g in hours.values() if g[1] > 0) / len(hours)
    if boot and n > 1:
        rng = random.Random(seed)
        means = sorted(math.fsum(rng.choices(pn, k=n)) / n for _ in range(boot))
        out["ci95"] = [means[int(0.025 * boot)], means[min(boot - 1, int(math.ceil(0.975 * boot)) - 1)]]
        se_t = statistics.pstdev(means)
        groups = list(hours.values())
        se_h = 0.0
        if len(groups) > 1:
            rng = random.Random(seed + 1)
            hm = []
            for _ in range(boot):
                tot = cnt = 0.0
                for g in rng.choices(groups, k=len(groups)):
                    cnt += g[0]
                    tot += g[1]
                hm.append(tot / cnt)
            se_h = statistics.pstdev(hm)
        out.update(se_trade=se_t, se_hour=se_h, se=max(se_t, se_h))
        if z is not None:
            out["lower"] = mean - z * max(se_t, se_h)
    return out


def small(st):
    """A row's numbers rounded for the report."""
    out = {}
    for k, v in (st or {}).items():
        if isinstance(v, float):
            out[k] = rnd(v, 3)
        elif isinstance(v, list):
            out[k] = [rnd(x, 3) if isinstance(x, float) else x for x in v]
        elif isinstance(v, dict):
            out[k] = {kk: [vv[0], rnd(vv[1], 2)] for kk, vv in v.items()}
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- calibration on the live lines

def _pair_live(cv):
    """[(buy line, [its sell lines])] of the live trades, each closed by its final sale."""
    out, cur = [], None
    for x in sorted(cv.buys + cv.sells, key=_line_t):
        if x["k"] == "buy":
            cur = (x, [])
            out.append(cur)
        elif cur is not None:
            cur[1].append(x)
            if x.get("final"):
                cur = None
    return [(b, s) for b, s in out if s and s[-1].get("final") and num(s[-1].get("pnl_pct")) is not None]


def _pair_shadow(cv):
    out, cur = [], None
    for x in sorted(cv.shadows, key=_line_t):
        act = x.get("act")
        if act == "open":
            cur = (x, [])
            out.append(cur)
        elif act == "sell" and cur is not None:
            cur[1].append(x)
            if x.get("final"):
                cur = None
        elif act == "drop":
            cur = None
            if out:
                out.pop()
    return [(o, s) for o, s in out if s and s[-1].get("final") and num(s[-1].get("pnl_pct")) is not None]


def _replay_pair(cv, mk, costs, usd, fee_sol, sol_in, t_in, outs, poll_s, mode):
    """The P&L % of a trade of `usd` (fee inside) filled at t_in, sold in parts at [(t, frac, sol)] by the study's
    cost model, each fill by fill_index(mode). -> (pnl %, tokens) or (None, None)."""
    i = fill_index(cv, t_in, poll_s, mode)
    if i < 0:
        return None, None
    got = costs.buy(cv.vs[i], cv.vt[i], sol_in, order=usd, fee_sol=fee_sol)
    if got is None:
        return None, None
    tok, cost, _ = got
    left, sold = tok, 0.0
    for t, frac, sol in outs:
        k = fill_index(cv, t, poll_s, mode)
        q = left if frac >= 0.999 else left * frac
        sold += costs.sell(cv.vs[k], cv.vt[k], q, sol, fee_sol=fee_sol)
        left -= q
    return (sold - cost) / cost * 100, tok


def calibrate(views, mk, costs, poll_s, l_in, l_out, cfg=None, x1=None):
    """The cost model against the bot's own booked P&L, trade by trade: the live trades (buy -> sells) and the shadow
    trades (open -> sells). Fills at the lines' own times ("at": the last reading at or before; "next": the next
    poll) and the study's own model (decided when the buy was free to go, buy_decided(), and at each sale's deciding
    reading, + L). With
    x1 (the bot's scalp exit as the study approximates it), that exit replayed from each live fill: how far the
    reference row's exits are from the bot's real ones (its tiers, NEWS and fading are not in it)."""
    cfg = cfg if cfg is not None else {}
    rows = []
    for cv in views:
        if not cv.buys and not cv.shadows:
            continue
        for b, sells in _pair_live(cv):
            tb = float(b["t"])
            sol = num(b.get("sol")) or mk.sol_at(tb)
            fee = num(b.get("fee_sol"))
            fee = costs.fee_sol if fee is None else fee
            usd = num(b.get("usd"))
            if not usd:
                continue
            outs = [(float(s["t"]), num(s.get("frac")) or 1.0, num(s.get("sol")) or mk.sol_at(float(s["t"])))
                    for s in sells]
            got = {}
            for mode in ("at", "next"):
                got[mode], tok = _replay_pair(cv, mk, costs, usd, fee, sol, tb, outs, poll_s, mode)
                if mode == "at":
                    raw = num(b.get("raw"))
                    got["tokens_vs_raw_pct"] = (tok / (raw / 1e6) - 1) * 100 if tok and raw else None
            free = buy_decided(cv, b)                   # its trigger's reading, or the RugCheck report it waited for
            dec_in = free[0] if free is not None and tb - free[0] <= 10.0 else tb - l_in
            outs_m = []
            for s, o in zip(sells, outs):
                lat = num(s.get("lat_ms"))
                dt = o[0] - lat / 1000.0 if lat is not None else o[0]
                outs_m.append((dt + l_out, o[1], o[2]))
            got["model"], _ = _replay_pair(cv, mk, costs, usd, fee, sol, dec_in + l_in, outs_m, poll_s, "next")
            if x1 is not None:                          # the reference row's exits, from this very fill, no latency
                tr, _ = simulate(cv, mk, tb, x1, Sim(Costs(cfg, usd), 0.0, 0.0, poll_s, "at"))
                got["x1"] = tr.pnl if tr is not None else None
            fin = sells[-1]
            px_in, px_out = num(b.get("px")), num(fin.get("px"))
            rows.append({"kind": "live", "mint": cv.mint, "sym": cv.sym, "t": iso(tb), "booked": num(fin.get("pnl_pct")),
                         "at": got["at"], "next": got["next"], "model": got["model"], "x1": got.get("x1"),
                         "tokens_vs_raw_pct": got["tokens_vs_raw_pct"], "why": fin.get("why"),
                         "decided_gain": (px_out / px_in - 1) * 100 if px_in and px_out else None,
                         "sales": len(sells)})
        for o, sells in _pair_shadow(cv):
            to = float(o["t"])
            sol = num(o.get("sol")) or mk.sol_at(to)
            usd = num(o.get("usd"))
            if not usd:
                continue
            outs = [(float(s["t"]), num(s.get("frac")) or 1.0, mk.sol_at(float(s["t"]))) for s in sells]
            at, tok = _replay_pair(cv, mk, costs, usd, costs.fee_sol, sol, to, outs, poll_s, "at")
            nx, _ = _replay_pair(cv, mk, costs, usd, costs.fee_sol, sol, to, outs, poll_s, "next")
            raw = num(o.get("raw"))
            rows.append({"kind": "shadow", "mint": cv.mint, "sym": cv.sym, "t": iso(to),
                         "booked": num(sells[-1].get("pnl_pct")), "at": at, "next": nx, "model": None, "x1": None,
                         "tokens_vs_raw_pct": (tok / (raw / 1e6) - 1) * 100 if tok and raw else None,
                         "why": sells[-1].get("why"), "decided_gain": None, "sales": len(sells)})

    def mae(kind, col):
        e = [abs(r[col] - r["booked"]) for r in rows if r["kind"] == kind and r[col] is not None]
        return (round(math.fsum(e) / len(e), 3), len(e)) if e else (None, 0)

    summary = {k: {c: mae(k, c) for c in ("at", "next", "model", "x1")} for k in ("live", "shadow")}
    for r in rows:
        for c in ("booked", "at", "next", "model", "x1", "tokens_vs_raw_pct", "decided_gain"):
            r[c] = rnd(r[c], 2)
    return {"rows": rows, "mae": summary}


def buy_decided(cv, b):
    """(when the live buy b was free to go, its RugCheck wait) or None: the later of its trigger's reading (tt) and
    the last RugCheck report the loop took for the coin by the buy (the bot holds a fired coin until a passing report
    lands: np_rug_timeout_s for a missing one, np_rug_recheck_s re-asks while too few holders show). From then to
    the buy line is the fill's own latency (the quotes, the honeypot sell simulation, the loop turn), which every
    rule pays; the wait before it is the current rule's RugCheck, which the study's rules leave out."""
    tb, tt = num(b.get("t")), num(b.get("tt"))
    if tb is None or tt is None or tb < tt - EPS:
        return None
    dec = tt
    for x in cv.rugs:
        t = num(x.get("t"))
        if t is not None and dec < t <= tb + EPS:
            dec = t
    return dec, dec - tt


def latency(views):
    """The tape's own latency: entries (a buy line's t against the moment it was free to go, buy_decided(): its
    trigger's reading or the RugCheck report it waited for, within 10 s), the RugCheck waits of those same buys
    (from the trigger's reading to that report: the REF rows' entries pay it on top), and exits (a sell line's
    lat_ms: its deciding reading to the fill). -> (entry latencies, exit latencies, the entries' RugCheck waits)"""
    lin, lout, wait = [], [], []
    for cv in views:
        for b in cv.buys:
            got = buy_decided(cv, b)
            if got is not None and float(b["t"]) - got[0] <= 10.0:
                lin.append(max(0.0, float(b["t"]) - got[0]))
                wait.append(got[1])
        for s in cv.sells:
            lat = num(s.get("lat_ms"))
            if lat is not None and 0 <= lat <= 10000:
                lout.append(lat / 1000.0)
    return lin, lout, wait


# ---------------------------------------------------------------- tape checks and coverage

def coverage(views, mk):
    """Per create hour: creates, skipped at the create, with readings, near a trigger, followed, ended by the cap
    (and how old then), with a gap, completed, late bloomers the tape cannot see (not near by 180 s but 10+ SOL in
    at their last reading); and the seconds unknown for every coin (the poller, lost lines), the holes by cause, and
    the heartbeat stretches the poller was judged on (with a failed call, holes; judged by the poller's own answer
    gaps or by its call counts, and the old tapes' holes over two stretches)."""
    rows = collections.OrderedDict()
    holes = mk.hole_seconds()
    for cv in sorted(views, key=lambda v: v.t0):
        r = rows.setdefault(cv.hour, collections.Counter())
        r["creates"] += 1
        r["skipped"] += 1 if cv.skip is not None else 0
        r["with_readings"] += 1 if len(cv.ts) > 1 else 0
        r["near"] += 1 if cv.near_t is not None else 0
        r["followed"] += 1 if cv.follow_t is not None else 0
        cap = [t for t, why in cv.ends if why == "cap"]
        if cap:
            r["cap_ends"] += 1
            if cap[0] - cv.t0 < 600:
                r["cap_before_600s"] += 1
        r["gap_coins"] += 1 if cv.gaps else 0
        r["completed"] += 1 if cv.cx[-1] else 0
        if cv.near_t is None and cv.rs[-1] - cv.rsol0 >= 10.0:
            r["late_bloomers_unseen"] += 1
    out = []
    for h, r in rows.items():
        d = {"hour": iso(h)[:13]}
        d.update(r)
        d["unknown_s"] = round(holes.get(h, 0.0), 1)
        out.append(d)
    total = collections.Counter()
    for r in rows.values():
        total.update(r)
    total = dict(total)
    total["unknown_s"] = round(sum(holes.values()), 1)
    by = {}
    for a, b, why in mk.parts:                          # each heartbeat stretch apart (they may overlap)
        d = by.setdefault(why, {"n": 0, "s": 0.0})
        d["n"] += 1
        d["s"] += b - a
    ps = dict(getattr(mk, "poll_stretches", None) or {})
    if ps:                                              # the regimes too, at 0 when none (Market.poller_stretch)
        for k in ("n", "failed_call", "hole", "by_gaps", "by_counts", "carried"):
            ps.setdefault(k, 0)
    return {"hours": out, "total": total,
            "holes_by_cause": {k: {"n": v["n"], "s": round(v["s"], 1)} for k, v in sorted(by.items())},
            "poller_stretches": ps}


def blind_cause(why):
    """The cause in a blind trade's why: "blind: tape hole" -> "tape hole", "stop -12% (filled blind: run end)" ->
    "run end", anything else as it is."""
    w = str(why or "")
    if "(filled blind: " in w:
        return w.split("(filled blind: ", 1)[1].rstrip(")").strip() or "?"
    if w.startswith("blind: "):
        return w[7:].strip() or "?"
    return w or "?"


def blind_causes(trades):
    """Every trade of every combination: how many there were, how many ended blind, and why (the tape's end of the
    coin, a gap, a hole, the run's end): to judge the 10 % blind gate on a real tape."""
    n, causes = 0, collections.Counter()
    for t in trades:
        n += 1
        if t.blind:
            causes[blind_cause(t.why)] += 1
    k = sum(causes.values())
    return {"trades": n, "blind": k, "share": rnd(k / n, 3) if n else None,
            "causes": dict(causes.most_common())}


def sanity(views):
    """The curve identity vsol = 30 + real SOL on every reading, and the real SOL a curve completes at (about 85);
    and the readings no standard pump.fun curve can be in by newpairs.curve_sane (the live bot's gate: odd ones it
    never prices, fills or exits on), counted on every reading of the tape (a completed curve's left out: its
    migration withdraws the reserves), with the coins that had any."""
    bad = n = 0
    done = []
    for cv in views:
        for j in range(1, len(cv.ts)):
            n += 1
            if abs(cv.vs[j] - 30.0 - cv.rs[j]) > 0.05:
                bad += 1
        if cv.cx[-1]:
            done.append(cv.rs[-1])
    odd = [cv.odd or 0 for cv in views]
    return {"readings": n, "vsol_not_30_plus_real": bad, "completed": len(done),
            "complete_real_sol": [rnd(min(done), 1), rnd(median(done), 1), rnd(max(done), 1)] if done else None,
            "odd_readings": sum(odd), "odd_coins": sum(1 for x in odd if x)}


def fire_check(views):
    """The live trigger recomputed with each run's own settings against the tape's first fire line per coin: the
    share found within 2 s, and the fires the tape has that the recomputation does not (and the other way). A SNIPE
    run's fire lines are its first readings bought, not momentum triggers: counted apart (snipe_fire_lines), never
    checked against the trigger (its runs recompute none)."""
    match = miss = extra = 0
    snipes = sum(len(cv.snipe_fires or ()) for cv in views)
    for cv in views:
        fires = sorted(float(x.get("tt") or x["t"]) for x in cv.fires)
        mine = cv.mom.get("RUN")
        if fires and mine is not None:
            if abs(fires[0] - mine) <= 2.0:
                match += 1
            else:
                miss += 1
        elif fires:
            miss += 1
        elif mine is not None:
            extra += 1
    n = match + miss
    return {"fire_lines": n, "found_within_2s": match, "share": rnd(match / n, 3) if n else None,
            "recomputed_without_a_line": extra, "snipe_fire_lines": snipes,
            "note": "a recomputed fire without a fire line is a coin the live bot had rejected before its trigger (a "
                    "RugCheck prefetch FAIL, a holder wait): the study's triggers leave RugCheck out on purpose"}


def pctl(xs, q):
    """The q quantile of xs (linear between the closest ranks: q 0.5 is the median), or None."""
    xs = sorted(xs)
    if not xs:
        return None
    k = q * (len(xs) - 1)
    i = int(math.floor(k))
    if i + 1 >= len(xs):
        return xs[-1]
    return xs[i] + (xs[i + 1] - xs[i]) * (k - i)


FIRST_NOTE = ("trades before the first reading (the launch block's snipers, the wallets that make +90 % in the first "
              "seconds) are invisible to the tape: the SNIPE rules can only buy after them")


def first_reading(views, mk, within=None):
    """How far the coins had moved before the bot could see their curve, on the coins the first-reading SNIPE rules
    may decide on (first_reading_why; the other watched coins, not skipped at their create, counted by why not in
    left_out): the delay from the create line to the first reading (s), the price's rise there over the create's
    own state (%), the real SOL in the curve then and how much of it came in since the create (SOL); with within
    (L + a poll), the share read by then (a SNIPE create order is priced on that reading). Descriptive, every part
    of the tape."""
    watched = 0
    left = collections.OrderedDict((k, 0) for k in FIRST_LEFT_OUT)
    delay, rise, real, since = [], [], [], []
    for cv in views:
        if cv.skip is not None:
            continue
        watched += 1
        t, why = first_reading_why(cv, mk)
        if t is None:
            left[why] += 1
            continue
        delay.append(t - cv.t0)
        rise.append(rise_at_first(cv))
        real.append(cv.rs[1])
        since.append(cv.rs[1] - cv.rsol0)
    n = len(delay)

    def share(ok):
        return rnd(sum(1 for x in rise if ok(x)) / n, 3) if n else None

    def q(xs, p, nd=3):
        v = rnd(pctl(xs, p), nd)
        return None if v is None else v + 0.0          # never "-0.0" (a curve read at the create's own state)

    return {"coins": n, "watched": watched, "left_out": dict(left), "max_delay_s": SNIPE_MAX_DELAY_S,
            "delay_s": {"median": q(delay, 0.5), "p90": q(delay, 0.9)},
            "create_fill": None if within is None else {
                "within_s": rnd(within, 3),
                "share": rnd(sum(1 for d in delay if d <= within + EPS) / n, 3) if n else None},
            "rise_pct": {"median": q(rise, 0.5, 2), "p75": q(rise, 0.75, 2), "p90": q(rise, 0.9, 2),
                         "share_50": share(lambda x: x >= 50.0 - 1e-9), "share_le_10": share(lambda x: x <= 10.0 + 1e-9),
                         "share_le_30": share(lambda x: x <= 30.0 + 1e-9)},
            "real_sol": {"median": q(real, 0.5), "p90": q(real, 0.9)},
            "sol_in_since_create": {"median": q(since, 0.5), "p90": q(since, 0.9)},
            "note": FIRST_NOTE}


# ---------------------------------------------------------------- the study

def load(data, since=None, until=None):
    """The coins (load_tapes, by create time) and the system lines (load_system, from 6 hours before `since`, so a
    run's start line is there, to 2 hours after `until`, so its exits are)."""
    s, u = recorder._epoch(since), recorder._epoch(until)
    tapes = recorder.load_tapes(data, s, u)
    system = recorder.load_system(data, s - 6 * 3600 if s is not None else None, u + 7200 if u is not None else None)
    return tapes, system, s, u


def study(data=None, since=None, until=None, split=0.7, cfg=None, max_rules=300, balance=85.0, order_usd=None,
          lat=None, fill="next", poll_s=1.0, min_trades=15, top=15, seed=7, boot=2000, embargo_s=900.0, out=None,
          loaded=None):
    """The whole study -> the report dict (see the module's doc). loaded: (tapes, system, since, until) already read."""
    say = out or (lambda *a: None)
    wall = time.monotonic()
    cfg = cfg if cfg is not None else bot.load_config(bot.CONFIG)
    tapes, system, since, until = loaded if loaded is not None else load(data, since, until)
    mk = Market(system, cfg)
    order_usd = float(order_usd) if order_usd else float(cfg.get("position_pct", 10.0)) / 100.0 * float(balance)
    costs = Costs(cfg, order_usd)
    views, no_create = views_of(tapes, mk, cfg)
    say("study · %d coins (%d without a create line) · %.1f s" % (len(views), no_create, time.monotonic() - wall))
    rep = {"generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "data": data,
           "limits": LIMITS}
    if not views:
        rep.update(verdict={"pass": [], "text": "no rule beats costs on held-out data",
                            "why": ["no coins on the tape (%s)" % (data or "no data")]})
        rep["markdown"] = markdown(rep)
        return rep
    t0s = [cv.t0 for cv in views]
    lo = since if since is not None else min(t0s)
    hi = until if until is not None else max(t0s) + 1e-3
    split_t = lo + float(split) * (hi - lo)
    for cv in views:
        cv.part = "held" if cv.t0 >= split_t else ("embargo" if cv.t0 >= split_t - embargo_s else "first")
    lin, lout, waits = latency(views)
    l_in = float(lat) if lat is not None else (median(lin) if len(lin) >= 3 else DEFAULT_LAT_S)
    l_out = float(lat) if lat is not None else (median(lout) if len(lout) >= 3 else DEFAULT_LAT_S)
    rug_wait = median(waits) if len(waits) >= 3 else 0.0      # the current rule's RugCheck: the REF rows only
    sim = Sim(costs, l_in, l_out, poll_s, fill)
    sim_ref = sim.but(l_in=l_in + rug_wait)
    sol_med = median(mk.sol_v) if mk.sol_v else DEFAULT_SOL_USD
    rep["settings"] = {"split": split, "split_at": iso(split_t), "since": iso(lo), "until": iso(hi),
                       "first_part_h": round((split_t - lo) / 3600, 2), "held_out_h": round((hi - split_t) / 3600, 2),
                       "embargo_s": embargo_s, "order_usd": round(order_usd, 2), "balance": balance,
                       "latency_in_s": round(l_in, 3), "latency_out_s": round(l_out, 3),
                       "rugcheck_wait_s": round(rug_wait, 3), "fill": fill,
                       "poll_s": poll_s, "min_trades": min_trades, "top": top, "seed": seed, "boot": boot,
                       "max_rules": max_rules, "sol_usd_median": rnd(sol_med, 2),
                       "sol_prices": len(mk.sol_v), "default_sol": not mk.sol_v}
    rep["latency"] = {"entries": {"n": len(lin), "median_s": rnd(median(lin), 3),
                                  "from": "the trigger's reading, or the RugCheck report the buy waited for"},
                      "rugcheck_wait": {"n": len(waits), "median_s": rnd(median(waits), 3),
                                        "mean_s": rnd(math.fsum(waits) / len(waits), 3) if waits else None,
                                        "waited": sum(1 for w in waits if w > 0.05),
                                        "applies_to": "the REF rows only (the study's rules leave RugCheck out)"},
                      "exits": {"n": len(lout), "median_s": rnd(median(lout), 3)},
                      "used": {"in": round(l_in, 3), "out": round(l_out, 3), "ref_in": round(l_in + rug_wait, 3)},
                      "from": "--latency" if lat is not None else "the tape's own fills, 1 s when it has fewer than 3"}
    rep["costs"] = {"model": "bot.curve_buy_tokens / curve_sell_sol, curve_fee_pct %g, latency_slip_pct %g each way, "
                             "%g SOL a order" % (costs.fee_pct, costs.slip, costs.fee_sol),
                    "round_trip": [costs.round_trip(v, sol_med) for v in (32.0, 40.0, 60.0)],
                    "round_trip_2x": costs.__class__(cfg, 2 * order_usd).round_trip(40.0, sol_med)}
    rep["tape"] = {"coins": len(views), "no_create_line": no_create, "hours": round((hi - lo) / 3600.0, 2),
                   "runs": len(mk.runs), "stops": mk.stops, "holes": len(mk.holes),
                   "first_part_coins": sum(1 for v in views if v.part == "first"),
                   "held_out_coins": sum(1 for v in views if v.part == "held"),
                   "embargo_coins": sum(1 for v in views if v.part == "embargo")}
    rep["coverage"] = coverage(views, mk)
    rep["sanity"] = sanity(views)
    rep["fire_check"] = fire_check(views)
    rep["first_reading"] = first_reading(views, mk, l_in + poll_s)
    rep["calibration"] = calibrate(views, mk, costs, poll_s, l_in, l_out, cfg, exit_policies(cfg)["X1"][0])
    say("study · tape, costs and calibration done (%.1f s)" % (time.monotonic() - wall))

    # the combinations, capped
    combos, entries = build_grid()
    room = max(0, int(max_rules) - (OVERLAY_ROOM if int(max_rules) > 2 * OVERLAY_ROOM else 0))
    left_out = max(0, len(combos) - room)
    combos = combos[:room]
    exits = exit_policies(cfg)
    used_entries = collections.OrderedDict()
    for c in combos:
        used_entries[c["entry"]] = entries[c["entry"]]
    dec = decide(views, mk, used_entries, seed)
    say("study · %d entry rules decided (%.1f s)" % (len(used_entries), time.monotonic() - wall))
    trades = {}
    skipped = collections.Counter()
    for c in combos:
        pol = exits[c["exit"]][0]
        s = sim_ref if c["family"] == "REF" else sim       # the tape's fire lines waited for RugCheck too
        got = []
        for i, t in dec[c["entry"]].items():
            cv = views[i]
            if cv.part == "embargo":
                continue
            tr, why = simulate(cv, mk, t, pol, s)
            if tr is None:
                skipped[why] += 1
                continue
            got.append(tr)
        got.sort(key=lambda x: x.dec)
        trades[c["key"]] = got
    rep["coverage"]["blind"] = blind_causes(t for c in combos for t in trades[c["key"]])
    say("study · %d combinations simulated (%.1f s)" % (len(combos), time.monotonic() - wall))

    def part(key, which):
        return [t for t in trades[key] if views[t.cv].part == which]

    # rank the selectable ones on the first part
    rows = []
    for c in combos:
        st = stats(part(c["key"], "first"), order_usd)
        rows.append(dict(c, first=st))
    ranked = sorted([r for r in rows if r["selectable"] and r["first"]["n"] >= min_trades and r["first"].get("score")
                     is not None], key=lambda r: (-r["first"]["score"], -r["first"]["mean"], r["key"]))
    # the quality overlays on the two best first-part rules of the older families (never a SNIPE row: at a first
    # reading or a create Q1, Q3, Q4 and Q5 are unknowable, no metadata or RugCheck line yet and the first 5 s not
    # over, and Q2 repeats SNIPE's own dev filter), and on the reference
    over = []
    base_two = [r for r in ranked if r["family"] != "SNIPE"][:2]
    ref_key = next((c["key"] for c in combos if c["kind"] == "ref"), None)
    for src, kind in [(r, "rule") for r in base_two] + ([({"key": ref_key}, "ref")] if ref_key else []):
        for q in sorted(OVERLAYS):
            if len(combos) + len(over) >= int(max_rules):
                break
            key = "%s + %s" % (src["key"], q)
            kept, unknown = [], 0
            for t in trades[src["key"]]:
                ok = overlay(q, views[t.cv], t.dec)
                if ok is None:
                    unknown += 1
                elif ok:
                    kept.append(t)
            trades[key] = kept
            c = {"key": key, "kind": "overlay" if kind == "rule" else "ref", "family": "Q", "entry": src["key"],
                 "exit": q, "text": OVERLAYS[q], "selectable": kind == "rule", "unknown": unknown}
            over.append(c)
            rows.append(dict(c, first=stats(part(key, "first"), order_usd)))
    combos = combos + over
    M = sum(1 for c in combos if c["selectable"])
    z = zq(1 - 0.025 / max(1, M))
    ranked = sorted([r for r in rows if r["selectable"] and r["first"]["n"] >= min_trades and r["first"].get("score")
                     is not None], key=lambda r: (-r["first"]["score"], -r["first"]["mean"], r["key"]))
    for k, r in enumerate(ranked, 1):
        r["rank"] = k
    rep["tried"] = {"combos": len(combos), "selectable": M, "left_out_by_cap": left_out, "max_rules": max_rules,
                    "ranked": len(ranked), "z": round(z, 3),
                    "bonferroni": "two-sided (1 - 0.05/%d) interval: z = %.3f, lower = mean - z x max(SE trade, SE "
                                  "hour)" % (M, z),
                    "not_traded": dict(skipped)}
    say("study · %d combinations tried, %d selectable (Bonferroni z %.2f), %d ranked" % (len(combos), M, z, len(ranked)))

    # the held-out part, once, for EVERY ranked combination: z above already pays for all M of them, so judging them
    # all costs no error rate, and a family that ranked low on the first part is still judged. The gates that need
    # no bootstrap and no re-run come first; the bootstrap, the stress table, the nulls and the marks to market only
    # for the top --top (shown) and for every one that passes those.
    finalists, judged = [], []
    seen = collections.Counter()                        # the rank among the older families, and among SNIPE's rows
    for r in ranked:
        key = r["key"]
        held = part(key, "held")
        snipe = r["family"] == "SNIPE"
        seen[snipe] += 1
        shown = seen[snipe] <= min(SNIPE_SHOWN if snipe else INF, max(0, int(top)))   # SNIPE pushes no older row out
        quick = stats(held, order_usd)
        cheap = gates(quick, r["first"], {}, min_trades)
        row = {"rank": r["rank"], "key": key, "family": r["family"], "n": quick["n"], "mean": rnd(quick.get("mean"), 3)}
        if not shown and not all(cheap[g]["ok"] for g in CHEAP_GATES):
            row.update(passes=False, failed=[g for g in CHEAP_GATES if not cheap[g]["ok"]])
            judged.append(row)
            continue
        st = stats(held, order_usd, boot=boot, seed=seed, z=z)
        f = {"rank": r["rank"], "key": key, "family": r["family"], "kind": r["kind"], "text": r["text"],
             "exit_text": exits[r["exit"]][1] if r["exit"] in exits else OVERLAYS.get(r["exit"]),
             "first": small(r["first"]), "held": small(st), "shown": shown}
        f["stress"] = stress(r, held, views, mk, sim, exits, order_usd, cfg)
        f["gates"] = gates(st, r["first"], f["stress"], min_trades)
        f["passes"] = all(g["ok"] for g in f["gates"].values())
        row.update(passes=f["passes"], failed=[g for g, v in f["gates"].items() if not v["ok"]])
        judged.append(row)
        if not (shown or f["passes"]):
            continue
        done = [t for t in held if t.complete]
        f["completions"] = {"n": len(done), "share": rnd(len(done) / len(held), 3) if held else None,
                            "mean_part": rnd(math.fsum(t.pnl for t in done) / len(held), 3) if held else None,
                            "mean_without": rnd(stats([t for t in held if not t.complete], order_usd).get("mean"), 3),
                            "note": "sold into the curve's last state at the completing reading (no price after it on "
                                    "the tape); mean_part: their share of the held-out mean, in points"}
        f["nulls"] = nulls(r, held, views, mk, sim, exits, seed)
        f["mtm"] = mtm(held, views, mk, sim)
        f["trades"] = [{"mint": views[t.cv].mint, "sym": views[t.cv].sym, "dec": iso(t.dec),
                        "age": rnd(t.dec - t.t0, 1), "pnl": rnd(t.pnl, 2), "gross": rnd(t.gross, 2),
                        "why": t.why, "blind": t.blind, "complete": t.complete} for t in held][:400]
        finalists.append(f)
    rep["finalists"] = finalists
    fams = collections.Counter(r["family"] for r in ranked)
    rep["held_out"] = {"judged": len(judged), "shown": sum(1 for f in finalists if f["shown"]),
                       "passed": sum(1 for r in judged if r["passes"]), "families": dict(sorted(fams.items())),
                       "note": "every ranked combination is judged on the held-out part at the same Bonferroni z; "
                               "the top --top of the older families by first-part rank are shown, the best %d SNIPE "
                               "rows apart, and every one that passes" % SNIPE_SHOWN,
                       "rows": judged}
    rep["ranking"] = [{"rank": r["rank"], "key": r["key"], "family": r["family"], "first": small(
        {k: r["first"].get(k) for k in ("n", "mean", "median", "win", "score", "blind_share", "gross")})}
        for r in ranked]
    rep["first_part"] = [{"key": r["key"], "kind": r["kind"], "family": r["family"], "selectable": r["selectable"],
                          "first": small({k: r["first"].get(k) for k in ("n", "mean", "median", "win", "score",
                                                                         "blind_share", "gross", "complete")}),
                          "flag": "blind > 10 %" if (r["first"].get("blind_share") or 0) > BLIND_MAX else None}
                         for r in rows]
    rep["tried"]["blind_flagged"] = sum(1 for r in rep["first_part"] if r["flag"])
    # the base, control and reference rows: descriptive, on every part
    desc = []
    for c in combos:
        if c["selectable"]:
            continue
        allp = [t for t in trades[c["key"]]]
        desc.append({"key": c["key"], "kind": c["kind"], "family": c["family"], "text": c["text"],
                     "exit_text": exits[c["exit"]][1] if c["exit"] in exits else OVERLAYS.get(c["exit"]),
                     "first": small(stats(part(c["key"], "first"), order_usd)),
                     "held": small(stats(part(c["key"], "held"), order_usd)),
                     "all": small(stats(allp, order_usd, boot=min(boot, 1000), seed=seed))})
    desc += booked_rows(views, order_usd)
    rep["base_rows"] = desc
    passed = [f for f in finalists if f["passes"]]
    hours = (hi - lo) / 3600.0
    if passed:
        text = "PASS: " + "; ".join("%s (held-out %d trades, mean %+.2f %%, Bonferroni lower %+.2f %%)" % (
            f["key"], f["held"]["n"], f["held"]["mean"], f["held"]["lower"]) for f in passed[:10])
        if len(passed) > 10:
            text += "; and %d more (the held-out table)" % (len(passed) - 10)
    else:
        text = "no rule beats costs on held-out data"
    why = []
    if not ranked:
        why.append("no selectable rule made %d trades on the first part: too little tape to rank" % min_trades)
    elif not passed:
        thin = sum(1 for r in judged if (r["n"] or 0) < min_trades)
        if thin == len(judged):
            why.append("none of the %d ranked combinations had %d held-out trades: too little tape to judge" % (
                len(judged), min_trades))
    cmae = (rep["calibration"]["mae"]["live"]["at"] or (None, 0))[0]
    if cmae is not None and cmae > CAL_WARN_PP:
        why.append("the cost model is %.2f points off the bot's own booked trades (mean abs error): every P&L here is "
                   "that much less sure (calibration table)" % cmae)
    if hours < DAYS_FOR_VERDICT * 24:
        why.append("only %.1f h of tape (under %g days): every number here is descriptive, not a verdict" % (
            hours, DAYS_FOR_VERDICT))
    rep["verdict"] = {"pass": [f["key"] for f in passed], "text": text, "why": why}
    rep["rules"] = RULES_NOTE
    rep["wall_s"] = round(time.monotonic() - wall, 1)
    rep["markdown"] = markdown(rep)
    say("study · done (%.1f s)" % rep["wall_s"])
    return rep


CHEAP_GATES = ("held_out_n", "held_out_mean", "first_part_mean", "blind_share")   # need no bootstrap, no re-run


def gates(st, first, stress_rows, min_trades):
    """The PASS gates of a combination on the held-out part (module doc): each {"ok", "value", "need"}. A completion
    is sold into the curve's last state at the completing reading (no latency, no next poll: the tape has no price
    after it), so a pass must also hold with every completion at x0.85."""
    n = st.get("n") or 0
    lat2 = (stress_rows.get("latency +2 s") or {}).get("mean")
    cx = (stress_rows.get("complete x0.85") or {}).get("mean")
    return {
        "held_out_n": {"ok": n >= min_trades, "value": n, "need": ">= %d" % min_trades},
        "held_out_mean": {"ok": n > 0 and st["mean"] > 0, "value": rnd(st.get("mean"), 3), "need": "> 0"},
        "bonferroni_lower": {"ok": n > 1 and (st.get("lower") or -INF) > 0, "value": rnd(st.get("lower"), 3),
                             "need": "> 0"},
        "first_part_mean": {"ok": (first.get("mean") or -INF) > 0, "value": rnd(first.get("mean"), 3), "need": "> 0"},
        "blind_share": {"ok": n > 0 and st["blind_share"] <= BLIND_MAX + 1e-12, "value": rnd(st.get("blind_share"), 3),
                        "need": "<= %g" % BLIND_MAX},
        "latency_plus_2s": {"ok": lat2 is not None and lat2 >= 0, "value": rnd(lat2, 3), "need": ">= 0"},
        "complete_x085": {"ok": cx is not None and cx >= 0, "value": rnd(cx, 3), "need": ">= 0"},
    }


def _resim(r, held, views, mk, sim, exits):
    """The held-out trades of a finalist again under another fill model (the same decisions)."""
    out = []
    pol = _policy_of(r, exits)
    for t in held:
        cv = views[t.cv]
        tr, _ = simulate(cv, mk, t.dec, pol, sim)
        if tr is not None:
            out.append(tr)
    return out


def _policy_of(r, exits):
    """A combination's exit policy (an overlay's is its source rule's)."""
    key = r["key"]
    if r.get("family") == "Q":
        key = r["entry"]
    return exits[key.split(" | ")[1]][0]


def stress(r, held, views, mk, sim, exits, order_usd, cfg):
    """A finalist's held-out mean under harder fills: no latency, 2 s more latency each way than the tape's own
    (never easier than the base, whatever the tape measured), a slow poll (the next reading up to 3 s late), costs+
    (1 % more haircut each way and a 0.003 SOL tip an order), a 2x order, completions at x0.85, blind exits at
    x0.7."""
    out = {}
    variants = {
        "latency 0 s": sim.but(l_in=0.0, l_out=0.0),
        "latency +2 s": sim.but(l_in=sim.l_in + STRESS_LAT_S, l_out=sim.l_out + STRESS_LAT_S),
        "slow poll 3 s": sim.but(poll_s=3.0),
        "costs+": sim.but(costs=Costs(cfg, sim.costs.order, extra_slip_pct=1.0, extra_fee_sol=0.003)),
        "2x order": sim.but(costs=Costs(cfg, 2 * sim.costs.order)),
        "complete x0.85": sim.but(haircut=0.85),
    }
    for name, s in variants.items():
        got = _resim(r, held, views, mk, s, exits)
        st = stats(got, s.costs.order)
        out[name] = {"n": st["n"], "mean": rnd(st.get("mean"), 3)}
    if held:
        out["blind x0.7"] = {"n": len(held), "mean": rnd(math.fsum(t.pess for t in held) / len(held), 3)}
    return out


def nulls(r, held, views, mk, sim, exits, seed):
    """Two matched nulls for a finalist's held-out trades (the skeptic's): TIMING, the same coin at a random polled
    time in [45 s, 600 s]; SELECTION, another coin of the same create hour, near a trigger and polled at the same age.
    The same exit. -> the paired mean difference (rule - null) and how many trades had a null."""
    pol = _policy_of(r, exits)
    by_hour = collections.defaultdict(list)
    for cv in views:
        if cv.part == "held" and cv.near_t is not None:
            by_hour[cv.hour].append(cv)
    out = {}
    rng = random.Random("%s:%s" % (seed, r["key"]))
    for name in ("timing", "selection"):
        diffs = []
        for t in held:
            cv = views[t.cv]
            age = t.dec - cv.t0
            other, at = None, None
            if name == "timing":
                lo = max(45.0, (cv.near_t - cv.t0) if cv.near_t is not None else 45.0)
                if lo < 600.0:
                    a = cv.t0 + rng.uniform(lo, 600.0)
                    if cv.polled(mk, a) and not cv.complete_by(a):
                        other, at = cv, a
            else:
                pool = [v for v in by_hour.get(cv.hour, ()) if v is not cv]
                rng.shuffle(pool)
                for v in pool[:5]:
                    a = v.t0 + age
                    if v.near_t <= v.t0 + min(age, NEAR_TOO_LATE) + EPS and v.polled(mk, a) and not v.complete_by(a):
                        other, at = v, a
                        break
            if other is None:
                continue
            tr, _ = simulate(other, mk, at, pol, sim)
            if tr is not None:
                diffs.append(t.pnl - tr.pnl)
        out[name] = {"n": len(diffs), "mean_diff": rnd(math.fsum(diffs) / len(diffs), 3) if diffs else None,
                     "se": rnd(statistics.stdev(diffs) / math.sqrt(len(diffs)), 3) if len(diffs) > 1 else None}
    return out


def mtm(held, views, mk, sim):
    """Design 1's entry check: the net % if each held-out trade had been sold everything at +60 / +180 / +300 s
    after its fill (the exit's own path left out). Every trade counts: one the tape lost before its mark (an
    unfollowed coin leaves it at 181 s) at its blind sale, the last state the tape knew, n_blind of them; the still
    polled ones alone apart (mean_polled: only the coins the recorder followed, the ones that came near a trigger,
    are left by +180 s, so alone they are survivors)."""
    out = {}
    for h in (60.0, 180.0, 300.0):
        got, polled = [], []
        for t in held:
            cv = views[t.cv]
            tr, _ = simulate(cv, mk, t.dec, Hold(h), sim)
            if tr is None:
                continue
            got.append(tr.pnl)
            if not tr.blind:
                polled.append(tr.pnl)
        out["+%ds" % h] = {"n": len(got), "mean": rnd(math.fsum(got) / len(got), 3) if got else None,
                           "n_blind": len(got) - len(polled),
                           "mean_polled": rnd(math.fsum(polled) / len(polled), 3) if polled else None}
    return out


def booked_rows(views, order_usd):
    """The live trades and the shadow trades as the bot booked them (their final sale's pnl_pct), by create part."""
    out = []
    for kind, pairs in (("live", _pair_live), ("shadow", _pair_shadow)):
        rows = []
        for cv in views:
            for o, sells in pairs(cv):
                p = num(sells[-1].get("pnl_pct"))
                if p is not None:
                    rows.append(Trade(cv.idx, cv.t0, float(o["t"]), float(o["t"]), float(sells[-1]["t"]), p, p, False,
                                      False, p, sells[-1].get("why")))
        if not rows:
            continue
        part = {"first": [t for t in rows if views[t.cv].part == "first"],
                "held": [t for t in rows if views[t.cv].part == "held"]}
        out.append({"key": "BOOKED %s" % kind, "kind": "booked", "family": "BOOKED",
                    "text": "the %s trades on the tape as the bot booked them" % kind, "exit_text": "as traded",
                    "first": small(stats(part["first"], order_usd)), "held": small(stats(part["held"], order_usd)),
                    "all": small(stats(rows, order_usd))})
    return out


RULES_NOTE = {
    "families": {
        "SURV": "designers 1 F1 (late survivor accumulation) and 3 F1 (survivor flow) merged: ages [A, A+180] for A in "
                "180 / 300 / 420 (1's 240 / 420 and 3's 180-600 / 300-600), net SOL in over 120 s >= f (1, 3, 6: the "
                "union), SOL in since launch >= S (0, 8: 1's 8; its 15 dropped for the budget), SOL buy share over 60 "
                "s >= 0.6 (3), no sell over 2 SOL in 120 s (1) nor 1.5 SOL in 60 s (3), at most 20 % under the high "
                "(the middle of 1's 25 % and 3's 15 %), real SOL under 50 (3: the late curve is GRAD's).",
        "DIP": "designers 1 F2 (dip after the first dump) and 3 F2 (dump absorbed) merged: the dump a single sell of "
               "1.5 or 3 SOL (3; 1's 2 SOL lies between) or a 35 % drawdown (1), after 3 SOL in (3), with 40 % of "
               "the real SOL left (3) and half the SOL in left at the entry (1); the rebound C = 0.3 / 1.0 (3) / 2.0 "
               "SOL net in since the trough (1's +5 / +12 % rebound is about 0.9 / 2.0 SOL at vsol 35), within 90 s "
               "(1: 120, 3: 60), at most 360 s old, no sell of 1 SOL since the dump, still 10 % under the high.",
        "ORG": "designer 1 F3 as proposed.",
        "GRAD": "designers 1 F4 and 3 F3 merged: real SOL >= 35 / 50 / 65 (the union), +2 / +5 SOL in 60 s (1), no "
                "single 3 SOL sell, at most 15 % under the high (1), by 780 s; exits E3 (out at 80 SOL), E2 and "
                "X3(180). The skeptic's X1 dropped here: a +10 % cap defeats the convex payoff the family bets on.",
        "BRK": "designer 1 F5 as proposed.",
        "MOM": "design 2's exit families on the live trigger recomputed (E_MOM_BASE) and the loose one (E_MOM_LOOSE), "
               "as proposed; the skeptic's F4 steelman (5 SOL, a 10 % pullback) under X1 / E2 / X3(180). Merged: "
               "design 2's untuned anchor (TP +13 / SL -4.5 curve / 300 s / quiet 3 min) is X1, the same exits in the "
               "bot's own gain terms; the skeptic's X2 tail exit is E2 (the same shape: a -20 % stop, armed at "
               "+20..+30 %, a 20..25 % trail); X3(H) and design 2's time-only hold H are one rule. Dropped: the "
               "skeptic's 1 s resampled buy counts (design 2 needs the raw reading counts to reproduce the live fire "
               "lines; they differ only on coins polled at 0.5 s, rare before an entry).",
        "Q1-Q5": "designer 1 F6 as proposed, each filter alone on the two best first-part rules of the older "
                 "families (counted in M; never a SNIPE row, where all but Q2 are unknowable and Q2 repeats its dev "
                 "filter) and on the reference row; a coin with no metadata, no RugCheck report or no reading in its "
                 "first 5 s or a decision inside them is an \"unknown\" class, left out and counted.",
        "SNIPE": "the ask (buy new coins in their first seconds, like the wallets that make +90 % in 5 s), "
                 "measured before any live change: decided at the coin's create line (SNIPE create, the first moment "
                 "the bot sees it; filled on the first reading when it came by t + L + a poll) or at its first curve "
                 "reading (a poll later; only the bot's first sight, at most 10 s after the create), on the create "
                 "and that reading only; any / the price at most +10 or +30 % over the create's / +10 % and the dev "
                 "at most 3 %; under X3 5 / 10 / 30 s, X1@175 and S1 (+25 % / -15 % / 30 s). 5 x 5 on purpose: each "
                 "combination costs Bonferroni width. BASE-A first is its base row. Never the overlays' source; its "
                 "best 5 are shown apart from the older rows' --top.",
        "BASE / C0 / REF": "the skeptic's BASE-A and BASE-B (near by the run's own thresholds, design 1's form, not "
                           "the strictest across runs), one random-time control C0 for designers 1 C0, 2 E_RAND and "
                           "3 BASE-C (one seeded draw per coin instead of 20), the fire lines under X1 and the "
                           "time-only holds (design 2's E_FIRE), the live and shadow trades as booked.",
    },
    "dropped": [
        "the skeptic's luck ceiling (the best of M random-entry twins, 1000 times) and Holm over 3 finalists: the "
        "Bonferroni bound over every combination tried answers the same question (selection among M) and is stricter",
        "rolling day folds and the 96 h / 4-day / weekend minimum: one chronological split by create time; under 3 "
        "days of tape the summary says the numbers are descriptive",
        "design 2's per-family finalist (one per family and entry set): every ranked combination is judged on the "
        "held-out part instead (the Bonferroni z already counts them all), the top 15 by the first-part lower bound "
        "(SNIPE's best 5 apart) shown with every pass",
        "the 1 s resampled buy counts (see MOM), the 20 random draws per coin (one), the per-rule \"lost to the cap\" "
        "counts (the coverage table counts the cap ends by hour for every coin)",
        "trimmed means, the mean without the top trades, the matched nulls and the rest of the stress table are "
        "reported, not gates (the gates: n, mean, Bonferroni, first-part mean, blind share, 2 s more latency each "
        "way, completions at x0.85)",
    ],
}


# ---------------------------------------------------------------- the summary

def _pct(x, nd=2):
    return "-" if x is None else ("%+." + str(nd) + "f") % x


def markdown(rep):
    """The study in Markdown: the verdict, the tape, the costs, the calibration, the finalists, the base rows."""
    out = ["## Edge study (memebot/study.py)", ""]
    v = rep.get("verdict") or {}
    out.append("**Verdict: %s**" % v.get("text", "no rule beats costs on held-out data"))
    for w in v.get("why") or []:
        out.append("- %s" % w)
    out.append("")
    st, tp = rep.get("settings") or {}, rep.get("tape") or {}
    if tp:
        out.append("Tape %s → %s (%.1f h, %d coins, recorded runs: %d) · split at %s: first part %.1f h (%d coins), "
                   "held-out %.1f h (%d coins), %d left out around the split" % (
                       st.get("since"), st.get("until"), tp.get("hours") or 0, tp.get("coins") or 0, tp.get("runs") or 0,
                       st.get("split_at"), st.get("first_part_h") or 0, tp.get("first_part_coins") or 0,
                       st.get("held_out_h") or 0, tp.get("held_out_coins") or 0, tp.get("embargo_coins") or 0))
        out.append("")
    c = rep.get("costs") or {}
    if c:
        rt = (c.get("round_trip") or [None, None])[1] or {}
        out.append("**Costs** (%s): a $%.2f order at SOL $%.0f, vsol %g, round trip at an unchanged price %s %% = pump.fun "
                   "fee %s · impact %s · haircut %s · fixed fee %s; at 2x the order %s %%" % (
                       c.get("model"), rt.get("order_usd") or 0, rt.get("sol_usd") or 0, rt.get("vsol") or 0,
                       _pct(rt.get("total_pct")), _pct(rt.get("pumpfun_fee_pct")), _pct(rt.get("impact_pct")),
                       _pct(rt.get("haircut_pct")), _pct(rt.get("fixed_fee_pct")),
                       _pct((c.get("round_trip_2x") or {}).get("total_pct"))))
        out.append("")
    lat = rep.get("latency") or {}
    cal = rep.get("calibration") or {}
    if lat:
        rw = lat.get("rugcheck_wait") or {}
        out.append("**Latency** (%s): entries %s s median over %d buys (from the trigger's reading, or the RugCheck "
                   "report a buy waited for), exits %s s over %d sales → used %s / %s s; the RugCheck wait before "
                   "those buys %s s median (%d of %d waited), on the REF rows only (entries %s s)" % (
                       lat.get("from"), lat["entries"].get("median_s"), lat["entries"].get("n") or 0,
                       lat["exits"].get("median_s"), lat["exits"].get("n") or 0, lat["used"]["in"], lat["used"]["out"],
                       rw.get("median_s"), rw.get("waited") or 0, rw.get("n") or 0, lat["used"].get("ref_in")))
    if cal:
        mae = cal.get("mae") or {}
        lv, sh = mae.get("live") or {}, mae.get("shadow") or {}
        out.append("**Calibration** (study P&L vs the bot's booked P&L, mean abs error in points): live trades: fills at "
                   "their own times %s (n %d), next poll %s, the study's model (decision + L) %s; the bot's scalp exit "
                   "(X1) replayed from each live fill %s · shadows: %s (n %d)" % (
                       (lv.get("at") or [None])[0], (lv.get("at") or [0, 0])[1], (lv.get("next") or [None])[0],
                       (lv.get("model") or [None])[0], (lv.get("x1") or [None])[0], (sh.get("at") or [None])[0],
                       (sh.get("at") or [0, 0])[1]))
        rows = [r for r in cal.get("rows") or [] if r["kind"] == "live"][:20]
        if rows:
            out.append("")
            out.append("| live trade | booked % | study (own times) % | study (model) % | X1 from its fill % | deciding "
                       "reading's gain % | exit |")
            out.append("|---|---|---|---|---|---|---|")
            for r in rows:
                out.append("| %s %s | %s | %s | %s | %s | %s | %s |" % (
                    r["sym"], (r["t"] or "")[11:16], _pct(r["booked"]), _pct(r["at"]), _pct(r["model"]),
                    _pct(r.get("x1")), _pct(r["decided_gain"]), str(r["why"] or "")[:40].replace("|", "/")))
        out.append("")
    san, fc = rep.get("sanity") or {}, rep.get("fire_check") or {}
    cov = (rep.get("coverage") or {}).get("total") or {}
    if cov:
        out.append("**Tape checks**: %d readings, %d off vsol = 30 + real SOL, %d odd by newpairs.curve_sane (%d coins: "
                   "no standard pump.fun curve reads so; the live bot never prices them); %d completions at real SOL %s "
                   "(min / median / max); fire lines found by the recomputed trigger within 2 s: %s of %d (%d more fires "
                   "without RugCheck; %d SNIPE fire lines apart, never checked against it) · coverage: %d "
                   "creates, %d near, %d followed, %d ended by the cap (%d before 600 s), %d with a gap, %d late "
                   "bloomers the tape cannot see, %.0f s unknown for every coin" % (
                       san.get("readings") or 0, san.get("vsol_not_30_plus_real") or 0, san.get("odd_readings") or 0,
                       san.get("odd_coins") or 0, san.get("completed") or 0,
                       san.get("complete_real_sol"), fc.get("found_within_2s") or 0, fc.get("fire_lines") or 0,
                       fc.get("recomputed_without_a_line") or 0, fc.get("snipe_fire_lines") or 0,
                       cov.get("creates") or 0, cov.get("near") or 0, cov.get("followed") or 0,
                       cov.get("cap_ends") or 0, cov.get("cap_before_600s") or 0, cov.get("gap_coins") or 0,
                       cov.get("late_bloomers_unseen") or 0, cov.get("unknown_s") or 0))
        out.append("")
    covd = rep.get("coverage") or {}
    bl, hb, ps = covd.get("blind") or {}, covd.get("holes_by_cause") or {}, covd.get("poller_stretches") or {}
    if bl or hb:
        out.append("**Blind exits** (every trade of every combination, overlays aside): %d of %d%s · causes: %s · "
                   "tape holes by cause (each heartbeat stretch): %s · not traded: %s" % (
                       bl.get("blind") or 0, bl.get("trades") or 0,
                       "" if bl.get("share") is None else " (%.1f %%)" % (bl["share"] * 100),
                       ", ".join("%s %d" % kv for kv in (bl.get("causes") or {}).items()) or "none",
                       (", ".join("%s %d (%.0f s)" % (k, v["n"], v["s"]) for k, v in hb.items()) or "none") + (
                           "; heartbeat stretches %d (%d judged by the poller's own answer gaps, %d by its call "
                           "counts), %d with a failed call, %d holes (a silence of %g s or more; by counts, a failed "
                           "call with that much unaccounted, %d of them over two stretches)" % (
                               ps.get("n") or 0, ps.get("by_gaps") or 0, ps.get("by_counts") or 0,
                               ps.get("failed_call") or 0, ps.get("hole") or 0, Market.POLL_FAIL_S,
                               ps.get("carried") or 0) if ps else ""),
                       ", ".join("%s %d" % kv for kv in sorted(((rep.get("tried") or {}).get("not_traded") or {})
                                                               .items(), key=lambda kv: -kv[1])) or "none"))
        out.append("")
    fr = rep.get("first_reading") or {}
    if fr:
        d, r, rl, si = (fr.get(k) or {} for k in ("delay_s", "rise_pct", "real_sol", "sol_in_since_create"))
        share = lambda x: "-" if x is None else "%.0f %%" % (x * 100)
        num_ = lambda x, nd=1: "-" if x is None else ("%." + str(nd) + "f") % x
        lo, cf = fr.get("left_out") or {}, fr.get("create_fill") or {}
        out.append("**First reading** (the earliest the bot sees a coin's curve, a poll after its create line: the "
                   "first-reading SNIPE rules' decision; of %d watched coins %d with a first reading a rule may decide "
                   "on, %d without one, %d complete at it, %d not on the tape (a gap line, lost lines or a known hole "
                   "before it), %d not polled then, %d later than %g s): %s s after the create line (median; p90 %s "
                   "s)%s; the price %s %% over the create's state (median; p75 %s %%, p90 %s %%; +50 %% or more on %s "
                   "of them, at most +10 %% on %s, +30 %% on %s); real SOL in the curve %s (median; p90 %s), %s of it "
                   "in since the create (p90 %s); %s" % (
                       fr.get("watched") or 0, fr.get("coins") or 0, lo.get("no reading") or 0, lo.get("complete") or 0,
                       (lo.get("gap") or 0) + (lo.get("tape hole") or 0), lo.get("not polled") or 0,
                       lo.get("late") or 0, fr.get("max_delay_s") or SNIPE_MAX_DELAY_S, num_(d.get("median"), 2),
                       num_(d.get("p90"), 2), "" if cf.get("within_s") is None else ", within %.1f s (L + a poll: a "
                       "SNIPE create order is priced on it) on %s" % (cf["within_s"], share(cf.get("share"))),
                       _pct(r.get("median"), 1), _pct(r.get("p75"), 1), _pct(r.get("p90"), 1), share(r.get("share_50")),
                       share(r.get("share_le_10")), share(r.get("share_le_30")), num_(rl.get("median"), 2),
                       num_(rl.get("p90"), 2), num_(si.get("median"), 2), num_(si.get("p90"), 2),
                       fr.get("note") or FIRST_NOTE))
        out.append("")
    tr = rep.get("tried") or {}
    if tr:
        out.append("**Rules**: %d combinations tried (%d selectable, %d left out by --max-rules %s), %d with %d+ first-part "
                   "trades ranked, %d flagged for over 10 %% blind exits on the first part; Bonferroni: %s" % (
                       tr.get("combos") or 0, tr.get("selectable") or 0, tr.get("left_out_by_cap") or 0,
                       tr.get("max_rules"), tr.get("ranked") or 0, st.get("min_trades") or 0,
                       tr.get("blind_flagged") or 0, tr.get("bonferroni")))
        out.append("")
    fins = rep.get("finalists") or []
    ho = rep.get("held_out") or {}
    if fins:
        extra = sum(1 for f in fins if not f.get("shown", True))
        sn = sum(1 for f in fins if f.get("shown", True) and f.get("family") == "SNIPE")
        out.append("**The held-out part**: every one of the %d ranked combinations judged at the same Bonferroni z "
                   "(families %s); shown: the top %d%s by first-part rank%s%s (net %% of a $%.2f order after every "
                   "cost):" % (ho.get("judged") or 0, " · ".join("%s %d" % kv for kv in (ho.get("families") or {}).items()),
                               len(fins) - extra - sn, " of the older families" if sn else "",
                               ", SNIPE's best %d apart," % sn if sn else "",
                               " and %d more that pass" % extra if extra else "", st.get("order_usd") or 0))
        out.append("")
        out.append("| # | rule | first part n / mean % | held-out n | win | mean % | median % | 95 % CI | Bonferroni lower "
                   "| $ total | hours up | blind | completed (mean at x0.85) | +2 s lat. | pass |")
        out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for f in fins:
            h, fp = f["held"], f["first"]
            ci = h.get("ci95")
            cx = f.get("completions") or {}
            out.append("| %d | %s | %d / %s | %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                f["rank"], f["key"].replace("|", "/"), fp.get("n") or 0, _pct(fp.get("mean")), h.get("n") or 0,
                "-" if h.get("win") is None else "%.0f%%" % (h["win"] * 100), _pct(h.get("mean")), _pct(h.get("median")),
                "[%s, %s]" % (_pct(ci[0]), _pct(ci[1])) if ci else "-", _pct(h.get("lower")), _pct(h.get("usd")),
                "-" if h.get("hours_up") is None else "%.0f%% of %d" % (h["hours_up"] * 100, h.get("hours") or 0),
                "-" if h.get("blind_share") is None else "%.0f%%" % (h["blind_share"] * 100),
                "-" if cx.get("share") is None else "%.0f%% (%s)" % (
                    cx["share"] * 100, _pct((f["stress"].get("complete x0.85") or {}).get("mean"))),
                _pct((f["stress"].get("latency +2 s") or {}).get("mean")),
                "**PASS**" if f["passes"] else "no: " + ", ".join(k for k, g in f["gates"].items() if not g["ok"])))
        for f in fins:
            if f["passes"]:
                bh = f["held"].get("by_hour") or {}
                out.append("- %s by create hour (held-out): %s" % (f["key"], " · ".join(
                    "%s %s (%d)" % (h[11:13] + "h", _pct(v[1], 1), v[0]) for h, v in bh.items())))
        out.append("")
    elif tr:
        out.append("No selectable rule made %d first-part trades: nothing to take to the held-out part." % (
            st.get("min_trades") or 0))
        out.append("")
    base = rep.get("base_rows") or []
    if base:
        out.append("**Base, control and reference rows** (descriptive, never selected): n / mean % / median % / win, "
                   "first part · held-out")
        out.append("")
        out.append("| row | exit | first part | held-out |")
        out.append("|---|---|---|---|")
        for b in base:
            def cell(s):
                if not s or not s.get("n"):
                    return "0"
                return "%d / %s / %s / %.0f%%" % (s["n"], _pct(s.get("mean")), _pct(s.get("median")),
                                                  (s.get("win") or 0) * 100)
            out.append("| %s | %s | %s | %s |" % (b["key"].replace(" | ", " / "), (b.get("exit_text") or "")[:70],
                                                  cell(b.get("first")), cell(b.get("held"))))
        out.append("")
    out.append("_Paper on paper: a pass is a hint for a small live paper test, never a change to config.json "
               "(memebot/study.py's doc lists the honest limits)._")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- command line

def main(argv=None, out=print):
    ap = argparse.ArgumentParser(description="Meme Bot edge study: rule families judged on the recorded tapes (paper).")
    ap.add_argument("--data", required=True, help="a folder of tapes (one subfolder per job works too)")
    ap.add_argument("--since", help="UTC ISO time: coins created from then (default: the tape's start)")
    ap.add_argument("--until", help="UTC ISO time: coins created before then (default: the tape's end)")
    ap.add_argument("--split", type=float, default=0.7, help="the first part's share of the create-time range")
    ap.add_argument("--out", help="write the report (JSON) here")
    ap.add_argument("--md", help="append the Markdown summary to this file (e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--max-rules", type=int, default=300, help="rule x parameter combinations at most")
    ap.add_argument("--config", default=bot.CONFIG, help="the settings (default memebot/config.json)")
    ap.add_argument("--balance", type=float, default=85.0, help="the book an order is position_pct of")
    ap.add_argument("--order-usd", type=float, default=None, help="the order size in $ (default position_pct x balance)")
    ap.add_argument("--latency", type=float, default=None, help="seconds from a decision to its fill (default: the tape's)")
    ap.add_argument("--fill", choices=("next", "at"), default="next", help="the next poll's state, or the last at or before")
    ap.add_argument("--poll-s", type=float, default=1.0, help="how long the next poll may take (s)")
    ap.add_argument("--min-trades", type=int, default=15)
    ap.add_argument("--top", type=int, default=15, help="how many to show in full (every ranked one is judged)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--boot", type=int, default=2000, help="bootstrap resamples")
    ap.add_argument("--embargo-s", type=float, default=900.0, help="coins created this close before the split are left out")
    a = ap.parse_args(argv)
    if not 0.1 <= a.split <= 0.95:
        ap.error("--split between 0.1 and 0.95")
    for name in ("since", "until"):
        if getattr(a, name) is not None and recorder._epoch(getattr(a, name)) is None:
            ap.error("--%s: a UTC ISO time like 2026-11-26T08:00:00Z" % name)
    for k in ("MEMEBOT_LIVE", "MEMEBOT_FEEDS", "MEMEBOT_REC"):    # never online, never a tape of its own
        os.environ[k] = "0"
    rep = study(a.data, a.since, a.until, a.split, bot.load_config(a.config), a.max_rules, a.balance, a.order_usd,
                a.latency, a.fill, a.poll_s, a.min_trades, a.top, a.seed, a.boot, a.embargo_s,
                out=lambda s: print(s, file=sys.stderr))
    out(rep["markdown"])
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(json_safe(rep), fh, indent=1, allow_nan=False, ensure_ascii=False)
    if a.md:
        with open(a.md, "a", encoding="utf-8") as fh:
            fh.write(rep["markdown"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
