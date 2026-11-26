"""memebot/feeds.py tests, offline: local fake servers only (a websocket server playing PumpPortal and the RPC
socket, an HTTP JSON-RPC server answering getMultipleAccounts, an HTTP gateway for metadata). No network.

The websocket tests need the 'websockets' package (pip install websockets==17.4, as the workflow does); without it
they are skipped and the rest still runs."""

import base64
import email.utils
import http.server
import json
import logging
import queue
import struct
import threading
import time
import unittest

from _load import load

fd = load("memebot", "feeds")
try:
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.server import serve as ws_serve
except ImportError:
    ws_serve = None
NEEDS_WS = "the 'websockets' package is not installed here (pip install websockets==17.4)"
logging.getLogger("websockets").addHandler(logging.NullHandler())      # no handshake chatter in the test output

VTOK0, VSOL0 = 1073000000 * 10 ** 6, 30 * 10 ** 9                       # a fresh pump.fun curve (the probe's numbers)


def account(vtok=VTOK0, vsol=VSOL0, rtok=793100000 * 10 ** 6, rsol=0, supply=10 ** 15, complete=False, size=166):
    raw = fd.CURVE_DISC + struct.pack("<5Q", vtok, vsol, rtok, rsol, supply) + bytes([1 if complete else 0])
    return raw + bytes(size - len(raw))


def b64(raw):
    return base64.b64encode(raw).decode()


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return bool(cond())


def events(q, kind=None):
    out = []
    while True:
        try:
            ev = q.get_nowait()
        except queue.Empty:
            return out
        if kind is None or ev.get("kind") == kind:
            out.append(ev)


class QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):        # a client that timed out and left: expected here
        pass


def http_server(handle):
    """A local HTTP server: handle(method, path, headers, body, peer) -> (status, bytes, headers)."""
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"                       # keep-alive possible (urllib closes anyway)

        def log_message(self, *a):
            pass

        def _do(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            st, raw, hdr = handle(method, self.path, dict(self.headers), body, self.client_address)
            self.send_response(st)
            for k, v in hdr.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            self._do("POST")

        def do_GET(self):
            self._do("GET")

    srv = QuietServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


class FakeRPC(object):
    """getMultipleAccounts from self.accounts {curve key: bytes or None}; self.script queues special answers."""

    def __init__(self):
        self.accounts = {}
        self.slot = 100
        self.calls = []
        self.peers = []
        self.script = []
        self.lock = threading.Lock()
        self.srv, base = http_server(self.handle)
        self.url = base + "/"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def value(self, key):
        raw = self.accounts.get(key)
        if raw is None:
            return None
        return {"data": [b64(raw), "base64"], "executable": False, "lamports": 2039280,
                "owner": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "rentEpoch": 0, "space": len(raw)}

    def handle(self, method, path, headers, body, peer):
        doc = json.loads(body)
        with self.lock:
            keys, opts = doc["params"]
            self.calls.append({"method": doc["method"], "keys": list(keys), "opts": opts, "ua": headers.get("User-Agent")})
            self.peers.append(peer)
            step = self.script.pop(0) if self.script else ("ok",)
            slot = self.slot
            vals = [self.value(k) for k in keys]
        kind = step[0]
        if kind == "429":
            return 429, b'{"error": "slow down"}', ({"Retry-After": step[1]} if step[1] is not None else {})
        if kind == "500":
            return 500, b"oops", {}
        if kind == "html":
            return 200, b"<html>busy</html>", {}
        if kind == "rpc429":
            return 200, json.dumps({"jsonrpc": "2.0", "id": doc["id"], "error": {"code": 429, "message": "Too many requests"}}).encode(), {}
        if kind == "rpcerr":
            return 200, json.dumps({"jsonrpc": "2.0", "id": doc["id"], "error": {"code": -32602, "message": "bad key"}}).encode(), {}
        if kind == "short":
            vals = vals[:-1]
        if kind == "sleep":
            time.sleep(step[1])
        if kind == "slot":
            slot = step[1]
        ctx = {"apiVersion": "2.2.0", "slot": slot}
        if kind == "ctx":                                   # an odd context: ("ctx", [1]) or ("ctx", {"slot": "5"})
            ctx = step[1]
        res = {"jsonrpc": "2.0", "id": doc["id"], "result": {"context": ctx, "value": vals}}
        return 200, json.dumps(res).encode(), {"Content-Type": "application/json"}


class WSServer(object):
    """A local websocket server; handler(ws, n) runs per connection (n = 1, 2, ...)."""

    def __init__(self, handler):
        self.handler = handler
        self.opened = []                # time.monotonic() of each connection
        self.closed = []
        self.stop = threading.Event()
        self.server = ws_serve(self._conn, "127.0.0.1", 0)
        self.url = "ws://127.0.0.1:%d" % self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _conn(self, ws):
        self.opened.append(time.monotonic())
        n = len(self.opened)
        try:
            self.handler(ws, n)
        except (ConnectionClosed, OSError, TimeoutError):
            pass
        finally:
            self.closed.append(time.monotonic())

    def hold(self, ws, on_frame=None):
        """Keep the connection until the client leaves or the test ends."""
        while not self.stop.is_set():
            try:
                raw = ws.recv(timeout=0.05)
            except TimeoutError:
                continue
            if on_frame:
                on_frame(raw)

    def close(self):
        self.stop.set()
        self.server.shutdown()


def create(mint, key=None, uri=None):
    return json.dumps({"signature": "sig-" + mint, "mint": mint, "traderPublicKey": "dev-" + mint, "txType": "create",
                       "initialBuy": 10493887.4851, "solAmount": 0.296296295, "bondingCurveKey": key or "curve-" + mint,
                       "vTokensInBondingCurve": 1073000000.0, "vSolInBondingCurve": 30.0, "marketCapSol": 27.96,
                       "name": mint + " coin", "symbol": mint, "uri": uri or "https://meta.example/" + mint,
                       "is_mayhem_mode": False, "pool": "pump"})


ACKS = ({"message": "Successfully subscribed to token creation events."}, {"message": "Subscribed to 'migration' events."})


# ---------------------------------------------------------------- pure parts

class Decode(unittest.TestCase):
    def test_a_fresh_curve_account(self):
        raw = account()
        self.assertEqual(len(raw), 166)
        d = fd.decode_curve(raw)
        self.assertEqual((d["vtok"], d["vsol"], d["rsol"], d["supply"], d["complete"]), (VTOK0, VSOL0, 0, 10 ** 15, False))
        self.assertAlmostEqual(d["price_sol"], 2.796e-08, delta=1e-11)            # (30 SOL) / (1.073 B tokens)
        self.assertEqual(sorted(d), ["complete", "price_sol", "rsol", "rtok", "supply", "vsol", "vtok"])
        self.assertEqual(fd.decode_curve(b64(raw)), d)                              # base64 text works too
        self.assertEqual(fd.decode_curve(memoryview(raw)), d)

    def test_the_probe_sample_and_the_complete_flag(self):
        vsol, vtok = 30.296296294999983, 1062506112.5149                            # FOLIO's create event
        d = fd.decode_curve(account(vtok=int(vtok * 1e6), vsol=int(vsol * 1e9), rsol=int(0.296296295 * 1e9)))
        self.assertAlmostEqual(d["price_sol"] / (vsol / vtok), 1.0, places=8)
        self.assertTrue(fd.decode_curve(account(complete=True))["complete"])

    def test_junk_is_none_never_an_exception(self):
        for junk in (None, b"", b"\x00" * 48, "not base64!!", 12345, [1, 2], {"data": 1}):
            self.assertIsNone(fd.decode_curve(junk))
        self.assertEqual(fd.decode_curve(bytes(49))["price_sol"], 0.0)              # vtok 0: no division by zero
        self.assertIsNone(fd.account_bytes(None))
        self.assertEqual(fd.account_bytes({"data": ["@@@", "base64"]}), b"")
        self.assertEqual(fd.account_bytes({"nodata": 1}), b"")
        self.assertEqual(fd.account_bytes({"data": [b64(b"abc"), "base64"]}), b"abc")


class Helpers(unittest.TestCase):
    def test_backoff_doubles_to_the_cap_and_resets_only_after_a_healthy_session(self):
        base, waits = 2.0, []
        for _ in range(7):
            wait, base = fd.retry_plan(base, 1.0, 2.0, 60.0, 60.0)
            waits.append(wait)
        self.assertEqual(waits, [2, 4, 8, 16, 32, 60, 60])
        self.assertEqual(fd.retry_plan(60.0, 59.9, 2.0, 60.0, 60.0), (60.0, 60.0))   # 59.9 s is not healthy
        self.assertEqual(fd.retry_plan(60.0, 61.0, 2.0, 60.0, 60.0), (2.0, 4.0))     # a healthy hour: back to 2 s
        self.assertEqual(fd.retry_plan(0.1, 0.0, 2.0, 60.0, 60.0), (2.0, 4.0))       # never under the floor

    def test_a_working_session_the_server_dropped_resets_the_backoff(self):
        """PumpPortal drops connections when it rebalances: a session that streamed events for productive_s was
        a working one and the wait goes back to 2 s; one that dropped at once or sent nothing still doubles."""
        self.assertEqual(fd.retry_plan(60.0, 30.0, 2.0, 60.0, 60.0, events=40, productive_s=15.0), (2.0, 4.0))
        self.assertEqual(fd.retry_plan(16.0, 30.0, 2.0, 60.0, 60.0, events=0, productive_s=15.0), (16.0, 32.0))
        self.assertEqual(fd.retry_plan(16.0, 3.0, 2.0, 60.0, 60.0, events=5, productive_s=15.0), (16.0, 32.0))
        self.assertEqual(fd.retry_plan(16.0, 30.0, 2.0, 60.0, 60.0, events=5), (16.0, 32.0))   # the old rule alone
        p = fd.PumpPortalFeed(queue.Queue())
        self.assertEqual((p.healthy_s, p.productive_s, p.backoff_min), (60.0, 15.0, 2.0))
        # 6 hours of a server dropping a working connection every 30-59 s: up nearly all night (it was 31-47 %,
        # the waits climbing 2, 4 ... 60 s for good)
        for session in (30.0, 45.0, 59.0):
            t = up = 0.0
            base = p.backoff_min
            while t < 6 * 3600:
                up += session
                t += session
                wait, base = fd.retry_plan(base, session, p.backoff_min, p.backoff_max, p.healthy_s, 50, p.productive_s)
                self.assertLessEqual(wait, 2.0)
                t += wait * 1.3                                                     # the most jitter
            self.assertGreater(up / t, 0.9)

    def test_retry_after(self):
        self.assertEqual(fd.retry_after("3"), 3.0)
        self.assertEqual(fd.retry_after("0"), 1.0)                                  # never a tight loop
        self.assertEqual(fd.retry_after(None, default=7), 7.0)
        self.assertEqual(fd.retry_after("soon", default=4), 4.0)
        self.assertEqual(fd.retry_after("100000"), 300.0)
        when = email.utils.formatdate(time.time() + 20, usegmt=True)
        self.assertTrue(17 <= fd.retry_after(when) <= 21)

    def test_the_curve_state_drops_repeats_and_older_slots(self):
        st = fd.CurveState()
        self.assertTrue(st.fresh("m", ("a",), 100))
        self.assertFalse(st.fresh("m", ("a",), 101))            # same reserves
        self.assertFalse(st.fresh("m", ("b",), 100))            # older than the last reading (slot 101)
        self.assertTrue(st.fresh("m", ("b",), 102))
        self.assertTrue(st.fresh("m", ("c",), None))            # no slot: decided on the reserves
        st.forget("m")
        self.assertTrue(st.fresh("m", ("c",), 1))               # a new watch starts with a first reading

    def test_ipfs_paths(self):
        self.assertEqual(fd.ipfs_path("https://ipfs.io/ipfs/QmAbc"), "QmAbc")
        self.assertEqual(fd.ipfs_path("https://cf-ipfs.com/ipfs/bafy1/meta.json?x=1"), "bafy1/meta.json")
        self.assertEqual(fd.ipfs_path("ipfs://QmAbc"), "QmAbc")
        self.assertEqual(fd.ipfs_path("ipfs://ipfs/QmAbc"), "QmAbc")
        self.assertEqual(fd.ipfs_path("https://bafy2.ipfs.w3s.link/m.json"), "bafy2/m.json")
        for no in ("https://meta.uxento.io/data/18cf", "", None, "ftp://ipfs/x", "https://ipfs.io/ipfs/"):
            self.assertIsNone(fd.ipfs_path(no))

    def test_x_links_profile_full_post_weak(self):
        self.assertEqual(fd.x_kind("https://x.com/coin"), "profile")
        self.assertEqual(fd.x_kind("twitter.com/coin/"), "profile")
        self.assertEqual(fd.x_kind("@coin"), "profile")
        self.assertEqual(fd.x_kind("https://x.com/elonmusk/status/1234"), "post")
        self.assertEqual(fd.x_kind("https://twitter.com/i/communities/1"), "community")
        self.assertEqual(fd.x_kind("https://x.com/search?q=%24COIN"), "other")
        self.assertEqual(fd.x_kind("https://t.me/coin"), "other")
        self.assertEqual(fd.x_kind(""), "")
        self.assertEqual(fd.x_kind(None), "")

    def test_x_kind_on_whatever_a_creator_typed_never_raises(self):
        for junk in ("http://[::1", "https://[x.com/a", "@", 42, ["x"]):
            self.assertIn(fd.x_kind(junk), ("other", ""))                         # urlsplit raises on "http://["

    def test_pctl_is_nearest_rank(self):
        self.assertEqual(fd.pctl(list(range(1, 11)), 0.9), 9)                     # not the maximum
        self.assertEqual(fd.pctl(list(range(1, 111)), 0.9), 99)
        self.assertEqual(fd.pctl([4, 1, 3, 2], 0.5), 2)
        self.assertEqual(fd.pctl([7], 0.9), 7)
        self.assertIsNone(fd.pctl([], 0.9))

    def test_a_reading_without_a_slot_keeps_the_newest_slot_known(self):
        st = fd.CurveState()
        self.assertTrue(st.fresh("m", ("a",), 100))
        self.assertTrue(st.fresh("m", ("b",), None))
        self.assertFalse(st.fresh("m", ("a",), 99))             # a lagging node's old reserves stay out

    def test_socials_from_several_layouts(self):
        s = fd.socials({"name": "A", "twitter": " https://x.com/a ", "telegram": "", "website": "https://a.io",
                        "description": "d" * 500, "extensions": {"telegram": "https://t.me/a"}})
        self.assertEqual((s["twitter"], s["telegram"], s["website"], len(s["description"])),
                         ("https://x.com/a", "https://t.me/a", "https://a.io", 300))
        self.assertEqual(fd.socials({"external_url": "https://b.io"})["website"], "https://b.io")
        self.assertEqual(fd.socials("junk"), {"twitter": "", "telegram": "", "website": "", "description": ""})


# ---------------------------------------------------------------- PumpPortal

class FastStop(threading.Event):
    """A stop flag whose wait() returns at once and records the wait; set after n waits."""

    def __init__(self, n):
        threading.Event.__init__(self)
        self.n, self.waits = n, []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if len(self.waits) >= self.n:
            self.set()
        return self.is_set()


class PumpPortalNoServer(unittest.TestCase):
    def setUp(self):
        self.saved = fd.ws_connect

    def tearDown(self):
        fd.ws_connect = self.saved

    def test_without_the_websockets_package_it_says_so_and_runs_nothing(self):
        fd.ws_connect = None
        feed = fd.PumpPortalFeed()
        feed.start()
        self.assertFalse(feed.running())
        st = feed.status()
        self.assertEqual(sorted(st), ["connected", "errors", "events", "last_error", "last_event_age_s", "name"])
        self.assertEqual((st["name"], st["connected"], st["events"]), ("pumpportal", False, 0))
        self.assertIn("websockets not installed", st["last_error"])
        self.assertTrue(feed.stop(0.1))
        sock = fd.CurveSocket()
        sock.watch("m", "k")
        self.assertFalse(sock.start().running())
        self.assertIn("websockets not installed", sock.status()["last_error"])
        hub = fd.Feeds({"curve_ws": True})
        self.assertTrue(hub.text().startswith("pumpportal off (no websockets)"))

    def test_refused_connections_back_off_2_4_8_never_less_than_2_s(self):
        tries = []

        def refuse(url, **kw):
            tries.append(kw.get("proxy"))
            raise OSError("connection refused")

        fd.ws_connect = refuse
        feed = fd.PumpPortalFeed(url="ws://127.0.0.1:9", backoff_min=0.1, rand=lambda: 0.0)
        self.assertEqual(feed.backoff_min, 2.0)                                     # a hard floor
        feed._stop = FastStop(7)
        feed._run()
        self.assertEqual(feed._stop.waits, [2, 4, 8, 16, 32, 60, 60])
        self.assertEqual(len(tries), 7)
        self.assertEqual((feed.errors, feed.connected), (7, False))
        self.assertIn("connection refused", feed.last_error)
        jittery = fd.PumpPortalFeed(rand=lambda: 1.0)
        jittery._stop = FastStop(2)
        fd.ws_connect = refuse
        jittery._run()
        self.assertEqual([round(w, 2) for w in jittery._stop.waits], [2.6, 5.2])    # +30 % jitter at most


@unittest.skipIf(ws_serve is None, NEEDS_WS)
class PumpPortalLive(unittest.TestCase):
    def setUp(self):
        self.subs = []
        self.feed = None
        self.srv = None

    def tearDown(self):
        if self.feed:
            self.feed.stop(3)
        if self.srv:
            self.srv.close()

    def subscribe(self, ws):
        self.subs.append([json.loads(ws.recv(timeout=2)), json.loads(ws.recv(timeout=2))])
        for a in ACKS:
            ws.send(json.dumps(a))

    def test_acks_events_a_drop_a_reconnect_after_2_s_and_a_clean_stop(self):
        def handler(ws, n):
            self.subscribe(ws)
            if n == 1:
                ws.send(create("AAA"))
                ws.send("not json")
                ws.send(json.dumps({"txType": "buy", "mint": "AAA"}))                # trades: not ours
                ws.send(json.dumps({"signature": "s", "mint": "AAA", "txType": "migrate", "pool": "pump-amm"}))
                time.sleep(0.2)
                ws.close()                                                          # the server drops us
                return
            ws.send(create("BBB"))
            self.srv.hold(ws)

        self.srv = WSServer(handler)
        q = queue.Queue()
        self.feed = fd.PumpPortalFeed(q, url=self.srv.url, proxy=None, rand=lambda: 0.0).start()
        self.assertTrue(wait_for(lambda: len(self.srv.opened) >= 2 and self.feed.connected and q.qsize() >= 3, 8))
        evs = events(q)
        self.assertEqual([(e["kind"], e["txType"], e["mint"]) for e in evs],
                         [("create", "create", "AAA"), ("migrate", "migrate", "AAA"), ("create", "create", "BBB")])
        self.assertTrue(all(abs(e["_rx"] - time.time()) < 10 for e in evs))
        self.assertEqual(evs[0]["bondingCurveKey"], "curve-AAA")
        self.assertEqual(self.subs, [[{"method": "subscribeNewToken"}, {"method": "subscribeMigration"}]] * 2)
        self.assertIn("Successfully subscribed to token creation events.", self.feed.notices)   # recorded, not queued
        gap = self.srv.opened[1] - self.srv.closed[0]
        self.assertGreaterEqual(gap, 1.9)
        self.assertGreaterEqual(self.feed.waits[0], 2.0)
        st = self.feed.status()
        self.assertEqual((st["connected"], st["events"]), (True, 3))
        self.assertGreaterEqual(st["errors"], 2)                                    # the junk frame and the drop
        self.assertLess(st["last_event_age_s"], 8)
        info = self.feed.info()
        self.assertEqual((info["connects"], info["creates"], info["migrates"]), (2, 2, 1))
        t0 = time.monotonic()
        self.assertTrue(self.feed.stop(3))
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertFalse(self.feed.status()["connected"])
        self.assertEqual(len(self.srv.opened), 2)                                   # ONE connection at a time
        self.feed.start()                                                           # restartable after a stop
        self.assertTrue(wait_for(lambda: len(self.srv.opened) == 3, 3))

    def test_a_server_that_drops_a_working_connection_is_reconnected_after_2_s_each_time(self):
        def handler(ws, n):
            self.subscribe(ws)
            for i in range(4):
                ws.send(create("C%d%d" % (n, i)))
                time.sleep(0.3)
            ws.close()                                                              # rebalancing, after 1.2 s

        self.srv = WSServer(handler)
        q = queue.Queue()
        self.feed = fd.PumpPortalFeed(q, url=self.srv.url, proxy=None, rand=lambda: 0.0, productive_s=1.0).start()
        self.assertTrue(wait_for(lambda: len(self.feed.waits) >= 2, 12))
        self.assertEqual(list(self.feed.waits)[:2], [2.0, 2.0])                    # not 2, 4, 8 ... 60
        self.assertGreaterEqual(q.qsize(), 8)

    def test_the_silence_watchdog_reconnects(self):
        def handler(ws, n):
            self.subscribe(ws)
            if n == 2:
                ws.send(create("CCC"))
            self.srv.hold(ws)                                                       # n == 1: acks, then nothing

        self.srv = WSServer(handler)
        q = queue.Queue()
        self.feed = fd.PumpPortalFeed(q, url=self.srv.url, silence_s=1.0, proxy=None, rand=lambda: 0.0).start()
        self.assertTrue(wait_for(lambda: q.qsize() >= 1, 8))
        self.assertIn("silent for 1 s", self.feed.last_error)
        self.assertEqual(events(q)[0]["mint"], "CCC")
        self.assertGreaterEqual(self.srv.opened[1] - self.srv.opened[0], 3.0)        # 1 s silence + 2 s back-off

    def test_an_odd_frame_does_not_cost_the_connection(self):
        def handler(ws, n):
            self.subscribe(ws)
            ws.send("[" * 100000 + "]" * 100000)                                    # json.loads: RecursionError
            ws.send(create("DDD"))
            self.srv.hold(ws)

        self.srv = WSServer(handler)
        q = queue.Queue()
        self.feed = fd.PumpPortalFeed(q, url=self.srv.url, proxy=None).start()
        self.assertTrue(wait_for(lambda: q.qsize() >= 1, 5))
        self.assertEqual(events(q)[0]["mint"], "DDD")
        time.sleep(0.3)
        self.assertEqual((len(self.srv.opened), self.feed.info()["connects"]), (1, 1))  # PumpPortal bans reconnects
        self.assertIn("bad frame", self.feed.last_error)

    def test_stop_during_the_back_off_is_prompt(self):
        def handler(ws, n):
            self.subscribe(ws)
            ws.close()

        self.srv = WSServer(handler)
        self.feed = fd.PumpPortalFeed(url=self.srv.url, proxy=None).start()
        self.assertTrue(wait_for(lambda: self.feed.retry_at is not None, 5))
        t0 = time.monotonic()
        self.assertTrue(self.feed.stop(3))
        self.assertLess(time.monotonic() - t0, 0.5)


# ---------------------------------------------------------------- the curve poller

class Poller(unittest.TestCase):
    def setUp(self):
        self.rpc = FakeRPC()
        self.q = queue.Queue()
        self.p = fd.CurvePoller(self.q, rpc=self.rpc.url, timeout=1.0)

    def tearDown(self):
        self.p.stop(2)
        self.rpc.close()

    def add(self, n, prefix="m"):
        for i in range(n):
            self.rpc.accounts["k%s%03d" % (prefix, i)] = account(vsol=VSOL0 + i)
            self.assertTrue(self.p.watch("%s%03d" % (prefix, i), "k%s%03d" % (prefix, i)))

    def test_one_batch_per_100_keys_processed_base64_and_change_only_events(self):
        self.add(150)
        self.assertEqual(self.p.poll(), 150)                                        # a first reading of each
        self.assertEqual([len(c["keys"]) for c in self.rpc.calls], [100, 50])
        c = self.rpc.calls[0]
        self.assertEqual((c["method"], c["opts"]), ("getMultipleAccounts", {"encoding": "base64", "commitment": "processed"}))
        self.assertEqual(len(set(self.rpc.calls[0]["keys"]) | set(self.rpc.calls[1]["keys"])), 150)
        evs = events(self.q)
        e = [x for x in evs if x["mint"] == "m007"][0]
        self.assertEqual(sorted(e), ["complete", "disc_ok", "key", "kind", "mint", "price_sol", "rsol", "rtok", "slot",
                                     "src", "t", "vsol", "vtok"])
        self.assertIs(e["disc_ok"], True)                                           # a pump.fun BondingCurve account
        self.assertEqual((e["kind"], e["key"], e["vsol"], e["vtok"], e["slot"], e["src"], e["complete"]),
                         ("curve", "km007", VSOL0 + 7, VTOK0, 100, "poll", False))
        self.assertAlmostEqual(e["price_sol"], 2.796e-08, delta=1e-11)
        self.assertEqual(self.p.poll(), 0)                                          # nothing changed: nothing queued
        self.rpc.accounts["km042"] = account(vsol=VSOL0 + 5 * 10 ** 9, rsol=5 * 10 ** 9)
        self.rpc.slot = 101
        self.assertEqual(self.p.poll(), 1)
        e = events(self.q)[0]
        self.assertEqual((e["mint"], e["rsol"], e["slot"]), ("m042", 5 * 10 ** 9, 101))
        self.rpc.accounts["km001"] = account(complete=True)
        self.assertEqual(self.p.poll(), 1)
        self.assertTrue(events(self.q)[0]["complete"])
        self.assertEqual((self.p.calls, self.p.ok, self.p.errors), (8, 8, 0))
        self.assertTrue(self.p.status()["connected"])
        self.assertIn("Mozilla/5.0", self.rpc.calls[0]["ua"])

    def test_an_account_that_is_not_a_bonding_curve_says_so(self):
        """The first dry run read a coin whose key was not a pump.fun curve as +4050 %: the event now carries
        disc_ok (the Anchor discriminator of account:BondingCurve) and newpairs.py rejects such a coin."""
        self.add(1)
        key = next(iter(self.p.watched().values()))
        self.rpc.accounts[key] = b"\x01" * 8 + account()[8:]
        self.assertEqual(self.p.poll(), 1)
        self.assertIs(events(self.q)[0]["disc_ok"], False)

    def test_429_pauses_for_its_retry_after(self):
        self.add(2)
        self.rpc.script = [("429", "2")]
        self.assertEqual(self.p.poll(), 0)
        self.assertEqual(self.p.limited, 1)
        self.assertTrue(1.5 < self.p.paused_until - time.monotonic() <= 2.0)
        st = self.p.status()
        self.assertEqual((st["connected"], st["errors"]), (False, 1))
        self.assertIn("429", st["last_error"])
        self.assertEqual(self.p.poll(), 0)
        self.assertEqual(len(self.rpc.calls), 1)                                    # paused: no call went out
        self.p.paused_until = 0
        self.assertEqual(self.p.poll(), 2)
        self.rpc.script = [("429", None)]                                           # no header: 5 s, doubling
        self.p.poll()
        self.assertTrue(4.0 < self.p.paused_until - time.monotonic() <= 5.0)
        self.p.paused_until = 0
        self.rpc.script = [("rpc429",)]                                             # a 429 inside a JSON-RPC answer
        self.p.poll()
        self.assertEqual(self.p.limited, 3)
        self.assertTrue(self.p.paused_until > time.monotonic())

    def test_null_accounts_and_curve_gone_once(self):
        self.add(1)
        self.p.watch("ghost", "kghost")
        self.assertEqual(self.p.poll(), 1)                                          # only the real one
        self.assertEqual([(e["kind"], e["mint"]) for e in events(self.q)], [("curve", "m000")])
        self.p.poll()
        self.assertEqual(events(self.q), [])
        self.p.poll()                                                               # the third null in a row
        gone = events(self.q)
        self.assertEqual([(e["kind"], e["mint"], e["key"]) for e in gone], [("curve_gone", "ghost", "kghost")])
        self.p.poll()
        self.assertEqual(events(self.q), [])                                        # once
        self.assertEqual(self.p.nulls, 4)
        self.rpc.accounts["kghost"] = account()
        self.p.poll()
        self.assertEqual([e["kind"] for e in events(self.q)], ["curve"])             # back
        del self.rpc.accounts["kghost"]
        for _ in range(3):
            self.p.poll()
        self.assertEqual([e["kind"] for e in events(self.q)], ["curve_gone"])        # gone again, once again

    def test_a_curve_that_comes_back_unchanged_after_gone_is_announced(self):
        self.add(1)
        self.assertEqual(self.p.poll(), 1)
        saved = self.rpc.accounts.pop("km000")
        for _ in range(3):
            self.p.poll()
        self.assertEqual([e["kind"] for e in events(self.q)], ["curve", "curve_gone"])
        self.rpc.accounts["km000"] = saved                                          # the same reserves as before
        self.assertEqual(self.p.poll(), 1)
        self.assertEqual([(e["kind"], e["mint"]) for e in events(self.q)], [("curve", "m000")])

    def test_odd_contexts_and_slots_never_raise_or_stick(self):
        self.add(2)
        self.rpc.script = [("ctx", [1])]                                            # a list, not {"slot": ...}
        self.assertEqual(self.p.poll(), 2)                                          # read, without a slot
        self.assertEqual({e["slot"] for e in events(self.q)}, {None})
        self.rpc.accounts["km000"] = account(vsol=VSOL0 + 1)
        self.rpc.script = [("ctx", {"slot": "500"})]                                # a string slot is no slot
        self.assertEqual(self.p.poll(), 1)
        self.rpc.accounts["km000"] = account(vsol=VSOL0 + 2)
        self.rpc.accounts["km001"] = account(vsol=VSOL0 + 3)
        self.rpc.slot = 501
        self.assertEqual(self.p.poll(), 2)                                          # int slots work again after
        self.assertEqual(self.p.errors, 0)

    def test_timeouts_and_bad_answers_are_counted_never_raised(self):
        self.add(1)
        slow = fd.CurvePoller(self.q, rpc=self.rpc.url, timeout=0.3)
        slow.watch("m000", "km000")
        self.rpc.script = [("sleep", 1.0)]
        t0 = time.monotonic()
        self.assertEqual(slow.poll(), 0)
        self.assertLess(time.monotonic() - t0, 0.9)
        self.assertEqual(slow.last_error, "timeout after 0.3 s")
        for step, text in ((("500",), "HTTP 500"), (("html",), "not JSON"), (("rpcerr",), "rpc error"),
                           (("short",), "odd answer")):
            self.rpc.script = [step]
            self.assertEqual(self.p.poll(), 0)
            self.assertIn(text, self.p.last_error)
        self.assertEqual((self.p.errors, self.p.fail_streak, self.p.connected), (4, 4, False))
        self.assertGreater(self.p._fail_pause(), 0)                                 # a sick RPC gets air
        dead = fd.CurvePoller(self.q, rpc="http://127.0.0.1:9/", timeout=0.5)       # nothing listens there
        dead.watch("m", "k")
        self.assertEqual(dead.poll(), 0)
        self.assertEqual(dead.errors, 1)
        self.assertEqual(self.p.poll(), 1)                                          # and it recovers
        self.assertEqual((self.p.fail_streak, self.p._fail_pause()), (0, 0.0))

    def test_an_older_slot_never_turns_the_price_back(self):
        self.add(1)
        self.rpc.slot = 200
        self.assertEqual(self.p.poll(), 1)
        self.rpc.accounts["km000"] = account(vsol=VSOL0 + 10 ** 9)
        self.rpc.script = [("slot", 199)]                                           # a lagging RPC node
        self.assertEqual(self.p.poll(), 0)
        self.rpc.slot = 201
        self.assertEqual(self.p.poll(), 1)

    def test_watch_unwatch_watched(self):
        self.assertFalse(self.p.watch("m", None))
        self.assertFalse(self.p.watch(None, "k"))
        self.add(2)
        self.assertEqual(self.p.watched(), {"m000": "km000", "m001": "km001"})
        self.p.poll()
        self.assertTrue(self.p.unwatch("m000"))
        self.assertFalse(self.p.unwatch("m000"))
        events(self.q)
        self.p.poll()
        self.assertEqual(self.rpc.calls[-1]["keys"], ["km001"])
        self.p.watch("m000", "km000")
        self.assertEqual(self.p.poll(), 1)                                          # re-watched: a first reading
        self.assertEqual([e["mint"] for e in events(self.q)], ["m000"])
        small = fd.CurvePoller(self.q, rpc=self.rpc.url, max_keys=2)
        self.assertTrue(small.watch("a", "1") and small.watch("b", "2"))
        self.assertFalse(small.watch("c", "3"))
        self.assertIn("watch list full", small.last_error)

    def test_watch_from_many_threads_while_polling(self):
        self.p.max_keys = 10000
        self.add(5, prefix="base")

        def churn(t):
            for i in range(200):
                self.p.watch("t%d-%d" % (t, i), "k%d-%d" % (t, i))
                if i % 2:
                    self.p.unwatch("t%d-%d" % (t, i))

        threads = [threading.Thread(target=churn, args=(t,)) for t in range(6)]
        for t in threads:
            t.start()
        for _ in range(5):
            self.p.poll()
        for t in threads:
            t.join()
        self.assertEqual(len(self.p.watched()), 5 + 6 * 100)
        self.assertEqual(self.p.errors, 0)

    def test_our_own_call_budget(self):
        tight = fd.CurvePoller(self.q, rpc=self.rpc.url, max_calls_10s=3)
        self.rpc.accounts["k"] = account()
        tight.watch("m", "k")
        for _ in range(5):
            tight.poll()
        self.assertEqual((tight.calls, tight.skipped), (3, 2))

    def test_a_full_queue_drops_and_says_so(self):
        p = fd.CurvePoller(queue.Queue(maxsize=1), rpc=self.rpc.url)
        for i in range(3):
            self.rpc.accounts["k%d" % i] = account(vsol=VSOL0 + i)
            p.watch("m%d" % i, "k%d" % i)
        self.assertEqual(p.poll(), 1)
        self.assertEqual((p.events, p.errors, p.last_error), (1, 2, "queue full · event dropped"))

    def test_the_thread_polls_at_the_interval_and_stops(self):
        self.add(1)
        self.p.set_interval(0.1)
        self.assertEqual(self.p.interval, 0.1)
        self.p.start()
        self.assertTrue(wait_for(lambda: self.q.qsize() == 1, 3))
        self.rpc.accounts["km000"] = account(vsol=VSOL0 + 1)
        self.assertTrue(wait_for(lambda: self.q.qsize() == 2, 3))
        n0, t0 = len(self.rpc.calls), time.monotonic()
        time.sleep(0.55)
        rate = (len(self.rpc.calls) - n0) / (time.monotonic() - t0)
        self.assertTrue(4 <= rate <= 12, rate)                                     # ~10 calls a second at 0.1 s
        self.p.set_interval(0.001)
        self.assertEqual(self.p.interval, 0.1)                                     # clamped
        self.assertTrue(self.p.stop(2))
        self.assertFalse(self.p.running())
        info = self.p.info()
        self.assertTrue(info["ms_median"] is not None and info["ms_p90"] >= info["ms_median"])

    def test_keep_alive_uses_one_connection(self):
        self.add(1)
        ka = fd.CurvePoller(self.q, rpc=self.rpc.url, keepalive=True)
        ka.watch("m000", "km000")
        self.rpc.accounts["km000"] = account()
        self.assertEqual(ka.poll(), 1)
        self.rpc.accounts["km000"] = account(vsol=VSOL0 + 1)
        self.assertEqual(ka.poll(), 1)
        self.assertEqual(len(set(self.rpc.peers[-2:])), 1)                          # the same client port
        self.assertEqual(ka.info()["connections"], 1)
        self.p.watch("m000", "km000")
        self.p.poll()
        self.p.poll()
        self.assertNotEqual(self.rpc.peers[-1], self.rpc.peers[-2])                # urllib: a new one per call
        ka.ka.close()
        self.rpc.script = [("429", "3")]
        ka.poll()                                                                   # a new connection, a 429
        self.assertEqual((ka.limited, ka.info()["connections"]), (1, 2))
        self.assertTrue(ka.stop(1))
        self.assertIsNone(ka.ka.conn)                                               # stop() closes it


# ---------------------------------------------------------------- the curve socket

@unittest.skipIf(ws_serve is None, NEEDS_WS)
class Socket(unittest.TestCase):
    def setUp(self):
        self.accounts = {}
        self.subs = []                  # (connection, key)
        self.unsubs = []
        self.sock = None
        self.srv = None
        self.slot = 300

    def tearDown(self):
        if self.sock:
            self.sock.stop(3)
        if self.srv:
            self.srv.close()

    def rpc_handler(self, drop_first_after=None):
        sids = {}

        def handler(ws, n):
            opened = time.monotonic()

            def on(raw):
                msg = json.loads(raw)
                if msg["method"] == "accountSubscribe":
                    key = msg["params"][0]
                    self.subs.append((n, key))
                    sid = len(self.subs) + 1000
                    sids[sid] = key
                    ws.send(json.dumps({"jsonrpc": "2.0", "result": sid, "id": msg["id"]}))
                    self.slot += 1
                    ws.send(json.dumps({"jsonrpc": "2.0", "method": "accountNotification", "params": {
                        "result": {"context": {"slot": self.slot}, "value": {
                            "data": [b64(self.accounts[key]), "base64"], "owner": "6EF8", "lamports": 1}},
                        "subscription": sid}}))
                elif msg["method"] == "accountUnsubscribe":
                    self.unsubs.append((n, msg["params"][0]))
                    ws.send(json.dumps({"jsonrpc": "2.0", "result": True, "id": msg["id"]}))

            while not self.srv.stop.is_set():
                if n == 1 and drop_first_after and time.monotonic() - opened > drop_first_after:
                    ws.close(code=1011)                                             # the public RPC's habit
                    return
                try:
                    on(ws.recv(timeout=0.05))
                except TimeoutError:
                    pass
        return handler

    def test_pushes_a_drop_resubscribe_after_1_s_and_unsubscribe(self):
        for i in range(3):
            self.accounts["k%d" % i] = account(vsol=VSOL0 + i)
        self.srv = WSServer(self.rpc_handler(drop_first_after=0.6))
        q = queue.Queue()
        self.sock = fd.CurveSocket(q, url=self.srv.url, proxy=None, rand=lambda: 0.0)
        for i in range(3):
            self.sock.watch("m%d" % i, "k%d" % i)
        self.sock.start()
        self.assertTrue(wait_for(lambda: q.qsize() == 3, 4))
        evs = events(q)
        self.assertEqual(sorted((e["mint"], e["src"], e["vsol"]) for e in evs),
                         [("m%d" % i, "ws", VSOL0 + i) for i in range(3)])
        self.accounts["k1"] = account(vsol=VSOL0 + 10 ** 9)                         # it moves while we are away
        self.assertTrue(wait_for(lambda: len(self.subs) == 6, 5))                   # all three again on connection 2
        self.assertEqual(sorted(k for n, k in self.subs if n == 2), ["k0", "k1", "k2"])
        self.assertGreaterEqual(self.srv.opened[1] - self.srv.closed[0], 0.95)      # back-off at least 1 s
        self.assertTrue(wait_for(lambda: q.qsize() == 1, 2))
        self.assertEqual([(e["mint"], e["vsol"]) for e in events(q)], [("m1", VSOL0 + 10 ** 9)])   # change-only
        self.assertTrue(self.sock.status()["connected"])
        self.assertIn("closed 1011", self.sock.status()["last_error"])
        self.sock.unwatch("m2")
        self.assertTrue(wait_for(lambda: len(self.unsubs) == 1, 2))
        self.assertEqual(self.sock.info()["subscribed"], 6)
        self.assertTrue(self.sock.stop(3))

    def test_at_most_n_subscribe_calls_per_window(self):
        for i in range(8):
            self.accounts["k%d" % i] = account(vsol=VSOL0 + i)
        self.srv = WSServer(self.rpc_handler())
        q = queue.Queue()
        self.sock = fd.CurveSocket(q, url=self.srv.url, proxy=None, sub_limit=5, sub_window_s=1.0)
        for i in range(8):
            self.sock.watch("m%d" % i, "k%d" % i)
        t0 = time.monotonic()
        self.sock.start()
        self.assertTrue(wait_for(lambda: len(self.subs) == 5, 2))
        time.sleep(0.3)
        if time.monotonic() - t0 < 0.9:
            self.assertEqual(len(self.subs), 5)                                     # the window holds the rest
        self.assertTrue(wait_for(lambda: len(self.subs) == 8, 3))
        self.assertTrue(wait_for(lambda: q.qsize() == 8, 2))

    def test_odd_frames_are_counted_and_the_session_stays(self):
        self.accounts["k"] = account()
        base = self.rpc_handler()

        def handler(ws, n):
            ws.send(json.dumps({"jsonrpc": "2.0", "id": [1], "result": 1}))            # unhashable id
            ws.send(json.dumps({"jsonrpc": "2.0", "method": "accountNotification", "params": [1]}))
            ws.send(json.dumps({"jsonrpc": "2.0", "method": "accountNotification", "params": {"subscription": [2]}}))
            base(ws, n)

        self.srv = WSServer(handler)
        q = queue.Queue()
        self.sock = fd.CurveSocket(q, url=self.srv.url, proxy=None)
        self.sock.watch("m", "k")
        self.sock.start()
        self.assertTrue(wait_for(lambda: q.qsize() == 1, 3))
        time.sleep(0.5)
        self.assertEqual(len(self.srv.opened), 1)                                   # no drop, no resubscribe
        self.assertEqual(self.sock.info()["drops"], 0)
        self.assertGreaterEqual(self.sock.errors, 2)
        self.assertIn("bad frame", self.sock.last_error)

    def test_nothing_watched_no_connection(self):
        self.srv = WSServer(self.rpc_handler())
        self.sock = fd.CurveSocket(url=self.srv.url, proxy=None).start()
        time.sleep(0.4)
        self.assertEqual(self.srv.opened, [])
        self.accounts["k"] = account()
        self.sock.watch("m", "k")
        self.assertTrue(wait_for(lambda: len(self.subs) == 1, 3))


# ---------------------------------------------------------------- metadata

class Meta(unittest.TestCase):
    DOC = {"name": "Coin", "symbol": "COIN", "description": "the coin", "image": "https://x/y.png",
           "twitter": "https://x.com/coin", "telegram": "https://t.me/coin", "website": "https://coin.io"}

    def setUp(self):
        self.hits = []
        self.srv, self.base = http_server(self.handle)
        self.gws = [self.base + "/gw1/ipfs/", self.base + "/gw2/ipfs/"]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def handle(self, method, path, headers, body, peer):
        self.hits.append((path, headers.get("User-Agent", "")))
        if path.startswith("/gw1/"):
            return 403, b"<html>Just a moment...</html>", {"Content-Type": "text/html"}
        if path.startswith("/gw2/ipfs/Qm"):
            return 200, json.dumps(dict(self.DOC, name=path)).encode(), {"Content-Type": "application/json"}
        if path == "/direct/meta.json":
            return 200, json.dumps({"twitter": "https://x.com/someone/status/9"}).encode(), {}
        if path == "/slow":
            time.sleep(1.0)
            return 200, b"{}", {}
        return 404, b"no", {}

    def test_a_403_gateway_then_the_next_and_the_good_one_first_after(self):
        m = fd.MetaFetcher(gateways=self.gws, timeout=1.0)
        r = m.fetch("https://ipfs.io/ipfs/QmOne")
        self.assertEqual((r["ok"], r["twitter"], r["telegram"], r["website"], r["description"], r["x_kind"]),
                         (True, "https://x.com/coin", "https://t.me/coin", "https://coin.io", "the coin", "profile"))
        self.assertEqual(r["source"], self.base[len("http://"):] + "/gw2")
        self.assertIsNone(r["error"])
        self.assertTrue(0 <= r["ms"] < 2000)
        self.assertEqual([h[0] for h in self.hits], ["/gw1/ipfs/QmOne", "/gw2/ipfs/QmOne"])
        self.assertTrue(all("Mozilla/5.0" in ua and "Chrome" in ua for _, ua in self.hits))   # a browser's UA
        self.assertEqual(m.order()[0], self.gws[1])
        m.fetch("ipfs://QmTwo")
        self.assertEqual(self.hits[-1][0], "/gw2/ipfs/QmTwo")
        self.assertEqual(len(self.hits), 3)                                         # the 403 one was not asked again
        self.assertEqual(m.info()["gateways"][self.gws[0]], {"ok": 0, "fail": 1, "last": "HTTP 403"})
        self.assertEqual(m.info()["preferred"], self.gws[1])

    def test_direct_uris_failures_and_junk_never_raise(self):
        m = fd.MetaFetcher(gateways=self.gws, timeout=0.3, budget_s=0.5)
        r = m.fetch(self.base + "/direct/meta.json")
        self.assertEqual((r["ok"], r["x_kind"], r["source"]), (True, "post", self.base[len("http://"):]))
        r = m.fetch(self.base + "/missing")
        self.assertEqual((r["ok"], r["error"]), (False, self.base[len("http://"):] + ": HTTP 404"))
        r = m.fetch("https://ipfs.io/ipfs/bafyNone")                                # 403, then 404
        self.assertFalse(r["ok"])
        self.assertIn("HTTP 404", r["error"])
        t0 = time.monotonic()
        r = m.fetch(self.base + "/slow")
        self.assertLess(time.monotonic() - t0, 0.9)
        self.assertIn("timeout", r["error"])
        for junk in (None, "", "ftp://x/y", "not a url", 42):
            r = m.fetch(junk)
            self.assertFalse(r["ok"])
            self.assertTrue(r["error"])
        self.assertEqual(sorted(r), ["description", "error", "ms", "ok", "source", "telegram", "twitter", "website",
                                     "x_kind"])
        self.assertFalse(m.status()["connected"])
        self.assertGreaterEqual(m.status()["errors"], 7)

    def test_a_host_that_trickles_bytes_is_cut_at_the_timeout(self):
        class Drip(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                try:
                    self.wfile.write(b'{"twitter": "https://x.com/a", "pad": "')
                    for _ in range(40):                                             # a byte every 0.1 s for 4 s
                        self.wfile.flush()
                        time.sleep(0.1)
                        self.wfile.write(b"x")
                    self.wfile.write(b'"}')
                except OSError:
                    pass

        srv = QuietServer(("127.0.0.1", 0), Drip)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            m = fd.MetaFetcher(gateways=self.gws, timeout=0.5, budget_s=1.0)
            t0 = time.monotonic()
            r = m.fetch("http://127.0.0.1:%d/meta.json" % srv.server_address[1])
            self.assertLess(time.monotonic() - t0, 1.5)                             # was 4 s: each read was in time
            self.assertFalse(r["ok"])
            self.assertIn("too slow", r["error"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_a_fetch_cancelled_by_stop_is_asked_again_after(self):
        m = fd.MetaFetcher(gateways=self.gws, timeout=1.0, workers=1).start()
        gate = threading.Event()
        m._pool.submit(gate.wait, 3)                                                # the one worker is busy
        f1 = m.submit("mintC", "https://ipfs.io/ipfs/QmC")
        m.stop()
        gate.set()
        self.assertTrue(f1.cancelled())
        f2 = m.submit("mintC", "https://ipfs.io/ipfs/QmC")
        self.assertIsNot(f2, f1)
        self.assertTrue(f2.result(3)["ok"])
        self.assertEqual(m.result("mintC")["telegram"], "https://t.me/coin")
        m.stop()

    def test_submit_in_the_pool_once_per_coin_with_an_event(self):
        q = queue.Queue()
        m = fd.MetaFetcher(q, gateways=self.gws, timeout=1.0).start()
        f1 = m.submit("mintA", "https://ipfs.io/ipfs/QmA")
        self.assertIs(m.submit("mintA", "https://ipfs.io/ipfs/QmA"), f1)
        self.assertTrue(wait_for(lambda: q.qsize() == 1, 4))
        ev = q.get_nowait()
        self.assertEqual((ev["kind"], ev["mint"], ev["ok"], ev["telegram"]), ("meta", "mintA", True, "https://t.me/coin"))
        self.assertEqual(m.result("mintA")["website"], "https://coin.io")
        self.assertIsNone(m.result("never"))
        self.assertEqual(len([h for h in self.hits if h[0].endswith("QmA")]), 2)    # gw1 (403) + gw2, only once
        st = m.status()
        self.assertEqual((st["name"], st["events"], st["connected"]), ("metadata", 1, True))
        m.stop()
        fut = m.submit("mintB", "ipfs://QmB")                                       # a stopped pool restarts
        self.assertTrue(fut.result(3)["ok"])
        m.stop()


# ---------------------------------------------------------------- the bundle and the probe

class Hub(unittest.TestCase):
    def setUp(self):
        self.rpc = FakeRPC()

    def tearDown(self):
        self.rpc.close()

    def test_config_watch_holding_status_and_text(self):
        hub = fd.Feeds({"rpc_http": self.rpc.url, "curve_poll_sec": 0.5, "curve_idle_sec": 1.0, "unrelated": 1})
        self.assertIsNone(hub.socket)                                               # off by default
        self.assertEqual(hub.poller.interval, 1.0)
        hub.set_holding(True)
        self.assertEqual(hub.poller.interval, 0.5)
        hub.set_holding(False)
        self.assertEqual(hub.poller.interval, 1.0)
        self.rpc.accounts["k"] = account()
        self.assertTrue(hub.watch("m", "k"))
        self.assertEqual(hub.watched(), {"m": "k"})
        hub.poller.poll()
        ev = hub.get(0.5)
        self.assertEqual((ev["kind"], ev["mint"]), ("curve", "m"))
        self.assertIsNone(hub.get(0.01))
        self.assertIsNone(hub.get(0))
        self.assertEqual([s["name"] for s in hub.status()], ["pumpportal", "curve-poll", "metadata"])
        self.assertIn("curve poll 1 s · 1 watched · ws off", hub.text())
        self.assertTrue(hub.unwatch("m"))
        self.assertEqual(hub.drain(), [])

    def test_odd_config_values_fall_back_never_raise(self):
        hub = fd.Feeds({"meta_workers": None, "feed_queue_max": None, "meta_timeout_s": "x", "curve_idle_sec": None,
                        "ipfs_gateways": "https://dweb.link/ipfs"})
        self.assertEqual((hub.meta.workers, hub.q.maxsize, hub.meta.timeout, hub.poller.interval), (4, 20000, 2.0, 1.0))
        self.assertEqual(hub.meta.gateways, ["https://dweb.link/ipfs/"])            # not one gateway per character
        self.assertEqual(fd.Feeds({"ipfs_gateways": [None, 3]}).meta.gateways, list(fd.IPFS_GATEWAYS))
        self.assertEqual(fd.Feeds("junk").poller.interval, 1.0)

    def test_start_and_stop_everything_with_pumpportal_unreachable(self):
        hub = fd.Feeds({"rpc_http": self.rpc.url, "pumpportal_url": "ws://127.0.0.1:9", "curve_ws": True,
                        "rpc_ws": "ws://127.0.0.1:9", "curve_idle_sec": 0.1})
        hub.pump.proxy = None
        hub.socket.proxy = None
        self.rpc.accounts["k"] = account()
        hub.watch("m", "k")
        hub.start()
        self.assertTrue(wait_for(lambda: any(e["kind"] == "curve" for e in hub.drain()), 3))
        t0 = time.monotonic()
        self.assertTrue(hub.stop(3))
        self.assertLess(time.monotonic() - t0, 2.5)
        st = {s["name"]: s for s in hub.status()}
        if fd.ws_connect is not None:
            self.assertGreaterEqual(st["pumpportal"]["errors"], 1)                  # refused, and backing off
        self.assertFalse(st["pumpportal"]["connected"])


class Probe(unittest.TestCase):
    def setUp(self):
        self.rpc = FakeRPC()
        self.gw, self.gw_base = http_server(lambda *a: (200, json.dumps(Meta.DOC).encode(), {}))
        self.srv = None
        self.ws = None

    def tearDown(self):
        for s in (self.srv, self.ws):
            if s:
                s.close()
        self.rpc.close()
        self.gw.shutdown()
        self.gw.server_close()

    @unittest.skipIf(ws_serve is None, NEEDS_WS)
    def test_the_cli_probe_against_local_fakes(self):
        self.rpc.accounts["kP1"] = account(vtok=int(1073000000.0 * 1e6), vsol=int(30.0 * 1e9))

        def pump(ws, n):
            ws.recv(timeout=2)
            ws.recv(timeout=2)
            ws.send(json.dumps(ACKS[0]))
            ws.send(create("P1", key="kP1", uri="ipfs://QmP1"))
            self.srv.hold(ws)

        self.srv = WSServer(pump)
        self.ws = WSServer(lambda ws, n: self.ws.hold(ws))                         # a socket that never pushes
        lines = []
        rc = fd.main(["--probe", "2.5", "--interval", "0.2", "--pumpportal", self.srv.url, "--rpc", self.rpc.url,
                      "--rpc-ws", self.ws.url, "--gateways", self.gw_base + "/ipfs/"], out=lines.append)
        self.assertEqual(rc, 0)
        text = "\n".join(lines)
        self.assertIn("=== FEEDS PROBE", text)
        self.assertIn("pumpportal  creates 1", text)
        summary = json.loads([x for x in lines if x.startswith("SUMMARY ")][0][len("SUMMARY "):])
        self.assertEqual(summary["pumpportal"]["creates"], 1)
        self.assertGreaterEqual(summary["curve_poll"]["ok"], 3)
        self.assertGreaterEqual(summary["curve_poll_keepalive"]["ok"], 3)
        self.assertEqual(summary["decode"]["checked"], 1)
        self.assertEqual(summary["decode"]["within_0.5pct"], 1)
        self.assertGreaterEqual(summary["decode"]["discriminator_ok"], 1)
        self.assertEqual(summary["metadata"]["results"], 1)
        self.assertEqual(summary["metadata"]["with_x"], 1)
        self.assertEqual(summary["coins"][0]["symbol"], "P1")
        self.assertIsNotNone(summary["curve_poll"]["ms_median"])
        self.assertGreaterEqual(summary["curve_ws"]["subscribe_calls"], 1)

    def test_the_cli_without_websockets_still_prints_a_summary(self):
        saved = fd.ws_connect
        fd.ws_connect = None
        try:
            lines = []
            self.assertEqual(fd.main(["--probe", "0.3", "--rpc", self.rpc.url, "--no-compare"], out=lines.append), 0)
        finally:
            fd.ws_connect = saved
        self.assertIn("websockets not installed", lines[0])
        self.assertTrue(lines[-1].startswith("SUMMARY "))
        self.assertEqual(fd.main([], out=lines.append), 2)                          # no --probe: the help


if __name__ == "__main__":
    unittest.main()
