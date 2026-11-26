#!/usr/bin/env python3
"""Meme Bot: an overnight meme-coin trading terminal on Solana, on paper.

Six agents, written as rules (the prompt in memebot/prompt.txt says the same in words):
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

Paper trading: no wallet and no key. Every buy and sell is priced by a real Jupiter quote for that
amount (pool fees and price impact included), less a network fee and a latency haircut. A real
trade can still do worse: a failed transaction, a sandwich, a price that moved in the seconds the
order takes. Standard library only.

    python3 memebot/bot.py run --data DIR                 # the night's session, 23:00 to 07:00
    python3 memebot/bot.py run --data DIR --minutes 15 --anytime     # a short run at any hour
    python3 memebot/bot.py report --data DIR              # the morning report

Not financial advice. Most new meme coins lose most of their value within hours.
"""

import argparse
import datetime
import json
import math
import os
import sys
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
    "tick_sec": 20,
    "scan_every_sec": 60,
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
    "no_entries_last_min": 30,
    # exits
    "tp1_pct": 50.0,
    "tp1_sell_frac": 0.5,
    "trail_pct": 20.0,
    "stop_pct": -25.0,
    "time_stop_min": 45,
    "time_stop_below_pct": 10.0,
    "flat_before_end_min": 5,
    # paper costs
    "network_fee_sol": 0.0005,
    "latency_slip_pct": 1.0,
    "slippage_bps": 300,
}

AGENTS = ["SCANNER", "NEWS", "RUG CHECK", "SIZER", "EXECUTOR", "RISK", "LOGGER"]


class MemeError(Exception):
    pass


# ---------------------------------------------------------------- settings and files

def load_config(path=CONFIG):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            mine = json.load(fh)
        for k, v in mine.items():
            if not k.startswith("_"):
                cfg[k] = v
    return cfg


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

class Market(object):
    """The public APIs the agents read. Every call has a timeout; a failure is an empty answer
    and a line in errors, never a crash: the night goes on."""

    def __init__(self, fetch=None, sleep=time.sleep):
        self.fetch = fetch or self._fetch
        self.sleep = sleep
        self.errors = []
        self.gt_backoff_until = 0.0

    @staticmethod
    def _fetch(url, timeout=20):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def get(self, url):
        del self.errors[:-50]
        try:
            return self.fetch(url)
        except urllib.error.HTTPError as e:
            self.errors.append("%s: HTTP %s" % (url.split("?")[0], e.code))
            return None
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.errors.append("%s: %s" % (url.split("?")[0], e))
            return None

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

    def pairs(self, mints):
        """{mint: its most liquid Solana pair} from DexScreener, 30 mints a call."""
        best = {}
        mints = list(dict.fromkeys(m for m in mints if m and m != SOL))
        for i in range(0, len(mints), 30):
            d = self.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i + 30]))
            for p in d if isinstance(d, list) else []:
                m = ((p or {}).get("baseToken") or {}).get("address")
                if not m or m not in mints:
                    continue
                liq = ((p.get("liquidity") or {}).get("usd")) or 0
                if m not in best or liq > (((best[m].get("liquidity") or {}).get("usd")) or 0):
                    best[m] = p
        return best

    def rug_report(self, mint):
        return self.get("https://api.rugcheck.xyz/v1/tokens/%s/report" % mint)

    def quote(self, input_mint, output_mint, amount, slippage_bps=300):
        if amount <= 0:
            return None
        q = self.get("https://lite-api.jup.ag/swap/v1/quote?" + urllib.parse.urlencode({
            "inputMint": input_mint, "outputMint": output_mint, "amount": int(amount), "slippageBps": slippage_bps}))
        if not isinstance(q, dict) or not q.get("outAmount"):
            return None
        return q

    def prices(self, mints):
        """{mint: {"usd": price, "decimals": d}} from Jupiter, SOL always included."""
        ids = list(dict.fromkeys([SOL] + list(mints)))
        out = {}
        for i in range(0, len(ids), 50):
            d = self.get("https://lite-api.jup.ag/price/v3?ids=" + ",".join(ids[i:i + 50]))
            for m, v in (d or {}).items() if isinstance(d, dict) else []:
                if isinstance(v, dict) and v.get("usdPrice"):
                    out[m] = {"usd": float(v["usdPrice"]), "decimals": v.get("decimals")}
        return out


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


# ---------------------------------------------------------------- the paper book

class Night(object):
    """One session: the balance, open positions, closed trades, every decision."""

    def __init__(self, data_dir, cfg, market, now_utc, out=print):
        self.dir = data_dir
        self.cfg = cfg
        self.m = market
        self.now = now_utc
        self.out = out
        os.makedirs(data_dir, exist_ok=True)
        self.state = read_json(os.path.join(data_dir, "state.json"), {})
        st = self.state
        st.setdefault("balance", float(cfg["start_balance"]))
        st.setdefault("positions", {})
        st.setdefault("nights", [])
        st.setdefault("holders", {})
        st.setdefault("rug_cache", {})

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

    def log(self, agent, coin, action, text):
        n = self.state.get("night") or {}
        row = {"t": self.ts(), "time": self.clock(), "agent": agent, "coin": coin, "action": action, "text": text}
        n.setdefault("decisions", []).append(row)
        n["lines"] = n.get("lines", 0) + 1
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
            self.log("EXECUTOR", pos["symbol"], "stuck", "can't sell (no Jupiter route): %s · retrying" % why)
            return False
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
            n["trades"].append(trade)
            n["streak"] = n["streak"] + 1 if pnl < 0 else 0
            if n["streak"] >= self.cfg["loss_streak"]:
                n["cooldown_until"] = self.epoch() + self.cfg["cooldown_min"] * 60
                self.log("RISK", None, "pause", "%d losses in a row · %d min pause" % (
                    n["streak"], self.cfg["cooldown_min"]))
            del self.state["positions"][mint]
            self.log("EXECUTOR", pos["symbol"], "exit", "EXIT %s %s (%+.0f%%) · %s" % (
                pos["symbol"], ("+" if pnl >= 0 else "") + usd(pnl), trade["pnl_pct"], why))
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
            del pos["hist"][:-60]
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

    def scan(self, prices, minutes_left):
        """SCANNER -> RUG CHECK -> RISK -> SIZER on fresh pairs."""
        cfg, n = self.cfg, self.state["night"]
        n["scans"] += 1
        now = self.epoch()
        self.trend = self.m.trending(now)
        mints = self.m.new_pools(pages=2, now=now) + self.m.promoted() + list(self.trend)
        mints = [m for m in dict.fromkeys(mints) if m not in n["entered"] and m not in self.state["positions"]]
        pairs = self.m.pairs(mints)
        sol_usd = (prices.get(SOL) or {}).get("usd")
        ready, seen = [], {"inside": 0, "near": None}
        for mint, p in pairs.items():
            try:
                self.consider(mint, p, now, ready, seen)
            except Exception as e:                      # noqa: BLE001 - one odd coin never stops the night
                self.m.errors.append("%s: %s: %s" % (mint, type(e).__name__, e))
        inside, near = seen["inside"], seen["near"]
        if n["scans"] % 5 == 1:                         # what the scanner sees, every 5 minutes
            self.log("SCANNER", None, "scan", "scanned %d new coins · %d pairs · %d pass the rules%s" % (
                len(mints), len(pairs), inside, "" if inside or not near else
                " · closest %s liq %s, 5m %s" % (near["symbol"], usd(near["liquidity"]), usd(near["vol5"]))))
        for fx, mint, rep in sorted(ready, key=lambda r: -r[0]["vol5"]):
            why = self.why_no_entries(minutes_left, prices)
            if why:
                last = n.get("risk_said") or {}
                if last.get("why") != why or self.epoch() - last.get("at", 0) >= 300:   # once in 5 minutes
                    n["risk_said"] = {"why": why, "at": self.epoch()}
                    self.log("RISK", fx["symbol"], "skip", "no entry: %s" % why)
                break
            if not sol_usd:
                break
            decimals = int(f(((rep or {}).get("token") or {}).get("decimals"), 6))
            size = min(self.state["balance"], self.equity(prices) * cfg["position_pct"] / 100.0)
            if size < cfg["min_order_usd"]:
                self.log("SIZER", fx["symbol"], "skip", "only %s left to size" % usd(size))
                break
            # honeypot: what the coins this buys would sell for right away
            pos = self.buy(mint, fx, size - self.fee_usd(sol_usd), sol_usd, decimals, fx.get("reason", ""))
            if not pos:
                continue                                # no quote right now: the next scan tries again
            back = self.sell_quote(pos, pos["raw"], sol_usd)
            loss = (1 - back / pos["cost"]) * 100 if back is not None else 100.0
            if back is None or loss > cfg["max_roundtrip_loss_pct"]:
                self.log("RUG CHECK", fx["symbol"], "reject", "sell simulation: %s" % (
                    "can't sell (honeypot?)" if back is None else "would lose %.0f%% at once" % loss))
                n["entered"].append(mint)
                n["filtered"] = n.get("filtered", 0) + 1
                n["last_rug"] = {"coin": fx["symbol"], "pass": False}
                continue
            eq = self.equity(prices)
            pos["size_pct"] = round(pos["cost"] / eq * 100, 1) if eq else None
            self.state["balance"] -= pos["cost"]
            self.state["positions"][mint] = pos
            n["entered"].append(mint)
            self.log("SIZER", fx["symbol"], "size", "%.0f%% of %s = %s" % (cfg["position_pct"], usd(eq), usd(pos["cost"])))
            self.log("EXECUTOR", fx["symbol"], "enter", "ENTER %s at %s · slip %.1f%% · %s" % (
                fx["symbol"], ("%.10f" % pos["entry_price"]).rstrip("0"), pos["impact_pct"], pos.get("reason") or "signal"))

    def consider(self, mint, p, now, ready, seen):
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
        rep = self.rug(mint, now)
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

    def rug(self, mint, now):
        c = self.state["rug_cache"].get(mint)
        if c and now - c["at"] < 600:
            return c["report"]
        rep = self.m.rug_report(mint)
        keep = None
        if isinstance(rep, dict):
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
        self.state["rug_cache"][mint] = {"at": now, "report": keep}
        if len(self.state["rug_cache"]) > 300:
            for k in sorted(self.state["rug_cache"], key=lambda k: self.state["rug_cache"][k]["at"])[:100]:
                self.state["rug_cache"].pop(k, None)
        return keep

    def tick(self, minutes_left, scan=True):
        prices = self.m.prices(list(self.state["positions"]))
        self.manage(prices, minutes_left)
        if scan and minutes_left > self.cfg["flat_before_end_min"]:
            self.scan(prices, minutes_left)
        n = self.state.get("night")
        if n is not None:
            curve = n.setdefault("curve", [])
            if not curve or self.epoch() - curve[-1][0] >= 60:
                curve.append([int(self.epoch()), round(self.equity(prices), 2)])
                del curve[:-600]
        self.save(prices)
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

    def save(self, prices=None):
        prices = prices or {}
        write_json(os.path.join(self.dir, "state.json"), self.state)
        write_json(os.path.join(self.dir, "panel.json"), panel(self.state, self.cfg, prices, self.ts(), self.m.errors[-5:]))


def panel(state, cfg, prices, at, errors=()):
    """What the page shows: the prompt, the balance, the night's log, open positions, trades."""
    n = state.get("night") or state.get("last_night") or {}
    eq = state["balance"]
    pos = []
    for mint, p in state["positions"].items():
        px = (prices.get(mint) or {}).get("usd") or p.get("last") or p["entry_price"]
        val = p["tokens_ui"] * px
        eq += val
        pos.append({"symbol": p["symbol"], "cost": round(p["cost"], 2), "value": round(val + p.get("sold_usd", 0), 2),
                    "pnl_pct": round(((val + p.get("sold_usd", 0)) / p["cost"] - 1) * 100, 1), "opened": p["opened"],
                    "url": p.get("url"), "reason": p.get("reason", ""), "size_pct": p.get("size_pct"),
                    "entry_price": p["entry_price"], "price": px, "slip": p.get("impact_pct"),
                    "tp1_done": p.get("tp1_done"), "hist": (p.get("hist") or [])[-60:]})
    try:
        with open(PROMPT, encoding="utf-8") as fh:
            prompt = fh.read()
    except OSError:
        prompt = ""
    return {"updated": at, "mode": "paper", "start_balance": cfg["start_balance"], "balance": round(state["balance"], 2),
            "equity": round(eq, 2), "session": "%s → %s" % (cfg["session_start"], cfg["session_end"]),
            "agents": AGENTS, "prompt": prompt, "night": {k: n.get(k) for k in ("id", "start_equity", "started", "scans",
                                                                              "flagged", "checked", "filtered", "lines",
                                                                              "last_rug")},
            "flags": (n.get("flags") or [])[-8:], "curve": (n.get("curve") or [])[-600:],
            "position_pct": cfg["position_pct"], "slip_cap": cfg["max_roundtrip_loss_pct"],
            "live": bool(state.get("night")), "positions": pos, "trades": (n.get("trades") or [])[-50:],
            "log": (n.get("decisions") or [])[-200:], "nights": state.get("nights", [])[-30:], "errors": list(errors)}


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


def run(data_dir, cfg, minutes=None, anytime=False, market=None, now_fn=utc_now, sleep=time.sleep, out=print):
    """Ticks until the session ends (or `minutes` pass). -> the night's id or None."""
    market = market or Market()
    t0 = now_fn()
    night_id = None
    last_scan = 0.0
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
        if s is None:                    # a test run: its own night, closed at its end
            s = (local(now, cfg["timezone"]).date().isoformat() + "-test", 0, 24 * 60)
        night_id, _, left = s
        bot = Night(data_dir, cfg, market, now, out)
        bot.begin(night_id)
        epoch = bot.epoch()
        do_scan = epoch - last_scan >= cfg["scan_every_sec"]
        if do_scan:
            last_scan = epoch
        try:
            bot.tick(left, scan=do_scan)
        except Exception as e:                          # noqa: BLE001 - log it, keep the night going
            bot.log("LOGGER", None, "error", "error in a step (%s: %s) · going on" % (type(e).__name__, e))
            bot.save()
        if s and left <= cfg["flat_before_end_min"] and not bot.state["positions"] and not anytime:
            bot.log("LOGGER", None, "end", "end of night · %s" % usd(bot.equity({})))
            bot.close_night()
            bot.save()
            break
        sleep(cfg["tick_sec"])
    if anytime and night_id and night_id.endswith("-test"):
        bot = Night(data_dir, cfg, market, now_fn(), out)
        prices = market.prices(list(bot.state["positions"]))
        sol_usd = (prices.get(SOL) or {}).get("usd")
        for mint in list(bot.state["positions"]):
            if sol_usd:
                bot.close_part(mint, 1.0, "end of test", sol_usd)
        bot.log("LOGGER", None, "end", "end of test · %s" % usd(bot.equity(prices)))
        if not bot.state["positions"]:
            bot.close_night()
        bot.save(prices)
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
