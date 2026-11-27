"""Odd curve readings (newpairs.curve_sane, the night of 2026-11-27): a reading no standard pump.fun curve can be in (a
zeroed or not yet initialized account, another program's layout, reserves off virtual SOL = 30 + real SOL, a reserves
product off the launch curve's, a price under the launch price, past the completion state, a supply that is not 1 B)
is never a price, a fill, an exit's reading or the LEARNER's: the watch lists' gate keeps it out of the coins, a
watched coin (a snipe's first reading) is rejected for good on it, a held one keeps its last sane state and sells by
Jupiter, or by the curve maths only on a sane reading; a curve-filled entry's sale on a Jupiter quote far beyond what
the curve state that decided it gives is booked by the curve maths on that state (the Coke-Cola mismatch). The root cause of the six records of that night, rebuilt from
synthetic account bytes with the bot's own maths. The tape marks an odd reading, the replay's market prices none, the
study counts them and keeps a SNIPE run's fire lines out of its momentum checks. Offline, made-up coins: these tests
say the code does what it says, never that a trade makes money."""

import base64
import hashlib
import json
import os
import shutil
import struct
import tempfile
import unittest
import unittest.mock

from _load import load

import test_newpairs_engine as E
import test_shadow as S
import test_snipe as SN

os.environ["MEMEBOT_LIVE"] = "0"
os.environ["MEMEBOT_FEEDS"] = "0"
mb = E.mb
npm = mb.newpairs
feeds = load("memebot", "feeds")
rec_mod = load("memebot", "recorder")
K = E.K
DISC = hashlib.sha256(b"account:BondingCurve").digest()[:8]
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def account(vtok, vsol, rsol, rtok=None, supply=10 ** 15, complete=False, disc=DISC, size=166):
    """A bonding-curve account's bytes: discriminator, five u64 (virtual tokens, virtual SOL, real tokens, real SOL,
    supply), the complete flag, zero padding to `size`."""
    if rtok is None:
        rtok = max(0, vtok - 279900000000000)
    raw = disc + struct.pack("<5Q", int(vtok), int(vsol), int(rtok), int(rsol), int(supply)) + bytes([1 if complete else 0])
    return raw + bytes(max(0, size - len(raw)))


def event(raw, mint="m", owner=PUMP, slot=1000):
    """feeds.py's curve event for these account bytes, as the poller makes it (_reading: decode, disc_ok, owner_ok)."""
    d = feeds.decode_curve(raw)
    if d is None:
        return None
    d["disc_ok"] = bytes(raw[:8]) == feeds.CURVE_DISC
    feeds._owner(d, {"owner": owner, "data": [base64.b64encode(raw).decode(), "base64"]})
    ev = {"kind": "curve", "mint": mint, "key": "curve-" + mint, "price_sol": d["price_sol"], "vtok": d["vtok"],
          "vsol": d["vsol"], "rtok": d["rtok"], "rsol": d["rsol"], "complete": d["complete"], "slot": slot, "src": "poll"}
    for k in ("supply", "disc_ok", "owner_ok"):
        ev[k] = d[k]
    return ev


def std(rsol, **kw):
    """A standard curve's account at rsol real SOL."""
    vsol = 30.0 + rsol
    return account(round(K / vsol * 1e6), round(vsol * 1e9), round(rsol * 1e9), **kw)


def odd_ev(mint, slot, vsol_lamports, vtok_raw, rsol_lamports=0, complete=False):
    """A curve event with these raw reserves (an account read as reserves that no standard curve has)."""
    return {"kind": "curve", "mint": mint, "key": "curve-" + mint, "vsol": int(vsol_lamports), "vtok": int(vtok_raw),
            "rtok": 0, "rsol": int(rsol_lamports), "complete": complete, "slot": slot, "src": "poll",
            "price_sol": (vsol_lamports / 1e9) / (vtok_raw / 1e6) if vtok_raw else 0.0}


# The six records of 2026-11-27, and the readings they were booked on, worked back from each record with the bot's own
# entry maths (np_shadow_curve_buy: impact = vsol / (vsol + net) of the order's SOL net of pump.fun's 1.25 %, tokens =
# vtok x that share, the 1 % haircut; SOL $72 that night, from the launch-state snipes): (symbol, cost, entry_price,
# slip %, sol_in, dev %, proceeds, exit_price, exit, sold by the curve maths, the reading in raw units).
SIX = [
    # a reading with 1.07 B virtual tokens (the launch's) and 0.0145 SOL virtual: "-100 % since the create", mcap 0
    ("Evelyn", 6.93, 7.456267275987445e-09, 86.64, 0.0, 0.0, 0.40, 9.739707222021297e-10, "stop -87%", True,
     (14504000, 1072300000000000, 0)),
    # the same kind with the creator's 2.25 % in the tokens (1.045 B) and no real SOL: Jupiter sold the 0.8 B tokens
    ("530A", 6.38, 7.898751970986586e-09, 77.23, -0.64, 2.25, 1416.50, 1.758423150915402e-09, "stop -78%", False,
     (25508000, 1044500000000000, 0)),
    ("SpaceXers", 114.08, 1.0904285799499903e-07, 98.43, 0.0, 0.0, 0.80, 1.6779902552763053e-09, "stop -98%", True,
     (24941000, 1072900000000000, 0)),
    # 1293 SOL virtual against 1.01 B tokens: "+4432 %", mcap 1267, the virtual SOL 1226 over 30 + real
    ("Pumpers", 37.79, 9.404338637612381e-05, 0.04, 66.82, 0.0, 1.30, 9.60563320491705e-05, "dump: 16.0 SOL sold", False,
     (1292800000000, 1012800000000000, 66820000000)),
    # 3.53 SOL virtual against 1.015 B tokens: "-88 %", mcap 3.5, the product 1/9 of a curve's
    ("Coke-Cola", 20.62, 2.7636520211189987e-07, 7.4, 0.19, 0.0, 253.74, 2.7681541033235036e-07, "5 min, only +0%", False,
     (3526600000, 1014900000000000, 190000000)),
    # a standard curve 86 % through (73.1 SOL in at 0.2 s: a bundled launch): +771 %, mcap 330, booked +10.4 %
    ("Thanksgiving", 130.35, 2.4783118236970233e-05, 1.7, 68.16, 15.17, 143.97, 2.8085003635875448e-05, "TP +10%",
     False, (103100000000, round(K / 103.1 * 1e6), 73100000000)),
]
SOL_USD = 72.0
FEE_SOL = 0.001                         # network + priority fee of one order


class Sanity(unittest.TestCase):
    def test_every_standard_state_is_sane_in_every_form(self):
        for r in (0.0, 0.3, 5.0, 30.0, 73.1, 85.0, npm.END_RSOL):
            self.assertIsNone(npm.curve_sane(event(std(r))), r)                       # raw, from the account's bytes
            v = 30.0 + r
            self.assertIsNone(npm.curve_sane({"vsol": v, "vtok": K / v, "rsol": r}), r)  # SOL and whole tokens
            self.assertIsNone(npm.curve_sane({"real_sol": r, "price_sol": v * v / K}), r)   # read_curve's other forms
            self.assertIsNone(npm.curve_sane({"vSolInBondingCurve": v, "vTokensInBondingCurve": K / v}), r)
        self.assertIsNone(npm.curve_sane(E.reading("a", 2.0, 5)))                      # the engine tests' readings
        self.assertIsNone(npm.curve_sane(event(std(1.0, complete=True))))              # complete at a standard state
        # the tolerances (newpairs.py says why): 1000 lamports off the identity, 0.5 % on the product, pass
        self.assertIsNone(npm.curve_sane({"vsol": 31000001000, "vtok": round(K / 31 * 1e6), "rsol": 10 ** 9}))
        self.assertIsNone(npm.curve_sane({"vsol": 31e9, "vtok": round(K / 31 * 1e6 * 1.005), "rsol": 1e9}))

    def test_odd_states_say_why(self):
        cases = [
            (event(bytes(166)), "not a pump.fun bonding curve (its Anchor discriminator)"),   # zeroed: no discriminator
            (dict(event(bytes(166)), disc_ok=True), "no reserves (a zeroed or not yet initialized account)"),
            (odd_ev("a", 1, 0, 0), "no reserves (a zeroed or not yet initialized account)"),
            (odd_ev("a", 1, 31e9, 0, 1e9), "no virtual tokens"),
            (odd_ev("a", 1, 25508000, 1044500000000000), "virtual SOL 0.02551 is not 30 + real SOL 0 (off -29.97 SOL)"),
            ({"vsol": 31e9, "vtok": 1.02e15, "rsol": 1e9}, "reserves product 0.9823x a standard curve's (31 SOL x 1.02e+09 "
                                                            "tokens)"),
            ({"vsol": 30e9, "vtok": 1.073e15 * 1.005, "rsol": 0}, "price 2.782e-08 SOL under the launch price 2.796e-08"),
            (event(std(87.0)), "past the completion state (87 real SOL, 2.751e+08 tokens left virtual)"),
            (event(std(1.0, supply=2 * 10 ** 15)), "supply 2e+09 tokens (a pump.fun coin has 1 B)"),
            (event(std(1.0), owner="SomeOtherProgram1111111111111111111111111"),
             "not a pump.fun bonding curve (the account's owner is not the pump.fun program)"),
            ({"vsol": float("nan"), "vtok": 1, "rsol": 0}, "vsol is not a number (nan)"),
            ({"price_sol": 3e-8}, "no reserves in the reading"),
            ("text", "not a reading"),
        ]
        for r, why in cases:
            self.assertEqual(npm.curve_sane(r), why, r)

    def test_a_foreign_layout_with_the_same_discriminator_reads_odd(self):
        # Anchor's discriminator is sha256("account:BondingCurve")[:8] for ANY program with an account of that name: a
        # fork that keeps virtual SOL first passes the discriminator check and reads as nonsense reserves
        raw = DISC + struct.pack("<5Q", 30 * 10 ** 9, 1073 * 10 ** 12, 0, 793100 * 10 ** 9, 10 ** 15) + bytes(118)
        ev = event(raw, owner="ForkLaunchpad111111111111111111111111111111")
        self.assertIs(ev["disc_ok"], True)
        self.assertIs(ev["owner_ok"], False)
        self.assertIn("owner is not the pump.fun program", npm.curve_sane(ev))
        ev = dict(ev, owner_ok=True)                    # even when the owner is not known: the reserves are not a curve's
        self.assertTrue(npm.curve_sane(ev).startswith("virtual SOL 1.073e+06 is not 30 + real SOL 7.931e+05"), ev)
        d = feeds.decode_curve(std(1.0))
        feeds._owner(d, {"data": ["", "base64"]})                               # an answer without an owner: not judged
        self.assertNotIn("owner_ok", d)
        self.assertIsNone(feeds.decode_curve(bytes(48)))                        # too short to be one at all
        d = feeds.decode_curve(bytes(166))
        self.assertEqual((d["vsol"], d["vtok"], d["price_sol"]), (0, 0, 0.0))  # a zeroed account: price 0, no check

    def test_the_six_records_of_2026_11_27_rebuilt_from_account_bytes(self):
        """The root cause, record by record: each reading (worked back from the record) as account bytes, decoded by
        feeds.decode_curve() as the live poller did, gives the record's own numbers through the bot's own entry maths
        (curve_buy_tokens: impact and entry price) and, where the curve maths sold it, its proceeds through
        curve_sell_sol on the same reading; where Jupiter sold it (530A, Coke-Cola, Pumpers), the proceeds are what a
        real market paid for tokens the reading had mispriced. curve_sane() rejects the five impossible readings and
        passes Thanksgiving's (a standard curve 86 % through)."""
        for sym, cost, entry_px, slip, sol_in, dev, proceeds, exit_px, why, by_curve, (vs, vt, rs) in SIX:
            ev = event(account(vt, vs, rs), sym)
            verdict = npm.curve_sane(ev)
            usd_in = cost - FEE_SOL * SOL_USD
            sol = usd_in / SOL_USD
            got = mb.curve_buy_tokens(ev["vsol"] / 1e9, ev["vtok"] / 1e6, sol, 1.25)
            spot = sol * (1 - 0.0125) / ev["price_sol"]
            impact = (1 - got / spot) * 100
            tokens = got * 0.99                             # the latency haircut
            self.assertAlmostEqual(impact, slip, delta=0.6, msg=sym)
            self.assertAlmostEqual(usd_in / tokens / entry_px, 1.0, delta=0.03, msg=sym)
            mcap = ev["price_sol"] * 1e9
            if sym == "Thanksgiving":
                self.assertIsNone(verdict)
                self.assertAlmostEqual(mcap, 330.2, delta=1.5)
                continue
            self.assertIsNotNone(verdict, sym)
            self.assertTrue(verdict.startswith("virtual SOL "), verdict)        # all five: vsol is not 30 + real SOL
            self.assertTrue(mcap < 27.9 or mcap > 411, (sym, mcap))
            if by_curve:                                    # sold by the curve maths on that very reading (unchanged)
                back = mb.curve_sell_sol(ev["vsol"] / 1e9, ev["vtok"] / 1e6, tokens, 1.25) * SOL_USD * 0.99 - FEE_SOL * SOL_USD
                self.assertAlmostEqual(back, proceeds, delta=0.1, msg=sym)
            else:                                           # Jupiter's quote of the real market, not the reading's
                ratio = proceeds / (tokens * exit_px)
                self.assertTrue(ratio > 10 or ratio < 0.1, (sym, ratio))
        # 530A's +$1410: 808 M tokens (80 % of the supply) for $6.31 on a 0.026 SOL reading; Jupiter paid 19.9 SOL
        sym, cost, entry_px, *_ = SIX[1]
        self.assertGreater((cost - FEE_SOL * SOL_USD) / entry_px, 793.1e6)  # more than a curve ever sells


class Gate(unittest.TestCase):
    def wl(self, **kw):
        c = dict(npm.DEFAULTS, np_need_social=False, np_max_dev_pct=70.0, np_serial_max=1)
        c.update(kw)
        return npm.Watchlist(c), c

    def test_a_snipes_odd_first_reading_rejects_it_for_good(self):
        wl, c = self.wl(np_snipe=True)
        for mint, ev in (("zero", odd_ev("zero", 5, 0, 0)), ("fork", odd_ev("fork", 5, 25508000, 1044500000000000))):
            coin = wl.on_create(E.create(mint, mint.upper()), 0.0)
            price = coin.price
            self.assertIsNone(wl.on_curve(mint, dict(ev, mint=mint), 1.0))
            self.assertTrue(coin.status.startswith("rejected: odd curve reading: "), coin.status)
            self.assertEqual((coin.readings, coin.price, coin.odd, coin.odd_now), (0, price, 1, True))
            self.assertEqual(wl.judge(coin, 1.0), ("wait", coin.status))        # never a fire
            self.assertIsNotNone(wl.on_curve(mint, E.reading(mint, 0.8, 6), 2.0))   # a sane one later: still rejected
            self.assertEqual(wl.judge(coin, 2.0)[0], "wait")
            self.assertTrue(coin.status.startswith("rejected: odd curve reading"))
        self.assertEqual(wl.odd, 2)

    def test_a_momentum_coin_with_an_odd_reading_is_rejected_and_never_takes_it(self):
        wl, c = self.wl()
        coin = wl.on_create(E.create("mom", "MOM"), 0.0)
        wl.on_curve("mom", E.reading("mom", 1.0, 5), 1.0)
        before = (coin.price, coin.rsol, coin.sells, coin.sell_max)
        self.assertIsNone(wl.on_curve("mom", odd_ev("mom", 6, 3526600000, 1014900000000000, 190000000), 2.0))
        self.assertEqual((coin.price, coin.rsol, coin.sells, coin.sell_max), before)    # no "sell" of 0.8 SOL
        self.assertEqual(coin.status, "rejected: odd curve reading: virtual SOL 3.527 is not 30 + real SOL 0.19 "
                                      "(off -26.66 SOL)")

    def test_a_held_coin_going_odd_keeps_its_last_sane_state_and_goes_stale_until_a_sane_reading(self):
        wl, c = self.wl()
        coin = wl.on_create(E.create("held", "HELD"), 0.0)
        wl.on_curve("held", E.reading("held", 2.0, 5), 1.0)
        wl.mark_entered(coin, 1.0)
        state = (coin.price, coin.vsol, coin.vtok, coin.rsol, coin.readings, coin.last_t)
        # an odd reading with a price near 0 (the old code: a "-100 %" stop and a 2 SOL "dump" on it)
        self.assertIsNone(wl.on_curve("held", odd_ev("held", 6, 25508000, 1044500000000000), 2.0))
        self.assertEqual((coin.price, coin.vsol, coin.vtok, coin.rsol, coin.readings, coin.last_t), state)
        self.assertEqual((coin.status, coin.odd, coin.odd_now, coin.sell_max_held), ("entered", 1, True, 0.0))
        self.assertIsNone(npm.exit_signal(coin, c))                         # no dump on it
        self.assertEqual(wl.touch(4.0, ["held"]), 0)                        # an unchanged poll keeps it no fresher
        self.assertEqual(coin.last_t, 1.0)
        self.assertTrue(npm.stale(coin, c, 7.0))                            # np_stale_s (5 s) after its last sane one
        self.assertIsNotNone(wl.on_curve("held", E.reading("held", 2.1, 7), 7.5))
        self.assertEqual((coin.odd_now, coin.readings, coin.last_t), (False, 2, 7.5))
        self.assertAlmostEqual(coin.rsol, 2.1)
        self.assertEqual(wl.touch(9.0, ["held"]), 1)                        # fresh again by the poll
        self.assertFalse(npm.stale(coin, c, 9.5))

    def test_a_migrated_curves_withdrawn_reserves_are_its_completion_not_an_odd_reading(self):
        wl, c = self.wl()
        coin = wl.on_create(E.create("done", "DONE"), 0.0)
        wl.on_curve("done", E.reading("done", 80.0, 5), 1.0)
        wl.mark_entered(coin, 1.0)
        price = coin.price
        got = wl.on_curve("done", odd_ev("done", 6, 115005000000, 279900000000000, 0, complete=True), 2.0)
        self.assertIs(got, coin)
        self.assertEqual((coin.complete, coin.price, coin.odd, wl.odd), (True, price, 0, 0))
        self.assertEqual(npm.exit_signal(coin, c), "migrating: take it")


def odd_counts(n):
    """The night's odd-reading counts (np_odd)."""
    return n.state["night"].get("odd")


class Bot(SN.Base):
    """The night itself (tests/test_snipe.py's harness: SNIPE on, SOL $100, Jupiter by the curve maths)."""

    def odd(self, n, mint, vs=25508000, vt=1044500000000000, rs=0, sec=1.0, left=400, keep_market=True):
        """An odd reading of mint; the market (Jupiter) stays where the real curve is (the fake feeds would move it)."""
        self.slot += 1
        before = self.m.curve.get(mint)
        self.turn(n, odd_ev(mint, self.slot, vs, vt, rs), sec=sec, left=left)
        if keep_market and before is not None:
            self.m.curve[mint] = before

    def test_a_snipe_whose_first_reading_is_odd_is_never_bought_counted_and_said_once(self):
        n = self.night(SN.cfg())
        seen = []
        if n.rec is not None:
            orig = n.rec.event
            n.rec.event = lambda ev: (seen.append(ev), orig(ev))[1]
        self.turn(n, E.create("bad", "BAD"), sec=0.5)
        self.odd(n, "bad", sec=0.4)
        coin = n.wl.get("bad")
        self.assertEqual(n.state["positions"], {})
        self.assertEqual(coin.status, "rejected: odd curve reading: virtual SOL 0.02551 is not 30 + real SOL 0 "
                                      "(off -29.97 SOL)")
        self.assertEqual(odd_counts(n), {"readings": 1, "coins": 1, "followed": 0, "mints": ["bad"]})
        self.odd(n, "bad", vs=26508000)                                      # a second odd reading: counted, not said
        for rsol in (0.8, 1.5):                                              # sane ones later: never bought
            self.read(n, "bad", rsol)
        self.assertEqual(n.state["positions"], {})
        self.assertEqual(odd_counts(n), {"readings": 2, "coins": 1, "followed": 0, "mints": ["bad"]})
        said = self.lines(n, "SCANNER", "odd")
        self.assertEqual(len(said), 1)
        self.assertIn("odd curve reading: virtual SOL 0.02551 is not 30 + real SOL 0", said[0])
        self.assertIn("never bought: rejected for good", said[0])
        self.assertFalse(self.lines(n, "EXECUTOR", "enter"))
        self.assertEqual(self.m.quotes, [])                                  # no Jupiter quote on it either
        if n.rec is not None:                                                # the tape keeps it as it is, marked
            c = [e for e in seen if e.get("kind") == "curve" and e.get("mint") == "bad"]
            self.assertTrue(c[0]["odd"].startswith("virtual SOL 0.02551"), c[0])
            self.assertEqual((c[0]["vsol"], c[0]["vtok"]), (25508000, 1044500000000000))
            self.assertNotIn("odd", c[-1])
        sp = n.panel_doc()["speed"]                                          # the page's speed line says it
        self.assertEqual((sp["odd_readings"], sp["odd_coins"]), (2, 1))
        self.assertIn("odd curve readings 2 (1 coin, never a price)", sp["feed"])
        # a good coin right after is bought as always
        self.assertEqual(self.first(n, "good", 0.8).status, "entered")

    def test_a_zeroed_first_reading_is_rejected_too_not_skipped(self):
        # before: np_curve dropped a price-0 reading before the watch list saw it, so the NEXT reading was the snipe's
        n = self.night(SN.cfg())
        self.turn(n, E.create("zero", "ZERO"), sec=0.5)
        self.slot += 1
        self.turn(n, E.reading("zero", 0.0, self.slot, zero=True), sec=0.4)
        self.read(n, "zero", 0.8, sec=0.4)
        self.assertEqual(n.state["positions"], {})
        self.assertEqual(n.wl.get("zero").status,
                         "rejected: odd curve reading: no reserves (a zeroed or not yet initialized account)")

    def test_a_held_snipe_whose_readings_turn_odd_keeps_its_last_sane_price_and_never_fills_on_them(self):
        n = self.night(SN.cfg())
        coin = self.first(n, "hold", 0.8)
        pos = n.state["positions"]["hold"]
        last, price = pos["last"], coin.price
        self.odd(n, "hold", sec=0.5)                     # a "-100 %" price: the old code sold it at its stop at once
        self.assertIn("hold", n.state["positions"])
        self.assertEqual((pos["last"], coin.price, coin.odd_now, coin.status), (last, price, True, "entered"))
        said = self.lines(n, "SCANNER", "odd")
        self.assertEqual(len(said), 1)
        self.assertIn("held: its exits decide on its last sane price", said[0])
        # Jupiter has no route now; the clock's exits still run (quiet curve, time stops) and want it out
        self.m.no_route.add("hold")
        for _ in range(4):
            self.tick(n, 60)
        self.assertIn("hold", n.state["positions"])                          # the curve maths never sells on it
        stuck = self.lines(n, "EXECUTOR", "stuck")
        self.assertTrue(stuck, self.lines(n))
        self.assertTrue(all(x.startswith("can't sell (no Jupiter route)") for x in stuck), stuck)
        self.assertEqual(n.state["night"]["trades"], [])
        # a sane reading (after the failed sale's 5 s wait): the exit decided on it (its +12 % takes the scalp's +10 %),
        # sold by the curve maths on that very state, Jupiter still without a route
        self.tick(n, 5)
        self.read(n, "hold", self.rsol(pos, 12.0), sec=5.5)
        self.assertNotIn("hold", n.state["positions"])
        tr = n.state["night"]["trades"][-1]
        self.assertEqual((tr["symbol"], tr["fill"], tr["why"]), ("HOLD", "curve", "TP +10%"))
        self.assertGreater(tr["pnl"], 0)
        tokens = pos["tokens_ui"]
        want = mb.curve_sell_sol(coin.vsol, coin.vtok, tokens, 1.25) * 100.0 * 0.99 - n.fee_usd(100.0, pos)
        self.assertAlmostEqual(tr["proceeds"], round(want, 2), places=2)
        self.assertAlmostEqual(tr["exit_price"], coin.price * 100.0)

    def test_the_coke_cola_mismatch_a_sale_is_booked_on_the_state_that_decided_it(self):
        n = self.night(SN.cfg())
        self.m.no_route.add("coke")
        coin = self.first(n, "coke", 0.8)                                    # filled by the curve maths (no route)
        pos = n.state["positions"]["coke"]
        self.assertEqual(pos["entry_fill"], "curve")
        self.m.no_route.discard("coke")
        self.m.tax["coke"] = -11.0                                           # Jupiter now pays 12x what the curve gives
        tokens, decided = pos["tokens_ui"], coin.price * 100.0
        self.assertTrue(n.close_part("coke", 1.0, "5 min, only +0%", 100.0))
        tr = n.state["night"]["trades"][-1]
        want = mb.curve_sell_sol(coin.vsol, coin.vtok, tokens, 1.25) * 100.0 * 0.99 - n.fee_usd(100.0, pos)
        self.assertAlmostEqual(tr["proceeds"], round(want, 2), places=2)     # not 12x
        ratio = tr["proceeds"] / (tokens * decided)
        self.assertTrue(0.9 < ratio < 1.0, ratio)                            # the sale's costs, nothing more
        self.assertEqual(tr["fill"], "curve")
        self.assertIn("Jupiter's quote 12.0x what the curve state that decided the sale gives (the entry was filled by "
                      "the curve maths): booked by the curve maths on that state", self.lines(n, "EXECUTOR", "exit")[-1])
        # a quote-filled entry's quote stands, under the state (a real sale fills worse) or over it (a move: the cap is
        # only for curve-filled entries far beyond their state, tests/test_curve_sane_review.py)
        coin2 = self.first(n, "cheap", 0.8)
        pos2 = n.state["positions"]["cheap"]
        self.m.tax["cheap"] = 0.3
        q = mb.curve_sell_sol(coin2.vsol, coin2.vtok, pos2["tokens_ui"], 1.25) * 0.7
        self.assertTrue(n.close_part("cheap", 1.0, "stop -10%", 100.0))
        tr2 = n.state["night"]["trades"][-1]
        self.assertAlmostEqual(tr2["proceeds"], round(q * 100.0 * 0.99 - n.fee_usd(100.0, pos2), 2), places=2)
        self.assertNotIn("fill", tr2)
        # and a quote a little over it (a fill a moment later) stands too
        coin3 = self.first(n, "up", 0.8)
        pos3 = n.state["positions"]["up"]
        self.m.tax["up"] = -0.03
        q = mb.curve_sell_sol(coin3.vsol, coin3.vtok, pos3["tokens_ui"], 1.25) * 1.03
        self.assertTrue(n.close_part("up", 1.0, "TP +10%", 100.0))
        self.assertAlmostEqual(n.state["night"]["trades"][-1]["proceeds"],
                               round(q * 100.0 * 0.99 - n.fee_usd(100.0, pos3), 2), places=2)

    def test_a_held_momentum_coin_is_gated_the_same_way(self):
        n = self.night(S.cfg())                                              # the momentum entry (no np_snipe)
        self.launch(n, "mom", E.RUNNER, social=(True, True, True))
        pos, coin = n.state["positions"]["mom"], n.wl.get("mom")
        self.assertNotIn("snipe", pos)
        last, rsol = pos["last"], coin.rsol
        self.odd(n, "mom", vs=3526600000, vt=1014900000000000, rs=190000000, sec=0.5)   # Coke-Cola's kind of reading
        self.assertIn("mom", n.state["positions"])                           # no stop and no "dump" on it
        self.assertEqual((pos["last"], coin.rsol, coin.odd_now, coin.sell_max_held), (last, rsol, True, 0.0))
        self.m.no_route.add("mom")                                           # Jupiter fails: the curve maths is the
        self.assertIsNone(n.sell_quote(pos, pos["raw"], 100.0))              # fallback, never on an odd newest reading
        self.read(n, "mom", rsol + 0.1, sec=0.5)                             # sane again: the curve maths may sell
        self.assertFalse(coin.odd_now)
        self.assertIsNotNone(n.sell_quote(pos, pos["raw"], 100.0))

    def test_the_shadows_never_open_or_exit_on_an_odd_reading(self):
        n = self.night(SN.cfg())
        with unittest.mock.patch.object(n, "why_no_entries", return_value="8 positions open (max)"):
            self.turn(n, E.create("sodd", "SODD"), sec=0.5)
            self.odd(n, "sodd", sec=0.4)                                     # RISK blocks it, and its reading is odd
            self.assertNotIn("sodd", n.shadows)                              # no shadow on it
            self.assertTrue(n.wl.get("sodd").status.startswith("rejected: odd curve reading"))
            self.turn(n, E.create("sh", "SH"), sec=0.5)
            self.read(n, "sh", 0.8, sec=0.4)                                 # a sane first reading: a snipe shadow
        sh = n.shadows["sh"]
        sc = n.np_shadow_wl.get("sh")
        last, price = sh["last"], sc.price
        self.odd(n, "sh", sec=0.5)                                           # its stop would fire on that price
        self.assertIn("sh", n.shadows)
        self.assertEqual((sh["last"], sc.price, sc.odd_now), (last, price, True))
        self.read(n, "sh", self.rsol(sh, 12.0), sec=0.5)                     # a sane +12 %: the plan sells it
        self.assertNotIn("sh", n.shadows)
        row = n.state["shadow"]["closed"][-1]
        self.assertEqual(row["exit"], "TP +10%")
        self.assertGreater(row["pnl_pct"], 0)


class Tape(unittest.TestCase):
    def test_the_recorder_keeps_an_odd_reading_as_it_is_marked(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        clock = [1795744800.0]
        r = rec_mod.Recorder(os.path.join(d, "tapes"), {}, clock=lambda: clock[0], log=lambda s: None)
        self.addCleanup(r.close, 5)
        cr = E.create("tt", "TT")
        cr.update(_rx=clock[0], t=clock[0])
        r.event(cr)
        ev = dict(odd_ev("tt", 7, 25508000, 1044500000000000), t=clock[0] + 1)
        r.event(dict(ev, odd=npm.curve_sane(ev)))
        r.event(dict(E.reading("tt", 0.8, 8), t=clock[0] + 2))
        clock[0] += 3
        self.assertTrue(r.close(5))
        lines = [x for _, p in rec_mod.tape_files(os.path.join(d, "tapes")) for x in rec_mod.read_lines(p)]
        c = [x for x in lines if x["k"] == "c"]
        self.assertEqual((c[0]["vs"], c[0]["vt"], c[0]["rs"]), (25508000, 1044500000000000, 0))
        self.assertTrue(c[0]["o"].startswith("virtual SOL 0.02551 is not 30 + real SOL 0"), c[0])
        self.assertNotIn("o", c[1])

    def test_the_replays_market_prices_no_quote_on_an_odd_reading(self):
        rp = load("memebot", "replay")
        tp = rp.Tape()
        tp.add({"k": "c", "m": "rr", "t": 10.0, "s": 1, "vs": 31 * 10 ** 9, "vt": round(K / 31 * 1e6), "rs": 10 ** 9})
        tp.add({"k": "c", "m": "rr", "t": 11.0, "s": 2, "vs": 25508000, "vt": 1044500000000000, "rs": 0})
        tp.finish()
        st = tp.state_at("rr", 12.0)
        self.assertEqual(st[0], 10.0)                                       # the last SANE state prices Jupiter's quote
        self.assertAlmostEqual(st[1], 31.0)


class Study(unittest.TestCase):
    def setUp(self):
        self.st = load("memebot", "study")
        import test_study as TS
        self.TS = TS
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def tape(self, cfg, entry=None):
        TS = self.TS
        tp = TS.Tape(cfg=cfg)
        m = tp.coin(TS.mint(1), TS.START + 100, [0.8, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5], fires=(1.0,))
        if entry:
            for x in tp.lines:
                if x["k"] == "fire":
                    x["entry"] = entry
        # an odd reading on another coin (a 530A-like account), and one on the first
        m2 = tp.coin(TS.mint(2), TS.START + 300, [0.5, 0.9])
        tp.add({"k": "c", "m": m2, "t": TS.START + 302.5, "s": 5, "vs": 25508000, "vt": 1044500000000000, "rs": 0},
               {"k": "c", "m": m, "t": TS.START + 109.5, "s": 6, "vs": 3526600000, "vt": 1014900000000000,
                "rs": 190000000})
        folder = os.path.join(self.dir, "t%d" % len(os.listdir(self.dir)))
        os.makedirs(folder)
        return self.TS.views_of(tp.write(folder))

    def test_a_snipe_runs_fire_lines_are_not_momentum_fires_and_the_odd_readings_are_counted(self):
        TS, st = self.TS, self.st
        views, mk, _ = self.tape(dict(TS.CFG, np_snipe=True))
        fc = st.fire_check(views)
        self.assertEqual((fc["fire_lines"], fc["found_within_2s"], fc["recomputed_without_a_line"],
                          fc["snipe_fire_lines"]), (0, 0, 0, 1))
        self.assertEqual(st.scan_fire(views[0], mk, [("REF", {})]), {})      # never a REF fire
        self.assertEqual(len(views[0].snipe_fires), 1)
        san = st.sanity(views)
        self.assertEqual((san["odd_readings"], san["odd_coins"]), (2, 2))
        md = st.markdown({"coverage": {"total": {"creates": 2}}, "sanity": san, "fire_check": fc})
        self.assertIn("2 odd by newpairs.curve_sane (2 coins", md)
        self.assertIn("1 SNIPE fire lines apart, never checked against it", md)
        # a fire line that says "entry": "snipe" is a snipe's whatever its run's start line says
        views, mk, _ = self.tape(dict(TS.CFG), entry="snipe")
        self.assertEqual((st.fire_check(views)["fire_lines"], st.fire_check(views)["snipe_fire_lines"]), (0, 1))
        # a momentum run's fire line is checked as before
        views, mk, _ = self.tape(dict(TS.CFG))
        fc = st.fire_check(views)
        self.assertEqual((fc["fire_lines"], fc["snipe_fire_lines"]), (1, 0))
        self.assertEqual(views[0].snipe_fires, [])


if __name__ == "__main__":
    unittest.main()
