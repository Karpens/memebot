"""memebot/newpairs.py tests, offline: PumpPortal create events and bonding-curve readings built
from the live probe's numbers (probe-result.md): a curve starts at 30 SOL / 1.073e9 tokens, FOLIO
rose +18 %, UZIM went 5.2 -> 27.1 SOL of real reserves (+163 %), the mayhem-mode SCAMSEM fell
-89 %, RugCheck flagged "Creator history of rugged tokens" and "Single holder ownership"."""

import base64
import json
import math
import random
import struct
import unittest

from _load import load

npm = load("memebot", "newpairs")
K = 30.0 * 1.073e9
T0 = 1795658400.0                       # 2026-11-26 02:00 UTC, 03:00 in Bratislava

FOLIO = {"signature": "5sig", "mint": "CT1x376p6atCGNmR2KqJ1vpnis4FPWPN22nfoZAbpump", "traderPublicKey": "DWrG7w9xPq",
         "txType": "create", "initialBuy": 10493887.4851, "solAmount": 0.296296295,
         "bondingCurveKey": "Fjjb4ZqTfX9PWkjdXSqdXNvmZP6RYodmJB9DFv6Qnqvx", "vTokensInBondingCurve": 1062506112.5149,
         "vSolInBondingCurve": 30.296296294999983, "marketCapSol": 28.51, "name": "Use Folio", "symbol": "FOLIO",
         "uri": "https://meta.uxento.io/data/18cf9133", "is_mayhem_mode": False, "pool": "pump"}
SOCIAL = {"name": "Uzi Monkey", "symbol": "UZIM", "twitter": "https://x.com/uzimonkey", "telegram": "https://t.me/uzim",
          "website": ""}


def create(mint, sym="UZIM", name="Uzi Monkey", dev_sol=0.7, creator=None, mayhem=False, **kw):
    """A PumpPortal create: the dev's buy of dev_sol on a fresh 30 SOL / 1.073e9 token curve."""
    vsol = 30.0 + dev_sol
    vtok = K / vsol
    ev = {"signature": "sig-" + mint, "mint": mint, "traderPublicKey": creator or "dev-" + mint, "txType": "create",
          "initialBuy": 1.073e9 - vtok, "solAmount": dev_sol, "bondingCurveKey": "curve-" + mint,
          "vTokensInBondingCurve": vtok, "vSolInBondingCurve": vsol, "marketCapSol": vsol / vtok * 1e9,
          "name": name, "symbol": sym, "uri": "https://pumpmeta.workers.dev/" + mint, "is_mayhem_mode": mayhem,
          "pool": "pump"}
    ev.update(kw)
    return ev


def dev_buy_for_pct(pct):
    """The dev's SOL that buys pct % of the supply on a fresh curve."""
    vtok = 1.073e9 - pct / 100.0 * 1e9
    return K / vtok - 30.0


def reading(rsol, slot=None, complete=False):
    """The decoded curve account in raw units (lamports, 1e-6 tokens), as memebot/probe.py gives it."""
    vsol = 30.0 + rsol
    vtok = K / vsol
    d = {"vsol": int(round(vsol * 1e9)), "vtok": int(round(vtok * 1e6)), "rsol": int(round(rsol * 1e9)),
         "rtok": int(round((vtok - 279.9e6) * 1e6)), "supply": 10 ** 15, "complete": complete}
    if slot is not None:
        d["slot"] = slot
    return d


def account(vsol_lamports, vtok_raw, rsol_lamports, complete=False):
    """The 166-byte bonding-curve account as the RPC returns it (base64)."""
    raw = bytes(range(8)) + struct.pack("<5Q", vtok_raw, vsol_lamports, max(0, vtok_raw - 279900000000000),
                                        rsol_lamports, 10 ** 15)
    raw += bytes([1 if complete else 0]) + bytes(166 - 49)
    return base64.b64encode(raw).decode()


def rise_to(coin_vsol0, pct):
    """The real SOL at which the price is pct % above a create at vsol0."""
    return coin_vsol0 * math.sqrt(1 + pct / 100.0) - 30.0


def report(mint, curve, holders=(1.1, 0.9, 0.6), risks=(), insiders=0, rugged=False, total=14, known=None,
           curve_pct=92.4, extra=()):
    """A RugCheck report for a coin on its curve: the curve's token account (owned by the curve)
    first, then the holders' pct (percent of the supply)."""
    th = [{"address": "ata-" + curve, "owner": curve, "pct": curve_pct, "insider": False}]
    th += [{"address": "h%d-%s" % (i, mint), "owner": "o%d" % i, "pct": p, "insider": False}
           for i, p in enumerate(holders)]
    th += list(extra)
    return {"mint": mint, "rugged": rugged, "risks": [dict(r) for r in risks], "topHolders": th,
            "graphInsidersDetected": insiders, "totalHolders": total, "knownAccounts": known or {},
            "markets": [{"pubkey": curve, "marketType": "pump_fun", "liquidityA": "ata-" + curve,
                         "liquidityAAccount": {"owner": curve}}],
            "creator": "dev-" + mint, "score": 1, "tokenMeta": {"name": "x", "symbol": "X"}}


# UZIM: a 0.7 SOL dev buy (2.3 %), the first reading 3 s after the launch at 5.2 SOL (snipers),
# readings every 0.8 s. At reading 10 it has 8 buys of 10 moves (80 %), 9.2 SOL in, +69 % and a
# 49 SOL market cap: all the entry rules hold for the first time there (reading 8 has 7 buys).
UZIM_RSOL = [5.2, 5.9, 6.4, 6.1, 7.0, 7.8, 8.3, 8.3, 9.1, 8.6, 9.9, 12.0, 15.5, 19.8, 23.0, 27.1]
UZIM_FIRE = 10


def uzim(wl, meta=SOCIAL):
    coin = wl.on_create(create("UZiMx9dQ4b2RkV7wq8zJcY3nTe5LpHf6aGsBvK1rpump"), T0)
    if meta is not False:
        wl.on_meta(coin.mint, meta)
    return coin


def t_of(i):
    return T0 + 3 + 0.8 * i


class Creates(unittest.TestCase):
    def test_the_probe_create_event(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = wl.on_create(FOLIO, T0)
        self.assertEqual((c.symbol, c.name, c.curve, c.creator), ("FOLIO", "Use Folio", FOLIO["bondingCurveKey"],
                                                                   "DWrG7w9xPq"))
        self.assertAlmostEqual(c.dev_pct, 1.0494, places=3)               # initialBuy / 1e9 * 100
        self.assertAlmostEqual(c.price0, 30.296296295 / 1062506112.5149, places=18)
        self.assertAlmostEqual(c.mcap_sol, 28.51, places=2)
        self.assertAlmostEqual(c.rsol0, 0.2963, places=4)                 # the dev's own buy is not inflow
        self.assertEqual((c.sol_in, c.buys, c.sells, c.rise_pct, c.evicted), (0.0, 0, 0, 0.0, []))
        self.assertEqual(npm.watch_text(c, T0 + 2), "new pair · 2 s old · 0.0 SOL in · dev 1.0% · socials pending")
        self.assertEqual(wl.curves(), {FOLIO["bondingCurveKey"]: FOLIO["mint"]})
        self.assertIsNone(wl.on_create(dict(FOLIO), T0 + 1))              # PumpPortal sent it twice

    def test_mayhem_mode_is_skipped_but_shown(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        ev = create("SCAMsEm1q2w3e4r5t6y7u8i9o0pAsDfGhJkLzXcVbN", "SCAMSEM", "Scam Season", dev_sol=0.0, mayhem=True,
                    uri="https://scamsem.mayhem.workers.dev/meta.json")
        ev["initialBuy"] = 0
        self.assertIsNone(wl.on_create(ev, T0))
        self.assertIsNone(wl.on_curve(ev["mint"], reading(4.0), T0 + 1))   # not watched
        rows = wl.rows(T0 + 5)
        self.assertEqual((rows[0]["symbol"], rows[0]["status"], rows[0]["age_s"]), ("SCAMSEM", "rejected: mayhem mode", 5))
        self.assertEqual(wl.curves(), {})
        self.assertEqual(wl.expire(T0 + 181), [])                          # nothing to unwatch ...
        self.assertEqual(wl.rows(T0 + 181), [])                            # ... and the row is gone

    def test_empty_symbol_name_curve_or_another_launchpad_are_skipped(self):
        wl = npm.Watchlist({})
        self.assertIsNone(wl.on_create(create("m1", sym="  "), T0))
        self.assertIsNone(wl.on_create(create("m2", name=""), T0))
        self.assertIsNone(wl.on_create(create("m3", bondingCurveKey=None), T0))
        self.assertIsNone(wl.on_create(create("m4", pool="bonk"), T0))
        self.assertIsNone(wl.on_create(dict(create("m5"), txType="migrate"), T0))
        why = sorted(r["why"] for r in wl.rows(T0))
        self.assertEqual(why, ["no bonding curve key", "no symbol or name", "no symbol or name",
                               "not a pump.fun curve (pool bonk)"])

    def test_the_dev_buy_rule(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        six = wl.on_create(create("dev6", dev_sol=dev_buy_for_pct(6.0)), T0)
        self.assertAlmostEqual(six.dev_pct, 6.0, places=6)
        self.assertEqual(npm.cheap_check(six, npm.DEFAULTS), (True, []))
        eight = wl.on_create(create("dev8", dev_sol=dev_buy_for_pct(8.0)), T0)
        self.assertTrue(npm.cheap_check(eight, npm.DEFAULTS)[0])           # at most 8 %: 8 passes
        big = wl.on_create(create("dev11", dev_sol=dev_buy_for_pct(11.2)), T0)
        self.assertEqual(npm.cheap_check(big, npm.DEFAULTS), (False, ["dev bought 11.2% (max 8%)"]))
        self.assertEqual(npm.cheap_check(six, dict(npm.DEFAULTS, np_max_dev_pct=5)),
                         (False, ["dev bought 6.0% (max 5%)"]))
        no_tokens = create("devsol", dev_sol=dev_buy_for_pct(6.0))
        del no_tokens["initialBuy"]                                        # from solAmount on the curve
        self.assertAlmostEqual(wl.on_create(no_tokens, T0).dev_pct, 6.0, places=6)

    def test_a_serial_launcher(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        a = wl.on_create(create("ser1", creator="SerialGuy"), T0)
        self.assertEqual(npm.cheap_check(a, npm.DEFAULTS), (True, []))
        b = wl.on_create(create("ser2", creator="SerialGuy"), T0 + 60)
        self.assertEqual(npm.cheap_check(b, npm.DEFAULTS), (False, ["serial launcher: 1 more coin in 60 min"]))
        self.assertEqual(npm.cheap_check(a, npm.DEFAULTS)[1], ["serial launcher: 1 more coin in 60 min"])
        mayhem = create("ser3", creator="SerialGuy", mayhem=True)          # skipped launches count too
        self.assertIsNone(wl.on_create(mayhem, T0 + 90))
        self.assertEqual(npm.cheap_check(b, npm.DEFAULTS)[1], ["serial launcher: 2 more coins in 60 min"])
        # another creator, an hour and a bit apart: not serial (and the history is forgotten)
        c = wl.on_create(create("once1", creator="Patient"), T0)
        wl.expire(T0 + 62 * 60)                                            # SerialGuy's last was at +90 s
        d = wl.on_create(create("once2", creator="Patient"), T0 + 62 * 60)
        self.assertEqual((c.serial, d.serial), (0, 0))
        self.assertNotIn("SerialGuy", wl.launches)


class Readings(unittest.TestCase):
    def test_the_decode_matches_the_create_price_exactly(self):
        b64 = account(30000000000, 1073000000000000, 0)
        d = npm.decode_curve(b64)
        self.assertEqual((d["vsol"], d["vtok"], d["rsol"], d["complete"]), (30000000000, 1073000000000000, 0, False))
        self.assertAlmostEqual(d["price_sol"], 2.796e-08, places=11)       # the probe's 2.796e-08 SOL per token
        self.assertEqual(npm.decode_curve([b64, "base64"]), d)
        self.assertEqual(npm.decode_curve(base64.b64decode(b64)), d)
        self.assertTrue(npm.decode_curve(account(85000000000, 279900000000000, 55000000000, True))["complete"])
        r = npm.read_curve(npm.decode_curve(account(int(30.296296295 * 1e9), int(1062506112.5149 * 1e6), 296296295)))
        wl = npm.Watchlist({})
        self.assertAlmostEqual(r["price"] / wl.on_create(FOLIO, T0).price0, 1.0, places=8)
        for junk in (None, "", "%%%", "AAAA", b"short", [None], 5, {"data": 1}):
            self.assertIsNone(npm.decode_curve(junk))

    def test_the_units_are_told_apart(self):
        raw = npm.read_curve(reading(14.36))
        sol = npm.read_curve({"price_sol": raw["price"], "real_sol": 14.36, "complete": False})      # curve_watch.py
        pp = npm.read_curve({"vSolInBondingCurve": 44.36, "vTokensInBondingCurve": K / 44.36})       # PumpPortal floats
        only = npm.read_curve({"rsol": 14.36})
        for r in (sol, pp, only):
            self.assertAlmostEqual(r["rsol"], 14.36, places=6)
            self.assertAlmostEqual(r["price"] / raw["price"], 1.0, places=6)
        feed = dict(reading(14.36, slot=310000123), kind="curve", mint="m", key="k", price_sol=raw["price"],
                    t=T0, src="http")                                                # feeds.py's curve event
        self.assertEqual(npm.read_curve(feed), dict(raw, slot=310000123))
        self.assertIsNone(npm.read_curve({"vsol": 44.36, "rsol": 14360000000}))     # SOL and lamports mixed
        self.assertIsNone(npm.read_curve({"price_sol": 4e-8}))                       # no real SOL: no trades
        self.assertIsNone(npm.read_curve({"vsol": -1, "vtok": 5}))

    def test_folio_rises_18_percent_without_enough_sol(self):
        """The probe's FOLIO create event with a made-up slow path: +18 % from the create in 14
        readings, buys 12 of 14, but only 2.6 SOL in: no entry. (The probe's own FOLIO had 14.36 SOL
        in at its first push: test_the_probes_real_folio_path.)"""
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = wl.on_create(FOLIO, T0)
        wl.on_meta(c.mint, {"twitter": "https://x.com/usefolio", "website": "https://usefolio.xyz"})
        top = rise_to(c.vsol0, 18.0)
        path = [c.rsol0 + (top - c.rsol0) * k / 12.0 for k in range(1, 13)]
        path = path[:4] + [path[3] - 0.05] + path[4:9] + [path[8] - 0.05] + path[9:]      # 2 small sells
        fired = []
        for i, rs in enumerate(path):
            got = wl.on_curve(c.mint, reading(rs), T0 + 1 + 0.68 * i)
            self.assertIs(got, c)
            act, why = wl.judge(c, T0 + 1 + 0.68 * i)
            fired += [i] if act == "fire" else []
        self.assertEqual(len(path), 14)
        self.assertEqual((c.buys, c.sells, fired), (12, 2, []))
        self.assertAlmostEqual(c.rise_pct, 18.0, places=6)
        self.assertEqual(why, "SOL in 2.6/3")
        self.assertEqual(c.status, "watching")

    def test_the_probes_real_folio_path(self):
        """FOLIO as the probe saw it: 14 pushes from 14.36 to 18.14 SOL of real reserves (+18 % between
        the first and the last push, but +114 % from the create at the first one). It fires once, at
        the 8th buy, 67 SOL of mcap: just under the 70 cap that the 12th push passes."""
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = wl.on_create(FOLIO, T0)
        wl.on_meta(c.mint, {"twitter": "https://x.com/usefolio"})
        first = None
        fired, whys = [], []
        for i in range(14):
            rs = 14.36 + (18.14 - 14.36) * i / 13.0
            t = T0 + 2 + 0.68 * i
            wl.on_curve(c.mint, reading(rs, slot=500 + i), t)
            first = first or c.price
            act, why = wl.judge(c, t)
            whys.append(why)
            if act == "fire":
                fired.append(i)
                wl.mark_entered(c, t)
        self.assertEqual(fired, [7])
        self.assertEqual(whys[7], "momentum · 16.1 SOL in · 8/8 buys (100%) · +135% since launch · mcap 67 SOL · 6 s old")
        self.assertAlmostEqual((c.price / first - 1) * 100, 17.8, places=1)
        self.assertGreater(c.mcap_sol, 70)

    def test_buys_and_sells_come_from_the_real_sol(self):
        wl = npm.Watchlist({})
        c = wl.on_create(create("bs"), T0)
        for i, rs in enumerate([1.5, 2.5, 2.5, 1.2, 3.0]):
            wl.on_curve(c.mint, reading(rs), T0 + i)
        self.assertEqual((c.buys, c.sells, c.readings), (3, 1, 5))
        self.assertAlmostEqual(c.sell_max, 1.3, places=6)
        self.assertAlmostEqual(c.sol_in, 2.3, places=6)                    # 3.0 real SOL, 0.7 was the dev's

    def test_an_older_or_repeated_reading_is_left_out(self):
        wl = npm.Watchlist({})
        c = wl.on_create(create("slots"), T0)
        self.assertIs(wl.on_curve(c.mint, reading(2.0, slot=100), T0 + 1), c)
        self.assertIs(wl.on_curve(c.mint, reading(3.0, slot=102), T0 + 2), c)
        self.assertIsNone(wl.on_curve(c.mint, reading(2.5, slot=101), T0 + 2.1))     # the WS push came late
        self.assertIsNone(wl.on_curve(c.mint, reading(3.0, slot=102), T0 + 2.5))     # the same state again
        self.assertEqual(c.last_t, T0 + 2.5)                                          # ... but it is fresh
        self.assertIs(wl.on_curve(c.mint, reading(3.0, slot=103), T0 + 3), c)        # nothing moved
        self.assertEqual((c.buys, c.sells, c.readings, c.slot), (2, 0, 3, 103))
        self.assertIsNone(wl.on_curve("unknown", reading(3.0), T0 + 3))

    def test_a_quiet_held_curve_stays_fresh_when_the_poll_round_is_good(self):
        """feeds.py sends a curve event only when the reserves change: a held coin nobody trades
        for 6 s is not stale while the poller reads it (wl.touch per good round), and is once the
        poller stops."""
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl)
        wl.on_curve(c.mint, reading(9.9, slot=10), T0 + 1)                 # the one event of 6 s
        wl.mark_entered(c, T0 + 1)
        self.assertTrue(npm.stale(c, npm.DEFAULTS, T0 + 7))               # without touch: stale
        for i in range(12):
            self.assertEqual(wl.touch(T0 + 1.5 + 0.5 * i), 1)
        self.assertFalse(npm.stale(c, npm.DEFAULTS, T0 + 7))
        self.assertTrue(npm.stale(c, npm.DEFAULTS, T0 + 12.6))            # the poller went quiet
        self.assertEqual((c.buys, c.sells, c.readings), (1, 0, 1))         # touch is not a reading
        self.assertEqual(wl.touch(T0 + 20, mints=["unknown", c.mint]), 1)
        self.assertEqual(wl.touch(T0 + 21, mints="junk"), 0)
        wl.on_migrate(c.mint, T0 + 22)
        self.assertEqual(wl.touch(T0 + 30), 0)                             # a finished curve is not read


class Trigger(unittest.TestCase):
    def test_uzim_fires_exactly_once_at_the_right_reading(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl)
        fired, whys = [], []
        for i, rs in enumerate(UZIM_RSOL):
            self.assertIs(wl.on_curve(c.mint, reading(rs, slot=1000 + i), t_of(i)), c)    # 7: same state, fresh
            act, why = wl.judge(c, t_of(i))
            whys.append(why)
            if act == "fire":
                fired.append(i)
                wl.mark_entered(c, t_of(i) + 0.4)
            self.assertIsNone(npm.exit_signal(c, npm.DEFAULTS))           # buyers only: no dump
        self.assertEqual(fired, [UZIM_FIRE])
        self.assertEqual(whys[8], "buys 7/8")
        self.assertEqual(whys[UZIM_FIRE],
                         "momentum · 9.2 SOL in · 8/10 buys (80%) · +69% since launch · mcap 49 SOL · 11 s old")
        self.assertEqual(whys[-1], "entered")
        self.assertEqual(c.trigger_t, t_of(UZIM_FIRE))
        self.assertEqual(npm.first_in_text(c, t_of(UZIM_FIRE) + 0.4),
                         "first in · 0.4 s after the trigger · 11 s after launch")
        first = npm.read_curve(reading(UZIM_RSOL[0]))["price"]
        self.assertAlmostEqual((c.price / first - 1) * 100, 163.1, places=1)          # 5.2 -> 27.1 SOL: +163 %
        # a holder dumps 3.2 SOL: out at once
        wl.on_curve(c.mint, reading(23.9, slot=2000), t_of(16))
        self.assertEqual(npm.exit_signal(c, npm.DEFAULTS), "dump: 3.2 SOL sold")

    def test_no_rugcheck_report_yet_fires_again_on_the_next_reading(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl)
        acts = []
        for i, rs in enumerate(UZIM_RSOL[:13]):
            wl.on_curve(c.mint, reading(rs), t_of(i))
            acts.append(wl.judge(c, t_of(i))[0])
            if i == UZIM_FIRE + 1:
                wl.mark_entered(c, t_of(i) + 0.3)                          # the report came a reading later
        self.assertEqual(acts[UZIM_FIRE:], ["fire", "fire", "wait"])
        self.assertEqual(c.trigger_t, t_of(UZIM_FIRE))
        self.assertEqual(npm.first_in_text(c, t_of(UZIM_FIRE + 1) + 0.3),
                         "first in · 1.1 s after the trigger · 12 s after launch")

    def test_no_report_is_no_verdict_and_the_next_reading_fires(self):
        """The OVERRIDES: no RugCheck report in time -> no entry on this reading, try again on the
        next. rug_check_np says so with ok None (a False would read as a FAIL and reject for good)."""
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl)
        acts = []
        for i, rs in enumerate(UZIM_RSOL[:13]):
            wl.on_curve(c.mint, reading(rs, slot=100 + i), t_of(i))
            act, why = wl.judge(c, t_of(i))
            acts.append(act)
            if act == "fire":
                rep = None if i == UZIM_FIRE else report(c.mint, c.curve)     # the report comes a reading late
                ok, why, n = npm.rug_check_np(rep, c, npm.DEFAULTS)
                if ok is None:
                    self.assertEqual((why, n, c.status, npm.rug_text(c)),
                                     (["no RugCheck report"], 0, "watching", "no RugCheck report"))
                elif not ok:
                    wl.reject(c, why[0])
                else:
                    wl.mark_entered(c, t_of(i) + 0.3)
        self.assertEqual(acts[UZIM_FIRE:], ["fire", "fire", "wait"])
        self.assertEqual(c.status, "entered")
        self.assertEqual(npm.first_in_text(c, t_of(UZIM_FIRE + 1) + 0.3),
                         "first in · 1.1 s after the trigger · 12 s after launch")

    def test_a_coin_the_bot_did_not_buy_is_offered_once_per_trigger(self):
        """It passed RugCheck and the LEARNER said skip, but nobody called reject: the next readings
        do not ask again (the learner's "1 in 5 skips is explored" would buy it 4 readings later).
        retry() offers it again; a RugCheck FAIL nobody acted on rejects it at the next judge."""
        slow = UZIM_RSOL[:UZIM_FIRE + 1] + [10.3, 10.7, 11.1, 11.5, 11.9, 12.3, 12.7]   # the rules hold 8 readings
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl)
        acts = []
        for i, rs in enumerate(slow):
            wl.on_curve(c.mint, reading(rs, slot=100 + i), t_of(i))
            act, why = wl.judge(c, t_of(i))
            acts.append(act[0])
            if act == "fire":
                self.assertTrue(npm.rug_check_np(report(c.mint, c.curve), c, npm.DEFAULTS)[0])    # PASS, then a skip
            if i == 14:
                wl.retry(c)                                                # the book was full: try again
        self.assertEqual("".join(acts[UZIM_FIRE:]), "fwwwwfww")
        self.assertEqual((c.status, c.why), ("watching", "fired, not bought"))
        bad = npm.Watchlist(dict(npm.DEFAULTS))
        d = uzim(bad)
        for i, rs in enumerate(UZIM_RSOL[:UZIM_FIRE + 1]):
            bad.on_curve(d.mint, reading(rs, slot=100 + i), t_of(i))
        self.assertEqual(bad.judge(d, t_of(UZIM_FIRE))[0], "fire")
        npm.rug_check_np(report(d.mint, d.curve, holders=(18.0,)), d, npm.DEFAULTS)   # FAIL, not acted on
        bad.on_curve(d.mint, reading(12.0, slot=200), t_of(UZIM_FIRE + 1))
        self.assertEqual(bad.judge(d, t_of(UZIM_FIRE + 1)), ("reject", "a holder has 18.0% (max 12%)"))
        npm.rug_check_np(None, d, npm.DEFAULTS)                            # a later "no report" keeps the FAIL
        self.assertFalse(d.rug["ok"])

    def test_the_trigger_time_restarts_when_the_rules_lapse(self):
        """Fired with no report, then sells broke the rules, then they held again 5 s later: the fill
        is 0.2 s after THAT trigger, not 5.8 s after the stale one."""
        path = UZIM_RSOL[:UZIM_FIRE + 1] + [9.0, 8.2, 7.4, 9.6, 10.4, 11.2, 12.0, 12.8, 13.6, 14.4, 15.2]
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl)
        acts = ""
        for i, rs in enumerate(path):
            wl.on_curve(c.mint, reading(rs, slot=100 + i), t_of(i))
            act, why = wl.judge(c, t_of(i))
            acts += act[0]
            if act == "fire" and i > UZIM_FIRE + 1:
                wl.mark_entered(c, t_of(i) + 0.2)
                break
            if act == "wait" and i > UZIM_FIRE:
                self.assertIsNone(c.trigger_t)
        self.assertEqual(acts, "w" * UZIM_FIRE + "ffwwwwwf")
        self.assertEqual(c.trigger_t, t_of(17))
        self.assertEqual(npm.first_in_text(c, t_of(17) + 0.2), "first in · 0.2 s after the trigger · 16 s after launch")

    def test_it_waits_for_the_socials_and_rejects_without_them(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl, meta=False)
        for i, rs in enumerate(UZIM_RSOL[:UZIM_FIRE + 1]):
            wl.on_curve(c.mint, reading(rs), t_of(i))
        self.assertEqual(wl.judge(c, t_of(UZIM_FIRE)), ("wait", "socials not read yet"))
        wl.on_meta(c.mint, {"twitter": "https://x.com/uzimonkey"})
        self.assertEqual(wl.judge(c, t_of(UZIM_FIRE))[0], "fire")
        bare = npm.Watchlist(dict(npm.DEFAULTS))
        d = uzim(bare, meta=None)                                          # every gateway failed
        wl_off = npm.Watchlist(dict(npm.DEFAULTS, np_need_social=False))
        e = uzim(wl_off, meta=False)
        for i, rs in enumerate(UZIM_RSOL[:UZIM_FIRE + 1]):
            bare.on_curve(d.mint, reading(rs), t_of(i))
            wl_off.on_curve(e.mint, reading(rs), t_of(i))
        self.assertEqual(bare.judge(d, t_of(UZIM_FIRE)), ("reject", "no social link"))
        self.assertEqual(d.status, "rejected: no social link")
        self.assertEqual(bare.judge(d, t_of(UZIM_FIRE + 1)), ("wait", "rejected: no social link"))
        self.assertEqual(wl_off.judge(e, t_of(UZIM_FIRE))[0], "fire")

    def test_too_late_too_big_too_old(self):
        cfgs = dict(npm.DEFAULTS)
        wl = npm.Watchlist(cfgs)
        c = uzim(wl)
        steps = [5.2, 9.0, 12.0, 15.0, 18.0, 19.0, 20.0, 21.0]             # 8 buys, but the mcap passed 70
        for i, rs in enumerate(steps):
            wl.on_curve(c.mint, reading(rs), t_of(i))
        self.assertEqual(wl.judge(c, t_of(7)), ("wait", "mcap 81 SOL (max 70)"))
        old = npm.Watchlist(cfgs)
        d = uzim(old)
        for i, rs in enumerate(UZIM_RSOL[:UZIM_FIRE + 1]):
            old.on_curve(d.mint, reading(rs), T0 + 170 + i)
        self.assertEqual(old.judge(d, T0 + 180)[0], "fire")                # 180 s is still in time
        self.assertEqual(old.judge(d, T0 + 180.5), ("wait", "too old: 181 s (max 180 s)"))
        strict = npm.Watchlist(dict(cfgs, np_min_rise_pct=80))
        e = uzim(strict)
        for i, rs in enumerate(UZIM_RSOL[:UZIM_FIRE + 1]):
            strict.on_curve(e.mint, reading(rs), t_of(i))
        self.assertEqual(strict.judge(e, t_of(UZIM_FIRE)), ("wait", "rise +69%/+80%"))
        self.assertEqual(npm.trigger(npm.Coin(), cfgs, T0), (False, "no curve reading yet"))

    def test_a_dump_before_the_entry_rejects_the_mayhem_coin(self):
        """SCAMSEM (watched when np_skip_mayhem is off): up to 62 SOL, then -89 % in big sells."""
        cfgs = dict(npm.DEFAULTS, np_skip_mayhem=False)
        wl = npm.Watchlist(cfgs)
        ev = create("SCAMsEm1q2w3e4r5t6y7u8i9o0pAsDfGhJkLzXcVbN", "SCAMSEM", "Scam Season", dev_sol=0.0, mayhem=True)
        c = wl.on_create(ev, T0)
        self.assertTrue(c.mayhem)
        wl.on_meta(c.mint, {"twitter": "https://x.com/scamsem"})
        peak = None
        for i, rs in enumerate([10.0, 25.0, 40.0, 62.0, 40.0, 20.0, 8.0, 0.5]):
            wl.on_curve(c.mint, reading(rs), T0 + 10 * i)
            peak = c.price if rs == 62.0 else peak
            act, why = wl.judge(c, T0 + 10 * i)
        self.assertAlmostEqual((c.price / peak - 1) * 100, -89.0, delta=0.2)
        self.assertEqual(c.status, "rejected: dump: 22.0 SOL sold")
        self.assertEqual(npm.cheap_check(c, cfgs)[1], ["dump: 22.0 SOL sold"])
        self.assertEqual(npm.exit_signal(c, cfgs), None)                   # never held: no exit
        self.assertEqual(npm.cheap_check(c, dict(npm.DEFAULTS))[1], ["mayhem mode", "dump: 22.0 SOL sold"])

    def test_the_curve_completing_is_an_exit(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl)
        for i, rs in enumerate(UZIM_RSOL[:UZIM_FIRE + 1]):
            wl.on_curve(c.mint, reading(rs), t_of(i))
        wl.mark_entered(c, t_of(UZIM_FIRE))
        wl.on_curve(c.mint, reading(8.4), t_of(11))                        # a 1.5 SOL sell: under 2
        self.assertIsNone(npm.exit_signal(c, npm.DEFAULTS))
        wl.on_curve(c.mint, reading(85.0, complete=True), t_of(12))
        self.assertEqual(npm.exit_signal(c, npm.DEFAULTS), "migrating: take it")
        self.assertIsNone(wl.on_curve(c.mint, reading(86.0), t_of(13)))   # a finished curve tells nothing
        d = uzim(npm.Watchlist(dict(npm.DEFAULTS)))
        wl2 = npm.Watchlist(dict(npm.DEFAULTS))
        wl2.adopt(d)
        wl2.on_migrate(d.mint, T0 + 30)
        self.assertEqual(npm.exit_signal(d, npm.DEFAULTS), "migrating: take it")
        self.assertEqual(npm.trigger(d, npm.DEFAULTS, T0 + 30), (False, "no curve reading yet"))

    def test_the_migration_reading_is_not_a_dump(self):
        """The reading that says complete may show the curve's SOL already moved to the AMM (real SOL
        0), or every reserve zeroed: either way it is "migrating: take it" at the last curve price,
        not an 84 SOL "dump", and not a reading to drop."""
        zeroed = npm.decode_curve(account(0, 0, 0, True))
        withdrawn = npm.decode_curve(account(115000000000, 279900000000000, 0, True))
        for d in (zeroed, withdrawn):
            wl = npm.Watchlist(dict(npm.DEFAULTS))
            c = uzim(wl)
            wl.on_curve(c.mint, reading(84.0, slot=10), T0 + 60)
            wl.mark_entered(c, T0 + 60)
            price = c.price
            ev = dict(d, kind="curve", mint=c.mint, key=c.curve, slot=11, t=T0 + 61, src="poll")   # feeds.py's event
            self.assertIs(wl.on_curve(c.mint, ev, T0 + 61), c)
            self.assertEqual((c.complete, c.sells, c.sell_max_held, c.price, c.last_t), (True, 0, 0.0, price, T0 + 61))
            self.assertEqual(npm.exit_signal(c, npm.DEFAULTS), "migrating: take it")
            self.assertIsNone(wl.on_curve(c.mint, ev, T0 + 62))
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl)
        wl.on_curve(c.mint, reading(84.0, slot=10), T0 + 60)
        self.assertIsNone(wl.on_curve(c.mint, dict(zeroed, complete=False), T0 + 61))   # zeroed, not complete: junk
        self.assertIs(wl.on_curve(c.mint, reading(85.1, slot=12, complete=True), T0 + 62), c)  # the last buy completes it
        self.assertEqual((c.complete, c.buys), (True, 2))


class RugCheck(unittest.TestCase):
    def setUp(self):
        self.wl = npm.Watchlist(dict(npm.DEFAULTS))
        self.c = uzim(self.wl)
        self.m, self.k = self.c.mint, self.c.curve

    def check(self, rep, cfg=npm.DEFAULTS):
        return npm.rug_check_np(rep, self.c, cfg)

    def test_a_clean_report_passes_without_the_curve(self):
        ok, why, n = self.check(report(self.m, self.k, holders=(2.3, 1.4, 0.9, 0.8)))
        self.assertEqual((ok, why, n), (True, [], 14))                     # the curve's 92 % is left out
        self.assertEqual(npm.rug_text(self.c), "PASS · 14 holders · top holder 2.3% · top 10 5% (curve left out)")

    def test_creator_history_and_single_holder_risks(self):
        hist = {"name": "Creator history of rugged tokens", "level": "warn", "description": "x"}
        single = {"name": "Single holder ownership", "level": "warn"}
        high = {"name": "High holder concentration", "level": "warn"}
        self.assertEqual(self.check(report(self.m, self.k, risks=[hist]))[:2],
                         (False, ["risk: Creator history of rugged tokens"]))
        self.assertEqual(self.check(report(self.m, self.k, risks=[single]))[:2],
                         (False, ["risk: Single holder ownership"]))
        self.assertTrue(self.check(report(self.m, self.k, risks=[high]))[0])           # a warning, numbers fine
        self.assertFalse(self.check(report(self.m, self.k, risks=[dict(high, level="danger")]))[0])
        scamsem = report(self.m, self.k, risks=[hist, single, high])                   # what the probe saw
        self.assertEqual(self.check(scamsem)[1], ["risk: Creator history of rugged tokens",
                                                   "risk: Single holder ownership"])
        self.assertTrue(self.check(scamsem, dict(npm.DEFAULTS, np_rug_bad_risks=[]))[0])
        self.assertEqual(npm.rug_text(self.c), "PASS · 14 holders · top holder 1.1% · top 10 3% (curve left out)")

    def test_an_18_percent_holder_top_10_and_insiders(self):
        self.assertEqual(self.check(report(self.m, self.k, holders=(18.0, 2.0)))[:2],
                         (False, ["a holder has 18.0% (max 12%)"]))
        self.assertEqual(npm.rug_text(self.c), "FAIL · a holder has 18.0% (max 12%)")
        self.assertTrue(self.check(report(self.m, self.k, holders=(12.0, 2.0)))[0])    # at most 12 %
        self.assertEqual(self.check(report(self.m, self.k, holders=[4.0] * 12))[1], ["top 10 hold 40% (max 35%)"])
        self.assertEqual(self.check(report(self.m, self.k, insiders=3))[1], ["3 insiders (graph)"])
        self.assertEqual(self.check(report(self.m, self.k, rugged=True))[1], ["RugCheck: rugged"])

    def test_the_curve_is_found_by_key_owner_and_known_accounts(self):
        by_address = report(self.m, self.k, curve_pct=0, extra=[{"address": self.k, "owner": "x", "pct": 88.0}])
        self.assertTrue(self.check(by_address)[0])
        amm = report(self.m, self.k, extra=[{"address": "pool-ata", "owner": "pool-pda", "pct": 60.0}],
                     known={"pool-pda": {"name": "Pump Fun AMM", "type": "AMM"}})
        self.assertTrue(self.check(amm)[0])
        creator = report(self.m, self.k, extra=[{"address": "dev-ata", "owner": "dev-pda", "pct": 15.0}],
                         known={"dev-pda": {"name": "Creator", "type": "CREATOR"}})
        self.assertEqual(self.check(creator)[1], ["a holder has 15.0% (max 12%)"])   # the dev is a holder
        no_market = report(self.m, self.k)
        no_market["markets"] = None
        self.assertTrue(self.check(no_market)[0])                         # the curve key alone finds it

    def test_no_report_or_another_coin(self):
        self.assertEqual(self.check(None), (None, ["no RugCheck report"], 0))     # no verdict: wait
        self.assertEqual(self.check({}), (None, ["no RugCheck report"], 0))
        self.assertEqual(self.check(report("other", self.k)), (None, ["RugCheck report is for another coin"], 0))
        self.assertEqual((self.c.status, self.c.rug["ok"]), ("watching", None))
        self.assertEqual(npm.rug_text(self.c), "RugCheck report is for another coin")
        self.assertEqual(npm.rug_text(npm.Coin()), "no RugCheck report")


class Watching(unittest.TestCase):
    def test_eviction_drops_the_oldest_but_never_a_held_coin(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS, np_max_watch=3))
        a, b, c = [wl.on_create(create(m), T0 + i) for i, m in enumerate(("ma", "mb", "mc"))]
        d = wl.on_create(create("md"), T0 + 3)
        self.assertEqual(d.evicted, ["ma"])
        wl.reject(c, "no social link")
        e = wl.on_create(create("me"), T0 + 4)
        self.assertEqual(e.evicted, ["mc"])                                # a rejected coin goes first
        wl.mark_entered(b, T0 + 5)
        f = wl.on_create(create("mf"), T0 + 6)
        self.assertEqual(f.evicted, ["md"])                                # the oldest one not held
        self.assertEqual(list(wl.coins), ["mb", "me", "mf"])
        one = npm.Watchlist(dict(npm.DEFAULTS, np_max_watch=1))
        one.mark_entered(one.on_create(create("held"), T0), T0)
        self.assertIsNone(one.on_create(create("late"), T0 + 1))
        self.assertEqual(one.rows(T0 + 1)[0]["status"], "rejected: watchlist full of held coins")
        # held coins restored over the cap: no room, and nothing is dropped behind the bot's back
        # (a coin dropped without being handed back to unwatch would stay polled for good)
        full = npm.Watchlist(dict(npm.DEFAULTS, np_max_watch=3))
        w = full.on_create(create("w1"), T0)
        for m in ("h1", "h2"):
            full.mark_entered(full.on_create(create(m), T0), T0)
        full.adopt(npm.Coin.from_dict(dict(full.get("h1").to_dict(), mint="h3", curve="curve-h3")))
        self.assertIsNone(full.on_create(create("new"), T0 + 1))
        self.assertIs(full.get("w1"), w)
        self.assertEqual(full.expire(T0 + 500), ["w1"])

    def test_expiry(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        a, b, c, d = [wl.on_create(create(m), T0) for m in ("xa", "xb", "xc", "xd")]
        wl.mark_entered(b, T0 + 20)
        wl.on_migrate("xc", T0 + 30)                                       # migrated before any entry
        self.assertEqual(wl.expire(T0 + 30), ["xc"])
        self.assertEqual(wl.expire(T0 + 180), [])                          # 180 s is still watched
        self.assertEqual(wl.expire(T0 + 180.5), ["xa", "xd"])
        self.assertEqual(wl.held(), ["xb"])
        wl.mark_exited(b, "TP +7%", T0 + 200)
        self.assertEqual(b.status, "exited: TP +7%")
        self.assertEqual(npm.trigger(b, npm.DEFAULTS, T0 + 201), (False, "exited: TP +7%"))
        self.assertEqual(wl.expire(T0 + 201), ["xb"])
        self.assertEqual((wl.coins, wl.curves()), ({}, {}))
        self.assertFalse(wl.drop("xb"))
        self.assertIsNone(wl.on_create(create("xa"), T0 + 300))          # a create seen once stays seen

    def test_rows_for_the_panel(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl)
        f_ = wl.on_create(FOLIO, T0 + 5)
        wl.on_meta(f_.mint, {"twitter": "https://x.com/i/status/123", "telegram": "null", "website": "javascript:alert(1)"})
        for i, rs in enumerate(UZIM_RSOL[:UZIM_FIRE + 1]):
            wl.on_curve(c.mint, reading(rs), t_of(i))
            if wl.judge(c, t_of(i))[0] == "fire":
                wl.mark_entered(c, t_of(i))
        rows = wl.rows(T0 + 12)
        self.assertEqual([r["symbol"] for r in rows], ["FOLIO", "UZIM"])   # newest first
        u = rows[1]
        self.assertEqual(sorted(u), sorted(["symbol", "name", "mint", "age_s", "sol_in", "buys", "sells", "rise_pct",
                                            "mcap_sol", "dev_pct", "big_sell", "x", "tg", "web", "status", "why"]))
        self.assertEqual((u["age_s"], u["sol_in"], u["buys"], u["sells"], u["rise_pct"], u["status"]),
                         (12, 9.2, 8, 2, 68.9, "entered"))
        self.assertEqual((u["x"], u["tg"], u["web"]), ("https://x.com/uzimonkey", "https://t.me/uzim", False))
        self.assertTrue(u["why"].startswith("momentum · 9.2 SOL in"))
        fo = rows[0]
        self.assertEqual((fo["x"], fo["tg"], fo["web"], fo["status"]), ("https://x.com/i/status/123", False, True, "watching"))
        self.assertEqual(npm.social_names(f_.meta), ["X post", "web"])
        json.dumps(rows, allow_nan=False)
        many = npm.Watchlist(dict(npm.DEFAULTS))
        for i in range(30):
            many.on_create(create("r%02d" % i), T0 + i)
        self.assertEqual([r["mint"] for r in many.rows(T0 + 30)][:2], ["r29", "r28"])
        self.assertEqual(len(many.rows(T0 + 30)), 20)
        self.assertEqual(npm.row(None), {})

    def test_labels_for_the_learner(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl)
        for i, rs in enumerate(UZIM_RSOL[:UZIM_FIRE + 1]):
            wl.on_curve(c.mint, reading(rs), t_of(i))
        wl.mark_entered(c, t_of(UZIM_FIRE))
        plan = {"tier": "runner", "score": 5, "parts": ["X", "Telegram", "inflow 9.2 SOL"]}
        self.assertEqual(npm.labels(c, c.meta, plan, hour=3),
                         ["np:dev:2-5", "np:sol:6-12", "np:buys:80-90", "np:age:<30s", "np:social:x", "np:social:tg",
                          "news:5-6", "tier:runner", "hour:03-05"])
        six = wl.on_create(create("six", dev_sol=dev_buy_for_pct(6.0)), T0)
        six.rsol, six.buys, six.age_s = six.rsol0 + 13.0, 19, 95.0
        self.assertEqual(npm.labels(six, {"website": "https://six.fun"}, {"tier": "moon", "score": 8, "entry_score": 7}),
                         ["np:dev:5-8", "np:sol:12+", "np:buys:90+", "np:age:90s+", "np:social:web", "news:7+", "tier:moon"])
        self.assertEqual(npm.labels(None, None, None), ["np:social:none"])
        self.assertEqual(npm.labels(c, {}, {"tier": "scalp", "score": 0})[-3:], ["np:social:none", "news:0-2", "tier:scalp"])
        # the learner's hour blocks (learner.py NIGHT_HOURS), and the entry's tier after an upgrade
        hours = [npm.labels(None, None, None, hour=h)[-1] for h in (23, 0, 2, 5, 6, 7, 12)]
        self.assertEqual(hours, ["hour:23-01", "hour:23-01", "hour:01-03", "hour:05-07", "hour:05-07", "hour:day", "hour:day"])
        upgraded = {"tier": "runner", "score": 6, "entry_tier": "swing", "entry_score": 3}
        self.assertEqual(npm.labels(None, None, upgraded)[-2:], ["news:3-4", "tier:swing"])

    def test_the_labels_are_ones_the_learner_keeps(self):
        try:
            learner = load("memebot", "learner")
        except (OSError, ImportError, SyntaxError):
            self.skipTest("memebot/learner.py is not next to this test")
        for h in range(24):
            lab = npm.labels(None, None, None, hour=h)
            self.assertEqual(lab[-1:], learner.features(None, None, None, None, h))
            self.assertIn(lab[-1], learner._labels(lab))

    def test_news_info_feeds_the_hold_plan(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl)
        for i, rs in enumerate(UZIM_RSOL[:UZIM_FIRE + 1]):
            wl.on_curve(c.mint, reading(rs), t_of(i))
        self.assertEqual(npm.news_info(c), {"twitter": "https://x.com/uzimonkey", "twitter_kind": "profile",
                                            "telegram": "https://t.me/uzim", "website": "", "sol_in": 9.2,
                                            "buys": 8, "sells": 2})

    def test_stale_held_coin_and_creates_per_minute(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl)
        wl.on_curve(c.mint, reading(5.2), T0 + 3)
        self.assertFalse(npm.stale(c, npm.DEFAULTS, T0 + 20))              # not held
        wl.mark_entered(c, T0 + 3)
        self.assertFalse(npm.stale(c, npm.DEFAULTS, T0 + 8))
        self.assertTrue(npm.stale(c, npm.DEFAULTS, T0 + 8.5))
        for i in range(10):
            wl.on_create(create("cpm%d" % i), T0 + 20 * i)
        self.assertEqual(wl.creates_per_min(T0 + 300), 2.2)                # 11 creates in 5 minutes

    def test_a_held_coin_survives_a_restart(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        c = uzim(wl)
        for i, rs in enumerate(UZIM_RSOL[:UZIM_FIRE + 1]):
            wl.on_curve(c.mint, reading(rs, slot=50 + i), t_of(i))
        wl.mark_entered(c, t_of(UZIM_FIRE))
        doc = json.loads(json.dumps(c.to_dict(), allow_nan=False))
        back = npm.Coin.from_dict(doc)
        self.assertEqual(back.to_dict(), c.to_dict())
        wl2 = npm.Watchlist(dict(npm.DEFAULTS))
        self.assertTrue(wl2.adopt(back))
        self.assertFalse(wl2.adopt(back))
        wl2.on_curve(back.mint, reading(6.0, slot=99), t_of(12))
        self.assertEqual(npm.exit_signal(back, npm.DEFAULTS), "dump: 3.9 SOL sold")

    def test_defaults_hold_every_np_key_of_the_spec(self):
        want = {"np_watch_s": 180, "np_max_watch": 40, "np_max_dev_pct": 8, "np_dump_sol": 2.0, "np_need_social": True,
                "np_rug_timeout_s": 3, "np_max_top10_pct": 35, "np_max_holder_pct": 12, "np_max_age_s": 180,
                "np_min_sol": 3, "np_min_buys": 8, "np_min_buy_share": 0.7, "np_min_rise_pct": 15,
                "np_max_mcap_sol": 70, "np_stale_s": 5, "np_serial_window_min": 60}
        for k, v in want.items():
            self.assertEqual(npm.DEFAULTS[k], v, k)
        self.assertTrue(all(k.startswith("np_") for k in npm.DEFAULTS))


class Garbage(unittest.TestCase):
    JUNK = [None, 0, -1, 1e308, float("nan"), float("inf"), "", "x", "nan", "true", [], [1, 2], {}, {"a": 1},
            {"mint": None}, {"mint": 5}, {"mint": " "}, {"mint": "m" * 100}, b"\x00\x01", object(), True, ("t",),
            {"mint": ["list"]}, {"address": {"x": 1}}]

    def test_nothing_raises_on_junk(self):
        wl = npm.Watchlist(dict(npm.DEFAULTS))
        good = uzim(wl)
        junk_cfg = {k: "junk" for k in npm.DEFAULTS}
        for j in self.JUNK:
            for cfgs in (None, j, junk_cfg):
                w = npm.Watchlist(cfgs)
                w.on_create(j, j)
                w.on_create(create("ok%d" % id(j)), T0)
                w.on_curve(j, j, j)
                w.on_meta(j, j)
                w.on_migrate(j, j)
                w.judge(j, j)
                w.reject(j, j)
                w.mark_entered(j, j, j)
                w.mark_exited(j, j, j)
                w.adopt(j)
                w.drop(j)
                w.expire(j)
                w.creates_per_min(j, j)
                json.dumps(w.rows(j, j), allow_nan=False)
                npm.cheap_check(j, cfgs)
                npm.cheap_check(good, cfgs)
                npm.rug_check_np(j, good, cfgs)
                npm.rug_check_np({"mint": good.mint, "risks": j, "topHolders": j, "markets": j, "knownAccounts": j,
                                  "rugged": j, "graphInsidersDetected": j, "totalHolders": j}, good, cfgs)
                npm.rug_check_np({"mint": good.mint, "risks": [j], "topHolders": [j, {"address": j, "owner": j, "pct": j}],
                                  "markets": [j, {"pubkey": j, "liquidityAAccount": j}],
                                  "knownAccounts": {"k": j}}, j, cfgs)
                npm.trigger(j, cfgs, j)
                npm.trigger(good, cfgs, j)
                npm.exit_signal(j, cfgs)
                npm.exit_signal(good, cfgs)
                npm.stale(j, cfgs, j)
                npm.labels(j, j, j, j)
                npm.labels(good, j, j, j)
                npm.row(j, j)
                json.dumps(npm.row(good, j), allow_nan=False)
                npm.watch_text(j, j)
                npm.watch_text(good, j)
                npm.first_in_text(j, j)
                npm.first_in_text(good, j)
                npm.rug_text(j)
                npm.news_info(j)
                npm.decode_curve(j)
                npm.read_curve(j)
                npm.socials(j)
                npm.Coin.from_dict(j)
                wl.on_curve(good.mint, j, T0 + 5)
                wl.on_curve(good.mint, {"vsol": j, "vtok": j, "rsol": j, "slot": j, "complete": j}, T0 + 5)
        json.dumps(good.to_dict(), allow_nan=False)

    def test_extreme_clocks_and_a_broken_state_never_raise(self):
        """A clock of +-1e308 (int() of the inf in between raised) and a state.json with numbers the
        panel's JSON can't carry."""
        wl = npm.Watchlist(dict(npm.DEFAULTS, np_need_social=False))
        c = wl.on_create(create("clock"), -1e308)
        for i, rs in enumerate(UZIM_RSOL[:UZIM_FIRE + 1]):
            wl.on_curve(c.mint, reading(rs, slot=i), 1e308 if i % 2 else -1e308)
            wl.judge(c, 1e308)
            npm.trigger(c, npm.DEFAULTS, -1e308)
        for now in (1e308, -1e308, 1.7976931348623157e308):
            json.dumps(wl.rows(now), allow_nan=False)
            npm.watch_text(c, now)
            npm.first_in_text(c, now)
            npm.labels(c, None, {"score": 1}, now)
        json.dumps(c.to_dict(), allow_nan=False)
        broken = {"mint": "m", "price0": 1e-300, "price": 1e300, "rsol": 1e308, "rsol0": -1e308, "t0": -1e308,
                  "buys": 1e300, "sells": -5, "age_s": 1e308, "slot": 1e300, "entry_price": 1e300,
                  "rug": {"ok": False, "reasons": 5}, "meta": {"x": float("nan"), "x_kind": float("nan")}}
        self.assertIsNone(npm.Coin.from_dict(broken))                     # no curve has those prices
        b = npm.Coin.from_dict(dict(broken, price0=2.8e-8, price=3e-8))
        for now in (T0, 1e308, None):
            json.dumps([npm.row(b, now), npm.news_info(b), npm.labels(b, b.meta, {"score": 2}, 3),
                        npm.trigger(b, {}, now), b.to_dict()], allow_nan=False)
        self.assertEqual((b.sells, b.entry_price, npm.rug_text(b)), (0, None, "FAIL · ?"))
        self.assertTrue(math.isfinite(b.rise_pct) and math.isfinite(b.sol_in))

    def test_random_events_and_readings(self):
        rnd = random.Random(7)
        vals = self.JUNK + [30.0, 1.073e9, 3e10, 1.073e15, 0.5, 85.0, "pump", "abc", 1e12, -5.0, 2 ** 64]
        for n in range(400):
            wl = npm.Watchlist(dict(npm.DEFAULTS, np_need_social=rnd.random() < 0.5))
            ev = create("rnd%d" % n)
            for k in rnd.sample(sorted(ev), rnd.randint(0, 6)):
                ev[k] = rnd.choice(vals)
            coin = wl.on_create(ev, T0)
            mint = ev.get("mint") if isinstance(ev.get("mint"), str) else "rnd%d" % n
            wl.on_meta(mint, rnd.choice([None, SOCIAL, {"twitter": rnd.choice(vals)}]))
            for i in range(12):
                r = reading(rnd.uniform(0, 60), slot=rnd.choice([None, i, 5]))
                for k in rnd.sample(sorted(r), rnd.randint(0, 2)):
                    r[k] = rnd.choice(vals)
                got = wl.on_curve(mint, r, T0 + i)
                if got is not None:
                    act, why = wl.judge(got, T0 + i)
                    if act == "fire":
                        wl.mark_entered(got, T0 + i)
                    npm.exit_signal(got, wl.cfg)
                    npm.rug_check_np(report(mint, got.curve, risks=[{"name": rnd.choice(vals), "level": rnd.choice(vals)}]),
                                     got, wl.cfg)
                    self.assertTrue(math.isfinite(got.rise_pct) and math.isfinite(got.mcap_sol))
            json.dumps(wl.rows(T0 + 12), allow_nan=False)
            json.dumps([npm.labels(c, c.meta, {"tier": "scalp", "score": 1}) for c in wl.coins.values()])
            wl.expire(T0 + 400)
            if coin is not None:
                json.dumps(coin.to_dict(), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
