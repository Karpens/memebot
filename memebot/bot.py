#!/usr/bin/env python3
"""Meme Bot: an overnight meme-coin trading terminal on Solana, on paper.

Two strategies (config.json "strategies"):
  new_pairs  the main one, first in and first out: brand-new pump.fun coins in their first seconds to
             minutes, still on the bonding curve (Axiom Pulse's "New Pairs" column). PumpPortal's free
             stream says when a coin is created, one batched getMultipleAccounts reads every watched
             curve every 0.5-1 s (memebot/feeds.py), memebot/newpairs.py holds the rules and
             memebot/plan.py the NEWS hold plan. Each reading of a held coin goes to its exits in the
             same loop turn; a reading that completes a watched coin's momentum trigger is bought in
             the same loop turn (RugCheck, the LEARNER, RISK, the SIZER, a Jupiter quote and the
             honeypot sell simulation in between).
  migrated   the older scanner (off by default): pairs a few minutes to hours old from GeckoTerminal
             and DexScreener, with its own rules below.

The agents, written as rules (the prompt in memebot/prompt.txt says the same in words):
  1. SCANNER    new pairs: every pump.fun create, watched for np_watch_s; migrated pairs: fresh DEX
                pairs, their liquidity, volume, buys against sells and holder growth. It flags, it
                never trades (with np_max_buy_sol, a trigger whose one buy reading was that big is rejected
                there for good; off by default).
  2. NEWS       the coin's X, Telegram and website (its metadata), the curve's momentum (SOL in, the
                share of buys) and, for migrated pairs, boosts and the trending lists: a NEWS score
                0-10 that picks the hold plan (+10 % scalp ... +100 % moon), read again every
                news_refresh_sec while the coin is held.
  3. RUG CHECK  RugCheck's report (rug history of the creator, one holder or the top 10 holding too
                much, insiders, danger risks; for migrated pairs also mint and freeze authority and
                the LP) and a sell simulation on Jupiter (a honeypot can't be sold). A new pair's report
                that sees fewer than np_min_holders wallets holding it, or more than np_max_sol_per_holder
                SOL in for each, is no PASS but a wait, asked again after np_rug_recheck_s while the trigger
                holds (newpairs.rug_cache_s).
  4. SIZER      how much of the balance goes into a coin.
  5. EXECUTOR   the paper fills: a Jupiter quote for the amount, less a latency haircut and the fees.
  6. RISK       how many positions and trades a night, a loss streak pause, a stop for the night.
  7. LEARNER    learns from the bot's own closed trades (memebot/learner.py, memory.json in the
                data dir): a coin that looks like past losers is skipped, every 5th such skip is
                bought at half size, and the size of any other buy follows the past results. A new pair
                it skips (or RISK blocks) after a RugCheck PASS is followed as a SHADOW trade: the same
                exits on the same readings, outside the book, learned at shadow_weight (np_shadow_*).
  8. LOGGER     every decision with its reason, and the morning report.

Paper trading: no wallet and no key. Every buy and sell is priced by a real Jupiter quote for that
amount (pool fees and price impact included), less a network fee and a latency haircut. A New Pair
whose quote fails is sold by the curve's own maths, but only when that maths was checked against a
Jupiter quote at the entry. A real trade can still do worse: a failed transaction, a sandwich, a
price that moved in the time the order takes. Standard library only, plus 'websockets' for
PumpPortal (without it the bot runs on, without new pairs).

Live every second: the loop waits on the feeds' queue at most 0.25 s at a time, runs the 1 s work
(prices, exits, time stops, the scanner) every watch_sec, and each second goes out as an MQTT
message to free public brokers (the page subscribes over WebSocket). The files (state.json,
panel.json) are written every 5 s and after each trade; git takes them every 30 s as before.

The tape (memebot/recorder.py): every create, curve reading, RugCheck summary, trigger, decision and
fill goes to DIR/tapes/YYYY-MM-DDTHH.jsonl (one file per UTC hour, written by its own thread every
5 s), and a coin that came near a trigger stays polled rec_follow_s after its launch, so a replay can
try other rules on what really happened. The tapes are not git data: the workflow keeps them out of the
meme-data commits and uploads each job's as an artifact. A replay is still not a real trade.

    python3 memebot/bot.py run --data DIR                 # the night's session, 23:00 to 07:00
    python3 memebot/bot.py run --data DIR --minutes 15 --anytime     # a short run at any hour
    python3 memebot/bot.py report --data DIR              # the morning report

Not financial advice. Most new meme coins lose most of their value within hours.
"""

import argparse
import collections
import concurrent.futures
import copy
import datetime
import http.client
import importlib.util
import json
import math
import os
import queue
import re
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from zoneinfo import ZoneInfo
except ImportError:                     # pragma: no cover - Python 3.8
    ZoneInfo = None

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
PROMPT = os.path.join(HERE, "prompt.txt")
PAGE = os.path.join(HERE, "page.html")
SOL = "So11111111111111111111111111111111111111112"
UA = "Mozilla/5.0 (meme-bot paper trader)"

DEFAULTS = {
    "start_balance": 70.0,
    "session_start": "23:00",
    "session_end": "07:00",
    "timezone": "Europe/Bratislava",
    # Speed, inside the free API limits (looked up 2026-11-26):
    #   Jupiter lite-api.jup.ag: price v3 and swap/v1 quote share one bucket. The free tier is 60
    #     requests a minute in a 60 s sliding window (https://dev.jup.ag/docs/api-rate-limit, the
    #     same table at https://hub.jup.ag/docs/api-rate-limit); the newer portal lists keyless use
    #     at 0.5 requests a second, 30 a minute, and says lite-api's limit will be lowered step by
    #     step (https://developers.jup.ag/docs/portal/rate-limits). So 30 a minute: prices at most
    #     every 3 s and only while 10 calls stay free for quotes, and Jupiter's last price stands in
    #     the seconds between (jup_hold_sec). A buy keeps room for a sale of each open coin; a sale
    #     itself is never held back by this budget.
    #   DexScreener: tokens/v1 300 requests a minute, token-profiles and token-boosts 60 a minute
    #     (https://docs.dexscreener.com/api/reference). The 1 s watch asks tokens/v1 only for the
    #     coins Jupiter has not priced lately (at most 60 a minute), a 5 s scan at most 5 calls of it
    #     (60 a minute) and 2 of profiles/boosts (24).
    #   GeckoTerminal: 30 calls a minute (https://apiguide.geckoterminal.com/faq); CoinGecko's
    #     keyless page says about 10 a minute per IP (https://docs.coingecko.com/docs/keyless-public-api).
    #     A round is 3 calls, so one round in 20 s: 9 a minute.
    #   RugCheck: no published limit found (https://api.rugcheck.xyz/swagger); a report is kept
    #     10 minutes and a scan asks for 8 at most.
    #   The CDNs in front (measured 2026-11-26): Jupiter price v3 "cache-control: max-age=5",
    #     DexScreener tokens/v1 max-age=30 (Cloudflare HIT, age up to 28 s, the same priceUsd),
    #     its profiles and boosts and GeckoTerminal's lists 60 s. So a held coin's price is new
    #     every few seconds at best, and DexScreener's can be half a minute old.
    "watch_sec": 1,                     # prices, exits and a live tick every second ("tick_sec": the old name)
    "scan_every_sec": 5,                # a scan starts this long after the last one started, once it has finished
    "gt_every_sec": 20,                 # GeckoTerminal at most once in this many seconds
    "save_every_sec": 5,                # state.json and panel.json; right after a trade too
    "jup_per_min": 30,                  # Jupiter calls a minute, quotes and prices together
    "jup_price_sec": 3,                 # Jupiter prices at most this often
    "jup_keep": 10,                     # calls of the minute a price read leaves free for quotes
    "jup_hold_sec": 10,                 # Jupiter's last price of a coin stands this long between its reads
    # the live channel: MQTT 3.1.1 over TLS to free public brokers. Public: paper data only, nothing personal.
    "live_enabled": True,               # MEMEBOT_LIVE=0 in the environment turns it off too
    "live_topic": "memebot/00ade2c3a60b7da8",
    "live_panel_sec": 15,               # the retained full panel; every second a small tick
    "live_brokers": [{"host": "broker.hivemq.com", "port": 8883, "ws": "wss://broker.hivemq.com:8884/mqtt"},
                     {"host": "broker.emqx.io", "port": 8883, "ws": "wss://broker.emqx.io:8084/mqtt"}],
    # universe
    "max_age_h": 6.0,
    "dexes": ["pumpfun", "pumpswap", "raydium", "meteora", "meteoradbc"],
    "min_liquidity_usd": 40000.0,
    "min_volume_5m_usd": 25000.0,
    "min_holders": 300,
    # scanner
    "min_buy_ratio": 0.70,
    "volume_spike_x": 5.0,
    "holder_growth_pct": 15.0,
    "holder_growth_min": 10,
    # rug check
    "max_dev_pct": 5.0,
    "max_top10_pct": 35.0,
    "min_lp_locked_pct": 95.0,
    "max_insiders": 0,
    "max_roundtrip_loss_pct": 10.0,
    # sizer and risk
    "position_pct": 25.0,
    "min_order_usd": 5.0,
    "max_open": 3,
    "max_trades_per_night": 12,
    "night_stop_pct": -35.0,
    "dump_loss_pct": 30.0,              # RISK: what one dump can still take off an open position (live: -25 to -29 %)
    "loss_streak": 3,
    "cooldown_min": 30,
    "reenter_min": -1,                  # minutes after an exit before the same coin may be bought again (-1: never tonight)
    "no_entries_last_min": 30,
    # exits
    "tp1_pct": 50.0,
    "tp1_sell_frac": 0.5,
    "trail_pct": 20.0,
    "stop_pct": -25.0,
    "time_stop_min": 45,
    "time_stop_below_pct": 10.0,
    "flat_before_end_min": 5,
    # the LEARNER (memebot/learner.py): past trades of the same kind of coin, kept in memory.json
    "learn_skip_pct": -6.0,             # a score (the buckets' shrunk mean P&L %) at or under this skips the coin,
    "learn_explore_every": 5,           # but every 5th such skip buys it at half size: a bucket can show it got better
    "learn_k": 10,                      # imaginary flat trades in each bucket, so a few trades weigh little
    "learn_min_n": 8,                   # a bucket counts in the score from this many trades on
    "learn_mult_min": 0.5,              # any other buy is sized x(1 + score / 20), between these two
    "learn_mult_max": 1.5,
    # SHADOW TRADES, so the LEARNER keeps learning (New Pairs). A coin whose trigger fired and that passed RugCheck,
    # but was not bought because the LEARNER skipped it (or, with shadow_on_risk, because RISK said no entry: the
    # pause after a losing streak, a full book, the night stop, the dump guard), is followed as if it had been
    # bought: a position of shadow_usd priced by the curve maths with every cost of a real fill (pump.fun's fee, the
    # impact, the latency haircut, the fees: the maths each real entry is checked against Jupiter by, 0.0 % off on the
    # live fills), run by the very same exit code (np_exit: the plan, the ladder, the stops, the dump and migration
    # exits, the time stops) on its curve's readings in the same loop turn, in a book of its own that never touches
    # the real one (balance, positions, trades, RISK, the loss streak, the night's counts) nor anything a real trade
    # needs: it never asks Jupiter (a failed quote would rest Jupiter's quotes for the next real entry, and a quote
    # on the loop holds every held coin's next reading) and never changes the curves' poll rate (the entry rules
    # count buy readings, so the poll rate must follow the real book only). When it closes, its P&L % after every
    # cost goes into the LEARNER's buckets with weight shadow_weight. Why: the live morning of 2026-11-26 the
    # LEARNER skipped about 80 % of the triggers (hour:day -17 % over 10, news:0-2 -17 % over 10), and a LEARNER that
    # buys almost nothing learns almost nothing (1 in 5 skips bought at the minimum): a bucket that went bad on 10
    # trades would stay bad for good. A shadow is still paper on paper: no failed or late transaction, no honeypot
    # sell simulation (it asks no quote; a pump.fun curve buys back by its maths), it sells at the very reading that
    # decided the sale, where a real sale fills a moment later (the live HOGZ sale filled 1.9 % under its reading),
    # and while no real coin is held its readings come curve_idle_sec (1 s) apart, not 0.5 s: a fast stop or dump
    # can be decided a reading later. Rosy on some exits, late on others: hence half a real trade's weight, a guess
    # from that mechanism, not a fit (eleven trades fit nothing).
    "shadow_on": True,
    "shadow_on_risk": True,             # RISK's "no entry" followed too (not the session's or a job's last minutes)
    "shadow_usd": 0.0,                  # the nominal order; 0: what the SIZER would size now (position_pct of the
                                        # equity, at least min_order_usd), never cut to the cash
    "shadow_weight": 0.5,               # a shadow's weight in the LEARNER's buckets (a real trade: 1; 0: not learned)
    "shadow_max_open": 20,              # shadows at once; their curves stay polled while open (rec_max_keys: the
                                        # watch list first, then the shadows, then the recorder's follow-up)
    # paper costs
    "network_fee_sol": 0.0005,
    "latency_slip_pct": 1.0,
    "slippage_bps": 300,
    # the strategies: "new_pairs" (pump.fun coins in their first minutes, on the bonding curve: the main
    # one) and "migrated" (the scanner above: older pairs from DexScreener and GeckoTerminal)
    "strategies": ["new_pairs"],
    # NEW PAIRS (memebot/newpairs.py reads these; the same defaults there). The live probe on GitHub
    # Actions (2026-11-26): 4-7 creates a minute, curves read in one getMultipleAccounts (median 245 ms),
    # RugCheck reports for 20 s old coins in 280-460 ms, Jupiter routes them ("Pump.fun") in 30-250 ms.
    "np_watch_s": 180,                  # each new curve is followed this long (a held coin until it is sold)
    "np_max_watch": 40,                 # curves followed at once, the oldest dropped first
    "np_skip_mayhem": True,             # is_mayhem_mode coins are never bought (SCAMSEM went -89 % in 80 s)
    "np_max_dev_pct": 8.0,              # the creator's initial buy, % of the 1 B supply
    "np_dump_sol": 2.0,                 # one sell this big: rejected before the entry, sold at once after it
    "np_need_social": True,             # X, Telegram or a website in the coin's metadata
    "np_serial_window_min": 60,         # a creator with another launch this recently is a serial launcher
    "np_rug_timeout_s": 3.0,            # no RugCheck report this long after the trigger: no entry on it (the
                                        # loop never waits for one: a report that lands wakes it, and buys then)
    "np_rug_cache_s": 30,               # a report is used this long
    "np_rug_retry_s": 5,                # RugCheck failed: asked again after this (its copy of a coin's
                                        # links: after this, then twice as long each time, up to a minute)
    "np_rug_workers": 4,                # RugCheck calls at once, on worker threads
    "np_rug_prefetch": True,            # ask RugCheck when a coin is half way to its trigger
    "np_max_holder_pct": 12.0,          # one holder other than the curve
    "np_max_top10_pct": 35.0,           # the top 10, the curve left out
    "np_rug_bad_risks": ["rugged tokens", "single holder ownership"],
    # ENTRY QUALITY (newpairs.py has the reasons in full): RugCheck must see enough WALLETS holding the coin (its
    # empty accounts and the curve's left out) for the SOL that came in. The live morning of 2026-11-26, measured
    # before the 45 s age floor: 7 of the 10 entries passed RugCheck on a report that saw 0-4 holders (not indexed
    # yet) and all 4 dumps were among them (2.9-4.4 SOL sold out of 3.3-4.3 SOL in: a handful of early wallets);
    # under the floor 3 of those dumps come before 45 s (np_dump_sol rejects them), and the rules' own evidence is
    # MEME, BST and YARL. Eleven trades fit no number: these are guesses from that mechanism, every one a backtest
    # lever (backtest.py DEFAULT_GRID), for the tapes to judge
    "np_min_holders": 10,               # fewer wallets holding the coin by RugCheck's report: no PASS, "waiting"
    "np_rug_recheck_s": 4.0,            # ... and it is asked for again after this, not reused for np_rug_cache_s
    "np_max_sol_per_holder": 0.5,       # more SOL in than this for each wallet holding it: "waiting" the same way
                                        # (0: off); never a SHADOW while it waits
    "np_max_buy_sol": 0.0,              # one buy reading above this many SOL rejects for good when it fires (0: off;
                                        # the first reading holds the launch's snipers, RugCheck judges wallets)
    "np_max_age_s": 180,                # the trigger, first in: within this many seconds of the launch
    "np_min_sol": 3.0,                  # real SOL in since the launch
    "np_min_buys": 8,                   # readings with the real SOL up
    "np_min_buy_share": 0.7,            # ... as a share of the readings that moved
    "np_min_rise_pct": 15.0,            # the price above the launch price
    "np_max_mcap_sol": 70.0,            # well before the curve completes (about 400 SOL)
    "np_stale_s": 5.0,                  # no curve reading this long while holding: Jupiter's price instead
    "np_quiet_min": 3.0,                # a held coin's curve without a trade this long: dead, sold
    "time_stop_late_x": 2.0,            # after this many times its tier's time stop, a coin still short of its
                                        # first ladder step is sold (before it: only one under time_stop_below_pct)
    "job_no_entries_min": 5,            # a job that ends before the night does (the next job takes over):
    "job_flat_min": 1,                  # no new pairs bought in its last 5 min, the held ones sold in its last one
    "np_validate_pct": 5.0,             # the curve maths within this of a Jupiter quote: a fallback for sales
    "np_tick_rows": 20,                 # watch rows in each live tick (the panel has them all, 20)
    "curve_fee_pct": 1.25,              # pump.fun's fee each way, for the curve maths
    "priority_fee_sol": 0.0005,         # what a fast real order would pay on top of the network fee
    # the feeds (memebot/feeds.py): curves polled every curve_poll_sec while holding, else curve_idle_sec
    "curve_poll_sec": 0.5,
    "curve_idle_sec": 1.0,
    "curve_ws": False,                  # the RPC WebSocket as extra pushes (it drops every 25-40 s)
    "feed_wait_sec": 0.25,              # the loop waits on the feeds' queue at most this long at a time
    "sol_price_sec": 30,                # SOL's dollar price is read this often when nothing else needs Jupiter,
                                        # on a worker thread (the loop keeps the last one meanwhile)
    "quote_timeout_s": 1.5,             # a Jupiter quote on the loop (the live probe: 30-250 ms): a hanging one
                                        # holds every held coin's exits this long at most
    # the NEWS hold plan (memebot/plan.py); without hold_tiers the tp1_* keys above make one "base" tier
    "breakeven_pct": 1.0,               # after the first step the rest is sold at entry +1 % at worst
    "news_refresh_sec": 30,             # held coins' NEWS read again this often
    "fade_buy_ratio": 0.45,             # fading: buys under this share of the last 5 minutes ...
    "fade_window_s": 300,               # ... (this window)
    # the TAPE RECORDER (memebot/recorder.py, the same defaults there): what New Pairs saw and did, to
    # <data>/tapes/YYYY-MM-DDTHH.jsonl for a replay backtester. It never trades and never waits for the disk.
    "rec_on": True,                     # MEMEBOT_REC=0 in the environment turns it off too
    "rec_follow_s": 900,                # a coin near a trigger (half the SOL and buys) stays polled this long after
                                        # its create, past np_watch_s, so its exits can be replayed ...
    "rec_follow_exit_s": 300,           # ... a bought one also this long after its last sale
    "rec_max_follow": 60,               # such coins at most, and never more curves polled in all than
    "rec_max_keys": 100,                # one getMultipleAccounts call takes (the watch list, held coins in it, first)
    "rec_max_mb_hour": 20.0,            # an hour's tape takes no more curve readings past this
    "rec_keep_days": 7,                 # older tapes are deleted when a run starts
}

AGENTS = ["SCANNER", "NEWS", "RUG CHECK", "SIZER", "EXECUTOR", "RISK", "LEARNER", "LOGGER"]


class MemeError(Exception):
    pass


def load_learner(path=os.path.join(HERE, "learner.py")):
    """(the LEARNER module, None) or (None, why not). Loaded by its path, not by `import`, so it is
    found however bot.py was started (a script, the workflow's sys.path, the tests' loader). A
    missing or broken learner.py must never stop the night: the bot then trades without it."""
    try:
        spec = importlib.util.spec_from_file_location("memebot_learner", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name in ("features", "decide", "update", "learned", "summary", "load", "save", "backfill"):
            if not callable(getattr(mod, name, None)):
                raise MemeError("no %s()" % name)
        return mod, None
    except Exception as e:                              # noqa: BLE001 - SyntaxError, ImportError, OSError ...
        return None, ("%s: %s" % (type(e).__name__, e))[:200]


learner, LEARNER_OFF = load_learner()


def load_part(name, needs, path=None):
    """(memebot/<name>.py as a module, None) or (None, why not), loaded by its path like the LEARNER.
    A missing or broken part switches off what needs it, never the night."""
    try:
        spec = importlib.util.spec_from_file_location("memebot_" + name, path or os.path.join(HERE, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for need in needs:
            if getattr(mod, need, None) is None:
                raise MemeError("no %s" % need)
        return mod, None
    except Exception as e:                              # noqa: BLE001 - SyntaxError, ImportError, OSError ...
        return None, ("%s: %s" % (type(e).__name__, e))[:200]


# New Pairs: the NEWS hold plan, the rules, the real-time feeds (feeds.py needs 'websockets' only for
# PumpPortal and loads without it)
holdplan, PLAN_OFF = load_part("plan", ("news_score", "new_plan", "tiers_from_cfg", "track", "decide_exit",
                                        "mark_step", "replan", "fade_exit", "plan_text", "panel_plan"))
newpairs, NEWPAIRS_OFF = load_part("newpairs", ("Watchlist", "Coin", "rug_check_np", "exit_signal", "labels",
                                                "news_info", "stale", "row", "first_in_text", "rug_text"))
feeds_mod, FEEDS_OFF = load_part("feeds", ("Feeds", "socials"))
# the tape recorder for the backtester (a broken recorder.py: no tape, the trading goes on)
recorder, RECORDER_OFF = load_part("recorder", ("Recorder", "load_tapes"))


# ---------------------------------------------------------------- settings and files

def load_config(path=CONFIG):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            mine = json.load(fh)
        for k, v in mine.items():
            if not k.startswith("_"):
                cfg[k] = v
        if "tick_sec" in mine and "watch_sec" not in mine:         # the old name of watch_sec
            cfg["watch_sec"] = mine["tick_sec"]
    return cfg


def watch_sec(cfg):
    return cfg.get("watch_sec") or cfg.get("tick_sec") or 1


def live_enabled(cfg):
    """The live channel is on unless the config or MEMEBOT_LIVE=0 says off (the tests run with it off)."""
    return bool(cfg.get("live_enabled", True)) and os.environ.get("MEMEBOT_LIVE", "1") != "0" \
        and bool(cfg.get("live_brokers"))


STRATEGIES = ("new_pairs", "migrated")


def strategies(cfg):
    """The strategies config.json turns on, known names only: ["new_pairs"] by default."""
    s = cfg.get("strategies", ["new_pairs"])
    s = [s] if isinstance(s, str) else s
    return [x for x in dict.fromkeys(s) if x in STRATEGIES] if isinstance(s, (list, tuple)) else ["new_pairs"]


def np_on(cfg):
    """New Pairs trades when the config asks for it and its two modules loaded."""
    return "new_pairs" in strategies(cfg) and newpairs is not None and holdplan is not None


def np_off_why(cfg):
    if "new_pairs" not in strategies(cfg):
        return "not in config.json strategies"
    if newpairs is None:
        return "newpairs.py did not load (%s)" % NEWPAIRS_OFF
    if holdplan is None:
        return "plan.py did not load (%s)" % PLAN_OFF
    return None


def feeds_for(cfg):
    """(the real-time feeds for New Pairs, None) or (None, why there are none). MEMEBOT_FEEDS=0 in the
    environment turns them off (the tests run without the network)."""
    if not np_on(cfg):
        return None, np_off_why(cfg)
    if os.environ.get("MEMEBOT_FEEDS", "1") == "0":
        return None, "MEMEBOT_FEEDS=0"
    if feeds_mod is None:
        return None, "feeds.py did not load (%s)" % FEEDS_OFF
    try:
        return feeds_mod.Feeds(cfg), None
    except Exception as e:                              # noqa: BLE001 - the night goes on without them
        return None, ("feeds: %s: %s" % (type(e).__name__, e))[:200]


def median(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)) and math.isfinite(x))
    if not xs:
        return None
    k = len(xs) // 2
    return xs[k] if len(xs) % 2 else (xs[k - 1] + xs[k]) / 2.0


def curve_buy_tokens(vsol, vtok, sol_in, fee_pct):
    """Whole tokens a buy of sol_in SOL gets from a pump.fun curve (vsol SOL against vtok whole tokens,
    constant product, the fee taken from the SOL going in), or 0."""
    net = sol_in * (1 - fee_pct / 100.0)
    if not (vsol > 0 and vtok > 0 and net > 0):
        return 0.0
    return vtok - vsol * vtok / (vsol + net)


def curve_sell_sol(vsol, vtok, tokens, fee_pct):
    """SOL a sale of `tokens` whole tokens gets from the curve, after the fee, or 0."""
    if not (vsol > 0 and vtok > 0 and tokens > 0):
        return 0.0
    return (vsol - vsol * vtok / (vtok + tokens)) * (1 - fee_pct / 100.0)


def np_win_save(win, gap=10.0):
    """A held coin's NEWS window for state.json: its baseline, then a snapshot every gap seconds and the
    newest (about 32 for 5 minutes, not 300): a restart counts the last 5 minutes within 10 s, not
    from the launch."""
    out = []
    for i, s in enumerate(win or []):
        if i == 0 or i == len(win) - 1 or s[0] - out[-1][0] >= gap:
            out.append([round(f(s[0]), 1), int(f(s[1])), int(f(s[2])), round(f(s[3]), 4)])
    return out


def np_win_load(x):
    """np_win_save()'s list back from state.json, or None when it is not one (a hand edit, an old file)."""
    if not isinstance(x, list) or not x:
        return None
    out = []
    for s in x:
        if not (isinstance(s, list) and len(s) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                            and math.isfinite(v) for v in s)):
            return None
        if out and s[0] < out[-1][0]:
            return None
        out.append([float(s[0]), int(s[1]), int(s[2]), float(s[3])])
    return out


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def hhmm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def local(now_utc, tz):
    """A naive UTC datetime -> naive local time in tz (Europe/Bratislava by default)."""
    if ZoneInfo is None:
        return now_utc + datetime.timedelta(hours=1)
    aware = now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(ZoneInfo(tz))
    return aware.replace(tzinfo=None)


ALWAYS_LEFT = 10 ** 6                    # minutes "left" in an always-on session: it never ends


def always_on(cfg):
    """session_start == session_end (e.g. 00:00 → 00:00): the bot trades around the clock. Each calendar day
    (Bratislava) is its own "night" in the book and the page; positions carry over midnight, nothing is
    sold for the day's end, and every job of the workflow starts the next one."""
    try:
        return hhmm(cfg["session_start"]) == hhmm(cfg["session_end"])
    except (KeyError, TypeError, ValueError):
        return False


def session_of(now_utc, cfg):
    """(night id, minutes into the session, minutes left) or None outside the session. The night
    is named after the evening it starts: 23:00 on the 25th to 07:00 on the 26th is 2026-11-25."""
    t = local(now_utc, cfg["timezone"])
    start, end = hhmm(cfg["session_start"]), hhmm(cfg["session_end"])
    m = t.hour * 60 + t.minute + t.second / 60.0
    length = (end - start) % (24 * 60) or 24 * 60
    into = (m - start) % (24 * 60)
    if into >= length:
        return None
    evening = t - datetime.timedelta(minutes=into)
    return evening.date().isoformat(), into, length - into


# ---------------------------------------------------------------- market data

class Budget(object):
    """Calls in the last 60 s against a free per-minute limit (Jupiter's), so the bot slows itself
    down before the API has to. The main thread and the SOL price's worker thread share it (a lock)."""

    def __init__(self, per_min, clock=time.monotonic):
        self.per_min = per_min
        self.clock = clock
        self.calls = collections.deque()
        self.lock = threading.Lock()

    def room(self, keep=0):
        with self.lock:
            return self._room(keep)

    def _room(self, keep):
        now = self.clock()
        while self.calls and now - self.calls[0] >= 60:
            self.calls.popleft()
        return len(self.calls) + keep < self.per_min

    def take(self, keep=0, force=False):
        """One call now, if the minute has room (keep calls left over); force: the call goes anyway
        and counts, so the calls that can wait do."""
        with self.lock:
            if not self._room(keep) and not force:
                return False
            self.calls.append(self.clock())
            return True


class Pause(object):
    """A rest after an API failed (a 429, a 5xx, a timeout): `first` seconds, twice as long after each
    failure in a row, `most` at most; a success starts it over. An API that hangs then costs one
    timeout in a while, not one every second of the 1 s loop."""

    def __init__(self, clock, first=10.0, most=60.0):
        self.clock, self.first, self.most = clock, first, most
        self.until, self.wait = float("-inf"), first

    def on(self):
        return self.clock() < self.until

    def fail(self):
        self.until = self.clock() + self.wait
        self.wait = min(self.most, self.wait * 2)

    def ok(self):
        self.wait = self.first


class Market(object):
    """The public APIs the agents read. Every call has a timeout; a failure is an empty answer
    and a line in errors, never a crash: the night goes on. The scanner thread calls the scan's
    APIs (GeckoTerminal, DexScreener, RugCheck), the main thread prices and Jupiter."""

    def __init__(self, fetch=None, sleep=time.sleep, cfg=None, clock=time.monotonic):
        cfg = cfg or DEFAULTS
        self.fetch = fetch or self._fetch
        self.sleep = sleep
        self.clock = clock
        self.errors = []
        self.gt_backoff_until = 0.0
        self.jup = Budget(cfg.get("jup_per_min", 30), clock)
        self.jup_price_sec = cfg.get("jup_price_sec", 3)
        self.jup_keep = cfg.get("jup_keep", 10)
        self.jup_hold_sec = cfg.get("jup_hold_sec", 10)
        self.quote_timeout = max(0.2, f(cfg.get("quote_timeout_s"), 1.5))
        self.jup_pause = Pause(clock)                   # Jupiter's prices after a failure (10 s, 20 s ... 60 s)
        self.quote_pause = Pause(clock)                 # its quotes, a bucket of their own
        self.dex_pause = Pause(clock, 5, 30)            # DexScreener's prices for the watch
        self.jup_price_at = float("-inf")
        self.jup_last = {}                              # mint -> (clock, price): Jupiter's last price of a held coin
        self.sol = None                                 # (clock, {"usd": ..}): the last SOL price Jupiter gave
        self.source = "jupiter"                         # where the last watch prices came from
        self.quote_why = None                           # why the last quote came back empty (the log says it)

    @staticmethod
    def _fetch(url, timeout=20):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def ask(self, url, timeout=20):
        """(the answer, None) or (None, what failed: "HTTP 429", "timed out" ...)."""
        del self.errors[:-50]
        try:
            return self.fetch(url, timeout), None
        except urllib.error.HTTPError as e:
            why = "HTTP %s" % e.code
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
            why = str(e) or type(e).__name__
        self.errors.append("%s: %s" % (url.split("?")[0], why))
        return None, why

    def get(self, url, timeout=20):
        return self.ask(url, timeout)[0]

    # fresh pools
    def new_pools(self, pages=2, now=None):
        """Base-token mints of GeckoTerminal's newest Solana pools (backs off a minute on 429)."""
        if now is not None and now < self.gt_backoff_until:
            return []
        out = []
        for page in range(1, pages + 1):
            d = self.get("https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=%d" % page)
            if not isinstance(d, dict) or "data" not in d:
                if now is not None:
                    self.gt_backoff_until = now + 60
                break
            for p in d.get("data") or []:
                rel = ((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}
                tid = str(rel.get("id") or "")
                if tid.startswith("solana_"):
                    out.append(tid[len("solana_"):])
        return out

    def promoted(self):
        """Solana mints DexScreener lists as new profiles and boosts."""
        out = []
        for url in ("https://api.dexscreener.com/token-profiles/latest/v1",
                    "https://api.dexscreener.com/token-boosts/latest/v1"):
            d = self.get(url)
            for x in d if isinstance(d, list) else []:
                if isinstance(x, dict) and x.get("chainId") == "solana" and x.get("tokenAddress"):
                    out.append(x["tokenAddress"])
        return out

    def trending(self, now=None):
        """Mints people are watching or paying to promote: GeckoTerminal's trending Solana pools and
        DexScreener's most boosted tokens."""
        out = set()
        if now is None or now >= self.gt_backoff_until:
            d = self.get("https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page=1")
            if isinstance(d, dict) and "data" in d:
                for p in d.get("data") or []:
                    tid = str((((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or "")
                    if tid.startswith("solana_"):
                        out.add(tid[len("solana_"):])
            elif now is not None:
                self.gt_backoff_until = now + 60
        d = self.get("https://api.dexscreener.com/token-boosts/top/v1")
        for x in d if isinstance(d, list) else []:
            if isinstance(x, dict) and x.get("chainId") == "solana" and x.get("tokenAddress"):
                out.add(x["tokenAddress"])
        return out

    def pairs(self, mints, timeout=15):
        """{mint: its most liquid Solana pair} from DexScreener, 30 mints a call."""
        return self.pairs_ask(mints, timeout)[0]

    def pairs_ask(self, mints, timeout=15):
        """(pairs() as above, whether a call failed)."""
        best, failed = {}, False
        mints = list(dict.fromkeys(m for m in mints if m and m != SOL))
        for i in range(0, len(mints), 30):
            d, why = self.ask("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i + 30]), timeout)
            failed = failed or why is not None
            for p in d if isinstance(d, list) else []:
                m = ((p or {}).get("baseToken") or {}).get("address")
                if not m or m not in mints:
                    continue
                liq = ((p.get("liquidity") or {}).get("usd")) or 0
                if m not in best or liq > (((best[m].get("liquidity") or {}).get("usd")) or 0):
                    best[m] = p
        return best, failed

    def rug_report(self, mint, timeout=20):
        return self.get("https://api.rugcheck.xyz/v1/tokens/%s/report" % mint, timeout)

    def rug_report_np(self, mint):
        """A New Pair's report, on a worker thread: 6 s at most (the live probe: 280-460 ms at 20 s old)."""
        return self.rug_report(mint, 6)

    def sol_price(self):
        """SOL in dollars for New Pairs (their prices come from the curve, in SOL): one Jupiter price call
        that keeps jup_keep calls of the minute for quotes, DexScreener's SOL pair while Jupiter rests or
        fails, else the last price known (None before any)."""
        if not self.jup_pause.on() and self.jup.room(self.jup_keep):
            got = self.prices([], keep=self.jup_keep)
            if got.get(SOL):
                self.jup_pause.ok()
                return got[SOL]["usd"]
            self.jup_pause.fail()
        if not self.dex_pause.on():
            px = self.dex_sol()
            if px > 0:
                self.sol = (self.clock(), {"usd": px})
                return px
        return self.sol[1]["usd"] if self.sol else None

    def can_quote(self, n=1):
        """Room in Jupiter's free minute for n quotes now (a buy and its sell simulation are 2)."""
        return self.jup.room(n - 1)

    def quote(self, input_mint, output_mint, amount, slippage_bps=300):
        """Jupiter's quote, or None with the reason in quote_why. A sale (into SOL: an exit, or a
        buy's sell simulation) is never held back by the bot's own minute: a stop must try at once
        (it counts all the same, so the price reads and the next buys wait). A buy waits for room.
        After a 429, a 5xx or a timeout the quotes rest 10 s (20 s, 40 s ... after more) and fail at
        once meanwhile, so a hanging Jupiter costs the 1 s loop one quote_timeout_s (1.5 s) in a while:
        the held coins' exits wait that long at most (a sale then falls back to the checked curve maths,
        an entry tries again on the next reading)."""
        self.quote_why = None
        if amount <= 0:
            self.quote_why = "nothing to sell"
            return None
        if self.quote_pause.on():
            self.quote_why = "Jupiter resting after a failure"
            return None
        if not self.jup.take(force=output_mint == SOL):
            self.quote_why = "Jupiter minute full"
            self.errors.append("jupiter: %d calls this minute, its free limit: quote skipped" % self.jup.per_min)
            return None
        q, why = self.ask("https://lite-api.jup.ag/swap/v1/quote?" + urllib.parse.urlencode({
            "inputMint": input_mint, "outputMint": output_mint, "amount": int(amount), "slippageBps": slippage_bps}),
            self.quote_timeout)
        if why is not None and (not why.startswith("HTTP 4") or why == "HTTP 429"):
            self.quote_pause.fail()
            self.quote_why = "Jupiter %s" % why
            return None
        self.quote_pause.ok()                           # an answer, a 400 too: Jupiter is up
        if not isinstance(q, dict) or not q.get("outAmount"):
            self.quote_why = "no Jupiter route"         # a 400 "could not find any route" or an empty one
            return None
        return q

    def prices(self, mints, keep=0):
        """{mint: {"usd": price, "decimals": d}} from Jupiter, SOL always included."""
        ids = list(dict.fromkeys([SOL] + list(mints)))
        out = {}
        for i in range(0, len(ids), 50):
            if not self.jup.take(keep):
                break
            d = self.get("https://lite-api.jup.ag/price/v3?ids=" + ",".join(ids[i:i + 50]), 4)
            for m, v in (d or {}).items() if isinstance(d, dict) else []:
                if isinstance(v, dict) and f(v.get("usdPrice")) > 0:
                    out[m] = {"usd": float(v["usdPrice"]), "decimals": v.get("decimals")}
        if out.get(SOL):
            self.sol = (self.clock(), out[SOL])
        return out

    def live_prices(self, mints):
        """Prices for the 1 s watch. Jupiter's (one price v3 call for all) when 3 s have passed and
        its free minute has room after the quotes' share; between two reads each coin keeps
        Jupiter's last price while it is under jup_hold_sec old. DexScreener's answer is a CDN copy
        up to 30 s old: mixed in every other second it would swing a coin between two prices and
        fire false trailing stops and targets. So DexScreener's most liquid pair prices only the
        coins Jupiter has not priced lately (it failed and rests 10 s or more, or has no price for
        the coin), and SOL comes from Jupiter's last price."""
        now = self.clock()
        held = [m for m in dict.fromkeys(mints) if m and m != SOL]
        used = []
        if not self.jup_pause.on() and now - self.jup_price_at >= self.jup_price_sec and \
                self.jup.room(self.jup_keep):
            self.jup_price_at = now
            got = self.prices(held, keep=self.jup_keep)
            if got.get(SOL):
                self.jup_pause.ok()
                used.append("jupiter")
                for m in held:
                    if m in got:
                        self.jup_last[m] = (now, got[m])
            else:
                self.jup_pause.fail()
        for m in [m for m in self.jup_last if m not in held]:   # a sold coin's price goes with it
            del self.jup_last[m]
        out = {m: self.jup_last[m][1] for m in held if m in self.jup_last and now - self.jup_last[m][0] < self.jup_hold_sec}
        if out:
            used.append("jupiter")
        missing = [m for m in held if m not in out]
        if missing and not self.dex_pause.on():
            best, failed = self.pairs_ask(missing, timeout=4)
            if failed:
                self.dex_pause.fail()
            else:
                self.dex_pause.ok()
            for m, p in best.items():
                if f(p.get("priceUsd")) > 0:
                    out[m] = {"usd": f(p.get("priceUsd"))}
            used.append("dexscreener")
        if self.sol and now - self.sol[0] < 600:        # SOL moves little in minutes; it only turns quotes into dollars
            out[SOL] = self.sol[1]
        elif not self.dex_pause.on():
            px = self.dex_sol()
            if px > 0:
                out[SOL] = {"usd": px}
                used.append("dexscreener")
        if used:
            self.source = " + ".join(dict.fromkeys(used))
        return out

    def dex_sol(self):
        """SOL in dollars from DexScreener's most liquid SOL/USDC or SOL/USDT pair (Jupiter out), or 0."""
        d, why = self.ask("https://api.dexscreener.com/tokens/v1/solana/" + SOL, 4)
        if why is not None:
            self.dex_pause.fail()
        else:
            self.dex_pause.ok()
        best = None
        for p in d if isinstance(d, list) else []:
            p = p if isinstance(p, dict) else {}
            if (p.get("baseToken") or {}).get("address") == SOL and \
                    str((p.get("quoteToken") or {}).get("symbol") or "").upper() in ("USDC", "USDT") and \
                    (best is None or f((p.get("liquidity") or {}).get("usd")) > f((best.get("liquidity") or {}).get("usd"))):
                best = p
        return f(best.get("priceUsd")) if best else 0.0


# ---------------------------------------------------------------- the agents' rules

def f(x, default=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def pair_facts(p, now_ms):
    """What the scanner reads from a DexScreener pair."""
    tx5 = ((p.get("txns") or {}).get("m5") or {})
    buys, sells = int(f(tx5.get("buys"))), int(f(tx5.get("sells")))
    vol = p.get("volume") or {}
    age_min = (now_ms - f(p.get("pairCreatedAt"), now_ms)) / 60000.0
    v5, v1h = f(vol.get("m5")), f(vol.get("h1"))
    # the 1h average of 5-minute volume, over the pair's own life when it is younger than an hour
    buckets = max(1.0, min(12.0, age_min / 5.0))
    avg5 = v1h / buckets if v1h > 0 else 0.0
    info = p.get("info") or {}
    socials = {str(x.get("type") or "").lower(): str(x.get("url") or "") for x in info.get("socials") or []
               if isinstance(x, dict)}
    return {
        "mint": (p.get("baseToken") or {}).get("address"),
        "x": socials.get("twitter") or socials.get("x") or "", "telegram": socials.get("telegram") or "",
        "web": bool(info.get("websites")), "boosts": int(f((p.get("boosts") or {}).get("active"))),
        "symbol": ((p.get("baseToken") or {}).get("symbol") or "?")[:16],
        "name": ((p.get("baseToken") or {}).get("name") or "")[:40],
        "dex": p.get("dexId"), "pair": p.get("pairAddress"), "url": p.get("url"),
        "age_min": round(age_min, 1), "liquidity": f((p.get("liquidity") or {}).get("usd")),
        "vol5": v5, "vol1h": v1h, "spike": round(v5 / avg5, 2) if avg5 > 0 else 0.0,
        "buys5": buys, "sells5": sells, "buy_ratio": round(buys / float(buys + sells), 3) if buys + sells else 0.0,
        "price": f(p.get("priceUsd")), "mcap": f(p.get("marketCap") or p.get("fdv")),
    }


def universe_ok(fx, cfg):
    """None when the pair is in the universe, else why not."""
    if fx["dex"] not in cfg["dexes"]:
        return "dex %s not in the list" % fx["dex"]
    if fx["age_min"] > cfg["max_age_h"] * 60:
        return "pair older than %g h" % cfg["max_age_h"]
    if fx["liquidity"] < cfg["min_liquidity_usd"]:
        return "liquidity %s under %s" % (usd(fx["liquidity"]), usd(cfg["min_liquidity_usd"]))
    if fx["vol5"] < cfg["min_volume_5m_usd"]:
        return "5m volume %s under %s" % (usd(fx["vol5"]), usd(cfg["min_volume_5m_usd"]))
    return None


def scanner_signals(fx, holders_hist, cfg, now, trending=()):
    """(flag, signals): the buy ratio is required, then at least one of: a volume spike against
    the hour's average, holder growth over holder_growth_min minutes, the NEWS agent's trending
    lists or a paid DexScreener boost. (Tracked smart-money and KOL wallets would need a Solana
    RPC provider: not in this bot.)"""
    sig = []
    if fx.get("mint") and fx["mint"] in trending:
        sig.append("trending")
    if fx.get("boosts"):
        sig.append("boosted")
    if fx["spike"] >= cfg["volume_spike_x"]:
        sig.append("volume %.1fx" % fx["spike"])
    hist = [(t, n) for t, n in holders_hist if now - t >= cfg["holder_growth_min"] * 60]
    if hist and holders_hist:
        t0, n0 = hist[-1]
        n1 = holders_hist[-1][1]
        if n0 > 0 and (n1 - n0) / float(n0) * 100 >= cfg["holder_growth_pct"]:
            sig.append("holders +%.0f%%" % ((n1 - n0) / float(n0) * 100))
    ok = fx["buy_ratio"] >= cfg["min_buy_ratio"] and bool(sig)
    return ok, sig


def news_text(fx, trending=()):
    """What the NEWS agent knows about a coin's promotion."""
    bits = []
    if fx.get("x"):
        handle = fx["x"].rstrip("/").split("/")[-1]
        bits.append("X @%s" % handle if handle and "search" not in fx["x"] else "X")
    if fx.get("telegram"):
        bits.append("Telegram")
    if fx.get("web"):
        bits.append("web")
    if fx.get("boosts"):
        bits.append("%d boosts" % fx["boosts"])
    if fx.get("mint") in trending:
        bits.append("trending")
    return ("promo: %s" % " · ".join(bits)) if bits else "no X, Telegram or web: anonymous"


def rug_check(report, cfg):
    """(ok, reasons, holders) from a RugCheck report: no mint or freeze authority, dev wallet
    under max_dev_pct, top 10 holders (pool accounts left out) under max_top10_pct, no insiders
    or bundle, LP burned or locked, no danger risk, at least min_holders holders."""
    if not isinstance(report, dict) or not report.get("mint"):
        return False, ["no RugCheck report"], 0
    bad = []
    tok = report.get("token") or {}
    if report.get("rugged"):
        bad.append("RugCheck: rugged")
    if report.get("mintAuthority") or tok.get("mintAuthority"):
        bad.append("mint authority open")
    if report.get("freezeAuthority") or tok.get("freezeAuthority"):
        bad.append("freeze authority open")
    supply = f(tok.get("supply"))
    dev = f(report.get("creatorBalance"))
    if supply > 0 and dev / supply * 100 > cfg["max_dev_pct"]:
        bad.append("dev holds %.1f%%" % (dev / supply * 100))
    pools = set()
    lp_pct = []
    for mk in report.get("markets") or []:
        mk = mk if isinstance(mk, dict) else {}
        for k in ("pubkey", "liquidityA", "liquidityB"):          # the pool and its token accounts
            if isinstance(mk.get(k), str):
                pools.add(mk[k])
        for k in ("liquidityAAccount", "liquidityBAccount"):      # {owner: the pool, ...}
            owner = (mk.get(k) or {}).get("owner") if isinstance(mk.get(k), dict) else None
            if isinstance(owner, str):
                pools.add(owner)
        lp = mk.get("lp") or {}
        if lp.get("lpLockedPct") is not None:
            lp_pct.append(f(lp.get("lpLockedPct")))
    for acc, info in (report.get("knownAccounts") or {}).items():
        if isinstance(info, dict) and str(info.get("type", "")).upper() in ("AMM", "LOCKER"):
            pools.add(acc)
    holders = [h for h in report.get("topHolders") or [] if isinstance(h, dict)
               and h.get("address") not in pools and h.get("owner") not in pools]
    top10 = sum(f(h.get("pct")) for h in holders[:10])
    if top10 > cfg["max_top10_pct"]:
        bad.append("top 10 hold %.0f%%" % top10)
    insiders = int(f(report.get("graphInsidersDetected"))) + sum(1 for h in holders[:10] if h.get("insider"))
    if insiders > cfg["max_insiders"]:
        bad.append("%d insiders" % insiders)
    if not lp_pct or max(lp_pct) < cfg["min_lp_locked_pct"]:
        bad.append("LP locked only %.0f%%" % (max(lp_pct) if lp_pct else 0))
    for r in report.get("risks") or []:
        name = str(r.get("name") or "")
        if str(r.get("level") or "").lower() == "danger" or "bundle" in name.lower():
            bad.append("risk: %s" % name)
    n = int(f(report.get("totalHolders")))
    if n < cfg["min_holders"]:
        bad.append("only %d holders (needs %d)" % (n, cfg["min_holders"]))
    return not bad, bad, n


def usd(x):
    return ("$%.2f" % x) if abs(x) < 1000 else ("$%.1fk" % (x / 1000.0))


# SHADOW TRADES in state.json ("shadow") and on the page (Night.np_shadow_*): counts and the last closed ones only.
# The open shadows live in memory: a restart drops them (the log says so), and nothing here can break a load.
SHADOW_ROWS = 20                        # the closed shadows the panel lists and state.json keeps
SHADOW_KINDS = ("learner", "risk")      # why it was not bought: the LEARNER skipped it, or RISK said no entry


def shadow_stat(x):
    """{"n", "wins", "sum" (of the P&L %)} back from state.json, repaired."""
    x = x if isinstance(x, dict) else {}
    n = int(max(0.0, min(1e9, f(x.get("n")))))
    return {"n": n, "wins": int(min(n, max(0.0, f(x.get("wins"))))), "sum": round(max(-1e9, min(1e9, f(x.get("sum")))), 4)}


def shadow_cell(v):
    """A shadow row's value from state.json: short text, a finite number, a bool or None (anything else: None)."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v[:120]
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else None
    return None


def shadow_book(x):
    """state.json's "shadow" back, repaired (a missing, old or hand-edited one starts over: it is a count and a view,
    never the book): {"n", "wins", "sum", "by": {"learner": {..}, "risk": {..}}, "closed": [the last SHADOW_ROWS
    rows], "open": [the symbols open at the last save]}."""
    x = x if isinstance(x, dict) else {}
    out = shadow_stat(x)
    by = x.get("by") if isinstance(x.get("by"), dict) else {}
    out["by"] = {k: shadow_stat(by.get(k)) for k in SHADOW_KINDS}
    rows = x.get("closed") if isinstance(x.get("closed"), list) else []
    out["closed"] = [{str(k)[:24]: shadow_cell(v) for k, v in list(r.items())[:30]}
                     for r in rows[-SHADOW_ROWS:] if isinstance(r, dict)]
    opened = x.get("open") if isinstance(x.get("open"), list) else []
    out["open"] = [s[:24] for s in opened[:100] if isinstance(s, str)]
    return out


def shadow_view(book, open_rows=None):
    """(panel "shadows": the open ones (open_rows, the Night's) then the last closed ones, newest first; panel
    "shadow_stats": the closed shadows' n, wins, win rate % and mean P&L % after every cost, all of them and by why
    they were not bought ("learner", "risk"), and how many are open). The stats count every shadow since state.json
    has them, not one night's."""
    book = shadow_book(book)

    def stat(s):
        n = s["n"]
        return {"n": n, "wins": s["wins"], "win_rate": round(s["wins"] * 100.0 / n, 1) if n else None,
                "mean_pnl_pct": round(s["sum"] / n, 2) if n else None}
    opened = list(open_rows or [])
    stats = dict(stat(book), open=len(opened))
    for k in SHADOW_KINDS:
        stats[k] = stat(book["by"][k])
    return opened + list(reversed(book["closed"])), stats


def skip_wait_min(cfg):
    """Minutes a coin the LEARNER skipped waits before the scanner looks at it again: reenter_min,
    like a coin after its exit, but 20 when that is under a minute (-1 means never again tonight
    for an exit), or a coin rescanned every 5 s would spend the 1 in 5 explore count on itself."""
    w = f(cfg.get("reenter_min"), -1)
    return w if w >= 1 else 20.0


# ---------------------------------------------------------------- the paper book

class Night(object):
    """One session: the balance, open positions, closed trades, every decision."""

    def __init__(self, data_dir, cfg, market, now_utc, out=print):
        self.dir = data_dir
        self.cfg = cfg
        self.m = market
        self.now = now_utc                              # run() moves it every second: one Night a run
        self.out = out
        self.traded = False                             # a buy or a sale since the last save
        self.live = None                                # the live channel, for its numbers in the panel
        self.fetcher = None                             # tick()'s scanner, fetching on this thread
        self.speed = {"source": None, "last_scan_ms": None, "watch_ms": None, "scans_per_min": 0}
        self.scan_times = collections.deque()
        self.saved_at = self.shown_at = float("-inf")
        os.makedirs(data_dir, exist_ok=True)
        self.state = read_json(os.path.join(data_dir, "state.json"), {})
        st = self.state
        st.setdefault("balance", float(cfg["start_balance"]))
        st.setdefault("positions", {})
        st.setdefault("nights", [])
        st.setdefault("holders", {})
        st.setdefault("rug_cache", {})
        self.mem_path = os.path.join(data_dir, "memory.json")
        self.mem, self.mem_off, self.mem_said = None, None, False    # mem_off: why the LEARNER is out
        self.load_memory()
        # NEW PAIRS, all on this thread but RugCheck's calls (worker threads hand reports back)
        self.clock_fn = None                            # run()'s now_fn: the latencies read the clock after a fill
        self.feeds, self.feeds_off = None, None         # the real-time feeds (run() attaches them), or why none
        self.wl = newpairs.Watchlist(cfg) if np_on(cfg) else None
        self.rug_pool = None                            # RugCheck's worker threads, made on the first call
        self.np_rug = {}                                # mint -> {"at", "report"}: reports of the last np_rug_cache_s
        self.np_jobs = {}                               # mint -> [future, asked at, why]
        self.np_fired = {}                              # mint -> when it fired with no report yet
        self.np_meta_rug = {}                           # mint -> [next ask, tries]: its links from RugCheck (the uri failed)
        self.np_hold = {}                               # mint -> no buy try before this (no Jupiter route a moment ago)
        self.np_win = {}                                # mint -> [[t, buys, sells, real SOL]] for the 5-minute NEWS window
        self.news_at = {}                               # mint -> the last NEWS refresh of a held coin
        self.np_said = {}                               # once-only lines: key -> when
        self.np_stats = {"creates": 0, "fired": 0}
        self.job_left = None                            # minutes left in this job when the next job takes the night over
        self.sol_px = None                              # (epoch, SOL in dollars)
        self.sol_pool = self.sol_job = None             # SOL's price, read again on a worker thread
        self.poll_ok = None                             # the curve poller's good rounds seen (the stale guard)
        self.feed_down_at = None                        # since when PumpPortal is down
        self.fill_note = ""                             # how the last sale was priced, for its log line
        self.quote_out = 0                              # the last quote's outAmount (before the haircut)
        self.rec = None                                 # the tape recorder (attach() starts it with the feeds)
        # SHADOW TRADES (np_shadow_*): coins not bought, followed as if they had been, in memory only (a restart
        # drops the open ones and says so); state.json keeps their counts and the last closed ones (st["shadow"])
        self.shadows = {}                               # mint -> a shadow position (a real position's fields)
        self.np_shadow_wl = newpairs.Watchlist(cfg) if self.wl is not None else None    # each shadow's own coin
        self.np_shadowed = {}                           # mint -> when it was shadowed: one shadow a coin a run
        st["shadow"] = shadow_book(st.get("shadow"))
        self.shadow_lost = st["shadow"].pop("open", [])   # open at the last save: dropped (said in begin())
        if self.wl is not None:
            for mint, pos in st["positions"].items():   # a restart keeps the book: held new pairs are watched again
                if pos.get("strategy") == "new_pairs":
                    coin = newpairs.Coin.from_dict(pos.get("np"))
                    if coin is not None and coin.mint == mint:
                        coin.status = "entered"
                        self.wl.adopt(coin)
                        win = np_win_load(pos.get("np_win"))
                        if win:                         # the NEWS window goes on where it was, not from the launch
                            self.np_win[mint] = win

    # time
    def ts(self):
        return self.now.replace(microsecond=0).isoformat() + "Z"

    def wall(self):
        """The time now (epoch s) by run()'s clock, read again: how long a fill took. Without run() (the
        tests' own steps), the step's time."""
        if self.clock_fn is None:
            return self.epoch()
        return (self.clock_fn() - datetime.datetime(1970, 1, 1)).total_seconds()

    def clock(self):
        return local(self.now, self.cfg["timezone"]).strftime("%H:%M:%S")

    def epoch(self):
        return (self.now - datetime.datetime(1970, 1, 1)).total_seconds()

    # the night
    def begin(self, night_id):
        st = self.state
        cur = st.get("night")
        if cur and cur.get("id") != night_id:
            self.close_night()
        if not st.get("night"):
            st["night"] = {"id": night_id, "start_equity": round(self.equity({}), 2), "started": self.ts(),
                           "decisions": [], "trades": [], "entered": [], "streak": 0, "cooldown_until": 0,
                           "scans": 0, "flagged": 0, "checked": 0, "filtered": 0, "lines": 0, "flags": [],
                           "curve": [], "last_rug": None}
            self.log("LOGGER", None, "start", "night %s · start %s · %d agents · %s → %s" % (
                night_id, usd(st["balance"]), len(AGENTS), self.cfg["session_start"], self.cfg["session_end"]))
        if self.mem_off and not self.mem_said:          # said here: the night's log exists now
            self.learner_off(self.mem_off)
        if self.shadow_lost:                            # the shadows a restart dropped (memory only), said once
            lost, self.shadow_lost = self.shadow_lost, []
            self.log("LEARNER", None, "shadow", "SHADOW %d open shadow trade%s dropped by the restart, not learned: %s" % (
                len(lost), "" if len(lost) == 1 else "s", ", ".join(lost[:8]) + (" ..." if len(lost) > 8 else "")))
        if self.wl is None and "new_pairs" in strategies(self.cfg) and "np_off" not in self.np_said:
            self.np_said["np_off"] = self.epoch()       # config.json asks for New Pairs, a module is missing
            self.log("LOGGER", None, "off", "new pairs off: %s · the night goes on without them" % np_off_why(self.cfg))

    def log(self, agent, coin, action, text):
        n = self.state.get("night") or {}
        n["lines"] = n.get("lines", 0) + 1
        # "n" is the row's line number in the night: the page merges the live ticks' rows by it
        row = {"t": self.ts(), "time": self.clock(), "agent": agent, "coin": coin, "action": action, "text": text,
               "n": n["lines"]}
        n.setdefault("decisions", []).append(row)
        del n["decisions"][:-600]
        self.out("%s %-9s %-10s %s" % (row["time"], agent, coin or "", text))

    def equity(self, prices):
        eq = self.state["balance"]
        for mint, p in self.state["positions"].items():
            px = (prices.get(mint) or {}).get("usd") or p.get("last") or p["entry_price"]
            eq += p["tokens_ui"] * px
        return eq

    # costs
    def fee_usd(self, sol_usd, pos=None):
        """The network fee of one order in dollars (a New Pair's order pays a priority fee too: pos["fee_sol"])."""
        fee = f((pos or {}).get("fee_sol"), -1)
        return (fee if fee >= 0 else self.cfg["network_fee_sol"]) * sol_usd

    def buy(self, mint, fx, usd_in, sol_usd, decimals, reason="", fee_sol=None):
        """A paper buy priced by Jupiter's quote for this amount. -> position or None."""
        lamports = usd_in / sol_usd * 1e9
        q = self.m.quote(SOL, mint, lamports, self.cfg["slippage_bps"])
        if not q:
            return None
        raw = int(int(q["outAmount"]) * (1 - self.cfg["latency_slip_pct"] / 100.0))
        if raw <= 0:
            return None
        self.quote_out = int(q["outAmount"])           # before the haircut: the curve maths is checked against it
        return self.position(mint, fx, usd_in, raw, decimals, reason, fee_sol, f(q.get("priceImpactPct")) * 100, sol_usd)

    def position(self, mint, fx, usd_in, raw, decimals, reason, fee_sol, impact_pct, sol_usd):
        """A new position's record: usd_in bought raw token units (after the haircut), the order's fee on top of
        the cost (buy(), and a SHADOW's entry by the curve maths)."""
        tokens_ui = raw / 10.0 ** decimals
        fee = {} if fee_sol is None else {"fee_sol": fee_sol}
        cost = usd_in + self.fee_usd(sol_usd, fee)
        pos = {"mint": mint, "symbol": fx["symbol"], "name": fx["name"], "url": fx.get("url"),
               "raw": raw, "decimals": decimals, "tokens_ui": tokens_ui, "cost": round(cost, 4),
               "entry_price": usd_in / tokens_ui, "opened": self.ts(), "peak": usd_in / tokens_ui,
               "last": usd_in / tokens_ui, "tp1_done": False, "sold_usd": 0.0, "reason": reason,
               "hist": [usd_in / tokens_ui], "impact_pct": round(impact_pct, 2)}
        pos.update(fee)
        return pos

    def sale_usd(self, sol, sol_usd, pos):
        """Dollars a sale that brings `sol` SOL leaves: less the latency haircut and the order's network fee."""
        return sol * sol_usd * (1 - self.cfg["latency_slip_pct"] / 100.0) - self.fee_usd(sol_usd, pos)

    def sell_quote(self, pos, raw, sol_usd, sim=False):
        """USD a paper sale of raw token units brings, after fees and the haircut, or None (no route). A
        New Pair's sale falls back to the curve maths when the quote fails and that maths was checked
        against Jupiter at the entry (never the honeypot simulation: sim)."""
        self.fill_note = ""
        q = self.m.quote(pos["mint"], SOL, raw, self.cfg["slippage_bps"])
        if not q:
            if sim or pos.get("strategy") != "new_pairs":
                return None
            return self.np_curve_sale(pos, raw, sol_usd)
        sol = int(q["outAmount"]) / 1e9
        self.quote_out = int(q["outAmount"])
        return self.sale_usd(sol, sol_usd, pos)

    def close_part(self, mint, frac, why, sol_usd, take=None, t_reading=None):
        """Sells frac of what is left of a position. take: plan.py's ladder decision (its log line);
        t_reading: when the price reading that decided it arrived (the log says how long the sale took;
        None for an exit no reading decided: a time stop, the end of the night, Jupiter's price). A sale
        that failed keeps its reading's time (exit_t0), so the retry that goes through 5 s later is timed
        from the reading that wanted it out, not from the reading it went out on."""
        pos = self.state["positions"][mint]
        raw = pos["raw"] if frac >= 0.999 else int(pos["raw"] * frac)
        got = self.sell_quote(pos, raw, sol_usd)
        if got is None:
            # again in 5 s, not every second: each try is a Jupiter call out of a small free minute
            pos["retry_at"] = self.epoch() + 5
            if t_reading is not None and pos.get("exit_t0") is None:
                pos["exit_t0"] = t_reading
            if self.epoch() - pos.get("stuck_said", float("-inf")) >= 60:
                pos["stuck_said"] = self.epoch()
                self.log("EXECUTOR", pos["symbol"], "stuck", "can't sell (%s): %s · retrying" % (
                    getattr(self.m, "quote_why", None) or "no Jupiter route", why))
            return False
        self.traded = True
        self.state["balance"] += got
        pos["sold_usd"] += got
        tail = self.fill_note
        lat = None
        t0 = pos.pop("exit_t0", None)
        t0 = min(t for t in (t0, t_reading) if t is not None) if t0 is not None or t_reading is not None else None
        if t0 is not None:                              # first out: the reading -> the fill
            lat = max(0.0, self.wall() - t0)
            self.np_speed_add("exit", lat * 1000)
            tail += " · out · %.1f s after the reading" % lat
        self.rec_call("sell", mint, pos, frac, why, got, raw, sol_usd, lat, self.fill_note)
        if frac >= 0.999:
            pnl = pos["sold_usd"] - pos["cost"]
            n = self.state["night"]
            trade = {"symbol": pos["symbol"], "mint": mint, "opened": pos["opened"], "closed": self.ts(),
                     "cost": round(pos["cost"], 2), "proceeds": round(pos["sold_usd"], 2), "pnl": round(pnl, 2),
                     "pnl_pct": round(pnl / pos["cost"] * 100, 1), "why": why, "url": pos.get("url"),
                     "reason": pos.get("reason", ""), "size_pct": pos.get("size_pct"),
                     "entry_price": pos["entry_price"], "exit_price": pos.get("last"), "slip": pos.get("impact_pct"),
                     "strategy": pos.get("strategy") or "migrated"}
            plan = pos.get("plan")
            if isinstance(plan, dict):
                # steps: the ladder steps sold, this sale too when it is the ladder's last ("TP +10%" on a scalp)
                last = int(f(take.get("steps"), 1)) if isinstance(take, dict) and take.get("kind") == "take" else 0
                trade.update(tier=plan.get("entry_tier") or plan.get("tier"), score=plan.get("entry_score"),
                             steps=int(f(plan.get("sold"))) + last, plan=holdplan.panel_plan(plan) if holdplan else None)
            for k in ("age_s", "mcap_sol", "dev_pct", "sol_in", "fill"):
                if k in pos:
                    trade[k] = pos[k]
            if lat is not None:
                trade["exit_ms"] = int(lat * 1000)
            if pos.get("learn"):
                trade["learn"] = pos["learn"]           # state.json keeps them: a lost memory.json can be rebuilt
            n["trades"].append(trade)
            # the pause is for losses in a row: a trade that sold its first step (a ladder step, the old
            # tp1) reached its target, and its rest coming back to break-even does not make it one
            n["streak"] = n["streak"] + 1 if pnl < 0 and not pos.get("tp1_done") else 0
            if n["streak"] >= self.cfg["loss_streak"]:
                n["cooldown_until"] = self.epoch() + self.cfg["cooldown_min"] * 60
                self.log("RISK", None, "pause", "%d losses in a row · %d min pause" % (
                    n["streak"], self.cfg["cooldown_min"]))
            del self.state["positions"][mint]
            if self.cfg.get("reenter_min", -1) >= 0:
                n.setdefault("reenter", {})[mint] = self.epoch() + self.cfg["reenter_min"] * 60
            self.log("EXECUTOR", pos["symbol"], "exit", "EXIT %s %s (%+.0f%%) · %s%s" % (
                pos["symbol"], ("+" if pnl >= 0 else "") + usd(pnl), trade["pnl_pct"], why, tail))
            self.learn_close(pos, trade)
            if pos.get("strategy") == "new_pairs":
                self.np_closed(mint, why)
        else:
            pos["raw"] -= raw
            pos["tokens_ui"] = pos["raw"] / 10.0 ** pos["decimals"]
            pos["tp1_done"] = True
            if isinstance(take, dict):                  # a ladder step: "TP +10% · sold 34% for $3.40 · stop to break-even"
                steps = int(f(take.get("steps"), 1))
                text = "%s · sold %.0f%%%s for %s" % (take.get("why") or "TP", frac * 100,
                                                      " (%d steps)" % steps if steps > 1 else "", usd(got))
                if take.get("after"):
                    text += " · " + str(take["after"])
            else:
                text = "TP %s · sold %.0f%% for %s" % (why, frac * 100, usd(got))
            self.log("EXECUTOR", pos["symbol"], "take", text + tail)
        return True

    # ---- the agents, one tick
    def manage(self, prices, minutes_left):
        """EXIT: every open position against its rules."""
        sol_usd = (prices.get(SOL) or {}).get("usd")
        if not sol_usd:
            return
        cfg = self.cfg
        for mint in list(self.state["positions"]):
            pos = self.state["positions"][mint]
            orphan = pos.get("strategy") == "new_pairs"
            if orphan and self.wl is not None:          # its curve's readings drive its exits (np_exit)
                continue
            px = (prices.get(mint) or {}).get("usd")
            if orphan:
                # a new pair held while New Pairs is off (a restart with newpairs.py or plan.py broken, or
                # config.json without "new_pairs"): run down on Jupiter's price, never left in the book
                self.np_orphan_said()
                if not px and minutes_left <= cfg["flat_before_end_min"]:
                    px = pos.get("last") or pos["entry_price"]      # the end of the night sells it, priced or not
                if px and holdplan is not None:
                    self.np_exit(mint, None, None, minutes_left, px_usd=px)
                    continue
            if not px:
                continue
            pos["last"] = px
            pos["peak"] = max(pos["peak"], px)
            chg = (px / pos["entry_price"] - 1) * 100
            held = (self.now - datetime.datetime.strptime(pos["opened"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds() / 60
            pos.setdefault("hist", []).append(px)
            del pos["hist"][:-120]                      # two minutes of seconds, what the page keeps
            if pos.get("retry_at", 0) > self.epoch():   # a sale that found no route waits its 5 s
                continue
            if minutes_left <= cfg["flat_before_end_min"]:
                self.close_part(mint, 1.0, "end of night", sol_usd)
            elif chg <= cfg["stop_pct"]:
                self.close_part(mint, 1.0, "stop %+.0f%%" % chg, sol_usd)
            elif not pos["tp1_done"] and chg >= cfg["tp1_pct"]:
                self.close_part(mint, cfg["tp1_sell_frac"], "%+.0f%%" % chg, sol_usd)
            elif pos["tp1_done"] and px <= pos["peak"] * (1 - cfg["trail_pct"] / 100.0):
                self.close_part(mint, 1.0, "trailing stop %.0f%% off the top" % cfg["trail_pct"], sol_usd)
            elif held >= cfg["time_stop_min"] and chg < cfg["time_stop_below_pct"] and not pos["tp1_done"]:
                self.close_part(mint, 1.0, "%d min, only %+.0f%%" % (held, chg), sol_usd)

    def np_orphan_said(self):
        """Said once: held new pairs with New Pairs off are run down by the plain rules."""
        if "orphan" not in self.np_said:
            held = [p["symbol"] for p in self.state["positions"].values() if p.get("strategy") == "new_pairs"]
            self.np_once("orphan", "RISK", None, "orphan", "%d held new pair%s (%s) run down without New Pairs (%s): "
                         "Jupiter's price, the stop, the %s and the end of the night" % (
                             len(held), "" if len(held) == 1 else "s", ", ".join(held[:8]),
                             np_off_why(self.cfg) or "off", "hold plan" if holdplan is not None else "time stop"))

    def exit_value(self, prices):
        """The book as if every open position were sold now: the cash plus each position's tokens at its last
        price less the sale's own costs (pump.fun's fee on a new pair, the latency haircut, the order's network
        fee). -> (that equity, the open positions' part of it). A position is worth what it sells for, about 4-6 %
        under its mark."""
        cfg = self.cfg
        sol = self.sol_px[1] if self.sol_px else 0.0
        held = 0.0
        for mint, p in self.state["positions"].items():
            px = (prices.get(mint) or {}).get("usd") or p.get("last") or p["entry_price"]
            cut = f(cfg.get("latency_slip_pct"), 1.0)
            if p.get("strategy") == "new_pairs":
                cut += f(cfg.get("curve_fee_pct"), 1.25)
            held += max(0.0, p["tokens_ui"] * px * (1 - cut / 100.0) - (self.fee_usd(sol, p) if sol else 0.0))
        return self.state["balance"] + held, held

    def why_no_entries(self, minutes_left, prices):
        n, cfg = self.state["night"], self.cfg
        if minutes_left <= cfg["no_entries_last_min"]:
            return "last %d min: no new entries" % cfg["no_entries_last_min"]
        if len(n["trades"]) + len(self.state["positions"]) >= cfg["max_trades_per_night"]:
            return "%d trades tonight (max)" % cfg["max_trades_per_night"]
        if len(self.state["positions"]) >= cfg["max_open"]:
            return "%d positions open (max)" % cfg["max_open"]
        start = n["start_equity"] or 1
        eq, held = self.exit_value(prices)
        dd = (eq / start - 1) * 100
        if dd <= cfg["night_stop_pct"]:
            return "night at %+.0f%%: stopped for the night" % dd
        # night_stop_pct is checked before an entry, so by itself it caps nothing: a full book bought just above
        # it and hit by one dump wave (the live morning: four dumps of -25 to -29 %) ends the day 20 points past it.
        # No entry while the open positions and this order, each losing dump_loss_pct in one dump, could take the
        # night past the line: the deeper the day, the fewer positions it may hold.
        w = max(0.0, f(cfg.get("dump_loss_pct"), 30.0)) / 100.0
        order = min(max(0.0, self.state["balance"]), self.equity(prices) * cfg["position_pct"] / 100.0)
        if w > 0 and ((eq - w * (held + order)) / start - 1) * 100 <= cfg["night_stop_pct"]:
            return "night at %+.0f%%: %d open and one more order could take it past %+.0f%% in one dump wave" % (
                dd, len(self.state["positions"]), cfg["night_stop_pct"])
        if self.epoch() < n.get("cooldown_until", 0):
            return "pause after a losing streak"
        return None

    def size_text(self, eq, mult, size, want, cost):
        """The SIZER line: "10% of $87.14 x0.50 LEARNER = $4.36, raised to the $5.00 minimum: $5.00" (the last
        figure is the order's cost, its fixed fee in it). The arithmetic it shows is the arithmetic it did."""
        cfg = self.cfg
        text = "%.0f%% of %s%s" % (cfg["position_pct"], usd(eq), "" if mult == 1.0 else " x%.2f LEARNER" % mult)
        raw = size * mult
        if want > raw + 0.005:
            return text + " = %s, raised to the %s minimum: %s" % (usd(raw), usd(cfg["min_order_usd"]), usd(cost))
        if want < raw - 0.005:
            return text + " = %s, cut to the %s of cash left: %s" % (usd(raw), usd(want), usd(cost))
        return text + " = %s" % usd(cost)

    def say_once(self, key, why, agent, coin, action, text, every=300):
        """A line a scan would write every 5 s: the same reason (why) once in `every` seconds."""
        n = self.state["night"]
        last = n.get(key) or {}
        if last.get("why") != why or self.epoch() - last.get("at", 0) >= every:
            n[key] = {"why": why, "at": self.epoch()}
            self.log(agent, coin, action, text)

    def skip_set(self, now):
        """Coins a scan leaves out: entered tonight, held, or still waiting to be bought again."""
        n = self.state["night"]
        wait = n.get("reenter") or {}
        return frozenset(set(n["entered"]) | set(self.state["positions"]) | {m for m, t in wait.items() if t > now})

    def scan_job(self):
        """What the scanner thread may know when a scan starts, as frozen copies: it never reads the
        book while this thread changes it."""
        now = self.epoch()
        fresh = frozenset(m for m, c in self.state["rug_cache"].items() if now - c["at"] < 600)
        return {"now": now, "skip": self.skip_set(now), "rug_fresh": fresh}

    def scan(self, prices, minutes_left, found=None):
        """SCANNER -> RUG CHECK -> RISK -> SIZER -> EXECUTOR on fresh pairs. `found` is what the
        scanner thread fetched; without it the fetching happens here first (tick(), the tests)."""
        if found is None:
            self.fetcher = self.fetcher or Scanner(self.m, self.cfg, threaded=False)
            found = self.fetcher.fetch(self.scan_job())
        if found.get("error"):
            self.m.errors.append(found["error"])
            return
        cfg, n = self.cfg, self.state["night"]
        n["scans"] += 1
        now = self.epoch()
        self.trend = found.get("trend") or frozenset()
        skip = self.skip_set(now)                       # the book now: what changed while the thread fetched
        pairs = {m: p for m, p in (found.get("pairs") or {}).items() if m not in skip}
        reports = found.get("reports") or {}
        sol_usd = (prices.get(SOL) or {}).get("usd")
        ready, seen = [], {"inside": 0, "near": None}
        for mint, p in pairs.items():
            try:
                self.consider(mint, p, now, ready, seen, reports)
            except Exception as e:                      # noqa: BLE001 - one odd coin never stops the night
                self.m.errors.append("%s: %s: %s" % (mint, type(e).__name__, e))
        inside, near = seen["inside"], seen["near"]
        if now - n.get("scan_said", float("-inf")) >= 60:   # what the scanner sees, once a minute
            n["scan_said"] = now
            self.log("SCANNER", None, "scan", "scanned %d new coins · %d pairs · %d pass the rules%s" % (
                len(found.get("mints") or []), len(pairs), inside, "" if inside or not near else
                " · closest %s liq %s, 5m %s" % (near["symbol"], usd(near["liquidity"]), usd(near["vol5"]))))
        for fx, mint, rep in sorted(ready, key=lambda r: -r[0]["vol5"]):
            why = self.why_no_entries(minutes_left, prices)
            if why:
                self.say_once("risk_said", why, "RISK", fx["symbol"], "skip", "no entry: %s" % why)
                break
            if not sol_usd:
                break
            decimals = int(f(((rep or {}).get("token") or {}).get("decimals"), 6))
            # the cash a buy may use: the balance cut to 1/100 of a cent, so a cost rounded to 4
            # places never takes it under 0 (a page showing "$-0.00")
            cash = max(0.0, math.floor(self.state["balance"] * 1e4) / 1e4)
            size = min(cash, self.equity(prices) * cfg["position_pct"] / 100.0)
            if size < cfg["min_order_usd"]:
                # once in 5 minutes like RISK: with the LEARNER's 1.5x sizes the cash can run out
                # before max_open, and every 5 s scan would say it again
                self.say_once("sizer_said", "cash", "SIZER", fx["symbol"], "skip", "only %s left to size" % usd(size))
                break
            # the buy, the LEARNER's re-size and the sell simulation are 3 quotes, and the minute
            # keeps one more for each open coin: a burst of buys never leaves the stops without
            # room (a sale goes anyway, but over the budget Jupiter itself may say 429). A full
            # minute waits for the next scan (no false honeypot).
            if not getattr(self.m, "can_quote", lambda k: True)(3 + len(self.state["positions"])):
                break
            pos = self.buy(mint, fx, size - self.fee_usd(sol_usd), sol_usd, decimals, fx.get("reason", ""))
            if not pos:
                continue                                # no quote right now: the next scan tries again
            labels, d, explore = self.learn_judge(fx, pos)   # the quote's price impact is one of the coin's facts
            if d is not None and d["action"] == "skip":
                # no ban for the night: the coin's buckets move as it grows and ages, so it is looked at again
                n.setdefault("reenter", {})[mint] = now + skip_wait_min(cfg) * 60
                continue
            mult = d["mult"] if d is not None else 1.0
            want = min(cash, max(cfg["min_order_usd"], size * mult))
            if abs(want - size) >= 0.01:                # another size: a real quote for that amount
                pos = self.buy(mint, fx, want - self.fee_usd(sol_usd), sol_usd, decimals, fx.get("reason", ""))
                if not pos:
                    continue
            # honeypot: what the coins this buys would sell for right away
            back = self.sell_quote(pos, pos["raw"], sol_usd)
            loss = (1 - back / pos["cost"]) * 100 if back is not None else 100.0
            if back is None or loss > cfg["max_roundtrip_loss_pct"]:
                self.log("RUG CHECK", fx["symbol"], "reject", "sell simulation: %s" % (
                    "can't sell (honeypot?)" if back is None else "would lose %.0f%% at once" % loss))
                n["entered"].append(mint)
                n["filtered"] = n.get("filtered", 0) + 1
                n["last_rug"] = {"coin": fx["symbol"], "pass": False}
                continue
            if labels:
                pos["learn"] = labels                   # what the bot knew at the entry: the close learns from it
            eq = self.equity(prices)
            pos["size_pct"] = round(pos["cost"] / eq * 100, 1) if eq else None
            self.state["balance"] = max(0.0, self.state["balance"] - pos["cost"])    # cost <= cash: only a float's last bit
            self.state["positions"][mint] = pos
            self.traded = True
            if explore is not None:                     # the explore is a trade now: its 1 in 5 count moves
                self.learn_commit(explore)
            if cfg["reenter_min"] < 0:
                n["entered"].append(mint)
            self.log("SIZER", fx["symbol"], "size", self.size_text(eq, mult, size, want, pos["cost"]))
            self.log("EXECUTOR", fx["symbol"], "enter", "ENTER %s at %s · slip %.1f%% · %s" % (
                fx["symbol"], ("%.10f" % pos["entry_price"]).rstrip("0"), pos["impact_pct"], pos.get("reason") or "signal"))

    def consider(self, mint, p, now, ready, seen, reports=None):
        """SCANNER and RUG CHECK on one pair: a coin that passes both goes into ready."""
        cfg, n = self.cfg, self.state["night"]
        fx = pair_facts(p, now * 1000)
        if universe_ok(fx, cfg):
            near = seen["near"]
            if fx["dex"] in cfg["dexes"] and fx["age_min"] <= cfg["max_age_h"] * 60 and \
                    (near is None or fx["liquidity"] > near["liquidity"]):
                seen["near"] = fx
            return
        seen["inside"] += 1
        rep = self.rug(mint, now, reports)
        if rep is None:                                 # RugCheck did not answer: the next scan asks again
            self.state["rug_cache"].pop(mint, None)
            return
        hold = self.state["holders"].setdefault(mint, [])
        ok_rug, bad, nh = rug_check(rep, cfg)
        if nh and (not hold or now - hold[-1][0] >= 60):
            hold.append([now, nh])
            del hold[:-30]
        flag, sig = scanner_signals(fx, hold, cfg, now, getattr(self, "trend", ()))
        if not flag:
            return
        fx["reason"] = " · ".join(["buys %.0f%%" % (fx["buy_ratio"] * 100)] + sig)
        fx["signals"], fx["holders"] = sig, nh          # what the LEARNER reads at the entry
        fs = n.setdefault("flag_seen", {})
        if now - fs.get(mint, 0) >= 600:                 # a flag is written once in 10 minutes
            fs[mint] = now
            n["flagged"] += 1
            self.log("SCANNER", fx["symbol"], "flag", "flagged · %s · liq %s · 5m %s · %s" % (
                fx["dex"], usd(fx["liquidity"]), usd(fx["vol5"]), fx["reason"]))
            self.log("NEWS", fx["symbol"], "news", news_text(fx, getattr(self, "trend", ())))
            n["flags"] = ([x for x in n.get("flags", []) if x.get("coin") != fx["symbol"]]
                          + [{"coin": fx["symbol"], "why": sig[0] if sig else "buys", "t": self.ts()}])[-8:]
        n["checked"] += 1
        if not ok_rug:
            self.log("RUG CHECK", fx["symbol"], "reject", "FAIL · %s" % " · ".join(bad[:3]))
            n["entered"].append(mint)                   # not checked again tonight
            n["filtered"] = n.get("filtered", 0) + 1
            n["last_rug"] = {"coin": fx["symbol"], "pass": False}
            return
        if not n.get("rug_passed", {}).get(mint):
            n.setdefault("rug_passed", {})[mint] = True
            self.log("RUG CHECK", fx["symbol"], "pass", "PASS · mint & freeze revoked · LP locked · %d holders" % nh)
        n["last_rug"] = {"coin": fx["symbol"], "pass": True}
        ready.append((fx, mint, rep))

    def rug(self, mint, now, fetched=None):
        """The coin's RugCheck report: the one the scan just fetched, else a cached one under 10
        minutes old. An older one is never used: a report from an hour or a night ago can miss a
        mint authority opened since, or reject a coin for the holders it had at its launch. It is
        asked again here without a scan's reports (fetched None), else by the next scan."""
        cache = self.state["rug_cache"]
        if fetched is not None and mint in fetched:
            rep = fetched[mint]
        else:
            c = cache.get(mint)
            if c and now - c["at"] < 600:
                return c["report"]
            if fetched is not None:                     # not asked this scan (8 at most): the next one asks
                return None
            rep = self.m.rug_report(mint)
        keep = trim_report(rep)
        cache[mint] = {"at": now, "report": keep}
        if len(cache) > 300:
            for k in sorted(cache, key=lambda k: cache[k]["at"])[:100]:
                cache.pop(k, None)
        return keep

    # ---- the LEARNER (memebot/learner.py), on this thread only: no locks
    def load_memory(self):
        """memory.json, read when the Night is made: once a run. A new memory first learns the closed
        trades state.json still has (only their hour, unless they carry their labels)."""
        self.state.pop("learner_summary", None)         # one from before would say the LEARNER works
        if learner is None:
            return self.learner_off("learner.py did not load (%s)" % LEARNER_OFF)
        try:
            new = not os.path.exists(self.mem_path)
            mem = learner.load(self.mem_path)
            old = [t for k in ("last_night", "night") for t in (self.state.get(k) or {}).get("trades") or []]
            if new and learner.backfill(mem, old, self.cfg["timezone"]):
                learner.save(self.mem_path, mem)
        except Exception as e:                          # noqa: BLE001 - the night goes on without it
            return self.learner_off("memory.json: %s: %s" % (type(e).__name__, e))
        self.mem = mem
        self.learn_summary()

    def learner_off(self, why):
        """The LEARNER is out for the rest of the run, said once in the log: the night trades on as it
        did before it learned (full size, no skips)."""
        self.mem, self.mem_off = None, self.mem_off or why
        self.state.pop("learner_summary", None)
        if self.state.get("night") and not self.mem_said:
            self.mem_said = True
            self.log("LEARNER", None, "off", "off · %s · trading without it" % self.mem_off)

    def learn(self, name, *args):
        """learner.<name>(*args), or None while the LEARNER is out. One that raises is switched off:
        a bug in it must not stop the buys."""
        if self.mem is None:
            return None
        try:
            return getattr(learner, name)(*args)
        except Exception as e:                          # noqa: BLE001
            self.learner_off("%s() failed: %s: %s" % (name, type(e).__name__, e))
            return None

    def learn_summary(self):
        s = self.learn("summary", self.mem, self.cfg.get("learn_k"), self.cfg.get("learn_min_n"))
        if s is not None:
            self.state["learner_summary"] = s           # panel()["learner"] and every tick

    def learn_save(self):
        """memory.json after each change (a closed trade, a skip, an explore) and the page's summary."""
        if self.learn("save", self.mem_path, self.mem) is False:
            self.m.errors.append("learner: memory.json not saved")
        self.learn_summary()

    def learn_judge(self, fx, pos):
        """LEARNER before a buy -> (the coin's labels, its decision, an explore's memory to commit),
        or (None, None, None) while it is out. The labels are what the bot knows now: the pair,
        RugCheck's holders, the buy quote's price impact, the scanner's signals and the hour in
        Bratislava. decide() moves the 1 in 5 count in the memory it gets, so it gets a copy: a
        skip is kept at once, an explore only once its half-size buy is in the book (learn_commit).
        An explore that fails (no quote, the sell simulation) leaves the count where it was, and
        the next coin like it is the explore."""
        labels = self.learn("features", fx, fx.get("holders"), pos.get("impact_pct"), fx.get("signals"),
                            local(self.now, self.cfg["timezone"]).hour)
        d, trial = self.learn_decide(labels, fx["symbol"])
        if d is None:
            return None, None, None
        return labels, d, trial

    def learn_decide(self, labels, symbol):
        """The LEARNER's decision on a coin's labels -> (decision, an explore's memory to commit), or
        (None, None) while it is out. A skip's count is kept at once (see learn_judge)."""
        trial = copy.deepcopy(self.mem)
        d = self.learn("decide", trial, labels, self.cfg) if labels is not None else None
        if d is None:
            return None, None
        if d["action"] != "buy" or d["mult"] != 1.0:   # "no bucket has 8 trades yet" on every buy is noise
            self.log("LEARNER", symbol, d["action"], "%s · %s" % (d["action"], d["why"]))
        if d["action"] == "explore":
            return d, trial
        self.mem = trial
        if d["action"] == "skip":                       # the 1 in 5 count moved
            self.learn_save()
        return d, None

    def learn_commit(self, mem):
        """An explore's memory, once its buy is in the book (unless the LEARNER went out meanwhile)."""
        if self.mem is not None:
            self.mem = mem
            self.learn_save()

    def learn_close(self, pos, trade):
        """LEARNER after a full exit: the trade's P&L % (fees included) goes into the buckets the coin
        had at its entry. A position bought before the LEARNER has none: nothing to learn."""
        if self.mem is None or not pos.get("learn"):
            return
        self.learn("update", self.mem, pos["learn"], trade["pnl_pct"])
        self.learn_save()
        said = self.learn("learned", self.mem, pos["learn"])
        if said:
            self.log("LEARNER", pos["symbol"], "learn", "learned · %s" % said)

    def mark(self, prices):
        """The equity curve: a point a minute."""
        n = self.state.get("night")
        if n is not None:
            curve = n.setdefault("curve", [])
            if not curve or self.epoch() - curve[-1][0] >= 60:
                curve.append([int(self.epoch()), round(self.equity(prices), 2)])
                del curve[:-600]

    def tick(self, minutes_left, scan=True):
        """One step all on this thread (the tests): prices, exits, a scan, the files."""
        prices = self.m.prices(list(self.state["positions"]))
        self.manage(prices, minutes_left)
        if scan and minutes_left > self.cfg["flat_before_end_min"] and "migrated" in strategies(self.cfg):
            self.scan(prices, minutes_left)
        self.mark(prices)
        self.save(prices)
        return prices

    def watch(self, minutes_left, scanner=None):
        """One second of run(): SOL's and the held coins' prices and EXIT on each, then the scan the
        scanner thread finished is applied here (this thread is the book's only writer) and the
        next one is started when it is due. New Pairs: their prices come with each curve reading
        (np_event); here only the time stops, the end of the night, the stale guard (a held coin
        without a reading for np_stale_s is priced by Jupiter), the NEWS refresh and the watch list's
        expiry. scanner None: no migrated strategy."""
        t = time.monotonic()
        pos = self.state["positions"]
        # held new pairs are priced here only while New Pairs is off (manage() runs them down then)
        held = [m for m in pos if pos[m].get("strategy") != "new_pairs" or self.wl is None]
        stale = self.np_stale() if self.wl is not None else []
        live = getattr(self.m, "live_prices", None)
        if scanner is not None or held or stale or self.wl is None:
            prices = live(held + stale) if live else self.m.prices(held + stale)
        else:
            prices = {}                                 # New Pairs only: SOL's price is all it needs here
        sol = self.sol_usd(prices, fetch=True)
        if sol and not (prices.get(SOL) or {}).get("usd"):
            prices[SOL] = {"usd": sol}
        self.manage(prices, minutes_left)
        if self.wl is not None:
            self.np_second(prices, minutes_left)
        now = self.epoch()
        first = False
        if scanner is None:
            self.mark(prices)
            self.speed["source"] = getattr(self.m, "source", None) or "jupiter"
            self.speed["watch_ms"] = int((time.monotonic() - t) * 1000)
            return prices
        if minutes_left > self.cfg["flat_before_end_min"] and scanner.due(now):
            first = scanner.first
            scanner.start(self.scan_job())
        # the run's first scan is waited for up to 3 s, so the first second has its candidates; one
        # that takes longer (DexScreener or GeckoTerminal hanging up to 20 s a call) is applied when
        # it lands, like any other, and the exits go on meanwhile
        found = scanner.poll(scanner.first_wait if first else 0)
        if found is not None:
            self.scan_times.append(now)
            while self.scan_times and now - self.scan_times[0] > 60:
                self.scan_times.popleft()
            self.speed["last_scan_ms"] = found.get("ms")
            self.speed["scans_per_min"] = len(self.scan_times)
            if minutes_left > self.cfg["flat_before_end_min"]:
                self.scan(prices, minutes_left, found)
        self.mark(prices)
        self.speed["source"] = getattr(self.m, "source", None) or "jupiter"
        self.speed["watch_ms"] = int((time.monotonic() - t) * 1000)
        return prices

    # ---- NEW PAIRS: pump.fun coins on their bonding curve (memebot/newpairs.py's rules, memebot/plan.py's
    # hold plan, memebot/feeds.py's events). Everything here runs on the main thread, the book's only
    # writer; RugCheck's calls run on worker threads and hand their reports back through futures.
    def attach(self, feeds, why=None):
        """run() hands over the feeds (None: there are none, and why). The held new pairs a restart kept
        are watched again at once: their exits need the readings."""
        self.feeds, self.feeds_off = feeds, why
        if self.wl is None:
            return
        if feeds is not None:
            self.rec_start()
            for mint in self.wl.held():
                coin = self.wl.get(mint)
                if coin is not None and coin.curve:
                    self.np_call("watch", mint, coin.curve)
        self.np_holding()

    def np_call(self, name, *args):
        """feeds.<name>(*args), never raising into the loop (a feed's trouble is the feed's)."""
        fn = getattr(self.feeds, name, None) if self.feeds is not None else None
        if fn is None:
            return None
        try:
            return fn(*args)
        except Exception as e:                          # noqa: BLE001
            self.m.errors.append("feeds.%s: %s: %s" % (name, type(e).__name__, e))
            return None

    def np_holding(self):
        """The curves are polled every curve_poll_sec while a new pair is held, else curve_idle_sec. The REAL book
        only: the poll rate is every watched coin's, and the entry rules count buy READINGS (np_min_buys, the buy
        share), so an open SHADOW must not change it, or a shadow would change which real coins are bought. A
        shadow's readings come at whatever rate the real book sets (1 s apart while nothing real is held): a fast
        stop or dump can be decided a reading later than a held coin's would be; shadow_weight discounts that."""
        self.np_call("set_holding", any(p.get("strategy") == "new_pairs" for p in self.state["positions"].values()))

    def np_once(self, key, agent, coin, action, text):
        if key in self.np_said:
            return False
        self.np_said[key] = self.epoch()
        self.log(agent, coin, action, text)
        return True

    # the tape recorder (memebot/recorder.py): one guarded call per hook; it never trades, never waits for
    # the disk (a writer thread does), and its first error switches it off for the run, said once
    def rec_start(self):
        if self.rec is not None or recorder is None or not self.cfg.get("rec_on", True) \
                or os.environ.get("MEMEBOT_REC", "1") == "0":
            return
        try:
            self.rec = recorder.Recorder(os.path.join(self.dir, "tapes"), self.cfg, wl=self.wl, clock=self.epoch,
                                         log=lambda text: self.log("LOGGER", None, "tape", text),
                                         stats=self.np_rec_stats,
                                         pool_accounts=getattr(newpairs, "_curve_accounts", None),
                                         start={"bal": self.state.get("balance"), "mem": self.mem,
                                                "held": len(self.state.get("positions") or {})})
        except Exception as e:                          # noqa: BLE001 - no tape, the night goes on
            self.rec = None
            self.log("LOGGER", None, "error", "tape recorder off: %s: %s" % (type(e).__name__, str(e)[:160]))
            return
        try:                                            # it follows the open shadows' coins (their readings on the
            self.rec.kept = lambda: list(self.shadows)  # tape, in rec_max_keys); a recorder without the hook just
        except Exception:                               # noqa: BLE001 - keeps the attribute
            pass

    def rec_call(self, name, *args):
        """rec.<name>(*args), never raising into the loop. -> its answer, or None (off, or it failed)."""
        rec = self.rec
        if rec is None:
            return None
        try:
            return getattr(rec, name)(*args)
        except Exception as e:                          # noqa: BLE001 - a tape is never worth a trade
            self.rec = None
            follows = []
            try:
                follows = list(rec.follow)
                rec.close(0, "off")                     # what it has goes to its writer; nobody waits for it
            except Exception:                           # noqa: BLE001
                pass
            for m in follows:                           # the curves it kept polled go: the poller is the exits'
                if self.wl is not None and self.wl.get(m) is None and m not in self.shadows:
                    self.np_call("unwatch", m)
            text = "tape recorder off: %s: %s: %s · the trading goes on, without a tape" % (
                name, type(e).__name__, str(e)[:160])
            self.m.errors.append(text)
            self.log("LOGGER", None, "error", text)
            return None

    def np_rec_tick(self):
        """The recorder's step (after each batch of events and each 1 s step): its decisions, the follow list,
        the hand-off to its writer. The curves it lets go are unwatched, never one the watch list has."""
        if self.rec is not None and self.sol_px:
            self.rec_call("sol", self.sol_px[1])
        for m in self.rec_call("tick", self.epoch()) or ():
            if self.wl.get(m) is None and m not in self.shadows:     # an open shadow keeps its curve
                self.np_call("unwatch", m)

    def np_rec_stats(self):
        """The heartbeat's numbers: SOL's dollar price, the poller's good calls (a gap in them is a gap in the
        tape), the curves polled, the coins held."""
        poller = getattr(self.feeds, "poller", None)
        return {"sol": self.sol_px[1] if self.sol_px else None, "poll_ok": getattr(poller, "ok", None),
                "poll_calls": getattr(poller, "calls", None), "poll_ms": getattr(poller, "last_ms", None),
                "watched": len(self.np_call("watched") or {}), "held": len(self.wl.held()), "shadows": len(self.shadows)}

    def np_unwatch(self, mint):
        shadow = mint in self.shadows                   # an open shadow keeps its curve polled, and its NEWS window
        if not self.rec_call("keep", mint) and not shadow:      # the recorder may keep it polled (rec_follow_s)
            self.np_call("unwatch", mint)
        for d in (self.np_fired, self.np_hold, self.np_meta_rug) + (() if shadow else (self.np_win, self.news_at)):
            d.pop(mint, None)

    def np_t(self, ev):
        """When an event's reading was taken (the feed's "t", or "_rx" when the feed received it: PumpPortal's
        migrate), or now when it has none or a wrong one. The time it waited in the queue counts."""
        now = self.epoch()
        ev = ev if isinstance(ev, dict) else {}
        t = f(ev.get("t") if ev.get("t") is not None else ev.get("_rx"), 0.0)
        return t if -1.0 < now - t < 60.0 else now

    def np_wake(self, mint):
        """A RugCheck call finished (on its worker thread): an event on the feeds' queue wakes the loop, so
        np_reports() takes the report at once (a coin that fired without one buys then)."""
        q = getattr(self.feeds, "q", None) if self.feeds is not None else None
        put = getattr(q, "put_nowait", None) or getattr(q, "append", None)
        if put is not None:
            try:
                put({"kind": "rug", "mint": mint})
            except Exception:                           # noqa: BLE001 - a full queue: the next turn takes it
                pass

    def np_events(self, wait, minutes_left):
        """Waits at most `wait` seconds for the feeds' next event, then handles it and all that queued up
        behind it, the held coins' readings first (first out). -> how many events."""
        if self.feeds is None or self.wl is None:
            return 0
        ev = self.feeds.get(wait)
        evs = [] if ev is None else [ev] + list(self.feeds.drain(2000) or [])
        held = self.state["positions"]
        evs.sort(key=lambda e: 0 if isinstance(e, dict) and e.get("kind") == "curve" and e.get("mint") in held else 1)
        for ev in evs:
            if self.clock_fn is not None:
                self.now = self.clock_fn()
            try:
                self.np_event(ev, minutes_left)
            except Exception as e:                      # noqa: BLE001 - one odd event never stops the night
                text = "error in a New Pairs event (%s: %s) · going on" % (type(e).__name__, e)
                self.m.errors.append(text)
                self.say_once("np_error_said", text, "LOGGER", None, "error", text, every=60)
        self.np_reports(minutes_left)
        self.np_touch()
        self.np_rec_tick()
        return len(evs)

    def np_event(self, ev, minutes_left):
        if not isinstance(ev, dict):
            return
        self.rec_call("event", ev)                      # on the tape as it came (create, curve, meta, ...)
        kind = ev.get("kind") or ev.get("txType")
        if kind == "curve":
            self.np_curve(ev, minutes_left)
        elif kind == "create":
            self.np_create(ev)
        elif kind == "meta":
            self.np_meta(ev, minutes_left)
        elif kind == "migrate":
            self.np_migrate(ev, minutes_left)
        elif kind == "curve_gone":
            self.np_gone(ev, minutes_left)
        # "rug" (np_wake): a RugCheck call finished; np_reports() after the batch takes it

    def np_create(self, ev):
        """SCANNER: a new pump.fun coin. Watched (its curve read with the others, its metadata fetched)
        unless newpairs skips it (mayhem mode, no symbol, not a pump.fun curve: a row on the page all the
        same). A creator buy over np_max_dev_pct or a serial launcher is rejected at once."""
        now = self.epoch()
        self.np_stats["creates"] += 1
        coin = self.wl.on_create(ev, now)
        if coin is None:
            return None
        for m in coin.evicted:
            self.np_unwatch(m)
        self.np_call("watch", coin.mint, coin.curve)
        self.np_call("fetch_meta", coin.mint, coin.uri)
        self.wl.judge(coin, now)
        return coin

    def np_meta(self, ev, minutes_left):
        """NEWS: the coin's metadata (X, Telegram, website). The feed's event itself is the document; when
        the uri could not be read, RugCheck's copy of the metadata is asked for instead."""
        mint = ev.get("mint")
        coin = self.wl.get(mint)
        if coin is None or coin.meta is not None:
            return
        if ev.get("ok"):
            self.wl.on_meta(mint, ev)
            self.np_judge(coin, self.epoch(), minutes_left)
        elif mint not in self.np_meta_rug:
            # RugCheck's copy of the metadata, asked now and again until it has it (a coin a second old is
            # often not indexed yet): the links stay unknown meanwhile, never "no social link"
            self.np_meta_rug[mint] = [self.epoch(), 0]
            self.np_meta_ask(mint)

    def np_meta_ask(self, mint):
        """RugCheck asked for a coin's links (its uri failed), then again np_rug_retry_s later, twice as long
        after each miss, a minute at most, while the coin can still be bought (np_max_age_s)."""
        st = self.np_meta_rug.get(mint)
        if st is None or mint in self.np_jobs or self.epoch() < st[0]:
            return
        st[0] = self.epoch() + min(60.0, f(self.cfg.get("np_rug_retry_s"), 5) * 2 ** st[1])
        st[1] += 1
        self.np_rug_ask(mint, "meta")

    def np_meta_due(self):
        """The 1 s step: the coins whose links RugCheck had not got yet are asked again when due; a coin
        too old to be bought, or no longer a candidate, is let go (its links stay unknown)."""
        now, top = self.epoch(), f(self.cfg.get("np_max_age_s"), 180)
        for mint in list(self.np_meta_rug):
            coin = self.wl.get(mint)
            if coin is None or coin.meta is not None or coin.status != "watching" or now - coin.t0 > top:
                del self.np_meta_rug[mint]
            else:
                self.np_meta_ask(mint)

    def np_rug_meta(self, rep):
        """(the links in a RugCheck report's fileMeta / tokenMeta, the uri's JSON as RugCheck read it) ->
        a links doc; {} when RugCheck read the metadata and it has none (a real "no social link"); None when
        there is no answer yet (the call failed, the coin is not indexed, fileMeta still empty)."""
        if not isinstance(rep, dict):
            return None
        read = False
        for key in ("fileMeta", "tokenMeta"):
            doc = rep.get(key)
            if isinstance(doc, dict):
                links = feeds_mod.socials(doc) if feeds_mod is not None else doc
                if any(links.get(k) for k in ("twitter", "telegram", "website")):
                    return links
                read = read or (key == "fileMeta" and any(doc.get(k) for k in ("name", "symbol", "image", "description")))
        return {} if read else None

    def np_curve(self, ev, minutes_left):
        """A curve reading: a held coin's exits at once (first out), else the watched coin's trigger
        (first in). A price of 0.0 (a zeroed or broken curve) is never a price."""
        mint = ev.get("mint")
        if not isinstance(mint, str):
            return
        if "price_sol" in ev and not ev.get("complete") and not f(ev.get("price_sol")) > 0:
            return
        t = self.np_t(ev)
        coin = self.wl.on_curve(mint, ev, t)
        if coin is not None and self.np_held(mint):
            pos = self.state["positions"][mint]
            pos["moved_t"] = t                          # readings come only when the reserves changed: a trade
            self.np_window(mint, coin, t)
            self.np_exit(mint, coin, t, minutes_left, reading=True)
        elif coin is not None:
            self.np_judge(coin, t, minutes_left)
        if mint in self.shadows:                        # a SHADOW's exits on the same reading, after the book's
            self.np_shadow_safe("np_shadow_curve", mint, ev, t, minutes_left)

    def np_held(self, mint):
        """A position New Pairs holds (the migrated strategy's coins have exits of their own)."""
        return (self.state["positions"].get(mint) or {}).get("strategy") == "new_pairs"

    def np_migrate(self, ev, minutes_left):
        """PumpPortal: a coin finished its curve. A held one is sold into the curve's last state (a SHADOW too)."""
        mint = ev.get("mint")
        if isinstance(mint, str) and mint in self.shadows:
            self.np_shadow_safe("np_shadow_end", mint, ev, minutes_left)
        coin = self.wl.on_migrate(mint, self.epoch()) if isinstance(mint, str) else None
        if coin is None:
            return
        self.log("SCANNER", coin.symbol, "migrate", "migrated: the curve is complete (%s)" % (ev.get("pool") or "pump-amm"))
        if self.np_held(mint):
            self.np_exit(mint, coin, self.np_t(ev), minutes_left)

    def np_gone(self, ev, minutes_left):
        """The curve account read as null 3 times: a held coin counts as migrated, a watched one that had
        readings is dropped. A watched coin that never had one is not: a load-balanced RPC node may not have
        seen a brand-new account yet (feeds.py: "closed, or not there yet"); its first reading comes when it
        does, or the watch list's expiry lets it go at np_watch_s. A SHADOW counts as migrated like a held coin."""
        mint = ev.get("mint")
        if isinstance(mint, str) and mint in self.shadows:
            self.np_shadow_safe("np_shadow_end", mint, ev, minutes_left)
        if self.np_held(mint):
            coin = self.wl.on_migrate(mint, self.epoch())
            self.np_exit(mint, coin, self.np_t(ev), minutes_left)
            return
        coin = self.wl.get(mint)
        if coin is not None and coin.readings == 0:
            return
        if self.wl.drop(mint):
            self.np_unwatch(mint)

    def np_judge(self, coin, t, minutes_left):
        pos = self.state["positions"].get(coin.mint)
        if pos is not None:                             # in the book already (the migrated strategy's): never twice
            if pos.get("strategy") != "new_pairs":
                self.wl.reject(coin, "held already (migrated)")
            return
        act, why = self.wl.judge(coin, t)
        if act == "fire":
            self.np_fire(coin, t, minutes_left)
        elif act == "wait" and coin.status == "watching":
            self.np_prefetch(coin)
        elif act == "reject" and str(why).startswith("concentrated"):
            # the trigger held, but one buy reading was bigger than np_max_buy_sol (newpairs.concentration): said
            # once (a reject is for good), never bought and never a SHADOW: the rule is about the coin
            self.np_once(("concentrated", coin.mint), "SCANNER", coin.symbol, "reject",
                         "%s · the trigger held, not bought: a buyer that big can sell it all back into the curve "
                         "at once" % why)

    # RugCheck on worker threads: asked when a coin is half way to its trigger, kept np_rug_cache_s
    def np_rug_ask(self, mint, why):
        job = self.np_jobs.get(mint)
        if job is not None:
            return job[0]
        now, cfg = self.epoch(), self.cfg
        c = self.np_rug.get(mint)
        if c and c["report"] is None and now - c["at"] < f(cfg.get("np_rug_retry_s"), 5):
            return None                                 # it failed a moment ago
        try:
            if self.rug_pool is None:
                self.rug_pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(1, int(f(cfg.get("np_rug_workers"), 4))), thread_name_prefix="rugcheck")
            fut = self.rug_pool.submit(getattr(self.m, "rug_report_np", None) or self.m.rug_report, mint)
        except RuntimeError as e:                       # the pool was shut down (the run is ending)
            self.m.errors.append("rugcheck: %s" % e)
            return None
        self.np_jobs[mint] = [fut, now, why]
        fut.add_done_callback(lambda _f, m=mint: self.np_wake(m))
        return fut

    def np_rug_take(self, mint, fut):
        """A finished RugCheck call into the cache -> the report, or None (failed, or for another coin)."""
        job = self.np_jobs.pop(mint, None)
        try:
            rep = fut.result(timeout=0)
        except Exception:                               # noqa: BLE001 - HTTP errors come back as None already
            rep = None
        rep = rep if isinstance(rep, dict) and rep.get("mint") == mint else None
        self.np_rug[mint] = {"at": self.epoch(), "report": rep}
        self.rec_call("rug", mint, rep, job[2] if job else None, job[1] if job else None)
        return rep

    def np_rug_get(self, mint):
        """The coin's RugCheck report: a cached one under np_rug_cache_s, or the call on its way when it has
        finished (asked now if need be). Never waited for: this thread runs every held coin's exits, so a
        slow RugCheck must not hold them. None: no report yet (no entry on this reading; the call wakes the
        loop when it lands, and np_reports() buys then if it came within np_rug_timeout_s of the trigger). A
        report that leaves the coin waiting (too few holders seen for its entry) is kept np_rug_recheck_s only
        (newpairs.rug_cache_s): the coin's next fire asks RugCheck again, one call a coin every 4 s at most and only
        while its trigger holds (the prefetch keeps np_rug_cache_s)."""
        cfg, now = self.cfg, self.epoch()
        c = self.np_rug.get(mint)
        if c and c["report"] is not None and now - c["at"] < newpairs.rug_cache_s(c["report"], self.wl.get(mint), cfg):
            return c["report"]
        fut = self.np_rug_ask(mint, "fire")
        if fut is None:
            return None
        return self.np_rug_take(mint, fut) if fut.done() else None

    def np_reports(self, minutes_left):
        """RugCheck calls that finished: into the cache; a coin waiting for its metadata gets it, a coin
        that fired without a report goes on now if the report came within np_rug_timeout_s, and a coin whose last
        report saw too few holders (newpairs.rug_waiting: its recheck) is judged again whenever the new one lands
        (judge() fires only while the trigger still holds)."""
        now, cfg = self.epoch(), self.cfg
        for mint, job in list(self.np_jobs.items()):
            if not job[0].done():
                continue
            rep = self.np_rug_take(mint, job[0])
            coin = self.wl.get(mint)
            if coin is None:
                continue
            if mint in self.np_meta_rug and coin.meta is None:
                links = self.np_rug_meta(rep)
                if links is None:                       # no answer yet: unknown, asked again (np_meta_due)
                    continue
                del self.np_meta_rug[mint]
                self.wl.on_meta(mint, links)            # {}: RugCheck read the metadata, it has no link
                self.rec_call("event", dict(links, kind="meta", mint=mint, ok=True, source="rugcheck"))
                self.np_judge(coin, now, minutes_left)
                continue
            fired = self.np_fired.pop(mint, None)
            if fired is not None and coin.status == "watching" and (
                    now - fired <= f(cfg.get("np_rug_timeout_s"), 3.0) or newpairs.rug_waiting(coin)):
                self.np_judge(coin, now, minutes_left)
        keep = 2 * max(f(cfg.get("np_rug_cache_s"), 30), f(cfg.get("np_rug_retry_s"), 5))
        for m in [m for m, c in self.np_rug.items() if now - c["at"] > keep]:
            del self.np_rug[m]
        for m in [m for m, t in self.np_fired.items() if now - t > 60]:
            del self.np_fired[m]

    def np_prefetch(self, coin):
        """RugCheck is asked before the trigger fires, when the coin is half way there (half the SOL and
        half the buys, cheap checks passed): the fire then finds its report waiting."""
        cfg = self.cfg
        if not cfg.get("np_rug_prefetch", True) or coin.mint in self.np_jobs or coin.mint in self.np_meta_rug:
            return                                      # (a coin waiting for its links from RugCheck: np_meta_due asks)
        c = self.np_rug.get(coin.mint)
        if c and self.epoch() - c["at"] < f(cfg.get("np_rug_cache_s"), 30):
            return
        if coin.sol_in >= f(cfg.get("np_min_sol"), 3.0) / 2 and coin.buys >= f(cfg.get("np_min_buys"), 8) / 2 \
                and newpairs.cheap_check(coin, cfg)[0]:
            self.np_rug_ask(coin.mint, "prefetch")

    def np_touch(self):
        """The poller read the curves again: unchanged held curves are fresh (feeds sends changes only)."""
        poller = getattr(self.feeds, "poller", None)
        ok = getattr(poller, "ok", None)
        if isinstance(ok, int) and not isinstance(ok, bool) and ok > (self.poll_ok or 0):
            self.poll_ok = ok
            self.wl.touch(self.epoch(), self.wl.held())
            if self.shadows and self.np_shadow_wl is not None:      # the shadows' curves are polled too
                self.np_shadow_wl.touch(self.epoch())

    def np_fire(self, coin, t, minutes_left):
        """FIRST IN: the trigger fired on reading t. RISK, RUG CHECK (the report the prefetch asked for;
        without one, the buy comes on the turn it lands), NEWS (the hold plan), the LEARNER, the SIZER, then
        a Jupiter quote and the honeypot sell simulation, and the coin is in the book, all on this loop turn."""
        cfg, n, mint = self.cfg, self.state["night"], coin.mint
        now = self.epoch()
        if mint in self.state["positions"]:             # held already (np_judge says so): never bought twice
            self.wl.reject(coin, "held already")
            return
        if self.np_hold.get(mint, 0) > now:             # no route a moment ago: not every reading
            self.wl.retry(coin)
            return
        why = self.why_no_entries(minutes_left, {}) or self.np_job_why()
        if why:
            self.say_once("risk_said", why, "RISK", coin.symbol, "skip", "no entry: %s" % why)
            self.rec_call("decide", mint, "risk", why)
            self.wl.retry(coin)
            self.np_shadow_safe("np_shadow_risk", coin, why, minutes_left)    # shadow_on_risk: a SHADOW if it passes RugCheck
            return
        # flagged once a trigger; a coin waiting for RugCheck to see its holders once in all: while it waits, its
        # trigger can lapse and hold again every few readings (each a new trigger_t), and each one is no new coin
        if ("fire", mint, coin.trigger_t) not in self.np_said and not (
                newpairs.rug_waiting(coin) and ("flag", mint) in self.np_said):
            self.np_said[("fire", mint, coin.trigger_t)] = now
            self.np_said[("flag", mint)] = now
            self.np_stats["fired"] += 1
            n["flagged"] = n.get("flagged", 0) + 1
            n["flags"] = ([x for x in n.get("flags", []) if x.get("coin") != coin.symbol]
                          + [{"coin": coin.symbol, "why": "momentum", "t": self.ts()}])[-8:]
            self.log("SCANNER", coin.symbol, "flag", coin.why)
            self.rec_call("fire", coin)
        rep = self.np_rug_get(mint)
        ok, bad, holders = newpairs.rug_check_np(rep, coin, cfg)
        if ok is None:                                  # no report yet: np_reports() goes on when it lands
            self.np_fired.setdefault(mint, now)
            if newpairs.rug_waiting(coin):              # too few holders for its entry: no PASS, no shadow either
                self.np_once(("rug wait", mint), "RUG CHECK", coin.symbol, "wait",
                             "%s · no entry until RugCheck sees enough: asked again every %g s while the trigger "
                             "holds" % (newpairs.rug_text(coin), f(cfg.get("np_rug_recheck_s"), 4.0)))
            return
        self.np_fired.pop(mint, None)
        n["checked"] = n.get("checked", 0) + 1
        if not ok:
            self.wl.reject(coin, bad[0])
            self.log("RUG CHECK", coin.symbol, "reject", newpairs.rug_text(coin))
            n["filtered"] = n.get("filtered", 0) + 1
            n["last_rug"] = {"coin": coin.symbol, "pass": False}
            return
        n["last_rug"] = {"coin": coin.symbol, "pass": True}
        if ("rug pass", mint) not in self.np_said:
            self.np_said[("rug pass", mint)] = now
            self.log("RUG CHECK", coin.symbol, "pass", newpairs.rug_text(coin))
        plan, labels = self.np_entry_plan(coin)        # NEWS: the hold plan; the LEARNER's labels
        d, explore = self.learn_decide(labels, coin.symbol)
        if d is not None and d["action"] == "skip":
            self.wl.reject(coin, "learner: skip")
            self.np_shadow_safe("np_shadow_open", coin, plan, labels, "learner", "learner skip")   # still followed: a SHADOW
            return
        mult = d["mult"] if d is not None else 1.0
        sol_usd = self.sol_usd()
        if not sol_usd:
            self.wl.retry(coin)
            return
        # the cash a buy may use: the balance cut to 1/100 of a cent (see scan())
        cash = max(0.0, math.floor(self.state["balance"] * 1e4) / 1e4)
        eq = self.equity({})
        size = min(cash, eq * cfg["position_pct"] / 100.0)
        if size < cfg["min_order_usd"]:
            self.say_once("sizer_said", "cash", "SIZER", coin.symbol, "skip", "only %s left to size" % usd(size))
            self.wl.retry(coin)
            return
        want = min(cash, max(cfg["min_order_usd"], size * mult))
        # the buy and its sell simulation are 2 quotes, and the minute keeps one for each open coin's sale
        if not getattr(self.m, "can_quote", lambda k: True)(2 + len(self.state["positions"])):
            self.np_hold[mint] = now + 2
            self.wl.retry(coin)
            return
        fee_sol = f(cfg.get("network_fee_sol"), 0.0005) + f(cfg.get("priority_fee_sol"), 0.0)
        fx = {"symbol": coin.symbol or "?", "name": coin.name, "url": "https://pump.fun/coin/%s" % mint}
        pos = self.buy(mint, fx, want - fee_sol * sol_usd, sol_usd, 6, coin.why, fee_sol=fee_sol)
        if not pos:
            self.np_hold[mint] = now + 3
            self.wl.retry(coin)
            qwhy = getattr(self.m, "quote_why", None) or "no Jupiter route"
            self.say_once("np_quote_said", qwhy, "EXECUTOR", coin.symbol, "wait", "no buy yet: %s" % qwhy, every=60)
            return
        check, bad = self.np_sell_sim(pos, coin, sol_usd)
        if bad == "jupiter":
            self.np_hold[mint] = now + 3                # Jupiter resting or its minute full: not a honeypot
            self.wl.retry(coin)
            return
        if bad:
            self.log("RUG CHECK", coin.symbol, "reject", "sell simulation: %s" % bad)
            self.wl.reject(coin, "sell simulation")
            n["filtered"] = n.get("filtered", 0) + 1
            n["last_rug"] = {"coin": coin.symbol, "pass": False}
            return
        self.np_shadow_safe("np_shadow_drop", mint, "bought for real")    # a RISK shadow of it: the real trade teaches now
        if labels:
            pos["learn"] = labels
        pos.update({"strategy": "new_pairs", "plan": plan, "age_s": int(coin.age_s), "mcap_sol": round(coin.mcap_sol, 1),
                    "dev_pct": round(coin.dev_pct, 2), "sol_in": round(coin.sol_in, 2), "holders": holders,
                    "curve_check": check, "size_pct": round(pos["cost"] / eq * 100, 1) if eq else None,
                    "moved_t": now})
        self.state["balance"] = max(0.0, self.state["balance"] - pos["cost"])    # cost <= cash: only a float's last bit
        self.state["positions"][mint] = pos
        self.traded = True
        if explore is not None:
            self.learn_commit(explore)
        self.wl.mark_entered(coin, now, coin.price)
        pos["np"] = coin.to_dict()
        self.np_win[mint] = [[coin.t0, 0, 0, coin.rsol0], [now, coin.buys, coin.sells, coin.rsol]]
        self.news_at[mint] = now
        self.np_holding()
        wall = self.wall()
        self.np_speed_add("first_in", (wall - (coin.trigger_t if coin.trigger_t is not None else t)) * 1000)
        self.np_speed_add("launch", wall - coin.t0)
        worst = max([abs(v) for v in check.values() if v is not None] or [float("inf")])
        self.log("SIZER", coin.symbol, "size", self.size_text(eq, mult, size, want, pos["cost"]))
        self.log("EXECUTOR", coin.symbol, "enter", "ENTER %s at %s · slip %.1f%% · %s · %s" % (
            coin.symbol, ("%.10f" % pos["entry_price"]).rstrip("0"), pos["impact_pct"],
            newpairs.first_in_text(coin, wall),
            ("curve maths %.1f%% off Jupiter" % worst) if worst != float("inf") else "curve maths not checked"))
        self.log("NEWS", coin.symbol, "plan", holdplan.plan_text(plan))
        self.rec_call("enter", mint, pos, coin, sol_usd)

    def np_entry_plan(self, coin):
        """NEWS at an entry (a real one or a SHADOW's): the coin's links and the curve's momentum -> (the hold plan,
        the LEARNER's labels of the coin now)."""
        cfg = self.cfg
        score, parts = holdplan.news_score(newpairs.news_info(coin), cfg)
        plan = holdplan.new_plan(score, parts, holdplan.tiers_from_cfg(cfg), cfg)
        return plan, newpairs.labels(coin, None, plan, local(self.now, cfg["timezone"]).hour)

    def np_sell_sim(self, pos, coin, sol_usd):
        """The honeypot test after a buy quote (the last quote: buy() set quote_out): Jupiter's sale of the tokens
        it got, right away. -> (the curve maths against both quotes {"buy_pct", "sell_pct"}, None when it passed,
        "jupiter" when Jupiter gave no answer now (resting, its minute full: no verdict), else why it fails). What
        the two quotes alone lose, the SOL back for these tokens against the SOL in (pump.fun's fee and the price
        impact both ways, about 3 %), never the haircut and the fixed fees, which are the same for every coin and
        only big on a small order (a $5 explore at SOL $250: 10 % of it)."""
        cfg = self.cfg
        fee = f(cfg.get("curve_fee_pct"), 1.25)
        sol_in = (pos["cost"] - self.fee_usd(sol_usd, pos)) / sol_usd   # the SOL the buy quote was for
        buy_out = self.quote_out                        # its tokens, before the haircut
        check = {"buy_pct": self.np_diff(curve_buy_tokens(coin.vsol, coin.vtok, sol_in, fee), buy_out / 1e6),
                 "sell_pct": None}
        back = self.sell_quote(pos, pos["raw"], sol_usd, sim=True)
        if back is None and (getattr(self.m, "quote_why", None) or "").startswith("Jupiter"):
            return check, "jupiter"
        loss = (1 - self.quote_out / 1e9 * (buy_out / float(pos["raw"])) / sol_in) * 100 if back is not None else 100.0
        if back is None or loss > cfg["max_roundtrip_loss_pct"]:
            return check, "can't sell (honeypot?)" if back is None else "would lose %.0f%% at once on the quotes" % loss
        check["sell_pct"] = self.np_diff(curve_sell_sol(coin.vsol, coin.vtok, pos["raw"] / 1e6, fee), self.quote_out / 1e9)
        return check, None

    def np_job_why(self):
        """RISK at the job's end (a night is two GitHub jobs, and the next one starts minutes later): no new
        pair is bought in the last job_no_entries_min of a job the next one takes over, None otherwise."""
        w = f(self.cfg.get("job_no_entries_min"), 5)
        if self.job_left is not None and self.job_left <= w:
            return "last %g min of this job: no new entries (the next job takes over)" % w
        return None

    @staticmethod
    def np_diff(curve, quote):
        """% the curve maths is off a Jupiter quote taken at the same moment, or None."""
        if not (curve > 0 and quote > 0):
            return None
        return round((curve / quote - 1) * 100, 2)

    def np_curve_sale(self, pos, raw, sol_usd):
        """A New Pair's sale when Jupiter's quote failed: the curve's own maths on the last reading, with
        pump.fun's fee, the haircut and the fees, but only when that maths was within np_validate_pct of
        Jupiter's quotes at the entry (an unchecked formula never prices a fill). -> dollars or None."""
        chk = pos.get("curve_check") or {}
        diffs = [abs(f(v)) for v in chk.values() if v is not None]
        coin = self.wl.get(pos["mint"]) if self.wl is not None else None
        if not diffs or max(diffs) > f(self.cfg.get("np_validate_pct"), 5.0) or coin is None:
            return None
        sol = curve_sell_sol(coin.vsol, coin.vtok, raw / 10.0 ** pos.get("decimals", 6), f(self.cfg.get("curve_fee_pct"), 1.25))
        if not sol > 0:
            return None
        self.fill_note = " · by the curve maths (Jupiter: %s; it was %.1f%% off a quote at the entry)" % (
            getattr(self.m, "quote_why", None) or "no quote", max(diffs))
        pos["fill"] = "curve"
        return self.sale_usd(sol, sol_usd, pos)

    def np_exit(self, mint, coin, t, minutes_left, px_usd=None, reading=False, shadow=False):
        """FIRST OUT: one held new pair against its rules, on the reading that came at t (t None: no reading
        decided it, the 1 s step's pass or Jupiter's price): the end of the night (and of a job the next one
        takes over), the dump and migration exits (newpairs), the NEWS refresh when it is due (np_news:
        replan, fading), then the hold plan (plan.py: the hard stop, the ladder, the tier's time stop,
        break-even and the trailing stop after the first step), decided after the replan, then the two
        exits for a coin going nowhere (np_late_exit). Only an exit a reading's price decided is timed
        from that reading (exit_ms, "out · 0.2 s after the reading"); the clock's exits are not reactions.
        shadow: a SHADOW trade's (self.shadows, coin its own copy of the coin): these very rules, its sales by
        np_shadow_sell in close_part's place, never the book."""
        pos = (self.shadows if shadow else self.state["positions"]).get(mint)
        if pos is None:
            return
        sell = self.np_shadow_sell if shadow else self.close_part
        cfg = self.cfg
        sol_usd = self.sol_usd()
        if not sol_usd:
            return
        if px_usd is None:
            if coin is None or not f(coin.price) > 0:
                return
            px_usd = coin.price * sol_usd
        pos["last"] = px_usd
        pos["peak"] = max(f(pos.get("peak")), px_usd)
        if reading:
            pos.setdefault("hist", []).append(px_usd)
            del pos["hist"][:-120]
        if pos.get("retry_at", 0) > self.epoch():      # a sale that found no route waits its 5 s
            return
        gain = (px_usd / pos["entry_price"] - 1) * 100
        plan = pos.get("plan")
        if not isinstance(plan, dict):                  # a position from before the hold plan
            plan, _ = holdplan.replan(None, 0, [], gain, cfg)
            pos["plan"] = plan
        peak = (pos["peak"] / pos["entry_price"] - 1) * 100
        pcfg = self.np_plan_cfg(pos, plan, coin, sol_usd)
        holdplan.track(plan, gain, pcfg)
        held = (self.now - datetime.datetime.strptime(pos["opened"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds() / 60
        if minutes_left <= cfg["flat_before_end_min"]:
            why, timed = "end of night", False
        elif self.job_left is not None and self.job_left <= f(cfg.get("job_flat_min"), 1):
            why, timed = "the job ends · the next job takes over", False
        else:
            why, timed = (newpairs.exit_signal(coin, cfg) if coin is not None else None), True
        if why:
            sell(mint, 1.0, why, sol_usd, t_reading=t if timed else None)
            return
        if coin is not None and self.epoch() - self.news_at.setdefault(mint, self.epoch()) >= \
                f(cfg.get("news_refresh_sec"), 30):
            self.news_at[mint] = self.epoch()
            plan, fade = self.np_news(mint, pos, coin, gain, pcfg)
            if fade:
                if not shadow:
                    self.log("NEWS", pos["symbol"], "fade", fade["why"])
                sell(mint, 1.0, fade["why"], sol_usd)
                return
        d = holdplan.decide_exit(plan, gain, peak, held, pcfg) or self.np_late_exit(pos, plan, coin, gain, held)
        if d is None:
            pos.pop("exit_t0", None)                    # nothing to sell now: a failed sale's reading no longer counts
            return
        tr = t if d.get("kind") in ("stop", "take", "trail", "breakeven") else None
        if d.get("final") or f(d.get("frac")) >= 0.999:
            sell(mint, 1.0, d["why"], sol_usd, take=d, t_reading=tr)
        elif sell(mint, d["frac"], d["why"], sol_usd, take=d, t_reading=tr):
            holdplan.mark_step(plan, d, pcfg)
            holdplan.track(plan, gain, self.np_plan_cfg(pos, plan, coin, sol_usd))   # the stop for what is left

    def np_plan_cfg(self, pos, plan, coin, sol_usd):
        """The config plan.py decides with: after the first ladder step, breakeven_pct is the gain at which
        selling the rest now leaves the whole trade at 0 after every cost (pump.fun's fee and the price
        impact by the curve maths, the latency haircut, the order's network and priority fees; the step
        already sold counted), at least breakeven_pct and at most the highest step sold. A stop at +1 % of
        the price sells the rest under its costs: "a winner never turns into a loser" is counted in dollars."""
        cfg = self.cfg
        if not (f(plan.get("sold")) > 0 or f(plan.get("step")) > 0):
            return cfg
        level = self.np_even_gain(pos, coin, sol_usd)
        if level is None:
            return cfg
        floor = f(cfg.get("breakeven_pct"), 1.0)
        top = f(plan.get("done_gain"))
        be = max(floor, min(level, top) if top > floor else floor)
        return dict(cfg, breakeven_pct=round(be, 3))

    def np_even_gain(self, pos, coin, sol_usd):
        """The price gain (% over the entry price) at which selling everything still held now leaves the whole
        trade at 0 after every cost: pump.fun's fee and the price impact by the curve maths, the latency
        haircut, the order's network and priority fees, the steps already sold counted. None when unknown."""
        cfg = self.cfg
        tokens, entry = f(pos.get("tokens_ui")), f(pos.get("entry_price"))
        if not (tokens > 0 and entry > 0 and f(sol_usd) > 0):
            return None
        fee = f(cfg.get("curve_fee_pct"), 1.25)
        k = 1 - fee / 100.0                             # what a sale brings of the price, the curve's impact aside
        if coin is not None and f(coin.vsol) > 0 and f(coin.vtok) > 0 and f(coin.price) > 0:
            got = curve_sell_sol(coin.vsol, coin.vtok, tokens, fee)
            if got > 0:
                k = got / (tokens * coin.price)
        k *= 1 - f(cfg.get("latency_slip_pct"), 1.0) / 100.0
        need = f(pos.get("cost")) - f(pos.get("sold_usd")) + self.fee_usd(sol_usd, pos)
        return (need / (tokens * k) / entry - 1) * 100 if k > 0 else None

    def np_late_exit(self, pos, plan, coin, gain, held):
        """Two exits for a coin going nowhere (or a full book of them would stop the night's entries): its
        curve had no trade for np_quiet_min (a pump.fun curve nobody trades keeps one price for good), or,
        before the first step, time_stop_late_x times its tier's time stop passed without its first step
        (the time stop itself spares a coin over time_stop_below_pct). -> plan.py's form of a decision, or None."""
        cfg = self.cfg
        quiet = f(cfg.get("np_quiet_min"), 3.0)
        moved = f(pos.get("moved_t"), 0.0)
        if coin is not None and quiet > 0 and moved > 0 and self.epoch() - moved >= quiet * 60:
            stale = newpairs.stale(coin, cfg, self.epoch())
            why = ("no curve reading for %g min" if stale else "curve quiet for %g min (no trade)") % quiet
            return {"frac": 1.0, "why": why, "final": True, "step": None, "kind": "quiet"}
        x = f(cfg.get("time_stop_late_x"), 2.0)
        ts = f(plan.get("time_stop_min"))
        ladder = plan.get("ladder") if isinstance(plan.get("ladder"), list) else []
        first = f(ladder[0][0], 0.0) if ladder and isinstance(ladder[0], (list, tuple)) and ladder[0] else 0.0
        if x > 0 and ts > 0 and first > 0 and not (f(plan.get("sold")) > 0 or f(plan.get("step")) > 0) \
                and held >= ts * x and gain < first:
            return {"frac": 1.0, "why": "%d min, no +%g%% yet (%+.0f%%)" % (int(held), first, gain), "final": True,
                    "step": None, "kind": "time"}
        return None

    def np_window(self, mint, coin, t):
        """The held coin's counts over time, for NEWS's momentum over the last fade_window_s (fading): one
        snapshot a second at most (the poller reads a held coin every 0.5 s: 600 readings in 5 minutes),
        every one inside the window and the last one before it (the baseline np_now_info counts from).
        Bounded by time, never by a count: a count cap drops the recent snapshots and leaves the launch as
        the baseline, the momentum since the launch instead of the last 5 minutes."""
        w = max(1.0, f(self.cfg.get("fade_window_s"), 300))
        gap = max(1.0, w / 300.0)                       # at most about 300 snapshots, whatever the window
        win = self.np_win.setdefault(mint, [[coin.t0, 0, 0, coin.rsol0]])
        snap = [t, coin.buys, coin.sells, coin.rsol]
        if len(win) >= 2 and win[-1][0] - win[-2][0] < gap:
            win[-1] = snap                              # the newest is under a second after the one before: it moves
        else:
            win.append(snap)
        base = 0
        for i, s in enumerate(win):
            if s[0] > t - w:
                break
            base = i
        if base:
            del win[:base]                              # the last one at or before t - w stays: the baseline

    def np_now_info(self, mint, coin):
        """plan.news_score()'s input for a held coin now: its links, and the curve's momentum over the
        last fade_window_s (since the launch while the coin is younger than that)."""
        info = newpairs.news_info(coin)
        win = self.np_win.get(mint) or [[coin.t0, 0, 0, coin.rsol0]]
        now = self.epoch()
        w = f(self.cfg.get("fade_window_s"), 300)
        base = win[0]
        for snap in win:
            if snap[0] <= now - w:
                base = snap
        info.update(buys=max(0, coin.buys - base[1]), sells=max(0, coin.sells - base[2]),
                    sol_in=round(coin.rsol - base[3], 3))
        return info

    def np_news(self, mint, pos, coin, gain, pcfg=None):
        """NEWS again (every news_refresh_sec while held, from np_exit): the score now may move the hold
        plan up or down a tier (np_exit decides the exits right after: a downgrade can bring a tighter
        trail or a step already passed), and a coin whose buyers left while its news faded is sold when in
        profit. pcfg: np_plan_cfg()'s (the stop after a step). -> (the plan, plan.fade_exit()'s decision or None)."""
        cfg = self.cfg
        info = self.np_now_info(mint, coin)
        score, parts = holdplan.news_score(info, cfg)
        plan, note = holdplan.replan(pos.get("plan"), score, parts, gain, pcfg or cfg)
        pos["plan"] = plan
        if note and not pos.get("shadow"):              # a SHADOW's replans are not the night's news
            self.log("NEWS", pos["symbol"], "replan", note)
        # "in profit" is counted in dollars: the gain must clear the trade's costs (np_even_gain), or a fading
        # exit would book a loss while saying it took a profit
        even = self.np_even_gain(pos, coin, self.sol_usd())
        fcfg = dict(cfg, fade_min_gain_pct=max(f(cfg.get("fade_min_gain_pct"), 0.0), even if even is not None else 0.0))
        return plan, holdplan.fade_exit(plan.get("entry_score"), info, gain, fcfg)

    def np_stale(self):
        """Held new pairs without a curve reading for np_stale_s (or not watched at all): Jupiter prices them."""
        now, out = self.epoch(), []
        for mint, pos in self.state["positions"].items():
            if pos.get("strategy") == "new_pairs":
                coin = self.wl.get(mint)
                if coin is None or newpairs.stale(coin, self.cfg, now):
                    out.append(mint)
        return out

    def np_second(self, prices, minutes_left):
        """The 1 s part of New Pairs: the watch list's expiry, each held coin's time stop and the end of
        the night (no reading needed for those), the stale guard, the NEWS refresh, the minute's line."""
        cfg, now = self.cfg, self.epoch()
        for m in self.wl.expire(now):
            self.np_unwatch(m)
        for m in [m for m, t in self.np_hold.items() if t <= now]:
            del self.np_hold[m]
        self.np_meta_due()
        stale = set(self.np_stale())
        for mint in list(self.state["positions"]):
            pos = self.state["positions"].get(mint)
            if not pos or pos.get("strategy") != "new_pairs":
                continue
            coin = self.wl.get(mint)
            px = None
            if mint in stale:
                px = (prices.get(mint) or {}).get("usd")
                if not pos.get("stale_said"):
                    pos["stale_said"] = True
                    self.log("EXECUTOR", pos["symbol"], "stale", "no curve reading for %g s · %s" % (
                        f(cfg.get("np_stale_s"), 5), "Jupiter's price $%.4g" % px if px else "no price: the last one stands"))
                if not px:
                    # the last price stands: no price exit can fire on it again, but the clock's exits do (the
                    # time stops, the quiet curve, the end of the night), so a coin nobody prices is never stuck
                    px = pos.get("last") or pos["entry_price"]
            else:
                pos.pop("stale_said", None)
            self.np_exit(mint, coin, None, minutes_left, px_usd=px)    # no reading decided these: not timed
        self.np_shadow_safe("np_shadow_second", minutes_left)
        self.np_summary(now)
        self.np_health(now)
        self.np_speed_view(now)
        self.np_rec_tick()

    def np_summary(self, now):
        """SCANNER once a minute: what the New Pairs column shows (each coin is a row on the page, not a line)."""
        n = self.state["night"]
        if now - n.get("np_said", float("-inf")) < 60:
            return
        coins = list(self.wl.coins.values())
        if not coins and not self.wl.skipped:           # nothing came yet: the feeds' line says why
            return
        n["np_said"] = now
        watching = [c for c in coins if c.status == "watching"]
        why = collections.Counter()
        for c in coins + list(self.wl.skipped):
            if str(c.status).startswith("rejected"):
                short = (c.why or "").split(" (")[0]
                why[(short.split(":")[0] if short.startswith(("serial", "dump")) else
                     " ".join(w for w in short.split() if not any(ch.isdigit() for ch in w)))[:32] or "?"] += 1
        near = max(watching, key=lambda c: (c.sol_in, c.buys), default=None)
        text = "new pairs · %.1f a minute · %d watched · %d rejected%s" % (
            self.wl.creates_per_min(now), len(watching), sum(why.values()),
            " (%s)" % ", ".join("%s %d" % kv for kv in why.most_common(3)) if why else "")
        if near is not None and near.why:
            text += " · closest %s: %s" % (near.symbol, near.why)
        self.log("SCANNER", None, "scan", text)

    def np_health(self, now):
        """The feeds' state in the log, once each: none, no websockets (no PumpPortal), PumpPortal down 60 s."""
        if self.feeds is None:
            self.np_once("nofeeds", "SCANNER", None, "off", "new pairs off: no feeds (%s) · the night goes on without them"
                         % (self.feeds_off or "?"))
            return
        st = next((x for x in (self.np_call("status") or []) if isinstance(x, dict) and x.get("name") == "pumpportal"), {})
        err = str(st.get("last_error") or "")
        if "websockets" in err and "not installed" in err:
            self.np_once("nows", "SCANNER", None, "off", "pumpportal off: %s · no new pairs; held coins are still read "
                                                         "from their curves" % err)
            return
        if st.get("connected"):
            if self.np_said.pop("down", None) is not None:
                self.log("SCANNER", None, "feed", "pumpportal live again · %s" % (self.np_call("text") or ""))
            self.feed_down_at = None
            self.np_once("up", "SCANNER", None, "feed", "feeds · %s" % (self.np_call("text") or "live"))
        elif st:
            self.feed_down_at = self.feed_down_at or now
            if now - self.feed_down_at >= 60:
                self.np_once("down", "SCANNER", None, "feed", "pumpportal down for %d s (%s) · no new pairs until it is "
                                                              "back; the exits go on" % (now - self.feed_down_at,
                                                                                          err or "connecting"))

    def np_speed_add(self, kind, value):
        """A latency of the night (ms; launch: s): the panel shows the median and the last one."""
        n = self.state.get("night")
        if n is None or not math.isfinite(value):
            return
        lst = n.setdefault("np_speed", {}).setdefault(kind, [])
        lst.append(int(round(max(0.0, value))))
        del lst[:-500]

    def np_speed_view(self, now):
        sp = (self.state.get("night") or {}).get("np_speed") or {}

        def last(k):
            return (sp.get(k) or [None])[-1]
        status = self.np_call("status") if self.feeds is not None else None
        if not isinstance(status, list):
            status = [{"name": "pumpportal", "connected": False, "events": 0, "last_event_age_s": None, "errors": 0,
                       "last_error": self.feeds_off or "no feeds"}]
        text = self.np_call("text") if self.feeds is not None else "new pairs off: no feeds (%s)" % (self.feeds_off or "?")
        self.speed.update({
            "first_in_ms": median(sp.get("first_in") or []), "first_in_last_ms": last("first_in"),
            "exit_ms": median(sp.get("exit") or []), "exit_last_ms": last("exit"),
            "launch_to_entry_s": median(sp.get("launch") or []),
            "creates_per_min": self.wl.creates_per_min(now),
            "watching": sum(1 for c in self.wl.coins.values() if c.status == "watching"),
            "followed": len(self.wl.coins), "feeds_status": status, "feeds_text": str(text or "")})

    def np_closed(self, mint, why):
        """A new pair sold to the last token: off the watch list's candidates, the poll slows when nothing is held."""
        coin = self.wl.get(mint) if self.wl is not None else None
        if coin is not None:
            self.wl.mark_exited(coin, why, self.epoch())
        for d in (self.np_win, self.news_at):
            d.pop(mint, None)
        self.np_holding()

    def np_rows(self):
        return self.wl.rows(self.epoch()) if self.wl is not None else []

    def np_sync(self):
        """Held new pairs' coins into state.json: a restart watches them again with what they showed."""
        if self.wl is None:
            return
        for mint, pos in self.state["positions"].items():
            coin = self.wl.get(mint)
            if pos.get("strategy") == "new_pairs" and coin is not None:
                pos["np"] = coin.to_dict()
                if self.np_win.get(mint):
                    pos["np_win"] = np_win_save(self.np_win[mint])

    def np_close(self):
        for name in ("rug_pool", "sol_pool"):
            pool = getattr(self, name)
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
                setattr(self, name, None)
        self.rec_call("close")                          # the tape's last lines to disk (rec_close_wait_s at most)

    # ---- SHADOW TRADES (DEFAULTS "shadow_*"): a coin whose trigger fired, that passed RugCheck and was not bought
    # (the LEARNER skipped it; with shadow_on_risk, RISK said no entry) is followed as if it had been. Its position
    # has a real position's fields and lives in self.shadows; its coin is a copy in self.np_shadow_wl, entered there
    # (the dump exit counts the sells since its own entry, the stale guard and the poller's touches work on it); and
    # np_exit() runs it by the very rules of a held coin on the same readings, after the book's. Its sales go through
    # np_shadow_sell(), never close_part(): the balance, the positions, the trades, RISK's counts, the loss streak and
    # the night's counts never see it. When it closes, the LEARNER learns its P&L % with shadow_weight. Paper on
    # paper: a shadow that made money says the LEARNER may have skipped a winner, not that it would have been one.
    def np_shadow_safe(self, name, *args):
        """self.<name>(*args) for a SHADOW hook in the real paths (np_fire, a reading, the 1 s step, the panel): a
        shadow's error must never cost a real trade or the exits' step. The first one switches the shadows off for the
        run, said once: the open ones are dropped (not learned) and their curves let go; the trading goes on."""
        if self.np_shadow_wl is None:
            return None
        try:
            return getattr(self, name)(*args)
        except Exception as e:                          # noqa: BLE001 - paper on paper is never worth a real trade
            mints, self.np_shadow_wl = list(self.shadows), None
            self.shadows.clear()
            for m in mints:
                try:
                    self.np_shadow_release(m)
                except Exception:                       # noqa: BLE001
                    pass
            text = "shadow trades off: %s: %s: %s · %d open dropped, not learned · the trading goes on" % (
                name, type(e).__name__, str(e)[:160], len(mints))
            self.m.errors.append(text)
            self.log("LEARNER", None, "error", text)
            return None

    def np_shadow_why_not(self, mint):
        """None when a shadow of mint may open now, else why not: "off", "already" (open now, or shadowed once this
        run: one a coin), or the cap: shadow_max_open, and rec_max_keys less np_max_watch (the curves one poll call
        still takes beside a full watch list: a shadow keeps its curve polled after the watch list lets it go)."""
        cfg = self.cfg
        if self.np_shadow_wl is None or not cfg.get("shadow_on", True):
            return "off"
        if mint in self.shadows or mint in self.np_shadowed:
            return "already"
        most = min(int(f(cfg.get("shadow_max_open"), 20)),
                   int(f(cfg.get("rec_max_keys"), 100)) - int(f(cfg.get("np_max_watch"), 40)))
        if len(self.shadows) >= most:
            return "%d shadows open (max %d)" % (len(self.shadows), max(0, most))
        return None

    def np_shadow_usd(self):
        """A shadow's nominal order: shadow_usd, or (0) what the SIZER would size now, position_pct of the equity and
        at least min_order_usd (never cut to the cash: a shadow spends none)."""
        cfg = self.cfg
        fixed = f(cfg.get("shadow_usd"), 0.0)
        if fixed > 0:
            return fixed
        return max(f(cfg.get("min_order_usd"), 5.0), self.equity({}) * f(cfg.get("position_pct"), 10.0) / 100.0)

    def np_shadow_open(self, coin, plan, labels, kind, why):
        """A SHADOW of a coin np_fire() did not buy after it passed RugCheck (kind "learner": the LEARNER skipped it;
        "risk": RISK said no entry, why). Priced as a real entry of np_shadow_usd() on the reading that fired, by the
        curve maths with every cost (np_shadow_curve_buy), never by Jupiter: a shadow's quote would share the real
        book's Jupiter (its free minute, and the rest after a failure: Market.quote_pause, 10-60 s with no quote at
        all, so the next REAL entry would wait) and its loop time. So it gets no honeypot sell simulation either.
        plan and labels: the entry's (np_entry_plan). -> the shadow, or None."""
        mint, cfg = coin.mint, self.cfg
        no = self.np_shadow_why_not(mint)
        if no is not None:
            if no not in ("off", "already"):
                self.say_once("shadow_said", no, "LEARNER", coin.symbol, "shadow", "SHADOW %s not followed: %s" % (
                    coin.symbol, no))
            return None
        sol_usd = self.sol_usd()
        if not sol_usd or not f(coin.price) > 0:
            return None
        now = self.epoch()
        self.np_shadowed[mint] = now
        fee_sol = f(cfg.get("network_fee_sol"), 0.0005) + f(cfg.get("priority_fee_sol"), 0.0)
        usd_in = self.np_shadow_usd() - fee_sol * sol_usd    # as np_fire: the order's fee inside its size
        fx = {"symbol": coin.symbol or "?", "name": coin.name, "url": "https://pump.fun/coin/%s" % mint}
        pos = self.np_shadow_curve_buy(coin, fx, usd_in, sol_usd, fee_sol) if usd_in > 0 else None
        sc = newpairs.Coin.from_dict(coin.to_dict()) if pos is not None else None
        if sc is None or not self.np_shadow_wl.adopt(sc):
            return None
        self.np_shadow_wl.mark_entered(sc, now, coin.price)
        pos.update({"strategy": "new_pairs", "plan": plan, "age_s": int(coin.age_s), "mcap_sol": round(coin.mcap_sol, 1),
                    "dev_pct": round(coin.dev_pct, 2), "sol_in": round(coin.sol_in, 2), "moved_t": now,
                    "shadow": kind, "shadow_why": str(why or kind)[:120]})
        if labels:
            pos["learn"] = labels
        self.shadows[mint] = pos
        self.np_win[mint] = [[coin.t0, 0, 0, coin.rsol0], [now, coin.buys, coin.sells, coin.rsol]]
        self.news_at[mint] = now
        self.log("LEARNER", coin.symbol, "shadow", "SHADOW %s · not bought (%s) · followed as if bought: %s at %s (curve "
                                                   "maths, every cost) · same exits, outside the book" % (
                     coin.symbol, pos["shadow_why"], usd(pos["cost"]), ("%.10f" % pos["entry_price"]).rstrip("0")))
        plan_doc = plan if isinstance(plan, dict) else {}
        self.np_rec_shadow(mint, "open", {"kind": kind, "why": pos["shadow_why"], "usd": pos["cost"], "raw": pos["raw"],
                                          "px": pos["entry_price"], "px_sol": coin.price, "sol": sol_usd,
                                          "fill": pos.get("fill"), "tier": plan_doc.get("entry_tier") or plan_doc.get("tier"),
                                          "score": plan_doc.get("entry_score"), "learn": labels})
        return pos

    def np_shadow_curve_buy(self, coin, fx, usd_in, sol_usd, fee_sol):
        """A shadow's entry by the curve maths on the reading that fired: usd_in of SOL through pump.fun's fee and the
        curve's price impact (curve_buy_tokens: the maths each real entry checks against Jupiter, 0.0 % off on the
        live fills), less the latency haircut, the order's network and priority fees on top: buy()'s costs. -> the
        position, or None."""
        cfg = self.cfg
        fee = f(cfg.get("curve_fee_pct"), 1.25)
        sol = usd_in / sol_usd
        got = curve_buy_tokens(coin.vsol, coin.vtok, sol, fee)
        raw = int(got * 1e6 * (1 - cfg["latency_slip_pct"] / 100.0))
        if not (usd_in > 0 and raw > 0 and f(coin.price) > 0):
            return None
        spot = sol * (1 - fee / 100.0) / coin.price     # the tokens at the reading's price, the fee paid
        pos = self.position(coin.mint, fx, usd_in, raw, 6, coin.why, fee_sol, max(0.0, (1 - got / spot) * 100), sol_usd)
        pos["fill"] = "curve"
        return pos

    def np_shadow_risk(self, coin, why, minutes_left):
        """RISK said no entry (np_fire, why): with shadow_on_risk, a coin that passes RugCheck, as a real entry would
        have to, is followed as a SHADOW ("risk"); never at the session's or a job's end (nothing is bought there,
        RISK or not). The report is the one the prefetch asked for; none yet: np_reports() judges the coin again when
        it lands (np_fired) and RISK sends it here again. Quiet: no RUG CHECK line, no night count (RISK said why)."""
        cfg, mint = self.cfg, coin.mint
        if not cfg.get("shadow_on_risk", True) or minutes_left <= cfg["no_entries_last_min"] \
                or self.np_job_why() is not None:
            return
        no = self.np_shadow_why_not(mint)
        if no is not None:                              # off, shadowed already, or the cap (said, no RugCheck call)
            if no not in ("off", "already"):
                self.say_once("shadow_said", no, "LEARNER", coin.symbol, "shadow", "SHADOW %s not followed: %s" % (
                    coin.symbol, no))
            return
        # a FAIL opens nothing: coin.rug says it, and judge() rejects the coin on its next reading
        ok = newpairs.rug_check_np(self.np_rug_get(mint), coin, cfg)[0]
        if ok is None:                                  # no report yet (or not one to judge on): when it lands
            self.np_fired.setdefault(mint, self.epoch())
        elif ok:
            plan, labels = self.np_entry_plan(coin)
            self.np_shadow_open(coin, plan, labels, "risk", why)

    def np_shadow_curve(self, mint, ev, t, minutes_left):
        """A curve reading of a SHADOW's coin: into its own copy, then its exits (np_exit), as a held coin's."""
        sc = self.np_shadow_wl.on_curve(mint, ev, t)
        pos = self.shadows.get(mint)
        if sc is None or pos is None:
            return
        pos["moved_t"] = t                              # readings come only when the reserves changed: a trade
        self.np_window(mint, sc, t)
        self.np_exit(mint, sc, t, minutes_left, reading=True, shadow=True)

    def np_shadow_end(self, mint, ev, minutes_left):
        """PumpPortal's migrate, or the curve account gone: a SHADOW is sold into its curve's last state, as a held coin."""
        sc = self.np_shadow_wl.on_migrate(mint, self.epoch())
        if sc is not None:
            self.np_exit(mint, sc, self.np_t(ev), minutes_left, shadow=True)

    def np_shadow_second(self, minutes_left):
        """The 1 s step's part for the SHADOWS (np_second): the time stops and the clock's other exits, as a held coin's.
        One without a reading for np_stale_s keeps its last price: a real coin asks Jupiter's price v3 then, a shadow
        never spends Jupiter's budget on that (its quiet-curve exit still comes)."""
        if self.np_shadow_wl is None:
            return
        now = self.epoch()
        for mint in list(self.shadows):
            pos, sc = self.shadows.get(mint), self.np_shadow_wl.get(mint)
            if pos is None:
                continue
            px = None
            if sc is None or newpairs.stale(sc, self.cfg, now):
                px = pos.get("last") or pos["entry_price"]
            self.np_exit(mint, sc, None, minutes_left, px_usd=px, shadow=True)    # no reading decided these
        self.np_shadow_wl.expire(now)
        for m in [m for m, t in self.np_shadowed.items() if now - t > 3600 and m not in self.shadows]:
            del self.np_shadowed[m]                     # a coin fires within np_max_age_s of its launch at most

    def np_shadow_sale(self, pos, coin, raw, sol_usd):
        """What selling raw of a SHADOW would bring after every cost: the curve maths on the shadow's last reading,
        pump.fun's fee and the impact (curve_sell_sol), the latency haircut and the order's fees (sale_usd), never a
        Jupiter quote (np_shadow_open says why). The curve maths prices the reading that decided the sale, as the
        real curve-maths fallback does; a real sale fills a moment later (the live HOGZ sale 1.9 % under its
        reading). -> (dollars, "curve") or (None, None)."""
        if raw <= 0 or not sol_usd:
            return None, None
        if coin is None or not (f(coin.vsol) > 0 and f(coin.vtok) > 0):
            return None, None
        sol = curve_sell_sol(coin.vsol, coin.vtok, raw / 10.0 ** pos.get("decimals", 6), f(self.cfg.get("curve_fee_pct"), 1.25))
        return (self.sale_usd(sol, sol_usd, pos), "curve") if sol > 0 else (None, None)

    def np_shadow_sell(self, mint, frac, why, sol_usd, take=None, t_reading=None):
        """close_part() for a SHADOW (np_exit's sale when shadow): frac of what is left, priced by np_shadow_sale(), the
        same arithmetic on the shadow's own record (raw, tokens, sold_usd, the first step done); the last sale closes it
        (np_shadow_close). A sale with no price at all waits 5 s, as a real one. -> True when it sold."""
        pos = self.shadows.get(mint)
        if pos is None:
            return False
        raw = pos["raw"] if frac >= 0.999 else int(pos["raw"] * frac)
        got, fill = self.np_shadow_sale(pos, self.np_shadow_wl.get(mint), raw, sol_usd)
        if got is None:
            pos["retry_at"] = self.epoch() + 5
            return False
        pos["sold_usd"] += got
        if pos.get("fill") != fill:
            pos["fill"] = "mixed"                       # Jupiter's quotes and the curve maths both priced it
        final = frac >= 0.999
        doc = {"frac": round(frac, 4), "raw": raw, "usd": got, "why": why, "fill": fill, "final": final, "px": pos.get("last")}
        if final:
            pnl = pos["sold_usd"] - pos["cost"]
            doc.update(pnl=round(pnl, 4), pnl_pct=round(pnl / pos["cost"] * 100, 2) if pos["cost"] > 0 else None)
        self.np_rec_shadow(mint, "sell", doc)
        if final:
            self.np_shadow_close(mint, why, take)
            return True
        pos["raw"] -= raw
        pos["tokens_ui"] = pos["raw"] / 10.0 ** pos["decimals"]
        pos["tp1_done"] = True
        return True

    def np_shadow_close(self, mint, why, take=None):
        """A SHADOW sold to the last token: its P&L % after every cost (as a trade's pnl_pct) teaches the LEARNER with
        shadow_weight, goes into the shadow counts and the panel's last closed, and the log says what it would have
        made. -> the closed row."""
        pos = self.shadows.pop(mint, None)
        if pos is None:
            return None
        self.np_shadow_wl.drop(mint)
        cost = f(pos.get("cost"))
        pnl = f(pos.get("sold_usd")) - cost
        pnl_pct = round(pnl / cost * 100, 1) if cost > 0 else 0.0
        row = self.np_shadow_row(mint, pos)
        last = int(f(take.get("steps"), 1)) if isinstance(take, dict) and take.get("kind") == "take" else 0
        text, w = self.np_shadow_learn(pos, pnl_pct)
        row.update(status="closed", closed=self.ts(), exit=str(why)[:120], pnl=round(pnl, 2), pnl_pct=pnl_pct,
                   steps=row["steps"] + last, weight=w)
        book = self.state["shadow"]
        night = self.state.get("night")
        tonight = shadow_stat(night.get("shadow")) if isinstance(night, dict) else None   # this night's own counts
        for st in (book, book["by"]["risk" if pos.get("shadow") == "risk" else "learner"], tonight):
            if st is None:
                continue
            st["n"] += 1
            st["wins"] += 1 if pnl > 0 else 0
            st["sum"] = round(st["sum"] + pnl_pct, 4)
        if tonight is not None:
            night["shadow"] = tonight                   # the morning report's figure (report_text): this night only
        book["closed"] = (book["closed"] + [row])[-SHADOW_ROWS:]
        self.log("LEARNER", pos["symbol"], "shadow", "SHADOW %s would have made %+.1f%% (%s) · %s" % (
            pos["symbol"], pnl_pct, why, text))
        self.np_shadow_release(mint)
        return row

    def np_shadow_learn(self, pos, pnl_pct):
        """The LEARNER learns a closed shadow: learner.update(labels, P&L %, weight=shadow_weight, shadow=True). -> (the
        log's words, the weight it learned with: 0 when it did not)."""
        w = min(1.0, max(0.0, f(self.cfg.get("shadow_weight"), 0.5)))
        if self.mem is None:
            return "not learned (LEARNER off)", 0.0
        if not pos.get("learn"):
            return "not learned (no labels)", 0.0
        if w <= 0:
            return "not learned (shadow_weight 0)", 0.0
        if not getattr(learner, "WEIGHTS", False):      # a learner.py without weights would count it as a whole trade
            return "not learned (learner.py takes no weights)", 0.0
        self.learn("update", self.mem, pos["learn"], pnl_pct, w, True)
        if self.mem is None:                            # update() failed: the LEARNER is out (its line says why)
            return "not learned (LEARNER off)", 0.0
        self.learn_save()
        said = self.learn("learned", self.mem, pos["learn"])
        return "learned x%g%s" % (w, " · " + said if said else ""), w

    def np_shadow_drop(self, mint, why):
        """A SHADOW ends without a result (why: "bought for real": from now on the real trade teaches the LEARNER): out
        of the shadow book, not learned, said in the log."""
        pos = self.shadows.pop(mint, None)
        if pos is None:
            return
        self.np_shadow_wl.drop(mint)
        self.log("LEARNER", pos["symbol"], "shadow", "SHADOW %s dropped: %s · not learned" % (pos["symbol"], why))
        self.np_rec_shadow(mint, "drop", {"why": why})
        self.np_shadow_release(mint)

    def np_shadow_release(self, mint):
        """A shadow that closed or dropped lets its curve go, unless the watch list still has the coin (its expiry lets
        it go) or the recorder follows it (its tick lets it go). Its NEWS window goes."""
        for d in (self.np_win, self.news_at):
            d.pop(mint, None)
        if self.wl is not None and self.wl.get(mint) is not None:
            return
        if self.rec is not None and mint in (getattr(self.rec, "follow", None) or {}):
            return
        self.np_call("unwatch", mint)

    def np_shadow_row(self, mint, pos):
        """A shadow as the panel lists it; an open one with its mark now (the price, the sale's costs not taken)."""
        plan = pos.get("plan") if isinstance(pos.get("plan"), dict) else {}
        cost = f(pos.get("cost"))
        mark = f(pos.get("tokens_ui")) * f(pos.get("last")) + f(pos.get("sold_usd"))
        return {"symbol": pos.get("symbol"), "mint": mint, "status": "open", "kind": pos.get("shadow"),
                "why": pos.get("shadow_why"), "opened": pos.get("opened"), "cost": round(cost, 2),
                "pnl_pct": round((mark / cost - 1) * 100, 1) if cost > 0 else None,
                "tier": plan.get("entry_tier") or plan.get("tier"), "score": plan.get("entry_score"),
                "steps": int(f(plan.get("sold"))), "fill": pos.get("fill"), "age_s": pos.get("age_s"),
                "sol_in": pos.get("sol_in")}

    def np_shadow_rows(self):
        """The open SHADOWS for the panel, the newest first."""
        return [self.np_shadow_row(m, p) for m, p in sorted(self.shadows.items(), key=lambda kv: str(kv[1].get("opened")),
                                                            reverse=True)]

    def np_rec_shadow(self, mint, act, doc):
        """A SHADOW's line on the tape ({"k": "shadow", "act": "open" | "sell" | "drop", ...}) when the recorder has
        the hook (an older recorder.py: no line, nothing else changes). Never a "buy" or "sell" line: a replay's live
        check compares those with the real trades."""
        if self.rec is not None and callable(getattr(self.rec, "shadow", None)):
            self.rec_call("shadow", mint, act, doc)

    def sol_read(self):
        """SOL in dollars from the market (Jupiter, then DexScreener: up to 4 s each when they hang), or 0."""
        try:
            getter = getattr(self.m, "sol_price", None)
            return f(getter()) if getter is not None else f(((self.m.prices([]) or {}).get(SOL) or {}).get("usd"))
        except Exception as e:                          # noqa: BLE001
            self.m.errors.append("sol price: %s: %s" % (type(e).__name__, e))
            return 0.0

    def sol_usd(self, prices=None, fetch=False):
        """SOL in dollars: from prices when they have it, else the last one. fetch (the 1 s step): read again
        every sol_price_sec on a worker thread, the last one standing meanwhile (New Pairs only turn curve
        prices into dollars with it: a half-minute-old one is fine, and a hanging Jupiter or DexScreener
        must never hold the exits). Read here only while there is none yet."""
        now = self.epoch()
        px = f(((prices or {}).get(SOL) or {}).get("usd"))
        if px > 0:
            self.sol_px = (now, px)
            return px
        job = self.sol_job
        if job is not None and job.done():
            self.sol_job = None
            try:
                px = f(job.result(timeout=0))
            except Exception:                           # noqa: BLE001 - sol_read() never raises; a cancel does
                px = 0.0
            if px > 0:
                self.sol_px = (now, px)
        if self.sol_px is not None:
            if fetch and now - self.sol_px[0] >= f(self.cfg.get("sol_price_sec"), 30) and self.sol_job is None:
                try:
                    if self.sol_pool is None:
                        self.sol_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="solprice")
                    self.sol_job = self.sol_pool.submit(self.sol_read)
                except RuntimeError:                    # the pool was shut down (the run is ending)
                    pass
            return self.sol_px[1]
        px = self.sol_read()                            # none yet: nothing can be priced without it
        if px > 0:
            self.sol_px = (now, px)
            return px
        return None

    def close_night(self):
        st = self.state
        n = st.get("night")
        if not n:
            return
        trades = n.get("trades") or []
        wins = [t for t in trades if t["pnl"] > 0]
        end_eq = round(self.equity({}), 2)
        st["nights"].append({"id": n["id"], "start": n["start_equity"], "end": end_eq,
                             "pnl": round(end_eq - n["start_equity"], 2), "trades": len(trades), "wins": len(wins),
                             "best": max((t["pnl"] for t in trades), default=0),
                             "worst": min((t["pnl"] for t in trades), default=0),
                             "flagged": n.get("flagged", 0), "scans": n.get("scans", 0)})
        del st["nights"][:-60]
        st["last_night"] = n
        st["night"] = None
        st["holders"] = {}
        now = self.epoch()                              # rug() uses no report over 10 minutes old: state.json needs none
        st["rug_cache"] = {k: c for k, c in (st.get("rug_cache") or {}).items() if now - f(c.get("at")) < 600}

    def panel_doc(self, prices=None):
        if self.wl is not None:
            self.np_speed_view(self.epoch())            # a trade's own panel has its latency
        return panel(self.state, self.cfg, prices or {}, self.ts(), self.m.errors[-5:], speed=self.speed,
                     live_stats=self.live.stats() if self.live is not None else None, watch=self.np_rows(),
                     shadows=self.np_shadow_safe("np_shadow_rows"))

    def save(self, prices=None, doc=None):
        self.np_sync()
        # the open SHADOWS by name only: a restart drops them and says which (begin())
        self.state["shadow"]["open"] = [str(p.get("symbol") or "?")[:24] for p in self.shadows.values()]
        doc = doc or self.panel_doc(prices)
        write_json(os.path.join(self.dir, "state.json"), self.state)
        write_json(os.path.join(self.dir, "panel.json"), doc)
        return doc

    def publish(self, prices, live=None, seq=0, memo=None, force=False):
        """The files every save_every_sec and right after a trade (not every second: the disk and
        git need no more), the retained live panel every live_panel_sec and after a trade, and the
        live tick every call."""
        now, cfg = self.epoch(), self.cfg
        save = force or self.traded or now - self.saved_at >= cfg.get("save_every_sec", 5)
        show = live is not None and (force or self.traded or now - self.shown_at >= cfg.get("live_panel_sec", 15))
        self.traded = False
        if save or show:
            doc = self.panel_doc(prices)
            if save:
                self.save(prices, doc)
                self.saved_at = now
            if show:
                live.panel(doc)
                self.shown_at = now
                if memo is not None:                    # the ticks leave out what this panel already says
                    memo["panel"] = steady_view(doc)
        if live is not None and memo is not None:
            if self.wl is not None and not (save or show):
                self.np_speed_view(now)
            live.tick(tick_msg(self.state, cfg, prices, self.ts(), seq, memo, self.speed, watch=self.np_rows()))


def trim_report(rep):
    """The parts of a RugCheck report rug_check() reads (the cache keeps only these)."""
    if not isinstance(rep, dict):
        return None
    keep = {k: rep.get(k) for k in ("mint", "rugged", "mintAuthority", "freezeAuthority", "token",
                                    "creatorBalance", "markets", "knownAccounts", "topHolders",
                                    "graphInsidersDetected", "risks", "totalHolders")}
    markets = []
    for mk in (keep.get("markets") or [])[:5]:
        mk = mk or {}
        row = {k: mk.get(k) for k in ("pubkey", "liquidityA", "liquidityB")}
        for k in ("liquidityAAccount", "liquidityBAccount"):
            acc = mk.get(k)
            row[k] = {"owner": acc.get("owner")} if isinstance(acc, dict) else None
        row["lp"] = {"lpLockedPct": (mk.get("lp") or {}).get("lpLockedPct")}
        markets.append(row)
    keep["markets"] = markets
    keep["topHolders"] = [{k: h.get(k) for k in ("address", "owner", "pct", "insider")}
                          for h in keep.get("topHolders") or []][:20]
    return keep


NIGHT_KEYS = ("id", "start_equity", "started", "scans", "flagged", "checked", "filtered", "lines", "last_rug")
FEEDS = ["geckoterminal", "dexscreener", "rugcheck", "jupiter"]


def positions_view(state, prices, hist=True):
    """(the open positions as the page shows them, the equity with the balance)."""
    eq = state["balance"]
    pos = []
    for mint, p in state["positions"].items():
        px = (prices.get(mint) or {}).get("usd") or p.get("last") or p["entry_price"]
        val = p["tokens_ui"] * px
        eq += val
        row = {"symbol": p["symbol"], "mint": mint, "cost": round(p["cost"], 2),
               "value": round(val + p.get("sold_usd", 0), 2),
               "pnl_pct": round(((val + p.get("sold_usd", 0)) / p["cost"] - 1) * 100, 1), "opened": p["opened"],
               "url": p.get("url"), "reason": p.get("reason", ""), "size_pct": p.get("size_pct"),
               "entry_price": sig(p["entry_price"]), "price": sig(px), "slip": p.get("impact_pct"),
               "tp1_done": p.get("tp1_done")}
        row.update(plan_view(p))
        if hist:
            row["hist"] = [sig(x) for x in (p.get("hist") or [])[-120:]]
        pos.append(row)
    return pos, eq


def sig(x, digits=6):
    """A price with 6 significant digits: 4.31474e-06, not 4.314739436872112e-06 (every tick carries them)."""
    v = f(x)
    return float("%.*g" % (digits, v)) if v else v


def plan_view(p):
    """A position's strategy and NEWS hold plan as the page shows them (small: every tick has them):
    {"strategy", "plan" {tier, score, parts, ladder, step, stop_pct, trail_pct}, "tier", "score",
    "steps", and for a New Pair what it showed at the entry: "age_s", "mcap_sol", "dev_pct", "sol_in"}."""
    plan = p.get("plan") if isinstance(p.get("plan"), dict) else None
    out = {"strategy": p.get("strategy") or "migrated",
           "plan": holdplan.panel_plan(plan) if plan is not None and holdplan is not None else None,
           "tier": None, "score": None, "steps": None}
    if out["plan"] is not None:
        out.update(tier=out["plan"]["tier"], score=out["plan"]["score"], steps=int(f(plan.get("sold"))))
    for k in ("age_s", "mcap_sol", "dev_pct", "sol_in"):
        out[k] = p.get(k)
    return out


def tick_rows(rows, n=20):
    """The watch list in a tick (the page replaces its list with it every second): the panel's rows with
    the name cut to 24 characters, the links as just true or false (the retained panel has them), no
    big_sell, the texts cut: 20 rows with real 44-character mints are about 5 KB."""
    out = []
    for r in (rows or [])[:max(0, int(f(n, 20)))]:
        c = {k: r.get(k) for k in ("symbol", "mint", "age_s", "sol_in", "buys", "sells", "rise_pct", "mcap_sol", "dev_pct")}
        c["name"] = str(r.get("name") or "")[:24]
        for k in ("x", "tg", "web"):
            c[k] = bool(r.get(k))
        c["status"] = str(r.get("status") or "")[:48]
        if c["status"] == "watching" and r.get("why"):
            c["why"] = str(r["why"])[:48]
        out.append(c)
    return out


def trades_view(trades):
    """The last 50 trades as the page shows them: without the LEARNER's labels, which state.json
    keeps for a rebuilt memory but the page has no use for (and a tick must stay small)."""
    return [{k: v for k, v in t.items() if k != "learn"} for t in (trades or [])[-50:]]


NP_SPEED = ("first_in_ms", "first_in_last_ms", "exit_ms", "exit_last_ms", "launch_to_entry_s", "creates_per_min",
            "watching", "followed")


def speed_view(cfg, speed=None):
    """panel["speed"]. With New Pairs: "feeds" is each feed's status ({name, connected, events,
    last_event_age_s, errors, last_error}; one "pumpportal" entry saying why when there are no feeds),
    "feed" the feeds' short text, first_in_ms (trigger -> fill, the night's median) and
    first_in_last_ms, exit_ms (reading -> fill) and exit_last_ms, launch_to_entry_s, creates_per_min,
    watching (candidates now) and followed (curves read). Without it, "feeds" names the REST sources."""
    s = speed or {}
    w = watch_sec(cfg)
    out = {"watch_sec": w, "tick_sec": w, "scan_sec": cfg["scan_every_sec"], "gt_sec": cfg.get("gt_every_sec"),
           "feed": "%s · scan %gs · live %gs" % (s.get("source") or "jupiter", cfg["scan_every_sec"], w),
           "last_scan_ms": s.get("last_scan_ms"), "watch_ms": s.get("watch_ms"),
           "scans_per_min": s.get("scans_per_min", 0), "feeds": list(FEEDS), "strategies": strategies(cfg)}
    if "feeds_status" in s:                             # New Pairs
        out["feeds"] = list(s.get("feeds_status") or [])
        out["feed"] = "%s · live %gs" % (s.get("feeds_text") or "feeds", w)
        if "migrated" in strategies(cfg):
            out["feed"] += " · migrated: %s · scan %gs" % (s.get("source") or "jupiter", cfg["scan_every_sec"])
        for k in NP_SPEED:
            out[k] = s.get(k)
    return out


def stream_view(cfg, on=None):
    """Where the page subscribes: the topic and the brokers' WebSocket addresses, in order. on: whether
    this bot runs a live channel (None: what the config and MEMEBOT_LIVE say)."""
    return {"topic": cfg.get("live_topic"), "enabled": live_enabled(cfg) if on is None else bool(on),
            "ws": [b["ws"] for b in cfg.get("live_brokers") or [] if b.get("ws")]}


def panel(state, cfg, prices, at, errors=(), speed=None, live_stats=None, watch=None, shadows=None):
    """What the page shows: the prompt, the balance, the night's log, open positions, trades, and
    "watch": the New Pairs being watched now (newpairs.row(): symbol, name, mint, age_s, sol_in, buys,
    sells, rise_pct, mcap_sol, dev_pct, big_sell, x, tg, web, status, why), newest first, 20 at most.
    "shadows": the SHADOW trades (coins not bought, followed for the LEARNER: Night.np_shadow_row(), the open ones
    from `shadows` then the last 20 closed) and "shadow_stats" (shadow_view()): never part of the balance or trades."""
    n = state.get("night") or state.get("last_night") or {}
    pos, eq = positions_view(state, prices)
    shadow_rows, shadow_stats = shadow_view(state.get("shadow"), shadows)
    try:
        with open(PROMPT, encoding="utf-8") as fh:
            prompt = fh.read()
    except OSError:
        prompt = ""
    return {"updated": at, "mode": "paper", "start_balance": cfg["start_balance"], "balance": round(state["balance"], 2),
            "equity": round(eq, 2), "session": "24/7" if always_on(cfg) else "%s → %s" % (cfg["session_start"], cfg["session_end"]),
            "agents": AGENTS, "prompt": prompt, "night": {k: n.get(k) for k in NIGHT_KEYS},
            "flags": (n.get("flags") or [])[-8:], "curve": (n.get("curve") or [])[-600:],
            "position_pct": cfg["position_pct"], "slip_cap": cfg["max_roundtrip_loss_pct"],
            "live": bool(state.get("night")), "positions": pos, "trades": trades_view(n.get("trades")),
            "log": (n.get("decisions") or [])[-200:], "nights": state.get("nights", [])[-30:], "errors": list(errors),
            "stream": stream_view(cfg, None if live_stats is None else True), "speed": speed_view(cfg, speed),
            "learner": state.get("learner_summary") or None, "live_stats": dict(live_stats or {}),
            "watch": list(watch or [])[:20], "strategies": strategies(cfg),
            "shadows": shadow_rows, "shadow_stats": shadow_stats}


SPEED_COUNTERS = ("watch_ms", "last_scan_ms", "scans_per_min", "creates_per_min", "watching", "followed")


def steady_view(doc):
    """The parts of a panel or a tick that change seldom: the flags, the LEARNER's summary and the
    speed without its counters (the timings, creates a minute, coins watched, each feed's events and
    the age of its last one, the feeds' text with its count: they move every second and come with
    each panel, every 15 s). A feed going down or up, and the latencies after a trade, still differ."""
    sp = {k: v for k, v in (doc.get("speed") or {}).items() if k not in SPEED_COUNTERS}
    if isinstance(sp.get("feed"), str):
        sp["feed"] = re.sub(r"\d+ watched", "N watched", sp["feed"])
    if isinstance(sp.get("feeds"), list):
        sp["feeds"] = [{k: x.get(k) for k in ("name", "connected", "last_error")} if isinstance(x, dict) else x
                       for x in sp["feeds"]]
    return {"night": (doc.get("night") or {}).get("id"), "flags": doc.get("flags"), "learner": doc.get("learner"),
            "speed": sp}


def tick_msg(state, cfg, prices, at, seq, memo, speed=None, watch=None):
    """The 1 s live message: what moves every second (equity, positions) and what is new since
    the last tick (log rows by their n, at most 40; the trades only when their number changed).
    The flags, the LEARNER's summary and the speed come only while they differ from the last
    retained panel (memo["panel"], from publish()): a viewer has that panel the moment they
    connect, so a tick needs no copy of it, and a phone on mobile data gets about 1 KB a second
    less with a full book. memo remembers what went out and starts over with a new night."""
    n = state.get("night") or state.get("last_night") or {}
    if memo.get("night") != n.get("id"):
        base = memo.get("panel")
        memo.clear()
        memo["night"] = n.get("id")
        if base and base.get("night") == n.get("id"):   # the panel of the new night, sent just before
            memo["panel"] = base
    last = memo.get("n", 0)
    rows = []
    for r in reversed(n.get("decisions") or []):
        if (r.get("n") or 0) <= last:
            break
        rows.append(r)
    rows = rows[::-1][-40:]
    if rows:
        memo["n"] = rows[-1]["n"]
    pos, eq = positions_view(state, prices, hist=False)
    trades, curve = n.get("trades") or [], n.get("curve") or []
    doc = {"v": 1, "kind": "tick", "seq": seq, "updated": at, "equity": round(eq, 2),
           "balance": round(state["balance"], 2), "live": bool(state.get("night")),
           "night": {k: n.get(k) for k in NIGHT_KEYS}, "positions": pos, "log": rows, "trades_n": len(trades),
           "flags": (n.get("flags") or [])[-8:], "curve_last": curve[-1] if curve else None,
           "speed": speed_view(cfg, speed), "learner": state.get("learner_summary") or None,
           "watch": tick_rows(watch, min(20, f(cfg.get("np_tick_rows"), 20)))}
    if memo.get("trades_n") != len(trades):
        memo["trades_n"] = len(trades)
        doc["trades"] = trades_view(trades)
    base = memo.get("panel")
    if base is not None:
        cur = steady_view(doc)
        for k in ("flags", "learner", "speed"):
            if cur[k] == base[k]:
                del doc[k]
    return doc


# ---------------------------------------------------------------- the scanner thread

class Scanner(object):
    """SCANNER's fetching, on a thread of its own so a slow API never holds up the 1 s watch:
    fresh pools, promoted and trending coins, their pairs and the RugCheck reports, 8 calls at a
    time, GeckoTerminal at most once in gt_every_sec (its lists are reused in between). It only
    fetches: the result goes back through a queue and the main thread applies the rules and
    trades, so the book has one writer. One scan at a time: the next starts after the last one
    was handed over."""

    def __init__(self, market, cfg, threaded=True):
        self.m, self.cfg, self.threaded = market, cfg, threaded
        self.jobs, self.done = queue.Queue(), queue.Queue()
        self.busy = False
        self.first = True
        self.first_wait = 3.0                           # seconds the run's first scan is waited for
        self.started = float("-inf")
        self.gt_at = float("-inf")
        self.pools, self.trend = [], set()
        self.thread = None

    def due(self, now):
        return not self.busy and now - self.started >= self.cfg["scan_every_sec"]

    def start(self, job, wait=False):
        self.busy, self.first, self.started = True, False, job["now"]
        if wait or not self.threaded:
            self.done.put(self.safe_fetch(job))
            return
        if self.thread is None:
            self.thread = threading.Thread(target=self.work, name="scanner", daemon=True)
            self.thread.start()
        self.jobs.put(job)

    def poll(self, wait=0):
        """The finished scan or None, after waiting at most `wait` seconds for it."""
        try:
            found = self.done.get(timeout=wait) if wait > 0 else self.done.get_nowait()
        except queue.Empty:
            return None
        self.busy = False
        return found

    def stop(self):
        if self.thread is not None:
            self.jobs.put(None)

    def work(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            self.done.put(self.safe_fetch(job))

    def safe_fetch(self, job):
        try:
            return self.fetch(job)
        except Exception as e:                          # noqa: BLE001 - a broken scan is an empty one
            return {"now": job["now"], "error": "scan: %s: %s" % (type(e).__name__, e)}

    def fetch(self, job):
        t, now, cfg, m = time.monotonic(), job["now"], self.cfg, self.m
        gt = now - self.gt_at >= cfg.get("gt_every_sec", 20)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            promo = ex.submit(m.promoted)
            if gt:
                self.gt_at = now
                pools, trend = ex.submit(m.new_pools, 2, now), ex.submit(m.trending, now)
                pools, trend = pools.result(), set(trend.result())
                self.pools = pools or self.pools        # a failed round keeps the last lists
                self.trend = trend or self.trend
            mints = [x for x in dict.fromkeys(list(self.pools) + list(promo.result()) + list(self.trend))
                     if x not in job["skip"]][:150]     # 5 DexScreener calls at most
            pairs = {}
            for d in ex.map(m.pairs, [mints[i:i + 30] for i in range(0, len(mints), 30)]):
                pairs.update(d or {})
            want = []
            for x, p in pairs.items():
                try:
                    fx = pair_facts(p, now * 1000)
                except Exception:                       # noqa: BLE001 - consider() logs the odd pair
                    continue
                if x not in job["rug_fresh"] and not universe_ok(fx, cfg):
                    want.append((-fx["vol5"], x))
            want = [x for _, x in sorted(want)[:8]]     # the busiest first, like the buys
            reports = dict(zip(want, ex.map(m.rug_report, want)))
        return {"now": now, "mints": mints, "pairs": pairs, "trend": frozenset(self.trend), "reports": reports,
                "gt": gt, "ms": int((time.monotonic() - t) * 1000)}


# ---------------------------------------------------------------- the live channel (MQTT 3.1.1)

def mqtt_length(n):
    """MQTT's remaining length: 7 bits a byte, the low ones first, the top bit set while more follow."""
    out = bytearray()
    while True:
        n, digit = n >> 7, n & 0x7F
        out.append(digit | (0x80 if n else 0))
        if not n:
            return bytes(out)


def mqtt_string(s):
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def mqtt_packet(first, body=b""):
    return bytes([first]) + mqtt_length(len(body)) + body


def mqtt_connect(client_id, keepalive=60):
    """CONNECT: protocol "MQTT" level 4 (3.1.1), clean session, no will, no login."""
    return mqtt_packet(0x10, mqtt_string("MQTT") + b"\x04\x02" + struct.pack("!H", keepalive) + mqtt_string(client_id))


def mqtt_publish(topic, payload, retain=False):
    """PUBLISH at QoS 0: no packet id, nothing comes back."""
    return mqtt_packet(0x31 if retain else 0x30, mqtt_string(topic) + payload)


MQTT_PINGREQ = b"\xc0\x00"
MQTT_DISCONNECT = b"\xe0\x00"


class Broker(object):
    """One MQTT broker on a daemon thread of its own. publish() only puts the message in a
    latest-only slot for its topic and wakes the thread, so the trading loop never waits on the
    network: a slow or dead broker loses old ticks, never time. The thread connects (TLS unless
    tls is false), sends, reads and drops whatever the broker sends, pings every ping_after
    seconds (a broker that stops answering counts as a failure) and after a failure reconnects in
    1 s, 2 s, 4 s ... up to 30 s."""

    def __init__(self, host, port=8883, tls=True, client_id=None, keepalive=60, ping_after=30, timeout=10):
        self.host, self.port, self.tls = host, int(port), bool(tls)
        self.client_id = client_id or "memebot-" + os.urandom(6).hex()
        self.keepalive, self.ping_after, self.timeout = keepalive, ping_after, timeout
        self.slots = {}
        self.retained = {}                              # topic -> the last retained payload, sent again on each connect
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.halt = threading.Event()
        self.sock = None
        self.stats = {"connected": False, "sent": 0, "errors": 0, "last_error": None}
        self.thread = threading.Thread(target=self.loop, name="mqtt %s" % host, daemon=True)
        self.thread.start()

    def publish(self, topic, payload, retain=False):
        with self.lock:
            self.slots[topic] = (payload, retain)
            if retain:
                self.retained[topic] = payload
        self.wake.set()

    def stop(self):
        """Sends what is waiting, says goodbye and ends the thread."""
        self.halt.set()
        self.wake.set()

    def loop(self):
        delay = 1.0
        while not self.halt.is_set():
            up = None
            try:
                self.connect()
                up = time.monotonic()
                self.serve()
            except Exception as e:                      # noqa: BLE001 - a broker's trouble stays on its thread
                self.stats["errors"] += 1
                self.stats["last_error"] = ("%s: %s" % (type(e).__name__, e))[:160]
            self.close()
            if up is not None and time.monotonic() - up >= 30:
                delay = 1.0                             # it was up a while: the backoff starts over
            if self.halt.wait(delay):
                break
            delay = min(30.0, delay * 2)

    def connect(self):
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host) if self.tls else raw
        except Exception:
            raw.close()
            raise
        self.sock = sock
        sock.settimeout(self.timeout)
        sock.sendall(mqtt_connect(self.client_id, self.keepalive))
        ack = b""
        while len(ack) < 4:                             # CONNACK: 0x20, 2, flags, return code
            chunk = sock.recv(4 - len(ack))
            if not chunk:
                raise MemeError("closed before CONNACK")
            ack += chunk
        if ack[0] != 0x20 or ack[1] != 2:
            raise MemeError("no CONNACK but %s" % ack.hex())
        if ack[3] != 0:
            raise MemeError("CONNACK refused, code %d" % ack[3])
        self.stats["connected"] = True
        with self.lock:                                 # a restarted broker has lost the panel: it goes again now
            for topic, payload in self.retained.items():
                self.slots.setdefault(topic, (payload, True))

    def serve(self):
        """Sends what waits, reads what came and pings every ping_after seconds, ticks or not: a QoS 0
        publish gets no answer, so a PINGRESP is the only sign the broker still reads. A hung broker
        or a half-open link takes every tick into the kernel's buffers for minutes and the stats
        would say connected; no answer in ping_after seconds and the link is dropped and made again."""
        sock = self.sock
        ping_at, asked = time.monotonic(), None         # the last PINGREQ, and the oldest one not answered
        while True:
            self.wake.wait(1.0)
            self.wake.clear()
            with self.lock:
                todo, self.slots = self.slots, {}
            try:
                for topic in sorted(todo, key=lambda t: not todo[t][1]):   # the retained panel first
                    payload, retain = todo[topic]
                    sock.sendall(mqtt_publish(topic, payload, retain))
                    del todo[topic]
                    self.stats["sent"] += 1
            finally:
                if todo:                                # not sent: back in its slot unless a newer one came
                    with self.lock:
                        for t, v in todo.items():
                            self.slots.setdefault(t, v)
            if self.halt.is_set():
                sock.sendall(MQTT_DISCONNECT)
                return
            if self.drain():
                asked = None
            now = time.monotonic()
            if asked is not None and now - asked > self.ping_after:
                raise MemeError("no PINGRESP in %g s: the broker stopped answering" % self.ping_after)
            if now - ping_at >= self.ping_after:
                sock.sendall(MQTT_PINGREQ)
                ping_at = now
                if asked is None:
                    asked = now

    def drain(self):
        """Reads and drops what the broker sent (PINGRESP and the like) without waiting for more."""
        got = False
        self.sock.setblocking(False)
        try:
            while True:
                try:
                    data = self.sock.recv(65536)
                except (BlockingIOError, InterruptedError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    return got
                if not data:
                    raise MemeError("the broker closed the connection")
                got = True
        finally:
            self.sock.settimeout(self.timeout)

    def close(self):
        self.stats["connected"] = False
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


class Live(object):
    """The live channel to every broker in cfg["live_brokers"]: <topic>/panel, the whole panel,
    retained so a viewer gets it the moment they connect, and <topic>/tick every second. Nothing
    here waits on the network or raises into the loop."""

    def __init__(self, cfg):
        self.topic = cfg["live_topic"]
        self.links = {}
        brokers = cfg.get("live_brokers") or []
        hosts = [b["host"] for b in brokers]
        for b in brokers:
            name = b["host"] if hosts.count(b["host"]) == 1 else "%s:%s" % (b["host"], b.get("port", 8883))
            self.links[name] = Broker(b["host"], b.get("port", 8883), b.get("tls", True))
        self.dropped = 0

    def send(self, sub, doc, retain=False):
        try:
            payload = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):                 # a NaN would be invalid JSON in the browser
            self.dropped += 1
            return False
        for link in self.links.values():
            link.publish(self.topic + "/" + sub, payload, retain)
        return True

    def panel(self, doc):
        return self.send("panel", doc, True)

    def tick(self, doc):
        return self.send("tick", doc, False)

    def stats(self):
        return {k: dict(v.stats) for k, v in self.links.items()}

    def stop(self, wait=3.0):
        for link in self.links.values():
            link.stop()
        end = time.monotonic() + wait
        for link in self.links.values():
            link.thread.join(max(0.0, end - time.monotonic()))


def live_for(cfg):
    return Live(cfg) if live_enabled(cfg) else None


def page_html(panel_doc, template=PAGE):
    """page.html with the panel's data in it (the JSON inside a <script>, every < escaped)."""
    with open(template, encoding="utf-8") as fh:
        html = fh.read()
    data = json.dumps(panel_doc, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return html.replace("/*DATA*/", data, 1)


def report_text(state):
    n = state.get("night") or state.get("last_night")
    if not n:
        return "Zatiaľ žiadna noc."
    trades = n.get("trades") or []
    pnl = sum(t["pnl"] for t in trades)
    lines = ["# Meme Bot · noc %s (paper)" % n["id"], "",
             "Začiatok %s, obchody %d, z toho v zisku %d, spolu %s." % (
                 usd(n["start_equity"]), len(trades), sum(1 for t in trades if t["pnl"] > 0),
                 ("+" if pnl >= 0 else "") + usd(pnl)), ""]
    for t in trades:
        lines.append("- %s: %s → %s (%+.0f %%), %s" % (t["symbol"], usd(t["cost"]), usd(t["proceeds"]), t["pnl_pct"], t["why"]))
    lines += ["", "Skenov %d, označených %d." % (n.get("scans", 0), n.get("flagged", 0))]
    # the SHADOW trades of THIS night (closed in it: night["shadow"]), and apart from them, said as such, every one
    # since state.json began ("shadow": the page's all-time stats); a night without shadows says so only when
    # earlier nights had some
    tonight, ever = shadow_stat(n.get("shadow")), shadow_book(state.get("shadow"))
    if tonight["n"] or ever["n"]:
        what = ("Tieňové obchody tejto noci (mince, ktoré bot nekúpil, sledované ako keby ich kúpil, len pre LEARNER; "
                "nie sú v zostatku ani v obchodoch): ")
        if tonight["n"]:
            what += "spolu %d, v zisku %d, priemer %+.1f %% po nákladoch." % (
                tonight["n"], tonight["wins"], tonight["sum"] / tonight["n"])
        else:
            what += "žiadne."
        if ever["n"] and ever["n"] != tonight["n"]:     # (a repaired state.json may have lost the all-time count)
            what += " Od začiatku (všetky noci): %d, v zisku %d, priemer %+.1f %%." % (
                ever["n"], ever["wins"], ever["sum"] / ever["n"])
        lines.append(what)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- command line

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def sell_all(bot, market, why, now_fn, sleep, tries=13):
    """Every open position sold at the end of a test run -> the last prices. A sale that fails
    (Jupiter resting after a failure, no route for a moment) is tried again every 5 s for a
    minute: a coin left open keeps the test night open, and the next real night would take it
    over as one of its own trades."""
    prices = {}
    for k in range(tries):
        if k:
            sleep(5)
        bot.now = now_fn()
        prices = getattr(market, "live_prices", market.prices)(list(bot.state["positions"]))
        sol_usd = (prices.get(SOL) or {}).get("usd")
        for mint in list(bot.state["positions"]):
            if sol_usd:
                bot.close_part(mint, 1.0, why, sol_usd)
        if not bot.state["positions"]:
            break
    return prices


def run(data_dir, cfg, minutes=None, anytime=False, market=None, now_fn=utc_now, sleep=time.sleep, out=print,
        live=None, scanner=None, feeds=None):
    """Runs until the session ends (or `minutes` pass). Every watch_sec: prices, exits and time stops, the
    scanner's results (migrated), a live tick; the files every save_every_sec and after a trade. In
    between, with New Pairs, the loop waits on the feeds' queue at most feed_wait_sec (0.25 s) at a time
    instead of sleeping, so a curve reading of a held coin goes to its exits, and a watched coin's
    trigger to its buy, in the same loop turn. One Night lives the whole run (state.json is read once).
    `live`: the live channel (default: the brokers in cfg unless MEMEBOT_LIVE=0). `feeds`: the New
    Pairs feeds (default: memebot/feeds.py's, unless MEMEBOT_FEEDS=0); without them the night runs on
    and says why. -> the night's id or None."""
    market = market or Market(cfg=cfg)
    migrated = "migrated" in strategies(cfg)
    scanner = scanner or (Scanner(market, cfg) if migrated else None)
    why_off = None
    if feeds is None and np_on(cfg):
        feeds, why_off = feeds_for(cfg)
    elif feeds is None:
        why_off = np_off_why(cfg)
    step = watch_sec(cfg)
    wait_most = max(0.01, f(cfg.get("feed_wait_sec"), 0.25))
    t0 = now_fn()
    night_id = bot = None
    prices, seq, memo, said = {}, 0, {}, {}
    next_at, started = None, False
    try:
        while True:
            now = now_fn()
            elapsed = (now - t0).total_seconds() / 60.0
            if minutes is not None and elapsed >= minutes:
                break
            s = session_of(now, cfg)
            if s is None and not anytime:
                if night_id is None:
                    out("Mimo noci (%s až %s, %s): nič na robenie." % (cfg["session_start"], cfg["session_end"], cfg["timezone"]))
                break
            if s is None:                # a test run: its own night, closed at its end
                s = (local(now, cfg["timezone"]).date().isoformat() + "-test", 0, 24 * 60)
            night_id, _, left = s
            if always_on(cfg) and not night_id.endswith("-test"):
                left = ALWAYS_LEFT                      # no end of the day: no last-10-minutes rule, no sale at 23:55
            if bot is None:
                bot = Night(data_dir, cfg, market, now, out)
                live = live if live is not None else live_for(cfg)
                bot.live = live
                bot.clock_fn = now_fn
                if bot.wl is not None and feeds is not None:
                    try:
                        feeds.start()
                    except Exception as e:              # noqa: BLE001 - the night goes on without them
                        why_off, feeds = "feeds did not start: %s: %s" % (type(e).__name__, e), None
                    started = feeds is not None
                bot.attach(feeds if bot.wl is not None else None, why_off)
            bot.now = now
            bot.begin(night_id)
            # a job that ends before the night does (the workflow's 340 minutes, then the next job: minutes
            # with no exits at all in between): New Pairs stops buying near its end and sells what it holds
            job_left = minutes - elapsed if minutes is not None and not anytime else None
            bot.job_left = job_left if job_left is not None and job_left < left else None
            if next_at is None or now >= next_at:       # the 1 s step
                behind = next_at is None or (now - next_at).total_seconds() >= step
                next_at = (now if behind else next_at) + datetime.timedelta(seconds=step)
                try:
                    prices = bot.watch(left, scanner)
                except Exception as e:                  # noqa: BLE001 - log it, keep the night going
                    text = "error in a step (%s: %s) · going on" % (type(e).__name__, e)
                    if said.get("text") != text or bot.epoch() - said["at"] >= 60:     # not every second
                        said = {"text": text, "at": bot.epoch()}
                        bot.log("LOGGER", None, "error", text)
                    # no save forced here: a trade before the error has set traded itself, the line goes out
                    # with the next tick and the files follow within save_every_sec (a step that fails
                    # every second must not rewrite state.json and resend the whole panel every second)
                seq += 1
                try:
                    bot.publish(prices, live, seq, memo)
                except Exception as e:                  # noqa: BLE001 - a full disk is no reason to stop the exits
                    market.errors.append("publish: %s: %s" % (type(e).__name__, e))
                if left <= cfg["flat_before_end_min"] and not bot.state["positions"] and not anytime:
                    bot.log("LOGGER", None, "end", "end of night · %s" % usd(bot.equity({})))
                    bot.close_night()
                    break
            if bot.feeds is not None:                   # the events until the next step: first in, first out
                wait = max(0.0, min(wait_most, (next_at - now_fn()).total_seconds()))
                try:
                    bot.np_events(wait, left)
                except Exception as e:                  # noqa: BLE001
                    market.errors.append("events: %s: %s" % (type(e).__name__, e))
                    sleep(wait)
                if bot.traded:                          # a trade is saved and shown at once
                    seq += 1
                    try:
                        bot.publish(prices, live, seq, memo)
                    except Exception as e:              # noqa: BLE001
                        market.errors.append("publish: %s: %s" % (type(e).__name__, e))
            else:
                sleep(max(0.0, (next_at - now_fn()).total_seconds()))   # only what is left of the second
    finally:
        if scanner is not None:
            scanner.stop()
        if started:
            try:
                feeds.stop()
            except Exception:                           # noqa: BLE001
                pass
        if bot is not None:
            bot.np_close()
    if bot is not None:
        bot.now = now_fn()
        if anytime and night_id and night_id.endswith("-test"):
            prices = sell_all(bot, market, "end of test", now_fn, sleep)
            bot.log("LOGGER", None, "end", "end of test · %s%s" % (usd(bot.equity(prices)), "" if not bot.state["positions"]
                    else " · %d coins not sold, still open" % len(bot.state["positions"])))
            if not bot.state["positions"]:
                bot.close_night()
        bot.publish(prices, live, seq + 1, memo, force=True)
        bot.rec_call("close")                           # an end-of-test sale's tape lines too
    if live is not None:
        live.stop()
    return night_id


def main(argv=None, out=print):
    ap = argparse.ArgumentParser(description="Meme Bot: overnight meme coins on Solana, on paper.")
    ap.add_argument("action", choices=["run", "report", "page"])
    ap.add_argument("--data", required=True)
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--minutes", type=float)
    ap.add_argument("--anytime", action="store_true", help="run outside 23:00-07:00 too (a test run)")
    ap.add_argument("--summary")
    ap.add_argument("--out", help="page: where the page goes (memebot/page.html with the night's data)")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.action == "page":
        doc = read_json(os.path.join(a.data, "panel.json"), None)
        if doc is None:
            st = read_json(os.path.join(a.data, "state.json"), {})
            st.setdefault("balance", cfg["start_balance"])
            st.setdefault("positions", {})
            doc = panel(st, cfg, {}, None)
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(page_html(doc))
        out("page: %s" % a.out)
        return 0
    if a.action == "report":
        text = report_text(read_json(os.path.join(a.data, "state.json"), {}))
    else:
        run(a.data, cfg, a.minutes, a.anytime, out=out)
        text = report_text(read_json(os.path.join(a.data, "state.json"), {}))
    out(text)
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
