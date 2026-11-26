#!/usr/bin/env python3
"""Meme Bot feeds: the real-time inputs for New Pairs, each a daemon thread writing to one queue.Queue.

  PumpPortalFeed  PumpPortal's free stream (wss://pumpportal.fun/api/data): every new pump.fun coin ("create") and
                  every coin that finished its curve ("migrate"). ONE connection, both streams subscribed again on
                  every connect, never a tight reconnect loop (PumpPortal bans for an hour).
  CurvePoller     the bonding curves of the watched coins: one batched getMultipleAccounts over HTTP every 0.5-1 s.
                  The reliable price path (live probe on GitHub Actions: 110/110 calls OK, median 245 ms, p90 638 ms).
  CurveSocket     the same curves over the RPC WebSocket (accountSubscribe): extra pushes only, because the public
                  server drops the connection every 25-40 s. Nothing starts it unless the caller does.
  MetaFetcher     a coin's metadata JSON (X, Telegram, website) from its uri, IPFS gateways in turn.
  Feeds           all of them on one queue, built from the bot's config.

Every feed is optional and never raises into the caller. status() gives {"name", "connected", "events",
"last_event_age_s", "errors", "last_error"} for the panel. The bot keeps working on its polling path when a feed
is down or the 'websockets' package (pip install websockets==17.4) is missing. That package is the only one outside
the standard library.

Events on the queue (dispatch on "kind"):
  {"kind": "create"|"migrate", "txType", ...PumpPortal's own fields..., "_rx": epoch seconds when it arrived}
  {"kind": "curve", "mint", "key", "price_sol", "vtok", "vsol", "rtok", "rsol", "complete", "slot", "t", "src"}
      The reserves are the raw on-chain integers: vsol and rsol in lamports (1e9 per SOL), vtok and rtok in token
      base units (1e6 per token). price_sol is SOL per whole token. src is "poll" or "ws". A curve event comes with
      the first reading of a coin, and after that only when its reserves changed.
  {"kind": "curve_gone", "mint", "key", "t", "src"}   the account read as null 3 times in a row (closed, or not
      there yet). It comes once, and again only after the account has been seen in between.
  {"kind": "meta", "mint", "twitter", "telegram", "website", "description", "x_kind", "ok", "source", "ms",
   "error", "t"}

    python3 memebot/feeds.py --probe 60     # every feed against the real network for 60 s, then a summary
"""

import argparse
import base64
import collections
import concurrent.futures
import email.utils
import hashlib
import http.client
import json
import math
import queue
import random
import socket
import statistics
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:                                    # the one package outside the standard library (pinned in the workflow)
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect as ws_connect
except ImportError:                     # pragma: no cover - depends on the machine
    ws_connect = None

    class ConnectionClosed(Exception):
        pass

PUMPPORTAL_URL = "wss://pumpportal.fun/api/data"
PUMP_SUBS = ({"method": "subscribeNewToken"}, {"method": "subscribeMigration"})     # free; trade streams need a key
RPC_HTTP = "https://api.mainnet-beta.solana.com"
RPC_WS = "wss://api.mainnet-beta.solana.com"
# The live probes from GitHub Actions (2026-11-26, memebot/probe_meta.py): filebase answered 5 of 5 (median 107 ms),
# 4everland 4 of 5; dweb.link, w3s.link, nftstorage.link and ipfs.io answer 403, gateway.pinata.cloud times out,
# cloudflare-ipfs.com no longer resolves. pump.fun's own pinata gateway answers some. The order is the probe's.
IPFS_GATEWAYS = ("https://ipfs.filebase.io/ipfs/", "https://4everland.io/ipfs/", "https://pump.mypinata.cloud/ipfs/",
                 "https://dweb.link/ipfs/", "https://ipfs.io/ipfs/")
UA = "Mozilla/5.0 (meme-bot paper trader)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
NO_WS = "websockets not installed (pip install websockets==17.4)"
CURVE_DISC = hashlib.sha256(b"account:BondingCurve").digest()[:8]     # Anchor's account discriminator
GONE_AFTER = 3                          # nulls in a row before a curve counts as gone (RPC nodes lag a little)
MAX_META = 512 * 1024                   # a metadata JSON is ~1 KB; never read a huge page into memory

FEED_DEFAULTS = {
    "pumpportal_url": PUMPPORTAL_URL,
    "rpc_http": RPC_HTTP,
    "rpc_ws": RPC_WS,
    "curve_poll_sec": 0.5,              # while holding anything
    "curve_idle_sec": 1.0,              # otherwise
    "curve_ws": False,                  # the RPC WebSocket: extra pushes, off unless asked for
    "rpc_keepalive": False,             # one kept-alive HTTPS connection instead of urllib's new one per call
    "meta_workers": 4,
    "meta_timeout_s": 2.0,
    "ipfs_gateways": list(IPFS_GATEWAYS),
    "feed_queue_max": 20000,
}


# ---------------------------------------------------------------- small pure helpers

def f(x, default=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _int(x, default, lo=1):
    """A whole number from config or a caller (None, "4", 4.0 all work), never below lo; never raises."""
    return max(lo, int(f(x, default)))


def _slot(ctx):
    """The slot of an RPC answer's "context", or None. Only a real integer counts: a string or float slot would
    break the newer-or-older comparison in CurveState for good."""
    s = ctx.get("slot") if isinstance(ctx, dict) else None
    return s if isinstance(s, int) and not isinstance(s, bool) else None


def decode_curve(data):
    """A pump.fun bonding-curve account -> {"vtok", "vsol", "rtok", "rsol", "supply", "complete", "price_sol"}, or
    None when the bytes can't be one. Layout (the live probe matched the create event's price exactly): an 8-byte
    Anchor discriminator, then five little-endian u64 (virtual token reserves, virtual SOL reserves, real token
    reserves, real SOL reserves, token supply), then the complete flag at byte 48 (the curve is done and the coin
    migrates). The account is 166 bytes; the rest is not needed. Accepts bytes or a base64 string."""
    if isinstance(data, str):
        try:
            data = base64.b64decode(data, validate=True)
        except ValueError:
            return None
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) < 49:
        return None
    raw = bytes(data[:49])
    vtok, vsol, rtok, rsol, supply = struct.unpack_from("<5Q", raw, 8)
    price = (vsol / 1e9) / (vtok / 1e6) if vtok else 0.0        # SOL per whole token (the coin has 6 decimals)
    return {"vtok": vtok, "vsol": vsol, "rtok": rtok, "rsol": rsol, "supply": supply, "complete": raw[48] != 0,
            "price_sol": price}


def account_bytes(value):
    """An RPC account value {"data": ["<base64>", "base64"], ...} -> bytes; None for a null account; b"" if odd."""
    if value is None:
        return None
    try:
        data = value["data"]
        return base64.b64decode(data[0] if isinstance(data, list) else data)
    except (KeyError, IndexError, TypeError, ValueError):
        return b""


def curve_sig(d):
    return (d["vtok"], d["vsol"], d["rtok"], d["rsol"], d["complete"])


def retry_plan(base, session_s, lo, hi, healthy_s, events=0, productive_s=None):
    """-> (seconds to wait before the next connect, the base after it). A session that stayed up healthy_s resets
    the base to lo, and so does one that streamed events for productive_s (a server that drops a working
    connection every 30-59 s to rebalance must not push the wait to 60 s for the rest of the night); every other
    wait doubles it up to hi. A server that accepts and drops at once, or never sends anything, must never make
    us reconnect in a tight loop."""
    if session_s >= healthy_s or (events > 0 and productive_s is not None and session_s >= productive_s):
        base = lo
    base = min(max(base, lo), hi)
    return base, min(base * 2.0, hi)


def retry_after(value, default=5.0):
    """Seconds to wait from a Retry-After header (seconds or an HTTP date), kept between 1 s and 5 min."""
    s = f(value, -1.0)
    if s < 0 and value:
        try:
            when = email.utils.parsedate_to_datetime(str(value))
            s = when.timestamp() - time.time()
        except (TypeError, ValueError, IndexError, OverflowError):
            s = -1.0
    if s < 0:
        s = default
    return min(max(s, 1.0), 300.0)


def pctl(values, q):
    """Nearest-rank percentile: p90 of 10 values is the 9th, not the largest."""
    v = sorted(values)
    if not v:
        return None
    rank = math.ceil(round(len(v) * q, 9))          # round: 10 * 0.9 is 9.000000000000002 in floats
    return v[min(len(v) - 1, max(0, rank - 1))]


def median(values):
    return statistics.median(values) if values else None


def net_error(e, timeout):
    """A short reason for a failed request: 'timeout after 5 s', 'ConnectionRefusedError: ...'."""
    r = getattr(e, "reason", None) or e
    if isinstance(e, (socket.timeout, TimeoutError)) or isinstance(r, (socket.timeout, TimeoutError)):
        return "timeout after %g s" % timeout
    return "%s: %s" % (type(r).__name__, str(r)[:120])


def closed_text(e):
    """'closed 1002 invalid status code' from a websockets ConnectionClosed (the public RPC closes this way)."""
    for frame in (getattr(e, "rcvd", None), getattr(e, "sent", None)):
        if frame is not None:
            return ("closed %s %s" % (frame.code, frame.reason or "")).strip()
    return "closed: %s" % str(e)[:120]


def _hdrs(h):
    try:
        return {k.lower(): v for k, v in h.items()}
    except AttributeError:
        return {}


def _answer(status, raw, hdr):
    try:
        return status, json.loads(raw.decode("utf-8")), hdr, None
    except ValueError:
        return status, None, hdr, "not JSON (HTTP %s)" % status


def post_json(url, doc, timeout=5.0, ua=UA):
    """POST doc as JSON -> (HTTP status or None, the answer's JSON or None, headers, error text or None). Never
    raises. urllib: a new connection per call, proxies from the environment."""
    try:
        req = urllib.request.Request(url, data=json.dumps(doc).encode("utf-8"), headers={
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _answer(r.status, r.read(), _hdrs(r.headers))
    except urllib.error.HTTPError as e:
        hdr = _hdrs(e.headers)
        try:
            e.close()
        except Exception:               # noqa: BLE001 - only tidying up
            pass
        return e.code, None, hdr, "HTTP %d" % e.code
    except Exception as e:              # noqa: BLE001 - a feed never raises
        return None, None, {}, net_error(e, timeout)


class KeepAlive(object):
    """One kept-alive HTTP(S) connection, for ONE thread: a poll every 0.5 s then skips the TCP + TLS handshake that
    urllib pays on every call. The same answer as post_json; never raises. It ignores proxy settings (none on
    GitHub Actions)."""

    def __init__(self, url, timeout=5.0, ua=UA):
        u = urllib.parse.urlsplit(url)
        self.https = u.scheme == "https"
        self.host, self.port = u.hostname, u.port
        self.path = (u.path or "/") + ("?" + u.query if u.query else "")
        self.timeout, self.ua = timeout, ua
        self.conn = None
        self.opened = 0                 # connections made (1 for a whole night when the server keeps it)

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:           # noqa: BLE001
                pass
            self.conn = None

    def post(self, doc):
        body = json.dumps(doc).encode("utf-8")
        hdr = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": self.ua}
        for attempt in (0, 1):
            fresh = self.conn is None
            try:
                if fresh:
                    cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
                    self.conn = cls(self.host, self.port, timeout=self.timeout)
                    self.opened += 1
                self.conn.request("POST", self.path, body, hdr)
                r = self.conn.getresponse()
                raw, st, h = r.read(), r.status, _hdrs(r.headers)
                if r.will_close:
                    self.close()
                return _answer(st, raw, h) if st == 200 else (st, None, h, "HTTP %d" % st)
            except (http.client.RemoteDisconnected, http.client.CannotSendRequest, ConnectionResetError,
                    BrokenPipeError) as e:
                self.close()            # the server closed a kept connection: once more on a new one
                if fresh or attempt:
                    return None, None, {}, net_error(e, self.timeout)
            except Exception as e:      # noqa: BLE001
                self.close()
                return None, None, {}, net_error(e, self.timeout)
        return None, None, {}, "no answer"      # pragma: no cover


class Window(object):
    """At most `limit` calls in any `span` seconds (the public RPC allows 40 per method per 10 s per IP)."""

    def __init__(self, limit, span, clock=time.monotonic):
        self.limit, self.span, self.clock = max(1, int(limit)), float(span), clock
        self.t = collections.deque()

    def room(self):
        now = self.clock()
        while self.t and now - self.t[0] >= self.span:
            self.t.popleft()
        return len(self.t) < self.limit

    def add(self):
        self.t.append(self.clock())


class CurveState(object):
    """The last reading of every curve, shared by the poller and the socket: an event goes out only when the
    reserves changed, and never for a reading older than the last one (two sources and load-balanced RPC nodes
    deliver out of order; an old reading after a new one would look like the price going back)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last = {}                 # mint -> (slot, signature)

    def fresh(self, mint, sig, slot):
        with self._lock:
            prev = self._last.get(mint)
            if prev is not None:
                pslot, psig = prev
                if slot is not None and pslot is not None and slot < pslot:
                    return False
                if slot is None:
                    slot = pslot        # keep the newest slot known, or a lagging node's reading could pass later
                if sig == psig:
                    self._last[mint] = (slot, psig)
                    return False
            self._last[mint] = (slot, sig)
            return True

    def forget(self, mint):
        with self._lock:
            self._last.pop(mint, None)


# ---------------------------------------------------------------- the feeds

class Feed(object):
    """What every feed shares: the queue, the stop flag, the counters behind status(), and a start() and stop()
    that never raise."""

    name = "feed"

    def __init__(self, out=None, maxsize=20000):
        self.out = out if out is not None else queue.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread = None
        self._ws = None
        self.connected = False
        self.events = 0
        self.errors = 0
        self.last_error = None
        self.last_event = None          # epoch seconds of the last queued event

    def available(self):
        return True

    def start(self):
        try:
            if not self.available() or (self._thread is not None and self._thread.is_alive()):
                return self             # never a second connection (PumpPortal: one at a time)
            self._stop.clear()
            self._thread = threading.Thread(target=self._guarded, name="feed-" + self.name, daemon=True)
            self._thread.start()
        except Exception as e:          # noqa: BLE001
            self._error("start: %s" % e)
        return self

    def _guarded(self):
        try:
            self._run()
        except Exception as e:          # noqa: BLE001 - the loops catch their own; this is the last net
            self._error("stopped by %s: %s" % (type(e).__name__, e))
        finally:
            self.connected = False

    def _run(self):                     # pragma: no cover - every feed has its own
        pass

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout=5.0):
        """Stop the thread and close the socket. -> True when the thread has ended."""
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()              # wakes a blocking recv() at once
            except Exception:           # noqa: BLE001
                pass
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        return not self.running()

    def info(self):
        return {}

    def status(self):
        age = None if self.last_event is None else round(max(0.0, time.time() - self.last_event), 1)
        return {"name": self.name, "connected": bool(self.connected), "events": self.events,
                "last_event_age_s": age, "errors": self.errors, "last_error": self.last_error}

    def _emit(self, ev):
        try:
            self.out.put_nowait(ev)
        except queue.Full:
            self._error("queue full · event dropped")
            return False
        self.events += 1
        self.last_event = time.time()
        return True

    def _error(self, text):
        self.errors += 1
        self.last_error = str(text)[:200]


class _Reconnecting(Feed):
    """A websocket feed: sessions one after the other, with the back-off of retry_plan() and jitter between them."""

    backoff_min, backoff_max, healthy_s = 2.0, 60.0, 60.0
    productive_s = 15.0                 # a session this long that queued events was a working one
    url = ""

    def available(self):
        return ws_connect is not None

    def _connect(self):
        return ws_connect(self.url, open_timeout=8, ping_interval=20, ping_timeout=20, close_timeout=2,
                          max_queue=4096, proxy=self.proxy)

    def _run(self):
        while not self._stop.is_set():
            if not self._wanted():
                self._stop.wait(0.5)
                continue
            before = self.events
            session = self._session()
            if self._stop.is_set():
                break
            wait, self.backoff = retry_plan(self.backoff, session, self.backoff_min, self.backoff_max, self.healthy_s,
                                            self.events - before, self.productive_s)
            wait *= 1.0 + 0.3 * self.rand()             # jitter: never in step with anyone else
            self.waits.append(round(wait, 2))
            self.retry_at = time.time() + wait
            self._stop.wait(wait)
            self.retry_at = None

    def _wanted(self):
        return True

    def _session(self):
        """One connection until it drops, goes silent or stop(). -> how long it was open (s)."""
        opened = None
        try:
            with self._connect() as ws:
                self._ws = ws
                if self._stop.is_set():
                    return 0.0
                opened = time.monotonic()
                self.connects += 1
                self._opened(ws)
                self.connected = True
                self._loop(ws)
        except ConnectionClosed as e:
            if not self._stop.is_set():
                self.drops += 1
                self._error(closed_text(e))
        except Exception as e:          # noqa: BLE001 - OSError, TimeoutError, InvalidHandshake, InvalidURI ...
            if not self._stop.is_set():
                self._error("%s: %s" % (type(e).__name__, str(e)[:150]))
        finally:
            self._ws = None
            self.connected = False
        return time.monotonic() - opened if opened is not None else 0.0

    def _reset(self, proxy, rand):
        self.proxy, self.rand = proxy, rand
        self.backoff = self.backoff_min
        self.connects = self.drops = 0
        self.waits = collections.deque(maxlen=50)       # the waits between sessions (seconds), for tests and probe
        self.retry_at = None
        if ws_connect is None:
            self.last_error = NO_WS


class PumpPortalFeed(_Reconnecting):
    """PumpPortal's free stream: new pump.fun coins and migrations. ONE websocket (their FAQ: one connection at a
    time; repeated reconnects or many connections earn a 1 h ban). Both streams are subscribed again on every
    connect. A reconnect waits at least 2 s plus jitter, doubling to 60 s, and the wait resets after a session
    that lasted 60 s, or 15 s with creates or migrations in it (a working connection the server dropped). No frame
    for 90 s (the silence watchdog) -> reconnect. Ack frames {"message": ...} go to notices, never to the queue."""

    name = "pumpportal"
    MIN_BACKOFF = 2.0                   # a hard floor: whatever the caller passes

    def __init__(self, out=None, url=PUMPPORTAL_URL, silence_s=90.0, backoff_min=2.0, backoff_max=60.0,
                 healthy_s=60.0, proxy=True, rand=random.random, productive_s=15.0):
        Feed.__init__(self, out)
        self.url = url
        self.silence_s = max(0.5, f(silence_s, 90.0))
        self.backoff_min = max(self.MIN_BACKOFF, f(backoff_min, 2.0))
        self.backoff_max = max(self.backoff_min, f(backoff_max, 60.0))
        self.healthy_s = f(healthy_s, 60.0)
        self.productive_s = max(1.0, f(productive_s, 15.0))     # never a reset after a session that dropped at once
        self._reset(proxy, rand)
        self.creates = self.migrates = 0
        self.notices = collections.deque(maxlen=10)
        self._rx = collections.deque(maxlen=5000)       # receive times of creates: creates per minute
        self._first = None

    def _opened(self, ws):
        for sub in PUMP_SUBS:           # a reconnect starts with no subscriptions
            ws.send(json.dumps(sub))
        if self._first is None:
            self._first = time.time()

    def _loop(self, ws):
        last = time.monotonic()
        while not self._stop.is_set():
            try:
                raw = ws.recv(timeout=1.0)      # short, so stop() and the watchdog are checked every second
            except TimeoutError:
                if time.monotonic() - last >= self.silence_s:
                    self._error("silent for %.0f s · reconnecting" % self.silence_s)
                    return
                continue
            last = time.monotonic()
            try:
                self._frame(raw)
            except Exception as e:      # noqa: BLE001 - one odd frame must not cost the connection (a reconnect
                self._error("bad frame: %s: %s" % (type(e).__name__, str(e)[:80]))     # is what PumpPortal bans)

    def _frame(self, raw):
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            self._error("not JSON: %s" % str(raw)[:80])
            return
        if not isinstance(msg, dict):
            self.notices.append(str(msg)[:200])
            return
        tx = msg.get("txType")
        if tx in ("create", "migrate"):
            msg["_rx"] = time.time()
            msg["kind"] = tx
            if tx == "create":
                self.creates += 1
                self._rx.append(msg["_rx"])
            else:
                self.migrates += 1
            self._emit(msg)
        elif tx is None:                # acks and refusals: {"message": "Successfully subscribed ..."}
            text = str(msg.get("message") or msg.get("errors") or msg.get("error") or msg)[:200]
            self.notices.append(text)
            if "error" in msg or "errors" in msg:
                self._error("server: %s" % text)
        # other txTypes (trades need a paid key) are not ours

    def per_min(self, window_s=300.0):
        """New coins per minute over the last window_s (or since the first connect, when that is shorter)."""
        now = time.time()
        n = sum(1 for t in list(self._rx) if now - t <= window_s)
        span = min(window_s, now - self._first) if self._first else window_s
        return round(n / max(span, 1.0) * 60.0, 2)

    def info(self):
        return {"connects": self.connects, "drops": self.drops, "creates": self.creates, "migrates": self.migrates,
                "creates_per_min": self.per_min(), "notices": list(self.notices)[-4:],
                "backoff_s": self.backoff, "retry_in_s": round(self.retry_at - time.time(), 1) if self.retry_at else None}


class _Curves(Feed):
    """What the poller and the socket share: the watched {mint: bonding curve key} set (thread-safe) and the
    change-only curve events."""

    src = "?"

    def __init__(self, out, state, max_keys):
        Feed.__init__(self, out)
        self.state = state if state is not None else CurveState()
        self.max_keys = _int(max_keys, 500)
        self._keys = {}
        self._lock = threading.Lock()

    def watch(self, mint, key):
        """Follow mint's bonding curve (key = bondingCurveKey of its create event). -> True when watched."""
        if not (isinstance(mint, str) and isinstance(key, str) and mint and key):
            return False
        with self._lock:
            if self._keys.get(mint) == key:
                return True
            if mint not in self._keys and len(self._keys) >= self.max_keys:
                self._error("watch list full (%d)" % self.max_keys)
                return False
            self._keys[mint] = key
        self.state.forget(mint)         # a new watch starts with a first reading
        return True

    def unwatch(self, mint):
        with self._lock:
            found = self._keys.pop(mint, None) is not None
        if found:
            self.state.forget(mint)
            self._dropped(mint)
        return found

    def _dropped(self, mint):
        pass

    def watched(self):
        with self._lock:
            return dict(self._keys)

    def _key(self, mint):
        with self._lock:
            return self._keys.get(mint)

    def _push(self, mint, key, d, slot, t):
        if self._key(mint) != key:      # unwatched (or re-keyed) while the answer was on its way
            return 0
        if not self.state.fresh(mint, curve_sig(d), slot):
            return 0
        ev = {"kind": "curve", "mint": mint, "key": key, "price_sol": d["price_sol"], "vtok": d["vtok"],
              "vsol": d["vsol"], "rtok": d["rtok"], "rsol": d["rsol"], "complete": d["complete"], "slot": slot,
              "t": t, "src": self.src}
        if "disc_ok" in d:
            ev["disc_ok"] = d["disc_ok"]
        return 1 if self._emit(ev) else 0


class CurvePoller(_Curves):
    """Every watched curve in ONE batched getMultipleAccounts over HTTP (commitment processed, base64, at most 100
    accounts a call, more calls when more) every interval: 0.5 s while holding, 1 s otherwise (set_interval; the
    caller decides). A 429 pauses for its Retry-After; our own budget stays under the public 40 calls per 10 s per
    method; errors, timeouts and null accounts are counted, never raised."""

    name = "curve-poll"
    src = "poll"

    def __init__(self, out=None, rpc=RPC_HTTP, interval=1.0, timeout=5.0, batch=100, max_calls_10s=36, state=None,
                 keepalive=False, max_keys=500, commitment="processed"):
        _Curves.__init__(self, out, state, max_keys)
        self.rpc = rpc
        self.interval = 1.0
        self.set_interval(interval)
        self.timeout = f(timeout, 5.0)
        self.batch = min(_int(batch, 100), 100)
        max_calls_10s = _int(max_calls_10s, 36)
        self.commitment = commitment
        self.window = Window(max_calls_10s, 10.0)
        self.ka = KeepAlive(rpc, self.timeout) if keepalive else None
        self.paused_until = 0.0         # time.monotonic() before which no call goes out (429)
        self.calls = self.ok = self.limited = self.nulls = self.readings = self.skipped = 0
        self.fail_streak = 0
        self.lat = collections.deque(maxlen=600)        # ms of the last successful calls
        self.last_ms = None
        self.discs = collections.Counter()              # account discriminators seen (the probe checks them)
        self._nulls = {}
        self._gone = set()
        self._turn = 0
        self.connected = False

    def set_interval(self, sec):
        self.interval = min(max(f(sec, 1.0), 0.1), 60.0)

    def stop(self, timeout=5.0):
        done = Feed.stop(self, timeout)
        if done and self.ka is not None:
            self.ka.close()             # only once its thread is gone: the connection is that thread's
        return done

    def _dropped(self, mint):
        with self._lock:
            self._nulls.pop(mint, None)
            self._gone.discard(mint)

    def _run(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.poll()
            except Exception as e:      # noqa: BLE001
                self._error("poll: %s: %s" % (type(e).__name__, e))
            wait = max(self.interval - (time.monotonic() - t0), 0.05)
            wait = max(wait, self.paused_until - time.monotonic(), self._fail_pause())
            self._stop.wait(wait)
        if self.ka is not None:
            self.ka.close()

    def _fail_pause(self):
        """Three failures in a row and the next call waits a little longer (up to 10 s): a sick RPC gets air."""
        n = self.fail_streak
        return 0.0 if n < 3 else min(0.5 * 2 ** (n - 3), 10.0)

    def poll(self):
        """One round over every watched curve. -> the number of curve events queued."""
        if time.monotonic() < self.paused_until:
            return 0
        items = sorted(self.watched().items())
        if not items:
            self.fail_streak = 0        # nothing to ask: no reason to hold the next first reading back
            return 0
        chunks = [items[i:i + self.batch] for i in range(0, len(items), self.batch)]
        first = self._turn % len(chunks)                # round robin: a tail cut by the budget goes first next time
        self._turn += 1
        n = 0
        for chunk in chunks[first:] + chunks[:first]:
            if not self.window.room():
                self.skipped += 1
                break
            self.window.add()
            try:
                n += self._call(chunk)
            except Exception as e:      # noqa: BLE001 - an answer of an odd shape: counted, never raised
                self._fail("odd answer: %s: %s" % (type(e).__name__, str(e)[:100]))
            if time.monotonic() < self.paused_until:
                break
        return n

    def _post(self, body):
        return self.ka.post(body) if self.ka is not None else post_json(self.rpc, body, self.timeout)

    def _call(self, chunk):
        self.calls += 1
        body = {"jsonrpc": "2.0", "id": self.calls, "method": "getMultipleAccounts",
                "params": [[k for _, k in chunk], {"encoding": "base64", "commitment": self.commitment}]}
        t0 = time.monotonic()
        st, doc, hdr, err = self._post(body)
        ms = (time.monotonic() - t0) * 1000.0
        rpc_err = doc.get("error") if isinstance(doc, dict) else None
        code = rpc_err.get("code") if isinstance(rpc_err, dict) else None
        if st == 429 or code in (429, -32429):
            return self._limited(hdr)
        if err or not isinstance(doc, dict):
            return self._fail(err or "HTTP %s" % st)
        if rpc_err is not None:
            return self._fail("rpc error %s" % str(rpc_err)[:120])
        res = doc.get("result")
        vals = res.get("value") if isinstance(res, dict) else None
        if not isinstance(vals, list) or len(vals) != len(chunk):
            return self._fail("odd answer: %s" % str(doc)[:100])
        self.ok += 1
        self.fail_streak = 0
        self.connected = True
        self.lat.append(ms)
        self.last_ms = round(ms)
        slot = _slot(res.get("context"))
        t = time.time()
        return sum(self._reading(mint, key, v, slot, t) for (mint, key), v in zip(chunk, vals))

    def _limited(self, hdr):
        self.limited += 1
        wait = retry_after(hdr.get("retry-after"), default=min(5.0 * 2 ** min(self.fail_streak, 4), 60.0))
        self.fail_streak += 1
        self.paused_until = time.monotonic() + wait
        self.connected = False
        self._error("HTTP 429 · paused %.0f s" % wait)
        return 0

    def _fail(self, text):
        self.fail_streak += 1
        self.connected = False
        self._error(text)
        return 0

    def _reading(self, mint, key, value, slot, t):
        raw = account_bytes(value)
        if raw is None:
            return self._null(mint, key, t)
        with self._lock:
            self._nulls.pop(mint, None)
            self._gone.discard(mint)
        d = decode_curve(raw)
        if d is not None:
            d["disc_ok"] = bytes(raw[:8]) == CURVE_DISC
        if d is None:
            self._error("%s: not a curve account (%d bytes)" % (mint[:8], len(raw)))
            return 0
        if len(self.discs) < 20 or raw[:8].hex() in self.discs:
            self.discs[raw[:8].hex()] += 1
        self.readings += 1
        return self._push(mint, key, d, slot, t)

    def _null(self, mint, key, t):
        self.nulls += 1
        with self._lock:
            if self._keys.get(mint) != key:
                return 0
            n = self._nulls[mint] = self._nulls.get(mint, 0) + 1
            if n < GONE_AFTER or mint in self._gone:
                return 0
            self._gone.add(mint)
        self.state.forget(mint)         # if it comes back, even unchanged, that is a first reading the caller sees
        self._emit({"kind": "curve_gone", "mint": mint, "key": key, "t": t, "src": self.src})
        return 0

    def info(self):
        ms = list(self.lat)
        return {"calls": self.calls, "ok": self.ok, "rate_limited": self.limited, "nulls": self.nulls,
                "readings": self.readings, "over_budget": self.skipped, "interval_s": self.interval,
                "watched": len(self.watched()), "last_ms": self.last_ms,
                "ms_median": round(median(ms)) if ms else None, "ms_p90": round(pctl(ms, 0.9)) if ms else None,
                "keepalive": self.ka is not None, "connections": self.ka.opened if self.ka is not None else None}


class CurveSocket(_Curves, _Reconnecting):
    """The same curves over the RPC WebSocket (accountSubscribe, base64, processed): a push on every change, often
    sooner than the next poll. The public server drops the connection every 25-40 s (live probe), so: reconnect
    after at least 1 s, resubscribe everything, at most 40 subscribe calls per 10 s, and never the only source.
    It only connects while something is watched."""

    name = "curve-ws"
    src = "ws"

    def __init__(self, out=None, url=RPC_WS, state=None, backoff_min=1.0, backoff_max=30.0, healthy_s=20.0,
                 sub_limit=40, sub_window_s=10.0, max_keys=500, proxy=True, rand=random.random):
        _Curves.__init__(self, out, state, max_keys)
        self.url = url
        self.backoff_min = max(1.0, f(backoff_min, 1.0))
        self.backoff_max = max(self.backoff_min, f(backoff_max, 30.0))
        self.healthy_s = f(healthy_s, 20.0)    # its sessions last 25-40 s at best: that is healthy here
        self._reset(proxy, rand)
        self.window = Window(sub_limit, sub_window_s)
        self.sent = self.subscribed = self.pushes = 0
        self._failed = {}               # mint -> time.monotonic() before which no new subscribe is tried
        self._rid = 0

    def _wanted(self):
        return bool(self.watched())

    def _opened(self, ws):
        self._s = {"asked": {}, "subs": {}, "sub_of": {}, "pending": {}, "unsub": []}

    def _loop(self, ws):
        s = self._s
        while not self._stop.is_set():
            self._sync(ws, s)
            try:
                raw = ws.recv(timeout=0.25)
            except TimeoutError:
                continue
            try:
                self._frame(raw, s)
            except Exception as e:      # noqa: BLE001 - one odd frame must not drop every subscription
                self._error("bad frame: %s: %s" % (type(e).__name__, str(e)[:80]))

    def _send(self, ws, s, method, params, tag):
        self._rid += 1
        s["pending"][self._rid] = tag
        self.window.add()
        self.sent += 1
        ws.send(json.dumps({"jsonrpc": "2.0", "id": self._rid, "method": method, "params": params}))

    def _sync(self, ws, s):
        """Bring the server's subscriptions to the watched set, within the subscribe budget."""
        want = self.watched()
        for mint, key in list(s["asked"].items()):
            if want.get(mint) != key:
                del s["asked"][mint]
                sid = s["subs"].pop(mint, None)
                if sid is not None:
                    s["sub_of"].pop(sid, None)
                    s["unsub"].append(sid)
        while s["unsub"] and self.window.room():
            self._send(ws, s, "accountUnsubscribe", [s["unsub"].pop(0)], None)
        now = time.monotonic()
        for mint, key in sorted(want.items()):
            if mint in s["asked"] or self._failed.get(mint, 0.0) > now:
                continue
            if not self.window.room():
                break
            s["asked"][mint] = key
            self._send(ws, s, "accountSubscribe", [key, {"encoding": "base64", "commitment": "processed"}],
                       (mint, key))

    def _frame(self, raw, s):
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            self._error("not JSON: %s" % str(raw)[:80])
            return
        if not isinstance(msg, dict):
            return
        rid = msg.get("id")
        if rid is not None and rid in s["pending"]:
            self._ack(s, s["pending"].pop(rid), msg)
        elif msg.get("method") == "accountNotification":
            self._note(s, msg.get("params") or {})

    def _ack(self, s, tag, msg):
        if tag is None:                 # an unsubscribe answered
            return
        mint, key = tag
        sid = msg.get("result")
        if "error" in msg or not isinstance(sid, int) or isinstance(sid, bool):
            self._error("subscribe %s: %s" % (mint[:8], str(msg.get("error"))[:100]))
            if s["asked"].get(mint) == key:
                del s["asked"][mint]
            self._failed[mint] = time.monotonic() + 30.0
            return
        if s["asked"].get(mint) != key:  # unwatched while the answer was on its way
            s["unsub"].append(sid)
            return
        self.subscribed += 1
        s["subs"][mint] = sid
        s["sub_of"][sid] = (mint, key)

    def _note(self, s, p):
        tag = s["sub_of"].get(p.get("subscription"))
        res = p.get("result")
        if not tag or not isinstance(res, dict):
            return
        raw = account_bytes(res.get("value"))
        if raw is None:                 # a closed account: the poller reports curve_gone
            return
        d = decode_curve(raw)
        if d is not None:
            d["disc_ok"] = bytes(raw[:8]) == CURVE_DISC
        if d is None:
            self._error("%s: not a curve account" % tag[0][:8])
            return
        self.pushes += 1
        self._push(tag[0], tag[1], d, _slot(res.get("context")), time.time())

    def _dropped(self, mint):
        self._failed.pop(mint, None)

    def info(self):
        return {"connects": self.connects, "drops": self.drops, "subscribe_calls": self.sent,
                "subscribed": self.subscribed, "pushes": self.pushes, "watched": len(self.watched()),
                "backoff_s": self.backoff}


# ---------------------------------------------------------------- metadata

def ipfs_path(uri):
    """'<cid>[/path]' of an IPFS uri (ipfs://<cid>, https://<any host>/ipfs/<cid>..., https://<cid>.ipfs.<host>/...),
    else None."""
    if not isinstance(uri, str):
        return None
    u = uri.strip()
    if u.lower().startswith("ipfs://"):
        rest = u[7:]
        rest = rest[5:] if rest.lower().startswith("ipfs/") else rest
        return rest.split("?")[0].split("#")[0] or None
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return None
    if p.scheme not in ("http", "https"):
        return None
    if "/ipfs/" in p.path:
        return p.path.split("/ipfs/", 1)[1] or None
    host = p.hostname or ""
    if ".ipfs." in host:
        cid = host.split(".ipfs.")[0]
        return cid + (p.path if p.path not in ("", "/") else "")
    return None


def x_kind(link):
    """What an X/twitter link points at: "profile" (full social proof), "post" (a status, often someone else's:
    weak), "community", "other" (not an X profile), or "" (none)."""
    s = (link or "").strip() if isinstance(link, str) else ""
    if not s:
        return ""
    if s.startswith("@") and "/" not in s:
        return "profile" if len(s) > 1 else "other"
    try:                                # the link is whatever the coin's creator typed: "http://[" must not raise
        p = urllib.parse.urlsplit(s if "://" in s else "https://" + s)
    except ValueError:
        return "other"
    host = (p.hostname or "").lower()
    if host.startswith("www.") or host.startswith("mobile."):
        host = host.split(".", 1)[1]
    if host not in ("x.com", "twitter.com"):
        return "other"
    parts = [x for x in p.path.split("/") if x]
    if "status" in parts:
        return "post"
    if parts[:2] == ["i", "communities"]:
        return "community"
    return "profile" if len(parts) == 1 and parts[0] not in ("i", "search", "home", "hashtag") else "other"


def socials(doc):
    """twitter / telegram / website / description from a coin's metadata JSON (top level, or under "extensions",
    "properties", "links" or "socials", as different launch sites write them)."""
    out = {"twitter": "", "telegram": "", "website": "", "description": ""}
    if not isinstance(doc, dict):
        return out
    places = [doc] + [doc[k] for k in ("extensions", "properties", "links", "socials") if isinstance(doc.get(k), dict)]
    names = {"twitter": ("twitter", "x", "twitter_url"), "telegram": ("telegram", "tg", "telegram_url"),
             "website": ("website", "web", "site", "external_url"), "description": ("description",)}
    for field, keys in names.items():
        for place in places:
            v = next((place[k] for k in keys if isinstance(place.get(k), str) and place[k].strip()), "")
            if v:
                out[field] = v.strip()[:300 if field == "description" else 200]
                break
    return out


def get_json(url, timeout, ua=BROWSER_UA):
    """GET a JSON document -> (doc or None, error text or None). Never raises. The timeout also bounds the whole
    body, not only each socket read: the uri is the coin creator's choice, and a host that trickles a byte a second
    would otherwise hold a pool worker for minutes (and the bot's exit, the pool threads are joined)."""
    end = time.monotonic() + timeout
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json,text/plain,*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = _read_until(r, end)
    except urllib.error.HTTPError as e:
        try:
            e.close()
        except Exception:               # noqa: BLE001 - only tidying up
            pass
        return None, "HTTP %d" % e.code
    except Exception as e:              # noqa: BLE001 - also ValueError for an odd url
        return None, net_error(e, timeout)
    if raw is None:
        return None, "too slow (over %g s)" % timeout
    if len(raw) > MAX_META:
        return None, "too big"
    try:
        return json.loads(raw.decode("utf-8", "replace")), None
    except (ValueError, RecursionError):
        return None, "not JSON"


def _read_until(r, end):
    """The body (MAX_META + 1 bytes at most), or None once time.monotonic() passed `end`. read1 is one socket read
    at a time, so the deadline is checked between reads."""
    parts, n = [], 0
    while n <= MAX_META:
        if time.monotonic() > end:
            return None
        chunk = r.read1(min(65536, MAX_META + 1 - n))
        if not chunk:
            break
        parts.append(chunk)
        n += len(chunk)
    return b"".join(parts)


def _where(url):
    p = urllib.parse.urlsplit(url)
    return p.netloc + (p.path.split("/ipfs/")[0] if "/ipfs/" in p.path else "")


class MetaFetcher(Feed):
    """A coin's metadata (its uri -> JSON with twitter / telegram / website), fetched in a small thread pool. For an
    IPFS uri the gateways are tried in turn with short timeouts, the one that answered last goes first. Results
    are cached per mint and queued as "meta" events. Never raises."""

    name = "metadata"

    def __init__(self, out=None, workers=4, timeout=2.0, budget_s=6.0, gateways=IPFS_GATEWAYS, ua=BROWSER_UA,
                 cache_max=3000):
        Feed.__init__(self, out)
        self.workers = _int(workers, 4)
        self.timeout = f(timeout, 2.0) if f(timeout, 2.0) > 0 else 2.0
        self.budget_s = max(f(budget_s, 6.0), self.timeout)
        if isinstance(gateways, str):   # one gateway written as a string, not a list of its characters
            gateways = [gateways]
        gws = [g.strip() for g in (gateways if isinstance(gateways, (list, tuple)) else ()) if isinstance(g, str)]
        self.gateways = [g if g.endswith("/") else g + "/" for g in (gws or IPFS_GATEWAYS) if g]
        self.ua = ua
        self.good = None                # the gateway that answered last
        self.gw = {g: {"ok": 0, "fail": 0, "last": None} for g in self.gateways}
        self.cache_max = _int(cache_max, 3000)
        self.fetched = self.found = 0
        self.ms = collections.deque(maxlen=300)
        self._jobs = collections.OrderedDict()          # mint -> Future
        self._lock = threading.Lock()
        self._pool = None

    def start(self):
        try:
            with self._lock:
                if self._pool is None:
                    self._pool = concurrent.futures.ThreadPoolExecutor(self.workers, thread_name_prefix="meta")
        except Exception as e:          # noqa: BLE001
            self._error("start: %s" % e)
        return self

    def running(self):
        return self._pool is not None

    def stop(self, timeout=None):
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        return True

    def order(self):
        """The gateways to try, the last good one first."""
        g = self.good
        return ([g] if g in self.gateways else []) + [x for x in self.gateways if x != g]

    def submit(self, mint, uri):
        """Fetch mint's metadata in the pool (once per mint). -> the Future (its result is fetch()'s), or None."""
        try:
            with self._lock:
                old = self._jobs.get(mint)
                if old is not None and not old.cancelled():    # cancelled by stop(): never fetched, so ask again
                    return old
                if self._pool is None:
                    self._pool = concurrent.futures.ThreadPoolExecutor(self.workers, thread_name_prefix="meta")
                fut = self._pool.submit(self.fetch, uri)
                self._jobs[mint] = fut
                while len(self._jobs) > self.cache_max:
                    self._jobs.popitem(last=False)
            fut.add_done_callback(lambda fu, m=mint: self._done(m, fu))
            return fut
        except Exception as e:          # noqa: BLE001 - e.g. the pool was shut down
            self._error("submit: %s" % e)
            return None

    def _done(self, mint, fut):
        if fut.cancelled():
            return
        try:
            res = fut.result()
        except Exception as e:          # noqa: BLE001 - fetch() never raises; still
            res = dict(socials(None), ok=False, source=None, ms=0, error=str(e)[:120], x_kind="")
        ev = dict(res, kind="meta", mint=mint, t=time.time())
        self._emit(ev)

    def result(self, mint):
        """The finished result for mint, or None (not asked, or still on its way)."""
        with self._lock:
            fut = self._jobs.get(mint)
        if fut is None or not fut.done() or fut.cancelled():
            return None
        try:
            return fut.result()
        except Exception:               # noqa: BLE001
            return None

    def fetch(self, uri):
        """-> {"twitter", "telegram", "website", "description", "x_kind", "ok", "source", "ms", "error"}."""
        t0 = time.monotonic()
        res = dict(socials(None), x_kind="", ok=False, source=None, ms=0, error=None)
        try:
            self._fetch(uri, t0, res)
        except Exception as e:          # noqa: BLE001
            res["error"] = "%s: %s" % (type(e).__name__, e)
        res["ms"] = int((time.monotonic() - t0) * 1000)
        with self._lock:                # several workers count at once
            self.fetched += 1
            self.ms.append(res["ms"])
            self.connected = res["ok"]
            if res["ok"]:
                self.found += 1
            else:
                self._error(res["error"] or "no metadata")
        return res

    def _fetch(self, uri, t0, res):
        if not isinstance(uri, str) or not uri.strip():
            res["error"] = "no uri"
            return
        path = ipfs_path(uri)
        if not path and not uri.strip().lower().startswith(("http://", "https://")):
            res["error"] = "not an http uri"   # the uri comes from strangers: never file:// or ftp://
            return
        tries = [(g, g + path) for g in self.order()] if path else [(None, uri.strip())]
        for gw, url in tries:
            left = self.budget_s - (time.monotonic() - t0)
            if left < 0.1:
                res["error"] = (res["error"] or "") + " · out of time"
                return
            doc, err = get_json(url, min(self.timeout, left), self.ua)
            if gw is not None:
                with self._lock:
                    g = self.gw[gw]
                    g["ok" if isinstance(doc, dict) else "fail"] += 1
                    g["last"] = "ok" if isinstance(doc, dict) else err
            if isinstance(doc, dict):
                if gw is not None:
                    self.good = gw
                res.update(socials(doc), ok=True, source=_where(url), error=None)
                res["x_kind"] = x_kind(res["twitter"])
                return
            res["error"] = "%s: %s" % (_where(url), err or "not a JSON object")

    def info(self):
        ms = list(self.ms)
        return {"fetched": self.fetched, "ok": self.found, "ms_median": round(median(ms)) if ms else None,
                "preferred": self.good, "gateways": {k: dict(v) for k, v in self.gw.items()}}


# ---------------------------------------------------------------- the bundle the bot runs

class Feeds(object):
    """PumpPortal + the curve poller (+ the curve socket when cfg["curve_ws"]) + metadata on ONE queue, from the
    bot's config (the FEED_DEFAULTS keys). Every method is safe to call when a feed is missing or down."""

    def __init__(self, cfg=None, out=None):
        c = dict(FEED_DEFAULTS)
        c.update({k: v for k, v in (cfg if isinstance(cfg, dict) else {}).items() if k in FEED_DEFAULTS})
        self.cfg = c
        self.q = out if out is not None else queue.Queue(maxsize=_int(c["feed_queue_max"], 20000))
        self.state = CurveState()
        self.pump = PumpPortalFeed(self.q, url=c["pumpportal_url"])
        self.poller = CurvePoller(self.q, rpc=c["rpc_http"], interval=c["curve_idle_sec"], state=self.state,
                                  keepalive=bool(c["rpc_keepalive"]))
        self.socket = CurveSocket(self.q, url=c["rpc_ws"], state=self.state) if c["curve_ws"] else None
        self.meta = MetaFetcher(self.q, workers=c["meta_workers"], timeout=c["meta_timeout_s"],
                                gateways=c["ipfs_gateways"])
        self.holding = False

    def feeds(self):
        return [x for x in (self.pump, self.poller, self.socket, self.meta) if x is not None]

    def start(self):
        for x in self.feeds():
            x.start()
        return self

    def stop(self, timeout=3.0):
        for x in self.feeds():
            x._stop.set()               # all at once, then wait
        return all([x.stop(timeout) for x in self.feeds()])

    def get(self, timeout=0.25):
        """The next event, or None after timeout seconds (the main loop waits here instead of sleeping)."""
        try:
            return self.q.get(timeout=timeout) if timeout and timeout > 0 else self.q.get_nowait()
        except queue.Empty:
            return None

    def drain(self, limit=1000):
        out = []
        while len(out) < limit:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                break
        return out

    def watch(self, mint, key):
        ok = self.poller.watch(mint, key)
        if self.socket is not None and ok:
            self.socket.watch(mint, key)
        return ok

    def unwatch(self, mint):
        if self.socket is not None:
            self.socket.unwatch(mint)
        return self.poller.unwatch(mint)

    def watched(self):
        return self.poller.watched()

    def set_holding(self, holding):
        """Poll every curve_poll_sec (0.5) while anything is held, else every curve_idle_sec (1.0)."""
        self.holding = bool(holding)
        self.poller.set_interval(self.cfg["curve_poll_sec"] if holding else self.cfg["curve_idle_sec"])

    def fetch_meta(self, mint, uri):
        return self.meta.submit(mint, uri)

    def status(self):
        return [x.status() for x in self.feeds()]

    def text(self):
        """A short line for the page: 'pumpportal live · curve poll 0.5 s · 12 watched · ws off'."""
        p, c, w = self.pump, self.poller, self.socket
        if not p.available():
            pp = "pumpportal off (no websockets)"
        else:
            pp = "pumpportal live" if p.connected else "pumpportal down"
        cp = ("curve poll %g s" % c.interval) if c.connected or not c.calls else "curve poll failing"
        ws = "ws off" if w is None else ("ws live" if w.connected else "ws down")
        return "%s · %s · %d watched · %s" % (pp, cp, len(self.watched()), ws)


# ---------------------------------------------------------------- the live probe (python3 memebot/feeds.py --probe 60)

class _Sink(object):
    """A queue that only counts (the keep-alive comparison poller's events are not needed)."""

    def __init__(self):
        self.n = 0

    def put_nowait(self, ev):
        self.n += 1


def probe(seconds=60.0, follow=20, meta_n=10, use_ws=True, compare=True, interval=1.0, pumpportal_url=PUMPPORTAL_URL,
          rpc=RPC_HTTP, rpc_ws=RPC_WS, gateways=IPFS_GATEWAYS, out=print, progress_s=15.0):
    """Every feed against the network for `seconds`: new coins from PumpPortal, the newest `follow` curves polled
    (and on the socket), the metadata of the first `meta_n`. -> a summary dict (also printed)."""
    q = queue.Queue(maxsize=100000)
    pump = PumpPortalFeed(q, url=pumpportal_url)
    poller = CurvePoller(q, rpc=rpc, interval=interval)
    ka = CurvePoller(_Sink(), rpc=rpc, interval=interval, keepalive=True) if compare else None
    sock = CurveSocket(q, url=rpc_ws) if use_ws else None              # its own state: every push of its own counts
    meta = MetaFetcher(q, gateways=gateways)
    feeds = [x for x in (pump, poller, ka, sock, meta) if x is not None]
    rec = {"creates": [], "migrates": [], "first": {}, "last": {}, "n": collections.Counter(), "meta": [],
           "check": [], "lag": [], "gone": 0, "px0": {}, "rx": {}, "asked": 0, "order": collections.deque()}
    if not pump.available():
        out("pumpportal: %s · no new coins, so nothing to follow" % NO_WS)
    for x in feeds:
        x.start()
    t0 = time.monotonic()
    tick = t0 + progress_s
    while time.monotonic() - t0 < seconds:
        try:
            ev = q.get(timeout=0.25)
        except queue.Empty:
            ev = None
        if ev is not None:
            _probe_event(ev, rec, [w for w in (poller, ka, sock) if w is not None], meta, follow, meta_n)
        if time.monotonic() >= tick:
            tick += progress_s
            out("+%3.0fs creates %d · migrates %d · watched %d · curve poll %d ws %d · meta %d · errors %s" % (
                time.monotonic() - t0, len(rec["creates"]), len(rec["migrates"]), len(poller.watched()),
                rec["n"]["poll"], rec["n"]["ws"], len(rec["meta"]),
                {x.name + ("-ka" if x is ka else ""): x.errors for x in feeds if x.errors}))
    for x in feeds:
        x._stop.set()
    for x in feeds:
        x.stop(3.0)
    summary = _probe_summary(seconds, rec, pump, poller, ka, sock, meta)
    for line in _probe_lines(summary):
        out(line)
    out("SUMMARY " + json.dumps(summary, default=str))
    return summary


def _probe_event(ev, rec, watchers, meta, follow, meta_n):
    kind = ev.get("kind")
    if kind == "create":
        rec["creates"].append(ev)
        mint, key = ev.get("mint"), ev.get("bondingCurveKey")
        if mint and key:
            rec["px0"][mint] = f(ev.get("vSolInBondingCurve")) / (f(ev.get("vTokensInBondingCurve")) or 1.0)
            rec["rx"][mint] = f(ev.get("_rx"))
            for w in watchers:
                w.watch(mint, key)
            rec["order"].append(mint)
            while len(rec["order"]) > follow:
                old = rec["order"].popleft()
                for w in watchers:
                    w.unwatch(old)
        if mint and ev.get("uri") and rec["asked"] < meta_n:
            rec["asked"] += 1
            meta.submit(mint, ev["uri"])
    elif kind == "migrate":
        rec["migrates"].append(ev)
    elif kind == "curve":
        rec["n"][ev["src"]] += 1
        mint = ev["mint"]
        if mint not in rec["first"]:
            rec["first"][mint] = ev
            if rec["px0"].get(mint):
                rec["check"].append(abs(ev["price_sol"] / rec["px0"][mint] - 1.0) * 100.0)
            if rec["rx"].get(mint):
                rec["lag"].append(ev["t"] - rec["rx"][mint])
        rec["last"][mint] = ev
    elif kind == "curve_gone":
        rec["gone"] += 1
    elif kind == "meta":
        rec["meta"].append(ev)


def _probe_summary(seconds, rec, pump, poller, ka, sock, meta):
    per_min = 60.0 / max(seconds, 1.0)
    first_create = rec["creates"][0] if rec["creates"] else {}
    coins = []
    by_mint = {e.get("mint"): e for e in rec["creates"]}
    for mint, last in list(rec["last"].items())[:12]:
        first = rec["first"][mint]
        coins.append({"symbol": (by_mint.get(mint) or {}).get("symbol"), "mint": mint,
                      "rsol_first": round(first["rsol"] / 1e9, 3), "rsol_last": round(last["rsol"] / 1e9, 3),
                      "price_change_pct": round((last["price_sol"] / (first["price_sol"] or 1.0) - 1) * 100, 1),
                      "complete": last["complete"]})
    m = rec["meta"]
    src = collections.Counter(e.get("source") for e in m if e.get("ok"))
    chk = rec["check"]
    return {
        "seconds": seconds,
        "pumpportal": dict(pump.status(), **pump.info(), creates_per_min_probe=round(len(rec["creates"]) * per_min, 2),
                           migrates_per_min=round(len(rec["migrates"]) * per_min, 2),
                           create_fields=sorted(k for k in first_create if not k.startswith("_"))),
        "curve_poll": dict(poller.status(), **poller.info()),
        "curve_poll_keepalive": dict(ka.status(), **ka.info()) if ka is not None else None,
        "curve_ws": dict(sock.status(), **sock.info()) if sock is not None else None,
        "curve_events": dict(rec["n"]), "curve_gone": rec["gone"],
        "decode": {"checked": len(chk), "within_0.5pct": sum(1 for x in chk if x <= 0.5),
                   "median_diff_pct": round(median(chk), 3) if chk else None,
                   "discriminator_ok": poller.discs.get(CURVE_DISC.hex(), 0),
                   "discriminator_other": {k: v for k, v in poller.discs.items() if k != CURVE_DISC.hex()}},
        "first_reading_s": {"median": round(median(rec["lag"]), 2) if rec["lag"] else None,
                            "p90": round(pctl(rec["lag"], 0.9), 2) if rec["lag"] else None},
        "metadata": dict(meta.status(), **meta.info(), results=len(m), sources=dict(src),
                         with_x=sum(1 for e in m if e.get("twitter")), x_kinds=dict(collections.Counter(
                             e.get("x_kind") for e in m if e.get("x_kind"))),
                         with_tg=sum(1 for e in m if e.get("telegram")), with_web=sum(1 for e in m if e.get("website")),
                         errors_seen=[e.get("error") for e in m if not e.get("ok")][:5]),
        "coins": coins,
        "sample_create": {k: v for k, v in first_create.items() if not k.startswith("_")},
    }


def _probe_lines(s):
    p, c, k, w, d, m = s["pumpportal"], s["curve_poll"], s["curve_poll_keepalive"], s["curve_ws"], s["decode"], \
        s["metadata"]
    lines = ["=== FEEDS PROBE · %.0f s ===" % s["seconds"],
             "pumpportal  creates %d (%.1f/min) · migrates %d (%.1f/min) · connects %d · drops %d · errors %d · "
             "last error %s · notices %s" % (p["creates"], p["creates_per_min_probe"], p["migrates"],
                                             p["migrates_per_min"], p["connects"], p["drops"], p["errors"],
                                             p["last_error"], p["notices"]),
             "            create fields: %s" % ", ".join(p["create_fields"]),
             "curve poll  calls %d · ok %d · errors %d (%s) · 429s %d · median %s ms · p90 %s ms · readings %d · "
             "events %d · nulls %d · gone %d · interval %g s" % (
                 c["calls"], c["ok"], c["errors"], c["last_error"], c["rate_limited"], c["ms_median"], c["ms_p90"],
                 c["readings"], s["curve_events"].get("poll", 0), c["nulls"], s["curve_gone"], c["interval_s"])]
    if k:
        lines.append("keep-alive  calls %d · ok %d · errors %d (%s) · median %s ms · p90 %s ms · connections %s" % (
            k["calls"], k["ok"], k["errors"], k["last_error"], k["ms_median"], k["ms_p90"], k["connections"]))
    if w:
        lines.append("curve ws    connects %d · drops %d · subscribe calls %d · acks %d · pushes %d · events %d · "
                     "errors %d (%s)" % (w["connects"], w["drops"], w["subscribe_calls"], w["subscribed"], w["pushes"],
                                         s["curve_events"].get("ws", 0), w["errors"], w["last_error"]))
    lines += ["decode      first reading vs the create event: %d checked · %d within 0.5 %% · median diff %s %% · "
              "discriminator account:BondingCurve %d ok, other %s" % (
                  d["checked"], d["within_0.5pct"], d["median_diff_pct"], d["discriminator_ok"],
                  d["discriminator_other"] or "none"),
              "first read  create -> first curve reading: median %s s · p90 %s s" % (
                  s["first_reading_s"]["median"], s["first_reading_s"]["p90"]),
              "metadata    results %d · ok %d · median %s ms · sources %s · X %d %s · Telegram %d · web %d · "
              "preferred %s" % (m["results"], m["ok"], m["ms_median"], m["sources"], m["with_x"], m["x_kinds"],
                                m["with_tg"], m["with_web"], m["preferred"]),
              "            gateways %s · failures %s" % (m["gateways"], m["errors_seen"])]
    for coin in s["coins"]:
        lines.append("coin %-10s %s… real SOL %.2f → %.2f · price %+.1f %%%s" % (
            (coin["symbol"] or "")[:10], coin["mint"][:8], coin["rsol_first"], coin["rsol_last"],
            coin["price_change_pct"], " · COMPLETE" if coin["complete"] else ""))
    return lines


def main(argv=None, out=print):
    ap = argparse.ArgumentParser(description="Meme Bot feeds: a live probe of the real-time sources.")
    ap.add_argument("--probe", type=float, metavar="SECONDS", help="run every feed for SECONDS and print a summary")
    ap.add_argument("--follow", type=int, default=20, help="curves followed at once (the newest coins)")
    ap.add_argument("--meta", type=int, default=10, help="metadata fetches")
    ap.add_argument("--interval", type=float, default=1.0, help="curve poll interval (s)")
    ap.add_argument("--no-ws", action="store_true", help="skip the RPC WebSocket")
    ap.add_argument("--no-compare", action="store_true", help="skip the keep-alive comparison poller")
    ap.add_argument("--pumpportal", default=PUMPPORTAL_URL)
    ap.add_argument("--rpc", default=RPC_HTTP)
    ap.add_argument("--rpc-ws", default=RPC_WS)
    ap.add_argument("--gateways", default=",".join(IPFS_GATEWAYS), help="IPFS gateways, comma-separated")
    a = ap.parse_args(argv)
    if a.probe is None:
        out(ap.format_help())
        return 2
    probe(a.probe, follow=a.follow, meta_n=a.meta, use_ws=not a.no_ws, compare=not a.no_compare,
          interval=a.interval, pumpportal_url=a.pumpportal, rpc=a.rpc, rpc_ws=a.rpc_ws,
          gateways=[g for g in a.gateways.split(",") if g], out=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
