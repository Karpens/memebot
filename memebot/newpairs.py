"""Meme Bot's NEW PAIRS rules: brand-new pump.fun coins in their first minutes, still on the bonding
curve (the "New Pairs" column of Axiom Pulse). First in, first out.

Pure logic over events: no network, no threads, no book, standard library only. The bot feeds it
PumpPortal create / migrate events, the coin's metadata JSON, bonding-curve readings (one batched
getMultipleAccounts every 0.5-1 s, plus the optional RPC WebSocket pushes) and RugCheck reports,
and asks it what to do. Garbage in (None, NaN, text, wrong types) never raises.

    wl = Watchlist(cfg)
    coin = wl.on_create(event, now)          # None: skipped (mayhem mode, no symbol, ...) or a repeat
    for m in coin.evicted: unwatch(m)        # at most np_max_watch curves at once, oldest out first
    wl.on_meta(mint, metadata_json)          # once: the uri's JSON (None when every gateway failed)
    coin = wl.on_curve(mint, reading, now)   # None: not watched, or nothing new in this reading
    wl.touch(now)                            # each good poll round: an unchanged curve is still fresh
    act, why = wl.judge(coin, now)           # "reject" | "wait" | "fire" (cheap_check, then trigger)
    ok, why, holders = rug_check_np(report, coin, cfg)    # on "fire" (report None when not in yet)
        # ok None: no report, or a report that sees too few wallets holding the coin ("waiting: RugCheck
        #   sees 3 holders (min 10)", "waiting: 6 holders for 3.3 SOL in (0.55 SOL a holder, max 0.5)")
        #   -> no entry now, judge fires again on the next reading
        # ok False: wl.reject(coin, why[0]) (judge does it on the next reading if the bot doesn't)
    keep = rug_cache_s(report, coin, cfg)    # how long the bot may reuse that report for this coin's entry:
        # np_rug_recheck_s for a report that leaves it waiting (ask again soon), else np_rug_cache_s
    rug_waiting(coin)                        # True while the coin waits for RugCheck to see enough holders
    # learner skip -> wl.reject(coin, "learner: skip"); book full or paused -> wl.retry(coin)
    wl.mark_entered(mint, now)               # after the paper fill, same loop turn
    why = exit_signal(coin, cfg)             # every reading of a held coin: None or "dump: 3.1 SOL sold"
    wl.mark_exited(mint, why, now)           # after the sale
    for m in wl.expire(now): unwatch(m)      # every loop turn
    panel["watch"] = wl.rows(now)

judge() fires once per trigger: on the reading where all the entry rules first hold. It fires again on
a later reading only while the bot had no RugCheck verdict for the coin (no report in time, or a report
that sees too few holders: "try again on the next reading"), after wl.retry(), or when the rules lapsed and
hold again (a new trigger). Whether the trigger's SOL came from enough WALLETS is RugCheck's to say (its
holders, rug_check_np: np_max_sol_per_holder), never the curve readings': a buy reading is a poll, and the
poll rate is not the number of buyers. On a fire judge() rejects only a single buy READING bigger than
np_max_buy_sol (concentration(); off by default). A coin that passed RugCheck and was not bought (the
LEARNER said skip) is not offered again while the rules keep holding, so the learner is asked once per
trigger, not once per reading (still: reject it).

A reading is the decoded bonding-curve account (decode_curve() below, or the feed's own decode):
{"vsol", "vtok", "rsol", "complete", "slot"} in lamports and raw token units, or in SOL and whole
tokens ({"price_sol", "real_sol", "complete"} works too). Buys and sells are counted from the real
SOL in the curve going up or down between two readings, so trades that land between two polls count
as one (net) move: the counts are a floor and the biggest sell is a net figure.

Times (now) are epoch seconds. Money is SOL; a coin's price is SOL per whole token.
"""

import base64
import binascii
import math
import struct

# pump.fun's curve: every coin starts with 30 SOL of virtual reserves (no real SOL) against
# 1,073,000,000 virtual tokens, constant product; 1 B tokens in total, 6 decimals.
START_VSOL = 30.0
START_VTOK = 1.073e9
K = START_VSOL * START_VTOK
SUPPLY = 1e9
LAMPORTS = 1e9
TOKEN_UNITS = 1e6
ROWS = 20                   # the panel's watch list: newest first, at most this many
EPS = 1e-9                  # boundaries are inclusive (8.0 % passes "at most 8 %"), floats or not
MOVE = 1e-6                 # a reserve change under 1000 lamports is float noise, not a trade
# far outside any pump.fun curve (30-115 SOL against 0.28-1.07 B tokens): a broken reading, and
# bounds that keep every derived number (price, rise, market cap) finite
MIN_VSOL, MAX_VSOL = 1e-3, 1e5
MIN_VTOK, MAX_VTOK = 1.0, 1e11
PRICE_MIN, PRICE_MAX = MIN_VSOL / MAX_VTOK, MAX_VSOL / MIN_VTOK
MAX_T = 1e11                # epoch seconds (year 5138): a clock past that is garbage, and the difference
                            # of two bounded times stays finite ("%d" or int() of inf raises)
MAX_N = 1e9                 # counts (buys, sells, readings)
# the LEARNER's hour family (memebot/learner.py NIGHT_HOURS): two-hour blocks of the night and
# "hour:day" outside it. The learner keeps only the labels it knows, so "hour:3" would be dropped.
NIGHT_HOURS = {23: "hour:23-01", 0: "hour:23-01", 1: "hour:01-03", 2: "hour:01-03",
               3: "hour:03-05", 4: "hour:03-05", 5: "hour:05-07", 6: "hour:05-07"}
DAY_HOURS = "hour:day"

DEFAULTS = {
    # SCANNER: what is watched, and for how long
    "np_watch_s": 180,              # follow each new curve this long (held coins stay until sold)
    "np_max_watch": 40,             # curves watched at once; the oldest is dropped for a new one
    # RUG CHECK lite, from the create event and the curve
    "np_skip_mayhem": True,         # is_mayhem_mode marks a special launch type: SCAMSEM went -89 %
    "np_max_dev_pct": 8.0,          # the creator's initial buy, % of the 1 B supply
    "np_dump_sol": 2.0,             # one sell this big since launch rejects; after entry it exits
    "np_need_social": True,         # at least one of X, Telegram, website in the metadata
    "np_serial_window_min": 60,     # a creator who launched another coin this recently: serial
    "np_serial_max": 0,             # other launches by the same creator allowed in that window
    # RUG CHECK, RugCheck's report (fetched by the bot in a worker thread)
    "np_rug_timeout_s": 3.0,        # no report this fast: no entry on this reading (caller's timer)
    "np_rug_cache_s": 30,           # the caller keeps a report this long
    "np_max_holder_pct": 12.0,      # one holder other than the curve above this rejects
    "np_max_top10_pct": 35.0,       # the top 10 holders, the curve left out
    "np_rug_bad_risks": ["rugged tokens", "single holder ownership"],   # risk names that reject
    # RugCheck must SEE the holders before its holder checks mean anything. The live morning of 2026-11-26
    # (measured BEFORE the 45 s age floor, np_min_age_s, was on: 7 of the 10 entries fired at 10-25 s): 7 of the
    # 10 entries passed on a report with 0-4 holders (6 of them at 13-25 s, when RugCheck had not indexed the coin
    # yet, so "top holder 0 %" was an empty list, not a spread-out coin; real reports count the holders by about
    # 22 s; MEME at 134 s with 4), and all 4 dumps were among those 7. Under the age floor 3 of those 4 (STOREB, TANOSHIMI, KIDSTOCK) dumped before 45 s, where
    # cheap_check's np_dump_sol already rejects them; the holder rules' own live evidence is MEME (134 s, 4
    # holders, then a -28 % dump) and the waits of BST (8 holders, -9 %) and YARL (9, -14 %). rug_holders()
    # counts the WALLETS HOLDING the coin: RugCheck's totalHolders counts every open token account, the empty ones
    # of wallets that sold out included (real reports: 7 accounts, 5 of them empty, 1 wallet holding), so it is
    # cut by the empty accounts and the curve's it lists (a list that shows an empty account, the biggest first,
    # shows every wallet holding: the unlisted ones are empty too). Fewer than np_min_holders is no PASS but
    # "waiting" (no entry on this reading, ask again in np_rug_recheck_s). 0 turns it off. Eleven trades fit no
    # number: the tapes decide.
    "np_min_holders": 10,
    "np_rug_recheck_s": 4.0,        # a report that leaves the coin waiting is asked for again after this (the
                                    # caller's np_rug_get: rug_cache_s()), not the 30 s cache: RugCheck's
                                    # index catches up in seconds; one call a coin every 4 s while it fires
    # CONCENTRATION, by RugCheck's wallets: at most this many SOL in (since the launch) for each wallet holding
    # the coin, or "waiting" as above ("waiting: 6 holders for 3.3 SOL in (0.55 SOL a holder, max 0.5)"): with
    # 10 holders at most 5 SOL, 16 holders for 7.8 SOL. Why: the live dumps sold 2.9-4.4 SOL out of 3.3-4.3 SOL
    # in: a handful of early wallets held it all and sold into the first pump. Wallets, not curve readings: a
    # buy reading is one poll that saw the SOL rise, and how many buyers land in one poll depends on the poll
    # rate (0.5 s while a coin is held, 1 s otherwise), not on the coin. What it does not catch: one big wallet
    # among many small ones (the average stays low); np_max_holder_pct (12 %) catches a wallet from about 4 SOL
    # bought early. 0.5 is a guess from the mechanism, not a fit: the grid tries 0.35, 0.8 and off (0).
    "np_max_sol_per_holder": 0.5,
    # the entry trigger: momentum, first in
    "np_max_age_s": 180,
    "np_min_age_s": 0,              # no entry before this age (s): the first seconds are the snipers' and the dev's
    "np_min_sol": 3.0,              # real SOL gained in the curve since the create
    "np_min_buys": 8,               # readings where the real SOL went up
    "np_min_buy_share": 0.7,        # ... as a share of the readings that moved
    "np_min_rise_pct": 15.0,        # price above the create price
    "np_max_mcap_sol": 70.0,        # well before the curve completes (about 400 SOL of mcap)
    # one buy READING above this many SOL rejects the coin when its trigger fires ("concentrated: one buy of
    # 2.4 SOL (max 2)"); 0: off. Off by default: the first reading holds the launch's snipers (UZIM: 4.5 SOL,
    # then +163 %), and RugCheck's top holder (np_max_holder_pct) judges the real wallets. A reading's size
    # also grows with the poll interval (several buys in one poll). A backtest lever (2 SOL = np_dump_sol: one
    # wallet that can sell a "dump" by itself)
    "np_max_buy_sol": 0.0,
    # held coins
    "np_stale_s": 5.0,              # no fresh reading this long while holding: Jupiter price (caller)
}

WATCHING, ENTERED = "watching", "entered"


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


def f(x, default=0.0):
    v = _num(x)
    return default if v is None else v


def _t(x):
    """A time in epoch seconds, or None for garbage (and for a clock beyond MAX_T)."""
    v = _num(x)
    return v if v is not None and abs(v) <= MAX_T else None


def _clamp(v, lo, hi):
    return min(hi, max(lo, v))


def _c(cfg, key):
    """A config number, or the default when it is missing or broken."""
    v = _num(cfg.get(key)) if isinstance(cfg, dict) else None
    return DEFAULTS[key] if v is None else v


def _on(cfg, key):
    """A config switch: a bool (or 0/1) in cfg, else the default."""
    v = cfg.get(key) if isinstance(cfg, dict) else None
    if isinstance(v, bool):
        return v
    n = _num(v)
    return DEFAULTS[key] if n is None else n != 0


def _risk_names(cfg):
    v = cfg.get("np_rug_bad_risks") if isinstance(cfg, dict) else None
    if not isinstance(v, (list, tuple)):
        v = DEFAULTS["np_rug_bad_risks"]
    return [s.strip().lower() for s in v if isinstance(s, str) and s.strip()]


def _text(x, n):
    """Printable text for the log and the panel (names come from strangers)."""
    if not isinstance(x, str):
        return ""
    return "".join(ch for ch in x if ch.isprintable()).strip()[:n]


def _key(x):
    """A Solana address-like key: text without spaces, or "" (never trusted further than that)."""
    if not isinstance(x, str):
        return ""
    s = x.strip()
    return s if 0 < len(s) <= 64 and not any(ch.isspace() for ch in s) else ""


def _pick(d, *keys):
    for k in keys:
        if d.get(k) is not None:
            return d.get(k)
    return None


def _yes(x):
    """True only for a real yes: True, a positive number or the text "true" (a flag that marks
    danger counts when in doubt)."""
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        return x.strip().lower() == "true"
    v = _num(x)
    return v is not None and v > 0


def _g(v):
    """5 instead of 5.0 in the text."""
    return "%g" % round(v, 2)


# ---------------------------------------------------------------- socials (the coin's metadata)

def _link(x):
    """A real link text: metadata JSON often holds "", "null" or "none" instead of nothing."""
    if not isinstance(x, str):
        return ""
    s = _text(x, 300)
    return s if len(s) >= 2 and s.lower() not in ("none", "null", "undefined", "n/a", "false", "nan") else ""


def twitter_kind(url):
    """"status" for a post or a search (someone else's words about the coin: weak, half a NEWS
    point), "profile" for an account or community, None when there is no link."""
    s = _link(url).lower()
    if not s:
        return None
    if "/status/" in s or "/statuses/" in s or "/search" in s or "?q=" in s:
        return "status"
    return "profile"


def socials(doc):
    """{"x", "tg", "web", "x_kind"} from a metadata JSON (the create event's uri: pump.fun's
    {"twitter", "telegram", "website", ...}, Metaplex's "extensions"), or from RugCheck's
    tokenMeta / fileMeta when the uri failed. Links are text ("" when there is none)."""
    out = {"x": "", "tg": "", "web": "", "x_kind": None}
    if not isinstance(doc, dict):
        return out
    for src in (doc, doc.get("extensions")):
        if not isinstance(src, dict):
            continue
        out["x"] = out["x"] or _link(src.get("twitter")) or _link(src.get("x"))
        out["tg"] = out["tg"] or _link(src.get("telegram")) or _link(src.get("tg"))
        out["web"] = out["web"] or _link(src.get("website")) or _link(src.get("web"))
    out["x_kind"] = twitter_kind(out["x"])
    return out


def has_social(meta):
    return isinstance(meta, dict) and any(_link(meta.get(k)) for k in ("x", "tg", "web"))


def social_names(meta):
    """["X", "Telegram", "web"] for the log ("X post" for a status link)."""
    if not isinstance(meta, dict):
        return []
    out = []
    if _link(meta.get("x")):
        out.append("X post" if meta.get("x_kind") == "status" else "X")
    if _link(meta.get("tg")):
        out.append("Telegram")
    if _link(meta.get("web")):
        out.append("web")
    return out


# ---------------------------------------------------------------- the bonding curve

def decode_curve(data):
    """The pump.fun bonding-curve account -> {"vtok", "vsol", "rtok", "rsol", "supply" (raw u64s:
    lamports and 1e-6 token units), "complete", "price_sol"} or None. The layout the live probe
    checked: an 8-byte discriminator, five u64 (virtual token, virtual SOL, real token, real SOL
    reserves, supply), then the complete flag at offset 48. data: base64 text, bytes, or the
    getAccountInfo / getMultipleAccounts form [base64, "base64"]."""
    if isinstance(data, (list, tuple)) and data:
        data = data[0]
    if isinstance(data, str):
        try:
            data = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return None
    if not isinstance(data, (bytes, bytearray)) or len(data) < 49:
        return None
    vtok, vsol, rtok, rsol, supply = struct.unpack_from("<5Q", data, 8)
    price = (vsol / LAMPORTS) / (vtok / TOKEN_UNITS) if vtok else None
    return {"vtok": vtok, "vsol": vsol, "rtok": rtok, "rsol": rsol, "supply": supply,
            "complete": data[48] != 0, "price_sol": price}


def read_curve(r):
    """A reading in SOL and whole tokens: {"vsol", "vtok", "rsol", "price", "complete", "slot"},
    or None when it can't be used. The units are told apart by size: a curve holds 30-115 SOL of
    virtual reserves (3e10+ lamports) and 0.28-1.07 B virtual tokens (2.8e14+ raw units). A
    reading with only the real SOL is priced from pump.fun's constant product."""
    if not isinstance(r, dict):
        return None
    vsol = _num(_pick(r, "vsol", "virtual_sol_reserves", "vSolInBondingCurve", "v_sol"))
    vtok = _num(_pick(r, "vtok", "virtual_token_reserves", "vTokensInBondingCurve", "v_tokens"))
    rsol = _num(_pick(r, "rsol", "real_sol_reserves", "real_sol"))
    price = _num(_pick(r, "price_sol", "price"))
    lamports = vsol > 1e4 if vsol is not None else (rsol is not None and rsol > 1e4)
    if vsol is not None and lamports:
        vsol /= LAMPORTS
    if rsol is not None and lamports:
        rsol /= LAMPORTS
    if vtok is not None and vtok > 1e12:
        vtok /= TOKEN_UNITS
    if vsol is not None and vsol <= 0 or vtok is not None and vtok <= 0 or rsol is not None and rsol < 0:
        return None
    if rsol is None and vsol is not None:
        rsol = max(0.0, vsol - START_VSOL)          # virtual = 30 + real on a pump.fun curve
    if vsol is None and rsol is not None:
        vsol = START_VSOL + rsol
    if rsol is None or vsol is None or rsol > vsol + EPS or vsol > MAX_VSOL:
        return None                                 # no real SOL, units mixed up, or not a curve
    if vtok is None:                                # priced from the reading, else pump.fun's k
        vtok = vsol / price if price is not None and price > 0 else K / vsol
    if not MIN_VTOK <= vtok <= MAX_VTOK or vsol < MIN_VSOL:
        return None
    price = vsol / vtok
    slot = _num(r.get("slot"))
    return {"vsol": vsol, "vtok": vtok, "rsol": rsol, "price": price, "complete": _yes(r.get("complete")),
            "slot": int(slot) if slot is not None and slot >= 0 else None}


# ---------------------------------------------------------------- a watched coin

class Coin(object):
    """One new pair: what its create event said, and what the curve readings showed since."""

    FIELDS = {
        "mint": "", "symbol": "", "name": "", "uri": "", "creator": "", "curve": "", "pool": "pump",
        "mayhem": False, "t0": 0.0, "dev_tokens": 0.0, "dev_sol": 0.0, "dev_pct": 0.0,
        "vsol0": START_VSOL, "price0": START_VSOL / START_VTOK, "rsol0": 0.0,
        "vsol": START_VSOL, "vtok": START_VTOK, "rsol": 0.0, "price": START_VSOL / START_VTOK,
        "complete": False, "slot": None, "readings": 0, "last_t": 0.0, "age_s": 0.0,
        "buys": 0, "sells": 0, "sell_max": 0.0, "last_sell": 0.0, "sell_max_held": 0.0, "buy_max": 0.0,
        "meta": None, "serial": 0, "status": WATCHING, "why": "", "trigger_t": None,
        "entry_t": None, "entry_price": None, "exit_t": None, "rug": None, "rearm": False,
    }
    # what from_dict accepts from a hand-edited or broken state.json: every derived number (rise %,
    # market cap, SOL in, ages) stays finite, so the panel's JSON never gets an Infinity
    BOUNDS = {"vsol0": (MIN_VSOL, MAX_VSOL), "vsol": (MIN_VSOL, MAX_VSOL), "vtok": (MIN_VTOK, MAX_VTOK),
              "rsol0": (0.0, MAX_VSOL), "rsol": (0.0, MAX_VSOL), "dev_sol": (0.0, MAX_VSOL),
              "sell_max": (0.0, MAX_VSOL), "last_sell": (0.0, MAX_VSOL), "sell_max_held": (0.0, MAX_VSOL),
              "buy_max": (0.0, MAX_VSOL),
              "dev_tokens": (0.0, 1e12), "dev_pct": (0.0, 1e5), "age_s": (0.0, 2 * MAX_T),
              "buys": (0, MAX_N), "sells": (0, MAX_N), "readings": (0, MAX_N), "serial": (0, MAX_N),
              "slot": (0, 2 ** 63)}
    TIMES = ("t0", "last_t", "trigger_t", "entry_t", "exit_t")
    PRICES = ("price0", "price", "entry_price")

    def __init__(self, **kw):
        for k, v in self.FIELDS.items():
            setattr(self, k, kw.get(k, v.copy() if isinstance(v, dict) else v))
        self.evicted = []           # mints dropped to make room for this coin (unwatch them)

    # what the rules read
    @property
    def sol_in(self):
        """Real SOL gained in the curve since the create (the dev's own buy left out)."""
        return self.rsol - self.rsol0

    @property
    def rise_pct(self):
        return (self.price / self.price0 - 1) * 100 if self.price0 > 0 else 0.0

    @property
    def mcap_sol(self):
        return self.price * SUPPLY

    @property
    def buy_share(self):
        n = self.buys + self.sells
        return self.buys / float(n) if n else 0.0

    def to_dict(self):
        """JSON-safe, for state.json (a held coin survives a restart: Coin.from_dict)."""
        d = {k: getattr(self, k) for k in self.FIELDS}
        d["meta"] = dict(self.meta) if isinstance(self.meta, dict) else None
        d["rug"] = dict(self.rug) if isinstance(self.rug, dict) else None
        return d

    @classmethod
    def from_dict(cls, d):
        """A Coin back from to_dict(); broken fields fall back to the defaults. None when d has no mint."""
        if not isinstance(d, dict) or not _key(d.get("mint")):
            return None
        kw = {}
        for k, v in cls.FIELDS.items():
            x = d.get(k)
            if isinstance(v, bool):
                kw[k] = x if isinstance(x, bool) else v
            elif isinstance(v, str):
                kw[k] = _text(x, 300) if isinstance(x, str) else v
            elif k == "meta":
                kw[k] = socials(x) if isinstance(x, dict) else None     # {"x", "tg", "web", "x_kind"} again
            elif k == "rug":
                kw[k] = _rug_doc(x)
            else:                                   # numbers, and slot / trigger_t / entry_t / ... (None)
                n = _t(x) if k in cls.TIMES else _num(x)
                if n is not None and k in cls.PRICES and not PRICE_MIN <= n <= PRICE_MAX:
                    if k != "entry_price":
                        return None                 # no curve prices like that: rise % would mean nothing
                    n = None
                if n is not None and k in cls.BOUNDS:
                    n = _clamp(n, *cls.BOUNDS[k])
                if n is None:
                    kw[k] = v
                else:
                    kw[k] = int(n) if isinstance(v, int) or k == "slot" else n
        kw["mint"] = _key(d.get("mint"))
        return cls(**kw)


def _reasons(x):
    """A clean list of reason texts (a state.json can hold anything)."""
    return [_text(r, 120) for r in x if isinstance(r, str) and _text(r, 120)][:8] if isinstance(x, list) else []


def _rug_doc(x):
    """coin.rug back from state.json: {"ok": True | False | None, "reasons", "holders", "top1_pct",
    "top10_pct"} with clean values, or None."""
    if not isinstance(x, dict):
        return None
    ok = x.get("ok") if isinstance(x.get("ok"), bool) else None
    d = {"ok": ok, "reasons": _reasons(x.get("reasons")), "holders": int(_clamp(f(x.get("holders")), 0, 1e9)),
         "top1_pct": _clamp(f(x.get("top1_pct")), 0.0, 100.0), "top10_pct": _clamp(f(x.get("top10_pct")), 0.0, 1000.0)}
    if ok is None and x.get("wait") is True:
        d["wait"] = True                            # "waiting: RugCheck sees N holders"
    return d


def _dev_tokens(ev):
    """The creator's initial buy in whole tokens: initialBuy, or from solAmount on a fresh curve."""
    tok = _num(ev.get("initialBuy"))
    if tok is not None and tok >= 0:
        tok = tok / TOKEN_UNITS if tok > START_VTOK else tok      # raw units slipped in
        return min(tok, 1e12)                                     # garbage stays finite (and fails)
    sol = min(MAX_VSOL, max(0.0, f(ev.get("solAmount"))))
    return START_VTOK - K / (START_VSOL + sol)


def coin_from_create(ev, now, cfg=None):
    """(Coin, None) for a PumpPortal create event, (Coin, why) when it is not to be watched, or
    (None, why) when it is not a usable create at all."""
    if not isinstance(ev, dict):
        return None, "not an event"
    mint = _key(ev.get("mint"))
    if not mint:
        return None, "no mint"
    tx = ev.get("txType")
    if tx is not None and tx != "create":
        return None, "not a create"
    rx = _t(ev.get("_rx"))                          # the feed thread's receive time: nearer the launch
    t0 = rx if rx is not None and now - 60 <= rx <= now else now
    c = Coin(mint=mint, symbol=_text(ev.get("symbol"), 16), name=_text(ev.get("name"), 40),
             uri=_text(ev.get("uri"), 300), creator=_key(ev.get("traderPublicKey")),
             curve=_key(ev.get("bondingCurveKey")), pool=_text(ev.get("pool"), 20) or "pump",
             mayhem=_yes(ev.get("is_mayhem_mode")), t0=t0, last_t=t0)
    c.dev_tokens = _dev_tokens(ev)
    c.dev_sol = min(MAX_VSOL, max(0.0, f(ev.get("solAmount"))))
    c.dev_pct = c.dev_tokens / SUPPLY * 100
    r = read_curve(ev)                              # vSolInBondingCurve / vTokensInBondingCurve
    if r is None:
        mcap = _num(ev.get("marketCapSol"))
        vsol = START_VSOL + c.dev_sol
        r = {"vsol": vsol, "vtok": K / vsol, "rsol": c.dev_sol,
             "price": mcap / SUPPLY if mcap is not None and 1.0 <= mcap <= 1e6 else vsol * vsol / K}
    c.vsol0, c.price0, c.rsol0 = r["vsol"], r["price"], r["rsol"]
    c.vsol, c.vtok, c.rsol, c.price = r["vsol"], r["vtok"], r["rsol"], r["price"]
    if c.mayhem and _on(cfg, "np_skip_mayhem"):
        return c, "mayhem mode"
    if not c.symbol or not c.name:
        return c, "no symbol or name"
    if not c.curve:
        return c, "no bonding curve key"
    if c.pool.lower() != "pump":
        return c, "not a pump.fun curve (pool %s)" % c.pool
    return c, None


def apply_reading(coin, r, now):
    """One curve reading (read_curve's form) into the coin. True when it was news: a newer state.
    A reading older than the last one (the HTTP poll and the WebSocket race) is left out, and the
    same state twice only counts as fresh (the stale guard), not as a trade."""
    if coin.complete:
        return False
    if r["slot"] is not None and coin.slot is not None:
        if r["slot"] < coin.slot:
            return False
        if r["slot"] == coin.slot and abs(r["rsol"] - coin.rsol) < MOVE:
            coin.last_t = max(coin.last_t, now)
            return False
    d = r["rsol"] - coin.rsol
    coin.last_sell = 0.0
    if r["complete"] and d < -MOVE:
        # the migration moved the curve's SOL to the AMM: not a sell (no "dump"), and the curve state
        # before it is the last one a held coin can be sold into ("migrating: take it")
        return _completed(coin, r["slot"], now)
    if d > MOVE:
        coin.buys += 1
        coin.buy_max = max(coin.buy_max, d)         # the biggest buy reading (the first one holds the snipers)
    elif d < -MOVE:
        coin.sells += 1
        coin.last_sell = -d
        coin.sell_max = max(coin.sell_max, -d)
        if coin.entry_t is not None:
            coin.sell_max_held = max(coin.sell_max_held, -d)
    coin.vsol, coin.vtok, coin.rsol, coin.price = r["vsol"], r["vtok"], r["rsol"], r["price"]
    coin.complete = coin.complete or r["complete"]
    if r["slot"] is not None:
        coin.slot = r["slot"]
    coin.readings += 1
    coin.last_t = max(coin.last_t, now)
    coin.age_s = max(0.0, now - coin.t0)
    return True


def _completed(coin, slot, now):
    coin.complete = True
    if slot is not None:
        coin.slot = slot if coin.slot is None else max(coin.slot, slot)
    coin.readings += 1
    coin.last_t = max(coin.last_t, now)
    coin.age_s = max(0.0, now - coin.t0)
    return True


# ---------------------------------------------------------------- the watchlist

class Watchlist(object):
    """The new pairs being watched right now, the creators seen this session, and the coins
    skipped at launch (for the panel). Every method takes the time; nothing reads the clock."""

    def __init__(self, cfg=None):
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.coins = {}             # mint -> Coin, oldest first (dicts keep insertion order)
        self.skipped = []           # Coins not watched (mayhem mode, ...), newest last
        self.launches = {}          # creator -> [[t, mint], ...] within np_serial_window_min
        self.seen = {}              # mint -> t of its create: PumpPortal may send a create twice
        self.create_times = []      # for creates per minute
        self.now = 0.0

    def _clock(self, now):
        n = _t(now)
        if n is not None:
            self.now = n
        return self.now

    def get(self, mint):
        return self.coins.get(mint) if isinstance(mint, str) else None

    def _coin(self, x):
        return x if isinstance(x, Coin) and self.coins.get(x.mint) is x else self.get(x)

    # events
    def on_create(self, event, now):
        """A PumpPortal create -> the new Coin (its .evicted: mints to unwatch), or None when it is
        skipped (mayhem mode, no symbol or name, no curve, not pump.fun: kept for the panel), a
        repeat, or not a create at all. The creator's launch is remembered either way."""
        now = self._clock(now)
        coin, why = coin_from_create(event, now, self.cfg)
        if coin is None or coin.mint in self.seen:
            return None
        self.seen[coin.mint] = now
        self.create_times.append(now)
        self._launch(coin, now)
        if why is None:
            room = self._make_room()
            if room is None:
                why = "watchlist full of held coins"
            else:
                coin.evicted = room
        if why is not None:
            coin.status, coin.why = "rejected: " + why, why
            self.skipped.append(coin)
            del self.skipped[:-ROWS]
            return None
        self.coins[coin.mint] = coin
        return coin

    def _launch(self, coin, now):
        """Serial launchers: every coin of a creator who launched more than one in the window gets
        the count of the others (an earlier coin still on the list is flagged too)."""
        if not coin.creator:
            return
        win = _c(self.cfg, "np_serial_window_min") * 60
        hist = [x for x in self.launches.get(coin.creator, []) if now - x[0] <= win and x[1] != coin.mint]
        hist.append([now, coin.mint])
        self.launches[coin.creator] = hist
        coin.serial = len(hist) - 1
        for _, m in hist[:-1]:
            other = self.coins.get(m)
            if other is not None:
                other.serial = max(other.serial, len(hist) - 1)

    def _make_room(self):
        """Mints dropped so one more fits under np_max_watch: rejected or sold coins first, then the
        oldest watched one; a held coin never (its exits need the readings). None: no room."""
        cap = max(1, int(min(1e6, _c(self.cfg, "np_max_watch"))))
        idle = [m for m, c in self.coins.items() if c.status not in (WATCHING, ENTERED)]
        watching = [m for m, c in self.coins.items() if c.status == WATCHING]
        need = len(self.coins) - cap + 1
        if need <= 0:
            return []
        out = (idle + watching)[:need]
        if len(out) < need:                         # held coins fill it: drop nothing (a dropped coin
            return None                             # nobody is told about would stay polled for good)
        for m in out:
            del self.coins[m]
        return out

    def on_curve(self, mint, reading, now):
        """A curve reading for a watched coin -> the Coin when it brought something new (a newer
        state, or a fresh copy of the same state), None otherwise. Readings after the curve
        completed are left out."""
        coin = self.get(mint)
        if coin is None:
            return None
        if isinstance(reading, dict) and reading.get("disc_ok") is False:
            # the account is not a pump.fun bonding curve (another launchpad, or a key we were sent wrong): its
            # bytes read as reserves are nonsense (a "+4050 %" coin in the dry run), so it is never traded
            if coin.status == WATCHING:
                self.reject(coin, "not a pump.fun bonding curve")
            return None
        r = read_curve(reading)
        if r is None:
            # a migrated curve can read with its reserves zeroed (feeds.py still sends it): the
            # complete flag is the news, the last usable state stays the price
            if isinstance(reading, dict) and _yes(reading.get("complete")) and not coin.complete:
                return coin if _completed(coin, None, self._clock(now)) else None
            return None
        return coin if apply_reading(coin, r, self._clock(now)) else None

    def touch(self, now, mints=None):
        """A poll round read these curves (all watched ones when mints is None) and found them
        unchanged: they are fresh for the stale guard. feeds.py sends a curve event only when the
        reserves change, so without this a quiet held coin reads stale after np_stale_s."""
        now = self._clock(now)
        if mints is None:
            coins = list(self.coins.values())
        elif isinstance(mints, (list, tuple, set, frozenset)):
            coins = [self.get(m) for m in mints]
        else:
            coins = []
        n = 0
        for c in coins:
            if c is not None and not c.complete:
                c.last_t = max(c.last_t, now)
                n += 1
        return n

    def on_meta(self, mint, doc):
        """The coin's metadata JSON, or None when it could not be read (every gateway failed and
        RugCheck had nothing either): then the coin counts as having no social link. Call once."""
        coin = self.get(mint)
        if coin is not None:
            coin.meta = socials(doc)
        return coin

    def on_migrate(self, mint, now):
        """PumpPortal's migrate event: the curve completed."""
        coin = self.get(mint)
        if coin is not None:
            self._clock(now)
            coin.complete = True
        return coin

    # the bot's decisions
    def judge(self, coin, now):
        """("reject" | "wait" | "fire", why) on a reading: cheap_check first (a failure rejects the
        coin for good), then the momentum trigger."""
        coin = self._coin(coin)
        if coin is None:
            return "wait", "not watched"
        now = self._clock(now)
        if coin.status != WATCHING:
            return "wait", coin.status
        ok, bad = cheap_check(coin, self.cfg)
        rug = coin.rug if isinstance(coin.rug, dict) else {}
        if ok and rug.get("ok") is False:           # a RugCheck FAIL is final, whoever forgot to say so
            ok, bad = False, (_reasons(rug.get("reasons")) or ["RugCheck: fail"])[:1]
        if not ok:
            self.reject(coin, bad[0])
            return "reject", bad[0]
        first = coin.trigger_t is None
        fire, why = trigger(coin, self.cfg, now)
        coin.why = why
        if not fire:
            return "wait", why
        ok, bad = concentration(coin, self.cfg)
        if not ok:                                  # one buy reading bigger than np_max_buy_sol: never bought
            self.reject(coin, bad)
            return "reject", bad
        if first or coin.rearm or rug.get("ok") is not True:
            coin.rearm = False                      # first fire, retry(), or no RugCheck verdict yet
            return "fire", why
        why = "fired, not bought"                   # it passed RugCheck and the bot said no
        coin.why = why
        return "wait", why

    def retry(self, coin):
        """No buy on this fire for a reason that may pass (the book is full, a pause): the next
        reading where the entry rules hold fires again."""
        coin = self._coin(coin)
        if coin is not None and coin.status == WATCHING:
            coin.rearm = True
        return coin

    def reject(self, coin, why):
        """No entry for this coin (it stays on the list for the panel until it expires)."""
        coin = self._coin(coin)
        if coin is not None and coin.status == WATCHING:
            why = _text(why, 120) or "rejected"
            coin.status, coin.why = "rejected: " + why, why
        return coin

    def mark_entered(self, coin, now, price=None):
        """After the paper buy: sells from now on are the dump exit's, and the coin stays watched."""
        coin = self._coin(coin)
        if coin is not None:
            now = self._clock(now)
            coin.status, coin.entry_t, coin.sell_max_held = ENTERED, now, 0.0
            p = _num(price)
            coin.entry_price = p if p is not None and p > 0 else coin.price
        return coin

    def mark_exited(self, coin, why, now):
        """After the last sale: the coin is no entry candidate again and expires as usual."""
        coin = self._coin(coin)
        if coin is not None:
            coin.exit_t = self._clock(now)
            why = _text(why, 120) or "sold"
            coin.status, coin.why = "exited: " + why, why
        return coin

    def adopt(self, coin):
        """Watch a coin again after a restart (Coin.from_dict of a held position's coin)."""
        if isinstance(coin, Coin) and coin.mint and coin.mint not in self.coins:
            self.coins[coin.mint] = coin
            self.seen[coin.mint] = coin.t0
            return True
        return False

    def drop(self, mint):
        """Stop watching a coin now. True when it was watched."""
        return self.coins.pop(mint, None) is not None if isinstance(mint, str) else False

    def expire(self, now):
        """Mints to unwatch: coins past np_watch_s that are not held, and completed curves (they
        migrated). Held coins stay until mark_exited. Old creator history is forgotten."""
        now = self._clock(now)
        keep = _c(self.cfg, "np_watch_s")
        out = [m for m, c in self.coins.items() if c.status != ENTERED and (now - c.t0 > keep or c.complete)]
        for m in out:
            del self.coins[m]
        self.skipped = [c for c in self.skipped if now - c.t0 <= keep]
        win = _c(self.cfg, "np_serial_window_min") * 60
        for who in list(self.launches):
            hist = [x for x in self.launches[who] if now - x[0] <= win]
            if hist:
                self.launches[who] = hist
            else:
                del self.launches[who]
        self.seen = {m: t for m, t in self.seen.items() if now - t <= max(win, keep) or m in self.coins}
        self.create_times = [t for t in self.create_times if now - t <= 600]
        return out

    # what the bot and the panel read
    def held(self):
        return [m for m, c in self.coins.items() if c.status == ENTERED]

    def curves(self):
        """{bonding curve key: mint} of every watched coin, for one batched getMultipleAccounts."""
        return {c.curve: m for m, c in self.coins.items() if c.curve}

    def creates_per_min(self, now, window_s=300):
        now = self._clock(now)
        w = max(1.0, f(window_s, 300))
        n = sum(1 for t in self.create_times if 0 <= now - t <= w)
        return round(n * 60.0 / w, 1)

    def rows(self, now=None, limit=ROWS):
        """The panel's "watch" list: watched and skipped coins, newest first, at most limit."""
        now = self._clock(now) if now is not None else None
        order = {m: i for i, m in enumerate(self.seen)}        # same second: the later create first
        allc = list(self.coins.values()) + list(self.skipped)
        allc.sort(key=lambda c: (c.t0, order.get(c.mint, -1)), reverse=True)
        return [row(c, now) for c in allc[:max(0, int(f(limit, ROWS)))]]


# ---------------------------------------------------------------- the rules

def cheap_check(coin, cfg=None):
    """(ok, reasons) from what the create event and the curve already told: the creator's initial
    buy at most np_max_dev_pct of the supply, no serial launcher, a social link (once the metadata
    is read), no single sell of np_dump_sol or more since launch. Mayhem mode and an empty symbol
    or name are refused here too (on_create already skips them)."""
    if not isinstance(coin, Coin):
        return False, ["no coin"]
    bad = []
    if coin.mayhem and _on(cfg, "np_skip_mayhem"):
        bad.append("mayhem mode")
    if not coin.symbol or not coin.name:
        bad.append("no symbol or name")
    cap = _c(cfg, "np_max_dev_pct")
    if coin.dev_pct > cap + EPS:
        bad.append("dev bought %.1f%% (max %s%%)" % (coin.dev_pct, _g(cap)))
    if coin.serial > max(0, int(_c(cfg, "np_serial_max"))):
        bad.append("serial launcher: %d more coin%s in %s min" % (
            coin.serial, "" if coin.serial == 1 else "s", _g(_c(cfg, "np_serial_window_min"))))
    if _on(cfg, "np_need_social") and coin.meta is not None and not has_social(coin.meta):
        bad.append("no social link")
    if coin.sell_max >= _c(cfg, "np_dump_sol") - EPS:
        bad.append("dump: %.1f SOL sold" % coin.sell_max)
    return not bad, bad


def _curve_accounts(report, coin):
    """The accounts that are the bonding curve or a pool, not holders: the curve (its token account
    is owned by it), the markets' accounts, and RugCheck's known AMM / curve / locker accounts."""
    out = set()
    if isinstance(coin, Coin) and coin.curve:
        out.add(coin.curve)
    markets = report.get("markets")
    for mk in markets if isinstance(markets, list) else []:
        if not isinstance(mk, dict):
            continue
        for k in ("pubkey", "liquidityA", "liquidityB"):
            out.add(_key(mk.get(k)))
        for k in ("liquidityAAccount", "liquidityBAccount"):
            acc = mk.get(k)
            out.add(_key(acc.get("owner")) if isinstance(acc, dict) else "")
    known = report.get("knownAccounts")
    for acc, info in known.items() if isinstance(known, dict) else []:
        kind = ("%s %s" % (info.get("type") or "", info.get("name") or "")).upper() if isinstance(info, dict) else ""
        if "AMM" in kind or "CURVE" in kind or "LOCKER" in kind:
            out.add(_key(acc))
    out.discard("")
    return out


def _holder_pcts(report, coin):
    """The top holders' % (desc), the curve and pool accounts left out."""
    skip = _curve_accounts(report, coin)
    pcts = []
    for h in report.get("topHolders") if isinstance(report.get("topHolders"), list) else []:
        if not isinstance(h, dict) or _key(h.get("address")) in skip or _key(h.get("owner")) in skip:
            continue
        p = f(h.get("pct"))
        if p > 0:
            pcts.append(min(p, 100.0))
    pcts.sort(reverse=True)
    return pcts


def holder_counts(report, coin=None):
    """What a RugCheck report lists, the account kinds counted: {"holding": accounts with a balance (pct > 0),
    "empty": accounts with none (a wallet that sold out and did not close its account: RugCheck lists them with
    amount 0), "curve": the curve's and the pools' accounts (_curve_accounts), "total": its totalHolders (every
    open token account, the empty ones and the curve's included; 0 while RugCheck has not counted)}."""
    out = {"holding": 0, "empty": 0, "curve": 0, "total": 0}
    if not isinstance(report, dict):
        return out
    skip = _curve_accounts(report, coin)
    for h in report.get("topHolders") if isinstance(report.get("topHolders"), list) else []:
        if not isinstance(h, dict):
            continue
        if _key(h.get("address")) in skip or _key(h.get("owner")) in skip:
            out["curve"] += 1
        elif f(h.get("pct")) > 0:
            out["holding"] += 1
        else:
            out["empty"] += 1
    out["total"] = int(max(0.0, min(1e9, f(report.get("totalHolders")))))
    return out


def rug_holders(report, coin=None):
    """How many WALLETS hold the coin by a RugCheck report. RugCheck lists at most 20 accounts, the biggest first
    (every real report: the empty ones last). So a list that shows an EMPTY account shows every account with a
    balance, and the accounts it does not list are empty too: the count is the holding accounts it lists, however
    many more totalHolders counts (2xhio_0: 14 wallets holding, 5 empty, the curve's; with totalHolders 30 still
    14, not 24). A list with no empty account: its totalHolders less the curve's accounts, or the holding accounts
    it lists when that is more (when it lists all of them, both are the same count; when there are more, the
    accounts it does not list may hold some too, so this is an upper bound; while totalHolders is still 0, the
    list). Real reports of 2026-11-26: 7 accounts, 5 empty, the curve's and 1 wallet holding -> 1, not 7. 0 for no
    report: a brand-new coin's report often lists nothing yet (RugCheck has not indexed it), not "spread out"."""
    if not isinstance(report, dict) or not report.get("mint"):
        return 0
    c = holder_counts(report, coin)
    if c["empty"] > 0:
        return c["holding"]
    return max(c["holding"], c["total"] - c["curve"])


def _short_of_holders(holders, coin, cfg):
    """None when `holders` wallets are enough for the coin's entry, else the "waiting: ..." words: fewer than
    np_min_holders ("waiting: RugCheck sees 3 holders (min 10)"), or more than np_max_sol_per_holder SOL in (since
    the launch) for each of them ("waiting: 6 holders for 3.3 SOL in (0.55 SOL a holder, max 0.5)")."""
    need = int(math.ceil(_c(cfg, "np_min_holders") - EPS))
    if need > 0 and holders < need:
        return "waiting: RugCheck sees %d holder%s (min %d)" % (holders, "" if holders == 1 else "s", need)
    cap = _c(cfg, "np_max_sol_per_holder")
    sol = coin.sol_in if isinstance(coin, Coin) else 0.0
    if cap > 0 and sol > 0 and sol > cap * holders + EPS:
        if holders <= 0:
            return "waiting: RugCheck sees no holder for %.1f SOL in (max %s SOL a holder)" % (sol, _g(cap))
        return "waiting: %d holder%s for %.1f SOL in (%.2f SOL a holder, max %s)" % (
            holders, "" if holders == 1 else "s", sol, sol / holders, _g(cap))
    return None


def rug_cache_s(report, coin=None, cfg=None):
    """How long (s) the bot may keep using this report for the coin's entry before asking RugCheck again:
    np_rug_recheck_s when it leaves the coin waiting (too few holders seen, or too much SOL a holder: RugCheck's
    index catches up within seconds and new buyers come, so the next look must be a fresh report, not the same
    one for 30 s), else np_rug_cache_s. bot.py's np_rug_get() reads it in place of np_rug_cache_s (the prefetch
    keeps the 30 s: only a coin whose trigger holds is asked again, one call every np_rug_recheck_s at most)."""
    keep = max(0.0, _c(cfg, "np_rug_cache_s"))
    if isinstance(report, dict) and report.get("mint") and _short_of_holders(rug_holders(report, coin), coin, cfg):
        return min(keep, max(0.5, _c(cfg, "np_rug_recheck_s")))
    return keep


def rug_waiting(coin):
    """True while the coin's last RugCheck verdict was "waiting: ..." (no FAIL, too few holders seen for its
    entry): the bot asks again after np_rug_recheck_s and may judge the coin as soon as that lands."""
    r = coin.rug if isinstance(coin, Coin) else None
    return isinstance(r, dict) and r.get("ok") is None and r.get("wait") is True


def rug_check_np(report, coin, cfg=None):
    """(ok, reasons, holders) from a RugCheck report for a coin on its curve. ok is None when there
    is no usable report (None, or one for another mint): no entry now, but no reject either. It
    rejects (ok False) a rugged
    coin, a "danger" risk, risk names in np_rug_bad_risks ("Creator history of rugged tokens",
    "Single holder ownership"), one holder other than the curve above np_max_holder_pct, the top
    10 above np_max_top10_pct (the curve and its token account left out), graph insiders.
    A report with none of those that sees too few wallets holding the coin (rug_holders) is no PASS either:
    ok None, "waiting: RugCheck sees 3 holders (min 10)" (its holder checks judged an empty or half-indexed
    list) or "waiting: 6 holders for 3.3 SOL in (0.55 SOL a holder, max 0.5)" (a few wallets hold all the SOL
    that came in: they can sell it back at once); the bot asks again after np_rug_recheck_s (rug_cache_s) while
    the trigger holds.
    pump.fun coins have mint and freeze authority revoked and no LP, so those are not read. The
    holder figures land on coin.rug for the log (rug_text)."""
    if not isinstance(report, dict) or not report.get("mint"):
        return _no_report(coin, "no RugCheck report")
    if isinstance(coin, Coin) and report.get("mint") != coin.mint:
        return _no_report(coin, "RugCheck report is for another coin")
    bad = []
    if _yes(report.get("rugged")):
        bad.append("RugCheck: rugged")
    names = _risk_names(cfg)
    for r in report.get("risks") if isinstance(report.get("risks"), list) else []:
        if not isinstance(r, dict):
            continue
        name = _text(r.get("name"), 80) or "unnamed"
        low = name.lower()
        if str(r.get("level") or "").lower() == "danger" or any(n in low for n in names):
            bad.append("risk: %s" % name)
    pcts = _holder_pcts(report, coin)
    top1, top10 = (pcts[0] if pcts else 0.0), sum(pcts[:10])
    cap1, cap10 = _c(cfg, "np_max_holder_pct"), _c(cfg, "np_max_top10_pct")
    if top1 > cap1 + EPS:
        bad.append("a holder has %.1f%% (max %s%%)" % (top1, _g(cap1)))
    if top10 > cap10 + EPS:
        bad.append("top 10 hold %.0f%% (max %s%%)" % (top10, _g(cap10)))
    insiders = int(max(0.0, f(report.get("graphInsidersDetected"))))
    if insiders > 0:
        bad.append("%d insider%s (graph)" % (insiders, "" if insiders == 1 else "s"))
    holders = rug_holders(report, coin)
    why = None if bad else _short_of_holders(holders, coin, cfg)
    if why is not None:
        if isinstance(coin, Coin):
            coin.rug = {"ok": None, "wait": True, "reasons": [why], "holders": holders,
                        "top1_pct": round(top1, 2), "top10_pct": round(top10, 2)}
        return None, [why], holders
    if isinstance(coin, Coin):
        coin.rug = {"ok": not bad, "reasons": list(bad), "holders": holders,
                    "top1_pct": round(top1, 2), "top10_pct": round(top10, 2)}
    return not bad, bad, holders


def _no_report(coin, why):
    """(None, [why], 0): no verdict. The OVERRIDES: no report in time -> no entry on this reading,
    try again on the next; so ok is None (not False) and the coin is not rejected. A FAIL already
    on the coin stays, and so does a "waiting" verdict (the report that saw too few holders) while
    RugCheck is asked again: the last thing RugCheck said about the coin, for the log and rug_waiting()."""
    if isinstance(coin, Coin) and not (isinstance(coin.rug, dict) and (
            coin.rug.get("ok") is False or rug_waiting(coin))):
        coin.rug = {"ok": None, "reasons": [why], "holders": 0, "top1_pct": 0.0, "top10_pct": 0.0}
    return None, [why], 0


def trigger(coin, cfg=None, now=None):
    """(fire, why): the momentum entry, first in. Fires on the reading where ALL hold, within
    np_max_age_s of the launch: real SOL gained >= np_min_sol, buys >= np_min_buys and >=
    np_min_buy_share of the moves, price >= np_min_rise_pct above the create price, market cap
    <= np_max_mcap_sol. Never on a coin cheap_check refuses, a completed curve, or before the
    socials are read (np_need_social). It is a test of the coin's state (Watchlist.judge decides
    whether a repeat fire is offered to the bot); coin.trigger_t is the time the rules started to
    hold, for "1.2 s after the trigger", and is cleared when they stop holding."""
    if not isinstance(coin, Coin):
        return False, "no coin"
    if coin.status != WATCHING:
        return False, coin.status
    t = _t(now)
    t = coin.last_t if t is None else t
    fire, why = _momentum(coin, cfg, t)
    if not fire:
        coin.trigger_t = None                       # the rules lapsed: a later fire is a new trigger
    elif coin.trigger_t is None:
        coin.trigger_t = t
    return fire, why


def _momentum(coin, cfg, t):
    if coin.readings < 1:
        return False, "no curve reading yet"
    if coin.complete:
        return False, "curve complete"
    ok, bad = cheap_check(coin, cfg)
    if not ok:
        return False, bad[0]
    age = t - coin.t0
    cap_age = _c(cfg, "np_max_age_s")
    if age > cap_age + EPS:
        return False, "too old: %d s (max %s s)" % (math.ceil(age), _g(cap_age))
    min_age = _c(cfg, "np_min_age_s")
    if age < min_age - EPS:
        # the live morning of 2026-11-26: 5 of 6 entries 10-25 s after the launch lost, 3 of them to a dump of
        # 2.9-4.4 SOL within seconds (-25 to -29 %): the launch's own buyers selling into the first momentum
        return False, "too young: %d s (min %s s)" % (int(age), _g(min_age))
    miss = []
    need_sol, need_buys = _c(cfg, "np_min_sol"), int(math.ceil(_c(cfg, "np_min_buys") - EPS))
    need_share, need_rise = _c(cfg, "np_min_buy_share"), _c(cfg, "np_min_rise_pct")
    cap_mcap = _c(cfg, "np_max_mcap_sol")
    if coin.sol_in < need_sol - EPS:
        miss.append("SOL in %.1f/%s" % (coin.sol_in, _g(need_sol)))
    if coin.buys < need_buys:
        miss.append("buys %d/%d" % (coin.buys, need_buys))
    if coin.buy_share < need_share - EPS:
        miss.append("buy share %.0f%%/%.0f%%" % (coin.buy_share * 100, need_share * 100))
    if coin.rise_pct < need_rise - EPS:
        miss.append("rise %+.0f%%/+%s%%" % (coin.rise_pct, _g(need_rise)))
    if coin.mcap_sol > cap_mcap + EPS:
        miss.append("mcap %.0f SOL (max %s)" % (coin.mcap_sol, _g(cap_mcap)))
    if _on(cfg, "np_need_social") and coin.meta is None:
        miss.append("socials not read yet")
    if miss:
        return False, " · ".join(miss)
    return True, "momentum · %.1f SOL in · %d/%d buys (%.0f%%) · %+.0f%% since launch · mcap %.0f SOL · %d s old" % (
        coin.sol_in, coin.buys, coin.buys + coin.sells, coin.buy_share * 100, coin.rise_pct, coin.mcap_sol, age)


def concentration(coin, cfg=None):
    """(ok, why): no single buy READING above np_max_buy_sol ("concentrated: one buy of 2.4 SOL (max 2)"; 0, the
    default: off). Judged when the trigger fires (Watchlist.judge rejects the coin for good on a False). The SOL
    a WALLET put in is RugCheck's to judge (rug_check_np: np_max_sol_per_holder), not the readings': a reading
    is a poll, and the poll rate is not the number of buyers."""
    if not isinstance(coin, Coin):
        return False, "no coin"
    big = _c(cfg, "np_max_buy_sol")
    if big > 0 and coin.buy_max > big + EPS:
        return False, "concentrated: one buy of %.1f SOL (max %s)" % (coin.buy_max, _g(big))
    return True, None


def exit_signal(coin, cfg=None):
    """None, or why a held new pair must be sold at once (first out): one sell of np_dump_sol or
    more since the entry ("dump: 3.1 SOL sold"), or the curve completed ("migrating: take it":
    sold into the last curve state). The hold plan's ladder and stops are plan.py's."""
    if not isinstance(coin, Coin):
        return None
    if coin.entry_t is not None and coin.sell_max_held >= _c(cfg, "np_dump_sol") - EPS:
        return "dump: %.1f SOL sold" % coin.sell_max_held
    if coin.complete:
        return "migrating: take it"
    return None


def stale(coin, cfg=None, now=None):
    """True when a held coin had no fresh reading for np_stale_s: the bot asks Jupiter's price
    once and says so in the log once."""
    if not isinstance(coin, Coin) or coin.status != ENTERED:
        return False
    t = _t(now)
    return t is not None and t - coin.last_t > _c(cfg, "np_stale_s") + EPS


# ---------------------------------------------------------------- the LEARNER, NEWS and the panel

def _bucket(v, edges):
    """The label of the first (limit, label) whose limit v is under; the last label otherwise."""
    for lim, name in edges[:-1]:
        if v < lim - EPS:
            return name
    return edges[-1][1]


# the concentration families (learner.py must list them in KNOWN, or it drops them), both from RugCheck's PASS at
# the entry: the SOL in for each wallet holding the coin, and how many wallets hold it. Both entry rules cut at
# 0.5 SOL and 10 holders by default, so the buckets past the cut ("np:solh:.5-1", "np:holders:<10") only fill
# under a looser config.
NP_SOLHOLD = [(0.25, "np:solh:<.25"), (0.5, "np:solh:.25-.5"), (1.0, "np:solh:.5-1"), (0, "np:solh:1+")]
NP_HOLDERS = [(10, "np:holders:<10"), (20, "np:holders:10-20"), (40, "np:holders:20-40"), (0, "np:holders:40+")]


def labels(coin, meta=None, plan=None, hour=None):
    """The LEARNER's labels for a new pair at entry (stable strings): "np:dev:0-2|2-5|5-8",
    "np:sol:3-6|6-12|12+", "np:buys:70-80|80-90|90+", "np:age:<30s|30-90s|90s+", after RugCheck's PASS
    "np:solh:<.25|.25-.5|.5-1|1+" (SOL in for each wallet holding it) and "np:holders:<10|10-20|20-40|40+"
    (the wallets holding it), one "np:social:x|tg|web" per link or "np:social:none",
    "news:0-2|3-4|5-6|7+" and "tier:<name>" from the hold plan (the entry's), and the learner's hour
    block ("hour:03-05", "hour:day") when the bot passes the hour in Bratislava. Values outside the
    entry rules (a looser config) get "8+", "<3", "<70", ".5-1" or "<10"."""
    out = []
    if isinstance(coin, Coin):
        dev = coin.dev_pct
        out.append("np:dev:" + ("8+" if dev > 8 + EPS else _bucket(dev, [(2, "0-2"), (5, "2-5"), (0, "5-8")])))
        out.append("np:sol:" + _bucket(coin.sol_in, [(3, "<3"), (6, "3-6"), (12, "6-12"), (0, "12+")]))
        out.append("np:buys:" + _bucket(coin.buy_share * 100, [(70, "<70"), (80, "70-80"), (90, "80-90"), (0, "90+")]))
        age = (coin.entry_t - coin.t0) if coin.entry_t is not None else coin.age_s
        out.append("np:age:" + _bucket(age, [(30, "<30s"), (90, "30-90s"), (0, "90s+")]))
        r = coin.rug if isinstance(coin.rug, dict) else {}
        if r.get("ok") is True:
            held = f(r.get("holders"))
            if held > 0:                            # at most the bucket's top (the rule's 0.5 passes: ".25-.5")
                out.append(_bucket(max(0.0, coin.sol_in) / held - 2 * EPS, NP_SOLHOLD))
            out.append(_bucket(held, NP_HOLDERS))
    m = meta if isinstance(meta, dict) else (coin.meta if isinstance(coin, Coin) else None)
    if isinstance(m, dict) and not any(k in m for k in ("x", "tg", "web")):
        m = socials(m)                              # a raw metadata JSON
    got = [k for k in ("x", "tg", "web") if isinstance(m, dict) and _link(m.get(k))]
    out += ["np:social:" + k for k in got] or ["np:social:none"]
    if isinstance(plan, dict):
        s = _num(plan.get("entry_score"))
        s = f(plan.get("score")) if s is None else s
        out.append("news:" + _bucket(s, [(3, "0-2"), (5, "3-4"), (7, "5-6"), (0, "7+")]))
        # the entry's tier with the entry's score (plan.labels() does the same): after an upgrade
        # "tier" is runner while "entry_score" still says 3-4
        out.append("tier:%s" % (_text(plan.get("entry_tier"), 20) or _text(plan.get("tier"), 20) or "?"))
    h = _num(getattr(hour, "hour", hour))          # the local hour (0-23), or a datetime
    if h is not None and abs(h) < 1e6:
        out.append(NIGHT_HOURS.get(int(h) % 24, DAY_HOURS))
    return list(dict.fromkeys(out))


def news_info(coin):
    """plan.news_score()'s input for a new pair: the metadata socials and the curve's momentum.
    The bot adds "boosts" / "trending" when DexScreener's pushes know the coin."""
    if not isinstance(coin, Coin):
        return {}
    m = coin.meta if isinstance(coin.meta, dict) else {}
    return {"twitter": _link(m.get("x")), "twitter_kind": m.get("x_kind"), "telegram": _link(m.get("tg")),
            "website": _link(m.get("web")), "sol_in": round(coin.sol_in, 3), "buys": coin.buys, "sells": coin.sells}


def _href(x):
    """A link for the page: the URL when it is http(s), True for another link text, else False."""
    s = _link(x)
    if not s:
        return False
    return s[:200] if s.lower().startswith(("https://", "http://")) else True


def row(coin, now=None):
    """The panel's "watch" row: {symbol, name, mint, age_s, sol_in, buys, sells, rise_pct, mcap_sol,
    dev_pct, big_sell, x, tg, web, status, why}. status is "watching", "entered", "rejected: why"
    or "exited: why"; x / tg / web are the http(s) link, True for another link, or False."""
    if not isinstance(coin, Coin):
        return {}
    t = _t(now)
    age = max(0.0, t - coin.t0) if t is not None else coin.age_s
    m = coin.meta if isinstance(coin.meta, dict) else {}
    return {"symbol": coin.symbol or "?", "name": coin.name, "mint": coin.mint, "age_s": int(age),
            "sol_in": round(coin.sol_in, 2), "buys": coin.buys, "sells": coin.sells,
            "rise_pct": round(coin.rise_pct, 1), "mcap_sol": round(coin.mcap_sol, 1),
            "dev_pct": round(coin.dev_pct, 1), "big_sell": round(coin.sell_max, 2),
            "x": _href(m.get("x")), "tg": _href(m.get("tg")), "web": _href(m.get("web")),
            "status": coin.status, "why": coin.why}


# ---------------------------------------------------------------- log lines

def watch_text(coin, now=None):
    """SCANNER, when a coin is watched: "new pair · 3 s old · 1.2 SOL in · dev 2.4% · X, Telegram"."""
    if not isinstance(coin, Coin):
        return ""
    t = _t(now)
    age = max(0.0, t - coin.t0) if t is not None else coin.age_s
    if coin.meta is None:
        soc = "socials pending"
    else:
        soc = ", ".join(social_names(coin.meta)) or "no socials"
    return "new pair · %d s old · %.1f SOL in · dev %.1f%% · %s" % (age, coin.sol_in, coin.dev_pct, soc)


def first_in_text(coin, now):
    """EXECUTOR, at the paper fill: "first in · 1.2 s after the trigger · 38 s after launch"."""
    if not isinstance(coin, Coin):
        return "first in"
    t = _t(now)
    t = coin.last_t if t is None else t
    bits = ["first in"]
    if coin.trigger_t is not None:
        bits.append("%.1f s after the trigger" % max(0.0, t - coin.trigger_t))
    bits.append("%d s after launch" % max(0.0, t - coin.t0))
    return " · ".join(bits)


def rug_text(coin):
    """RUG CHECK: "PASS · 14 holders · top holder 6.2% · top 10 21% (curve left out)" or
    "FAIL · risk: Creator history of rugged tokens"."""
    r = coin.rug if isinstance(coin, Coin) else None
    if not isinstance(r, dict):
        return "no RugCheck report"
    if r.get("ok") is None:
        return " · ".join(_reasons(r.get("reasons")) or ["no RugCheck report"])
    if not r.get("ok"):
        return "FAIL · " + " · ".join(_reasons(r.get("reasons")) or ["?"])
    return "PASS · %d holders · top holder %.1f%% · top 10 %.0f%% (curve left out)" % (
        f(r.get("holders")), f(r.get("top1_pct")), f(r.get("top10_pct")))
