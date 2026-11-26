#!/usr/bin/env python3
"""Meme Bot: an overnight meme-coin trading terminal on Solana, on paper.

The agents, written as rules (the prompt in memebot/prompt.txt says the same in words):
  1. SCANNER    fresh DEX pairs (GeckoTerminal new pools, DexScreener profiles and boosts), their
                liquidity, volume, buys against sells and holder growth. It flags, it never trades.
  2. RUG CHECK  RugCheck's report (mint and freeze authority, dev wallet, top 10 holders, insiders,
                LP burned or locked) and a sell simulation on Jupiter (a honeypot can't be sold).
  3. SIZER      how much of the balance goes into a flagged coin.
  4. RISK       how many positions and trades a night, a loss streak pause, a stop for the night.
  5. EXIT       first target, trailing stop, stop loss, time stop, everything sold before 07:00.
  6. JOURNAL    every decision with its reason, and the morning report.
  7. NEWS       what people promote: the coin's X and Telegram accounts (DexScreener), paid
                DexScreener boosts and the GeckoTerminal and DexScreener trending lists. A coin
                that trends or is boosted counts as a scanner signal. (Reading posts on X itself
                costs money with X's API: not in this bot.)
  8. LEARNER    learns from the bot's own closed trades (memebot/learner.py, memory.json in the
                data dir): a coin that looks like past losers is skipped, every 5th such skip is
                bought at half size, and the size of any other buy follows the past results.

Paper trading: no wallet and no key. Every buy and sell is priced by a real Jupiter quote for that
amount (pool fees and price impact included), less a network fee and a latency haircut. A real
trade can still do worse: a failed transaction, a sandwich, a price that moved in the seconds the
order takes. Standard library only.

Live every second: the main loop reads prices and runs the exits every watch_sec (1 s), the
SCANNER fetches on a thread of its own every scan_every_sec, and each second goes out as an MQTT
message to free public brokers (the page subscribes over WebSocket). The files (state.json,
panel.json) are written every 5 s and after each trade; git takes them every 30 s as before.

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
    # paper costs
    "network_fee_sol": 0.0005,
    "latency_slip_pct": 1.0,
    "slippage_bps": 300,
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
    down before the API has to. Used on the main thread only."""

    def __init__(self, per_min, clock=time.monotonic):
        self.per_min = per_min
        self.clock = clock
        self.calls = collections.deque()

    def room(self, keep=0):
        now = self.clock()
        while self.calls and now - self.calls[0] >= 60:
            self.calls.popleft()
        return len(self.calls) + keep < self.per_min

    def take(self, keep=0, force=False):
        """One call now, if the minute has room (keep calls left over); force: the call goes anyway
        and counts, so the calls that can wait do."""
        if not self.room(keep) and not force:
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

    def rug_report(self, mint):
        return self.get("https://api.rugcheck.xyz/v1/tokens/%s/report" % mint)

    def can_quote(self, n=1):
        """Room in Jupiter's free minute for n quotes now (a buy and its sell simulation are 2)."""
        return self.jup.room(n - 1)

    def quote(self, input_mint, output_mint, amount, slippage_bps=300):
        """Jupiter's quote, or None with the reason in quote_why. A sale (into SOL: an exit, or a
        buy's sell simulation) is never held back by the bot's own minute: a stop must try at once
        (it counts all the same, so the price reads and the next buys wait). A buy waits for room.
        After a 429, a 5xx or a timeout the quotes rest 10 s (20 s, 40 s ... after more) and fail at
        once meanwhile, so a hanging Jupiter costs the 1 s loop one 6 s timeout in a while."""
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
            "inputMint": input_mint, "outputMint": output_mint, "amount": int(amount), "slippageBps": slippage_bps}), 6)
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

    # time
    def ts(self):
        return self.now.replace(microsecond=0).isoformat() + "Z"

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
    def fee_usd(self, sol_usd):
        return self.cfg["network_fee_sol"] * sol_usd

    def buy(self, mint, fx, usd_in, sol_usd, decimals, reason=""):
        """A paper buy priced by Jupiter's quote for this amount. -> position or None."""
        lamports = usd_in / sol_usd * 1e9
        q = self.m.quote(SOL, mint, lamports, self.cfg["slippage_bps"])
        if not q:
            return None
        raw = int(int(q["outAmount"]) * (1 - self.cfg["latency_slip_pct"] / 100.0))
        if raw <= 0:
            return None
        tokens_ui = raw / 10.0 ** decimals
        cost = usd_in + self.fee_usd(sol_usd)
        return {"mint": mint, "symbol": fx["symbol"], "name": fx["name"], "url": fx.get("url"),
                "raw": raw, "decimals": decimals, "tokens_ui": tokens_ui, "cost": round(cost, 4),
                "entry_price": usd_in / tokens_ui, "opened": self.ts(), "peak": usd_in / tokens_ui,
                "last": usd_in / tokens_ui, "tp1_done": False, "sold_usd": 0.0, "reason": reason,
                "hist": [usd_in / tokens_ui], "impact_pct": round(f(q.get("priceImpactPct")) * 100, 2)}

    def sell_quote(self, pos, raw, sol_usd):
        """USD a paper sale of raw token units brings, after fees and the haircut, or None (no route)."""
        q = self.m.quote(pos["mint"], SOL, raw, self.cfg["slippage_bps"])
        if not q:
            return None
        sol = int(q["outAmount"]) / 1e9
        return sol * sol_usd * (1 - self.cfg["latency_slip_pct"] / 100.0) - self.fee_usd(sol_usd)

    def close_part(self, mint, frac, why, sol_usd):
        pos = self.state["positions"][mint]
        raw = pos["raw"] if frac >= 0.999 else int(pos["raw"] * frac)
        got = self.sell_quote(pos, raw, sol_usd)
        if got is None:
            # again in 5 s, not every second: each try is a Jupiter call out of a small free minute
            pos["retry_at"] = self.epoch() + 5
            if self.epoch() - pos.get("stuck_said", float("-inf")) >= 60:
                pos["stuck_said"] = self.epoch()
                self.log("EXECUTOR", pos["symbol"], "stuck", "can't sell (%s): %s · retrying" % (
                    getattr(self.m, "quote_why", None) or "no Jupiter route", why))
            return False
        self.traded = True
        self.state["balance"] += got
        pos["sold_usd"] += got
        if frac >= 0.999:
            pnl = pos["sold_usd"] - pos["cost"]
            n = self.state["night"]
            trade = {"symbol": pos["symbol"], "mint": mint, "opened": pos["opened"], "closed": self.ts(),
                     "cost": round(pos["cost"], 2), "proceeds": round(pos["sold_usd"], 2), "pnl": round(pnl, 2),
                     "pnl_pct": round(pnl / pos["cost"] * 100, 1), "why": why, "url": pos.get("url"),
                     "reason": pos.get("reason", ""), "size_pct": pos.get("size_pct"),
                     "entry_price": pos["entry_price"], "exit_price": pos.get("last"), "slip": pos.get("impact_pct")}
            if pos.get("learn"):
                trade["learn"] = pos["learn"]           # state.json keeps them: a lost memory.json can be rebuilt
            n["trades"].append(trade)
            n["streak"] = n["streak"] + 1 if pnl < 0 else 0
            if n["streak"] >= self.cfg["loss_streak"]:
                n["cooldown_until"] = self.epoch() + self.cfg["cooldown_min"] * 60
                self.log("RISK", None, "pause", "%d losses in a row · %d min pause" % (
                    n["streak"], self.cfg["cooldown_min"]))
            del self.state["positions"][mint]
            if self.cfg.get("reenter_min", -1) >= 0:
                n.setdefault("reenter", {})[mint] = self.epoch() + self.cfg["reenter_min"] * 60
            self.log("EXECUTOR", pos["symbol"], "exit", "EXIT %s %s (%+.0f%%) · %s" % (
                pos["symbol"], ("+" if pnl >= 0 else "") + usd(pnl), trade["pnl_pct"], why))
            self.learn_close(pos, trade)
        else:
            pos["raw"] -= raw
            pos["tokens_ui"] = pos["raw"] / 10.0 ** pos["decimals"]
            pos["tp1_done"] = True
            self.log("EXECUTOR", pos["symbol"], "take", "TP %s · sold %.0f%% for %s" % (why, frac * 100, usd(got)))
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
            px = (prices.get(mint) or {}).get("usd")
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

    def why_no_entries(self, minutes_left, prices):
        n, cfg = self.state["night"], self.cfg
        if minutes_left <= cfg["no_entries_last_min"]:
            return "last %d min: no new entries" % cfg["no_entries_last_min"]
        if len(n["trades"]) + len(self.state["positions"]) >= cfg["max_trades_per_night"]:
            return "%d trades tonight (max)" % cfg["max_trades_per_night"]
        if len(self.state["positions"]) >= cfg["max_open"]:
            return "%d positions open (max)" % cfg["max_open"]
        start = n["start_equity"] or 1
        dd = (self.equity(prices) / start - 1) * 100
        if dd <= cfg["night_stop_pct"]:
            return "night at %+.0f%%: stopped for the night" % dd
        if self.epoch() < n.get("cooldown_until", 0):
            return "pause after a losing streak"
        return None

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
            self.log("SIZER", fx["symbol"], "size", "%.0f%% of %s%s = %s" % (
                cfg["position_pct"], usd(eq), "" if mult == 1.0 else " x%.2f LEARNER" % mult, usd(pos["cost"])))
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
        trial = copy.deepcopy(self.mem)
        d = self.learn("decide", trial, labels, self.cfg) if labels is not None else None
        if d is None:
            return None, None, None
        if d["action"] != "buy" or d["mult"] != 1.0:   # "no bucket has 8 trades yet" on every buy is noise
            self.log("LEARNER", fx["symbol"], d["action"], "%s · %s" % (d["action"], d["why"]))
        if d["action"] == "explore":
            return labels, d, trial
        self.mem = trial
        if d["action"] == "skip":                       # the 1 in 5 count moved
            self.learn_save()
        return labels, d, None

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
        if scan and minutes_left > self.cfg["flat_before_end_min"]:
            self.scan(prices, minutes_left)
        self.mark(prices)
        self.save(prices)
        return prices

    def watch(self, minutes_left, scanner):
        """One second of run(): SOL's and the held coins' prices and EXIT on each, then the scan the
        scanner thread finished is applied here (this thread is the book's only writer) and the
        next one is started when it is due."""
        t = time.monotonic()
        held = list(self.state["positions"])
        live = getattr(self.m, "live_prices", None)
        prices = live(held) if live else self.m.prices(held)
        self.manage(prices, minutes_left)
        now = self.epoch()
        first = False
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
        return panel(self.state, self.cfg, prices or {}, self.ts(), self.m.errors[-5:], speed=self.speed,
                     live_stats=self.live.stats() if self.live is not None else None)

    def save(self, prices=None, doc=None):
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
            live.tick(tick_msg(self.state, cfg, prices, self.ts(), seq, memo, self.speed))


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
               "entry_price": p["entry_price"], "price": px, "slip": p.get("impact_pct"),
               "tp1_done": p.get("tp1_done")}
        if hist:
            row["hist"] = (p.get("hist") or [])[-120:]
        pos.append(row)
    return pos, eq


def trades_view(trades):
    """The last 50 trades as the page shows them: without the LEARNER's labels, which state.json
    keeps for a rebuilt memory but the page has no use for (and a tick must stay small)."""
    return [{k: v for k, v in t.items() if k != "learn"} for t in (trades or [])[-50:]]


def speed_view(cfg, speed=None):
    s = speed or {}
    w = watch_sec(cfg)
    return {"watch_sec": w, "tick_sec": w, "scan_sec": cfg["scan_every_sec"], "gt_sec": cfg.get("gt_every_sec"),
            "feed": "%s · scan %gs · live %gs" % (s.get("source") or "jupiter", cfg["scan_every_sec"], w),
            "last_scan_ms": s.get("last_scan_ms"), "watch_ms": s.get("watch_ms"),
            "scans_per_min": s.get("scans_per_min", 0), "feeds": list(FEEDS)}


def stream_view(cfg, on=None):
    """Where the page subscribes: the topic and the brokers' WebSocket addresses, in order. on: whether
    this bot runs a live channel (None: what the config and MEMEBOT_LIVE say)."""
    return {"topic": cfg.get("live_topic"), "enabled": live_enabled(cfg) if on is None else bool(on),
            "ws": [b["ws"] for b in cfg.get("live_brokers") or [] if b.get("ws")]}


def panel(state, cfg, prices, at, errors=(), speed=None, live_stats=None):
    """What the page shows: the prompt, the balance, the night's log, open positions, trades."""
    n = state.get("night") or state.get("last_night") or {}
    pos, eq = positions_view(state, prices)
    try:
        with open(PROMPT, encoding="utf-8") as fh:
            prompt = fh.read()
    except OSError:
        prompt = ""
    return {"updated": at, "mode": "paper", "start_balance": cfg["start_balance"], "balance": round(state["balance"], 2),
            "equity": round(eq, 2), "session": "%s → %s" % (cfg["session_start"], cfg["session_end"]),
            "agents": AGENTS, "prompt": prompt, "night": {k: n.get(k) for k in NIGHT_KEYS},
            "flags": (n.get("flags") or [])[-8:], "curve": (n.get("curve") or [])[-600:],
            "position_pct": cfg["position_pct"], "slip_cap": cfg["max_roundtrip_loss_pct"],
            "live": bool(state.get("night")), "positions": pos, "trades": trades_view(n.get("trades")),
            "log": (n.get("decisions") or [])[-200:], "nights": state.get("nights", [])[-30:], "errors": list(errors),
            "stream": stream_view(cfg, None if live_stats is None else True), "speed": speed_view(cfg, speed),
            "learner": state.get("learner_summary") or None, "live_stats": dict(live_stats or {})}


def steady_view(doc):
    """The parts of a panel or a tick that change seldom: the flags, the LEARNER's summary and the
    speed without its timings (those come with each panel, every 15 s)."""
    sp = {k: v for k, v in (doc.get("speed") or {}).items() if k not in ("watch_ms", "last_scan_ms", "scans_per_min")}
    return {"night": (doc.get("night") or {}).get("id"), "flags": doc.get("flags"), "learner": doc.get("learner"),
            "speed": sp}


def tick_msg(state, cfg, prices, at, seq, memo, speed=None):
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
           "speed": speed_view(cfg, speed), "learner": state.get("learner_summary") or None}
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
        live=None, scanner=None):
    """Watches every watch_sec until the session ends (or `minutes` pass): prices, exits and a
    live tick each second, a scan on the scanner thread every scan_every_sec, the files every
    save_every_sec and after a trade. One Night lives the whole run (state.json is read once).
    `live`: the live channel (default: the brokers in cfg unless MEMEBOT_LIVE=0).
    -> the night's id or None."""
    market = market or Market(cfg=cfg)
    scanner = scanner or Scanner(market, cfg)
    step = watch_sec(cfg)
    t0 = now_fn()
    night_id = bot = None
    prices, seq, memo, said = {}, 0, {}, {}
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
            if bot is None:
                bot = Night(data_dir, cfg, market, now, out)
                live = live if live is not None else live_for(cfg)
                bot.live = live
            bot.now = now
            bot.begin(night_id)
            try:
                prices = bot.watch(left, scanner)
            except Exception as e:                      # noqa: BLE001 - log it, keep the night going
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
            except Exception as e:                      # noqa: BLE001 - a full disk is no reason to stop the exits
                market.errors.append("publish: %s: %s" % (type(e).__name__, e))
            if left <= cfg["flat_before_end_min"] and not bot.state["positions"] and not anytime:
                bot.log("LOGGER", None, "end", "end of night · %s" % usd(bot.equity({})))
                bot.close_night()
                break
            sleep(max(0.0, step - (now_fn() - now).total_seconds()))   # only what is left of the second
    finally:
        scanner.stop()
    if bot is not None:
        bot.now = now_fn()
        if anytime and night_id and night_id.endswith("-test"):
            prices = sell_all(bot, market, "end of test", now_fn, sleep)
            bot.log("LOGGER", None, "end", "end of test · %s%s" % (usd(bot.equity(prices)), "" if not bot.state["positions"]
                    else " · %d coins not sold, still open" % len(bot.state["positions"])))
            if not bot.state["positions"]:
                bot.close_night()
        bot.publish(prices, live, seq + 1, memo, force=True)
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
