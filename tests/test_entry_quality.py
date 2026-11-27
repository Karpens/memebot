"""ENTRY QUALITY against the early-buyer dump (memebot/newpairs.py): RugCheck must SEE enough wallets holding the coin
before its holder checks count (np_min_holders; its empty accounts and the curve's are no holders), and one for every
np_max_sol_per_holder SOL that came in (a few wallets holding all the SOL can sell it back at once), else the coin
waits and RugCheck is asked again after np_rug_recheck_s; one buy reading above np_max_buy_sol rejects (off by
default); the LEARNER's concentration labels, and the backtest's levers for all of it (memebot/backtest.py
DEFAULT_GRID, judged by memebot/replay.py).

The coins are shaped on the live morning of 2026-11-26 (the first New Pairs trades): STOREB / TANOSHIMI bought at
6/6 buy readings, 3.3 SOL in, +22-23 %, RugCheck "PASS · 0 holders", then dumped 2.9 / 4.4 SOL 3 s after the entry;
TICKZ / SPRAYZ fired with 18-21 buy readings of 22-27, 7.8 SOL in, 30-31 holders (the LEARNER skipped them: their
outcome is unknown). Those entries fired before the 45 s age floor (np_min_age_s) was on. tests/fixtures/rugcheck holds
real RugCheck reports of 2026-11-26 (api.rugcheck.xyz): totalHolders counts every open token account, the empty ones of
wallets that sold out included. Paper money, eleven trades: these test the rules, not a promise that they pay."""

import json
import unittest.mock

import os
import unittest

from _load import load

os.environ["MEMEBOT_LIVE"] = "0"
os.environ["MEMEBOT_FEEDS"] = "0"
npm = load("memebot", "newpairs")
import test_newpairs as TN                      # noqa: E402 - its create / reading / report build plain dicts
import test_newpairs_engine as E                # noqa: E402 - the engine's FakeFeeds and CurveMarket

T0 = TN.T0
D = npm.DEFAULTS


def cfg(**kw):
    """The live config of that morning's trigger (6 buys, 2.5 SOL, 65 %, +12 %, no age floor) on the defaults."""
    c = dict(D, np_min_buys=6, np_min_sol=2.5, np_min_buy_share=0.65, np_min_rise_pct=12.0, np_need_social=False)
    c.update(kw)
    return c


def report(mint, curve, holders, pcts=(), empty=0, total=None, **kw):
    """A RugCheck report whose list shows the curve's account, `holders` wallets holding the coin (these %, the rest at
    0.4 %) and `empty` accounts with nothing (wallets that sold out); totalHolders counts every account it lists, as
    the real API does (total: another count, e.g. 0 while RugCheck has not counted yet)."""
    pcts = list(pcts)[:holders] + [0.4] * max(0, holders - len(pcts))
    extra = [{"address": "e%d-%s" % (i, mint), "owner": "eo%d" % i, "pct": 0, "amount": 0, "insider": False}
             for i in range(empty)]
    r = TN.report(mint, curve, holders=tuple(pcts), extra=extra, **kw)
    r["totalHolders"] = len(r["topHolders"]) if total is None else total
    return r


def empty_report(mint, curve):
    """What RugCheck said about STOREB at 25 s: a report, but nothing indexed (0 holders, only the curve listed)."""
    return TN.report(mint, curve, holders=(), total=0)


FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "rugcheck")


def real(name):
    """A real RugCheck report (api.rugcheck.xyz, 2026-11-26 13:49-13:55 UTC), as it came."""
    with open(os.path.join(FIX, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def coin_on(wl, mint, path, t0=T0, step=0.8, dev_sol=0.3):
    """A coin created at t0 with these real-SOL readings every `step` s (judged on each) -> (coin, [(act, why)])."""
    c = wl.on_create(TN.create(mint, mint[:6].upper(), mint + " coin", dev_sol=dev_sol), t0)
    wl.on_meta(c.mint, {"telegram": "https://t.me/" + mint})
    out = []
    for i, rs in enumerate(path):
        t = t0 + 1 + step * i
        wl.on_curve(c.mint, TN.reading(rs, slot=100 + i), t)
        out.append(wl.judge(c, t))
    return c, out


# STOREB: 6 buy readings of 0.55 SOL from the creator's 0.3 (3.3 SOL in, +23 %, mcap 35), then a 2.9 SOL sale
STOREB = [0.85, 1.4, 1.95, 2.5, 3.05, 3.6]
STOREB_DUMP = 0.7


def crowd_path(buys=21, sells=6, buy=0.45, sell=0.27, dev=0.3):
    """TICKZ: 21 buy readings and 6 sells (78 %), 7.8 SOL in: many small buyers, a sell after every few buys
    (from a creator's buy of `dev` SOL: TICKZ's +59 % at 7.8 SOL in and its mcap of 44 say it had none)."""
    out, r, b, s = [], dev, 0, 0
    while b < buys or s < sells:
        if s < sells and b >= 3 * (s + 1):
            r -= sell
            s += 1
        else:
            r += buy
            b += 1
        out.append(round(r, 6))
    return out


TICKZ = crowd_path(dev=0.0)


class Holders(unittest.TestCase):
    """1. RugCheck coverage: fewer than np_min_holders holders is no PASS but a wait."""

    def coin(self):
        wl = npm.Watchlist(cfg(np_max_sol_per_holder=0))                       # the holders rule alone
        c, acts = coin_on(wl, "storeb", STOREB)
        self.assertEqual(acts[-1][0], "fire")
        return wl, c

    def test_zero_holders_is_a_wait_not_a_pass(self):
        wl, c = self.coin()
        ok, why, n = npm.rug_check_np(empty_report(c.mint, c.curve), c, cfg())
        self.assertEqual((ok, why, n), (None, ["waiting: RugCheck sees 0 holders (min 10)"], 0))
        self.assertTrue(npm.rug_waiting(c))
        self.assertEqual(npm.rug_text(c), "waiting: RugCheck sees 0 holders (min 10)")
        self.assertEqual(c.status, "watching")                                  # no reject: asked again
        ok, why, n = npm.rug_check_np(empty_report(c.mint, c.curve), c, cfg(np_min_holders=0, np_max_sol_per_holder=0))
        self.assertEqual((ok, n), (True, 0))                                    # the old rules: the morning's PASS
        self.assertFalse(npm.rug_waiting(c))

    def test_the_boundary_is_inclusive(self):
        wl, c = self.coin()
        self.assertEqual(npm.rug_check_np(report(c.mint, c.curve, 9, (3.0,)), c, cfg())[:2],
                         (None, ["waiting: RugCheck sees 9 holders (min 10)"]))
        self.assertEqual(npm.rug_check_np(report(c.mint, c.curve, 10, (3.0,)), c, cfg()), (True, [], 10))
        self.assertEqual(npm.rug_check_np(report(c.mint, c.curve, 1, (3.0,)), c, cfg())[1],
                         ["waiting: RugCheck sees 1 holder (min 10)"])
        self.assertEqual(npm.rug_check_np(report(c.mint, c.curve, 19, (3.0,)), c, cfg(np_min_holders=20))[0], None)
        self.assertTrue(npm.rug_check_np(report(c.mint, c.curve, 19, (3.0,)), c, cfg(np_min_holders=19))[0])
        # a broken setting falls back to the default; a fraction rounds up (9.5 holders: 10 needed)
        self.assertIsNone(npm.rug_check_np(report(c.mint, c.curve, 9, (3.0,)), c, cfg(np_min_holders="x"))[0])
        self.assertIsNone(npm.rug_check_np(report(c.mint, c.curve, 9, (3.0,)), c, cfg(np_min_holders=9.5))[0])

    def test_the_count_is_the_wallets_holding_it(self):
        """totalHolders less the empty and the curve's accounts the report lists, or the holding accounts it lists
        when that is more (a report whose count is not filled in yet still shows who it lists)."""
        wl, c = self.coin()
        listed = report(c.mint, c.curve, 12, [2.0] * 12, total=0)              # counted 0 yet, 12 wallets listed
        self.assertEqual(npm.rug_holders(listed, c), 12)
        self.assertEqual(npm.rug_check_np(listed, c, cfg()), (True, [], 12))
        # 30 accounts it does not all list (the API lists 20 at most): the unlisted ones may hold or not
        self.assertEqual(npm.rug_holders(report(c.mint, c.curve, 3, [2.0] * 3, total=30), c), 29)
        self.assertEqual(npm.rug_holders(report(c.mint, c.curve, 3, [2.0] * 3, empty=8), c), 3)   # 12 accounts
        self.assertEqual(npm.rug_holders(empty_report(c.mint, c.curve), c), 0)    # the curve is no holder
        self.assertEqual(npm.rug_holders(None, c), 0)
        self.assertEqual(npm.rug_holders({"mint": c.mint, "totalHolders": float("nan")}, c), 0)
        self.assertEqual(npm.rug_holders({"mint": c.mint, "totalHolders": 1e30}, c), int(1e9))
        self.assertEqual(npm.holder_counts(report(c.mint, c.curve, 3, [2.0] * 3, empty=8), c),
                         {"holding": 3, "empty": 8, "curve": 1, "total": 12})

    def test_real_reports_count_the_empty_accounts_and_the_rule_does_not(self):
        """RugCheck's reports of 2026-11-26: when it lists every account (fewer than 20), totalHolders is that list,
        the accounts of wallets that sold everything (amount 0) included. The holders rule counts wallets holding
        the coin (the adversarial review's finding: it once read totalHolders, so 7 accounts passed "7 holders" with
        ONE wallet holding)."""
        for name, total, listed, empty, holders in (("CuVV71_7", 15, 15, 11, 4), ("2mYEHX_6", 7, 7, 5, 1),
                                                    ("5Uty_24", 10, 10, 7, 3)):
            rep = real(name)
            self.assertEqual((rep["totalHolders"], len(rep["topHolders"])), (total, listed), name)
            self.assertEqual(sum(1 for h in rep["topHolders"] if not h["amount"]), empty, name)
            self.assertEqual(npm.rug_holders(rep, None), holders, name)
        # counted 0 while it already lists 20 accounts (14 holding, 5 empty, the curve's); later 24 accounts
        self.assertEqual(npm.holder_counts(real("2xhio_0"), None), {"holding": 14, "empty": 5, "curve": 1, "total": 0})
        self.assertEqual(npm.rug_holders(real("2xhio_0"), None), 14)
        self.assertEqual(npm.rug_holders(real("2xhio_2"), None), 23)            # 24 accounts less the curve's: at most
        # 2mYEHX without its only FAIL (the creator's history): one wallet holds it, so it waits, at 5 holders too
        rep = real("2mYEHX_6")
        rep["risks"] = []
        c = npm.Coin(mint=rep["mint"], curve="")
        self.assertEqual(npm.rug_check_np(rep, c, dict(D, np_min_holders=5))[:2],
                         (None, ["waiting: RugCheck sees 1 holder (min 5)"]))
        # the review's made-up report in the same shape: 3 wallets hold, 8 sold out, the curve's: 12 accounts
        three = report(c.mint, "cv", 3, (9.0, 8.0, 1.0), empty=8)
        self.assertEqual(npm.rug_check_np(three, npm.Coin(mint=c.mint, curve="cv"), D)[:2],
                         (None, ["waiting: RugCheck sees 3 holders (min 10)"]))

    def test_a_list_that_shows_an_empty_account_shows_every_wallet_holding(self):
        """The post-fix review's finding: RugCheck lists the 20 biggest accounts, the biggest first (every real report:
        the empty ones last), so once its list shows an empty account, the accounts it does not list (totalHolders
        less the listed ones) are empty too. rug_holders() counted them as wallets past 20 accounts: 3 wallets
        holding among 30 accounts passed as 13 holders and were bought; 2xhio_0 with its count filled in (30) passed
        as 24 where 14 wallets hold it."""
        for name in ("2mYEHX_6", "2xhio_0", "2xhio_2", "5Uty_24", "CuVV71_7"):
            amounts = [h["amount"] for h in real(name)["topHolders"]]
            self.assertEqual(amounts, sorted(amounts, reverse=True), name)
        rep = real("2xhio_0")                                                  # 14 holding, 5 empty, the curve's
        rep["totalHolders"] = 30
        self.assertEqual(npm.rug_holders(rep, None), 14)
        coin = npm.Coin(mint=rep["mint"], curve="", rsol0=0.0, rsol=10.0)      # 10 SOL in: 20 wallets at 0.5
        self.assertEqual(npm.rug_check_np(rep, coin, D),
                         (None, ["waiting: 14 holders for 10.0 SOL in (0.71 SOL a holder, max 0.5)"], 14))
        # 2mYEHX_6's shape (ONE wallet holding, the rest sold out) past 20 accounts: still one wallet, still a wait
        rep = real("2mYEHX_6")
        rep["risks"] = []
        empty = [h for h in rep["topHolders"] if not h["amount"]][0]
        rep["topHolders"] += [dict(empty, address="sold%d" % i, owner="seller%d" % i)
                              for i in range(len(rep["topHolders"]), 20)]
        rep["totalHolders"] = 40
        self.assertEqual(npm.holder_counts(rep, None), {"holding": 1, "empty": 18, "curve": 1, "total": 40})
        self.assertEqual(npm.rug_check_np(rep, npm.Coin(mint=rep["mint"], curve="", rsol0=0.0, rsol=4.0), D),
                         (None, ["waiting: RugCheck sees 1 holder (min 10)"], 1))
        # the review's engine case: the curve's, 3 wallets holding, 16 sold out listed, 30 accounts counted
        c = npm.Coin(mint="few", curve="cv", rsol0=0.0, rsol=4.5)
        few = report(c.mint, c.curve, 3, (3.0, 2.0, 1.5), empty=16, total=30)
        self.assertEqual(npm.rug_holders(few, c), 3)
        self.assertEqual(npm.rug_check_np(few, c, D), (None, ["waiting: RugCheck sees 3 holders (min 10)"], 3))
        # a full list with no empty account still counts the unlisted accounts (they may hold some): an upper bound
        self.assertEqual(npm.rug_holders(report(c.mint, c.curve, 19, [1.0] * 19, total=30), c), 29)
        # and the tape keeps it: the replay rebuilds the report and counts the same 3
        rec, rp = load("memebot", "recorder"), load("memebot", "replay")
        line = rec.rug_summary(few, c.curve, npm._curve_accounts(few, c))
        self.assertEqual({k: line[k] for k in ("holders", "held", "empty", "curve")},
                         {"holders": 30, "held": 3, "empty": 16, "curve": 1})
        self.assertEqual(npm.rug_holders(rp.rug_report_of(dict(line, ok=True), c.mint), npm.Coin(mint=c.mint)), 3)

    def test_the_tape_keeps_the_count_for_the_replay(self):
        """The recorder's "rug" line keeps the accounts a report listed by kind ("held", "empty", "curve") beside its
        totalHolders ("holders"), so a replay rebuilds a report the holders rule counts as the live bot did, the real
        reports included; a line recorded before those fields counts the wallets its top lists (its totalHolders
        counted the empty accounts and the curve's: tests below)."""
        rec = load("memebot", "recorder")
        rp = load("memebot", "replay")
        cases = [real(n) for n in ("CuVV71_7", "2mYEHX_6", "5Uty_24", "2xhio_0", "2xhio_2")]
        cases.append(report("mm", "cv", 3, (9.0, 8.0, 1.0), empty=8))
        cases.append(report("mm", "cv", 30, [0.004] * 30, total=40))           # % that round to 0.00 on the tape
        for rep in cases:
            coin = npm.Coin(mint=rep["mint"], curve="cv" if rep["mint"] == "mm" else "")
            line = rec.rug_summary(rep, coin.curve, npm._curve_accounts(rep, coin))
            back = rp.rug_report_of(dict(line, ok=True), rep["mint"])
            self.assertEqual(npm.rug_holders(back, npm.Coin(mint=rep["mint"])), npm.rug_holders(rep, coin), rep["mint"])
            self.assertEqual(rec.rug_report(dict(line, ok=True))["topHolders"], back["topHolders"])
            self.assertEqual(npm.rug_check_np(back, npm.Coin(mint=rep["mint"]), dict(D, np_min_holders=0))[1:],
                             npm.rug_check_np(rep, npm.Coin.from_dict(coin.to_dict()), dict(D, np_min_holders=0))[1:])
        self.assertEqual({k: line[k] for k in ("holders", "held", "empty", "curve")},
                         {"holders": 40, "held": 30, "empty": 0, "curve": 1})
        old = {"ok": True, "holders": 14, "top": [3.0, 2.0, 1.5], "top1": 3.0, "top10": 6.5, "risks": [], "insiders": 0}
        self.assertEqual(npm.rug_holders(rp.rug_report_of(old, "mm"), None), 3)

    def test_an_old_tape_line_is_judged_on_the_wallets_its_top_lists(self):
        """The post-fix review's finding: every tape the first backtest reads was recorded by f16ee6e, whose "rug" line
        has "holders" (RugCheck's totalHolders: the empty accounts and the curve's counted) and "top" but no "held",
        and the replay judged the holder rules on that totalHolders: CuVV71 (15 accounts, 4 wallets) waits live and
        PASSED as 15 holders in the replay, so the base bought it and counted it as judged. Now an old line counts
        the wallets its top lists (every one when it lists fewer than 10: the biggest come first; else at least 10),
        never more than the live count: the replay never PASSes a coin the live rule holds back."""
        rec, rp = load("memebot", "recorder"), load("memebot", "replay")
        for name in ("CuVV71_7", "5Uty_24", "2mYEHX_6", "2xhio_0", "2xhio_2"):
            rep = real(name)
            rep["risks"] = []                                                  # only the holder rules can stop it
            rep["topHolders"] = [dict(h, pct=min(h["pct"], 5.0)) for h in rep["topHolders"]]
            coin = npm.Coin(mint=rep["mint"], curve="", rsol0=0.3, rsol=3.6)
            line = rec.rug_summary(rep, "", npm._curve_accounts(rep, coin))
            old = {k: v for k, v in line.items() if k not in ("held", "empty", "curve")}    # what f16ee6e wrote
            back = rp.rug_report_of(dict(old, ok=True), rep["mint"])
            live_n, old_n = npm.rug_holders(rep, coin), npm.rug_holders(back, None)
            self.assertEqual(old_n, live_n if len(old["top"]) < 10 else 10, name)
            for lever in ({}, {"np_min_holders": 5}, {"np_min_holders": 0}, {"np_min_holders": 20},
                          {"np_max_sol_per_holder": 0.35}):
                c = dict(D, **lever)
                live = npm.rug_check_np(rep, npm.Coin.from_dict(coin.to_dict()), c)[0]
                replayed = npm.rug_check_np(back, npm.Coin.from_dict(coin.to_dict()), c)[0]
                self.assertFalse(replayed is True and live is not True, (name, lever, live_n, old_n))
                if len(old["top"]) < 10:
                    self.assertEqual(replayed, live, (name, lever))
            self.assertTrue(rp.rug_line_old(dict(old, ok=True)) and not rp.rug_line_old(dict(line, ok=True)), name)
        self.assertEqual([npm.rug_holders(rp.rug_report_of(dict(
            {k: v for k, v in rec.rug_summary(real(n), "", npm._curve_accounts(real(n), None)).items()
             if k not in ("held", "empty", "curve")}, ok=True), "x"), None) for n in ("CuVV71_7", "5Uty_24", "2mYEHX_6")],
            [4, 3, 1])                                                         # not 15, 10, 7
        # the tape says how many of its reports are old: the backtest's report and summary page show it
        tape = rp.tape_of([dict(old, k="rug", t=T0, m="x", ok=True), dict(line, k="rug", t=T0 + 1, m="y", ok=True),
                           {"k": "rug", "t": T0 + 2, "m": "z", "ok": False, "ms": 300}])
        self.assertEqual(tape.summary()["rug_lines_old"], 1)

    def test_a_fail_beats_the_wait(self):
        """A report that already shows a 15 % holder among 3 is a FAIL, not a wait: too few holders never makes a
        bad report better."""
        wl, c = self.coin()
        ok, why, n = npm.rug_check_np(report(c.mint, c.curve, 3, (15.0, 2.0)), c, cfg())
        self.assertEqual((ok, why, n), (False, ["a holder has 15.0% (max 12%)"], 3))
        self.assertFalse(npm.rug_waiting(c))
        self.assertEqual(wl.judge(c, T0 + 10), ("reject", "a holder has 15.0% (max 12%)"))

    def test_the_wait_survives_a_missing_answer_and_a_pass_clears_it(self):
        wl, c = self.coin()
        npm.rug_check_np(report(c.mint, c.curve, 3, (3.0,)), c, cfg())
        npm.rug_check_np(None, c, cfg())                    # asked again, no answer yet: still the last word
        self.assertTrue(npm.rug_waiting(c))
        self.assertEqual(npm.rug_text(c), "waiting: RugCheck sees 3 holders (min 10)")
        self.assertEqual(c.rug["holders"], 3)
        back = npm.Coin.from_dict(c.to_dict())             # state.json keeps it
        self.assertTrue(npm.rug_waiting(back))
        self.assertEqual(npm.rug_check_np(report(c.mint, c.curve, 14, (3.0,)), c, cfg()), (True, [], 14))
        self.assertFalse(npm.rug_waiting(c))
        self.assertFalse(npm.rug_waiting(None))
        self.assertFalse(npm.rug_waiting(npm.Coin.from_dict(dict(c.to_dict(), rug={"ok": True, "wait": True}))))

    def test_a_waiting_coin_fires_again_on_the_next_reading(self):
        """The wait is ok None like a missing report: judge offers the coin again on its next reading (the bot then
        finds the report it cached, or the fresh one after np_rug_recheck_s), never "fired, not bought"."""
        wl = npm.Watchlist(cfg(np_max_sol_per_holder=0))
        c, acts = coin_on(wl, "storeb", STOREB)
        self.assertEqual(acts[-1][0], "fire")
        npm.rug_check_np(empty_report(c.mint, c.curve), c, wl.cfg)
        wl.on_curve(c.mint, TN.reading(3.7, slot=900), T0 + 7)
        self.assertEqual(wl.judge(c, T0 + 7)[0], "fire")
        npm.rug_check_np(report(c.mint, c.curve, 12, (3.0,)), c, wl.cfg)       # PASS, the bot did not buy
        wl.on_curve(c.mint, TN.reading(3.8, slot=901), T0 + 8)
        self.assertEqual(wl.judge(c, T0 + 8), ("wait", "fired, not bought"))


class Recheck(unittest.TestCase):
    """The recheck timing: rug_cache_s() is how long bot.py's np_rug_get may reuse a report."""

    def test_rug_cache_s(self):
        c = npm.Coin(mint="m1", curve="cv1")
        few, many = report("m1", "cv1", 3, (3.0,)), report("m1", "cv1", 14, (3.0,))
        self.assertEqual(npm.rug_cache_s(few, c, D), 4.0)                      # np_rug_recheck_s
        self.assertEqual(npm.rug_cache_s(many, c, D), 30)                      # np_rug_cache_s
        self.assertEqual(npm.rug_cache_s(None, c, D), 30)
        self.assertEqual(npm.rug_cache_s(few, c, dict(D, np_min_holders=0)), 30)
        self.assertEqual(npm.rug_cache_s(few, c, dict(D, np_rug_recheck_s=60)), 30)     # never past the cache
        self.assertEqual(npm.rug_cache_s(few, c, dict(D, np_rug_recheck_s=0)), 0.5)     # a floor: not every turn
        self.assertEqual(npm.rug_cache_s(few, c, dict(D, np_rug_recheck_s="x")), 4.0)
        self.assertEqual(npm.rug_cache_s(few, c, None), 4.0)

    def test_zero_holders_with_many_buys_is_asked_at_the_same_pace(self):
        """Decided: a "0 holders" report on a coin with 21 buy readings (RugCheck has certainly not indexed it) is
        asked for again no sooner than one that sees 3 holders: what we wait for is RugCheck's indexer, and asking
        it more often does not make it index faster; it only spends calls."""
        wl = npm.Watchlist(cfg())
        busy, _ = coin_on(wl, "tickz", TICKZ, dev_sol=0.0)
        thin, _ = coin_on(wl, "storeb", STOREB, t0=T0 + 1)
        self.assertGreaterEqual(busy.buys, 21)
        self.assertEqual(npm.rug_cache_s(empty_report(busy.mint, busy.curve), busy, D),
                         npm.rug_cache_s(report(thin.mint, thin.curve, 3, (3.0,)), thin, D))

    def ask_loop(self, c, cfg_, report_at, fire_every=0.5, until=40.0, cache=None):
        """bot.py's np_rug_get as the integrator wires it: a cached report is used while it is younger than
        rug_cache_s(report) (the old code: np_rug_cache_s), else RugCheck is asked again (its answer lands at once
        here). The trigger holds on every reading. -> (when it passed or None, [the times RugCheck was asked])."""
        got, asks = None, []
        t = 0.0
        while t <= until:
            keep = npm.rug_cache_s(got[1], c, cfg_) if (got and cache is None) else (cache or 0)
            if got is None or t - got[0] >= keep:
                asks.append(t)
                got = (t, report_at(t))
            if npm.rug_check_np(got[1], c, cfg_)[0]:
                return t, asks
            t = round(t + fire_every, 6)
        return None, asks

    def test_a_coin_is_bought_within_a_recheck_of_rugcheck_indexing_it(self):
        c = npm.Coin(mint="m2", curve="cv2")
        indexed = lambda t: report("m2", "cv2", 0 if t < 9.0 else 14, () if t < 9.0 else (3.0, 2.0))
        when, asks = self.ask_loop(c, D, indexed)
        self.assertEqual(asks, [0.0, 4.0, 8.0, 12.0])                          # one call every 4 s, no more
        self.assertEqual(when, 12.0)                                           # at most 4 s after the index caught up
        # the old 30 s cache would have waited until the report expired (or, without the holders rule, bought blind)
        self.assertEqual(self.ask_loop(c, D, indexed, cache=30), (30.0, [0.0, 30.0]))
        self.assertEqual(self.ask_loop(c, dict(D, np_min_holders=0), indexed), (0.0, [0.0]))
        # never indexed: asked every 4 s for as long as the trigger holds (the caller stops at np_max_age_s)
        when, asks = self.ask_loop(c, D, lambda t: report("m2", "cv2", 2, (3.0,)), until=180.0)
        self.assertIsNone(when)
        self.assertEqual(len(asks), 46)
        self.assertTrue(all(b - a >= 4.0 for a, b in zip(asks, asks[1:])))


def flow_readings(trades, step, until):
    """The cumulative real SOL a poller sees every `step` s (trades: [(t, sol)] after the create at 0)."""
    out, t = [], step
    while t <= until + 1e-9:
        out.append((t, sum(x for tt, x in trades if tt <= t)))
        t = round(t + step, 6)
    return out


class Concentration(unittest.TestCase):
    """2. The SOL that came in must come from enough WALLETS: at most np_max_sol_per_holder SOL in a wallet holding the
    coin by RugCheck's report, else the coin waits as for its holders (asked again every np_rug_recheck_s)."""

    def test_storeb_waits_while_six_wallets_hold_its_3_3_sol(self):
        wl = npm.Watchlist(cfg(np_min_holders=0))                              # the concentration rule alone
        c, acts = coin_on(wl, "storeb", STOREB)
        self.assertEqual(acts[-1][0], "fire")                                  # the trigger fires: RugCheck judges
        self.assertEqual((round(c.rise_pct), round(c.mcap_sol)), (23, 35))      # STOREB's numbers
        ok, why, n = npm.rug_check_np(report(c.mint, c.curve, 6, (2.0,) * 6), c, wl.cfg)
        self.assertEqual((ok, why, n), (None, ["waiting: 6 holders for 3.3 SOL in (0.55 SOL a holder, max 0.5)"], 6))
        self.assertTrue(npm.rug_waiting(c))
        self.assertEqual((c.status, npm.rug_cache_s(report(c.mint, c.curve, 6), c, wl.cfg)), ("watching", 4.0))
        self.assertTrue(npm.rug_check_np(report(c.mint, c.curve, 7, (2.0,) * 7), c, wl.cfg)[0])   # 0.47 a holder
        wl.on_curve(c.mint, TN.reading(STOREB_DUMP, slot=999), T0 + 9)          # the dump comes before a PASS
        self.assertEqual(wl.judge(c, T0 + 9), ("reject", "dump: 2.9 SOL sold"))

    def test_the_boundary_is_inclusive_and_0_turns_it_off(self):
        exact = [0.8, 1.3, 1.8, 2.3, 2.8, 3.3]                                 # 3.0 SOL in
        c, acts = coin_on(npm.Watchlist(cfg()), "exact", exact)
        self.assertEqual(acts[-1][0], "fire")
        six = report(c.mint, c.curve, 6)
        self.assertTrue(npm.rug_check_np(six, c, cfg(np_min_holders=0))[0])    # 0.5 a holder: in
        self.assertIsNone(npm.rug_check_np(report(c.mint, c.curve, 5), c, cfg(np_min_holders=0))[0])
        self.assertTrue(npm.rug_check_np(report(c.mint, c.curve, 5), c, cfg(np_min_holders=0, np_max_sol_per_holder=0))[0])
        self.assertTrue(npm.rug_check_np(report(c.mint, c.curve, 5), c, cfg(np_min_holders=0, np_max_sol_per_holder=0.6))[0])
        self.assertIsNone(npm.rug_check_np(report(c.mint, c.curve, 5), c,
                                           cfg(np_min_holders=0, np_max_sol_per_holder="junk"))[0])   # the default
        # no wallet seen at all and SOL in: a wait too (the holders rule off), never a division by zero
        self.assertEqual(npm.rug_check_np(empty_report(c.mint, c.curve), c, cfg(np_min_holders=0))[1],
                         ["waiting: RugCheck sees no holder for 3.0 SOL in (max 0.5 SOL a holder)"])
        # the holders rule speaks first; the defaults ask for 10 holders and one for each 0.5 SOL: 16 at 7.8 SOL
        tick = npm.Coin(mint="m", rsol0=0.0, rsol=7.8, buys=21)
        self.assertEqual(npm.rug_check_np(report("m", "cv", 15), tick, D)[1],
                         ["waiting: 15 holders for 7.8 SOL in (0.52 SOL a holder, max 0.5)"])
        self.assertTrue(npm.rug_check_np(report("m", "cv", 16), tick, D)[0])
        self.assertEqual(npm.rug_check_np(report("m", "cv", 9), tick, D)[1], ["waiting: RugCheck sees 9 holders (min 10)"])

    def test_tickz_and_sprayz_pass(self):
        """Many small buyers: 21 buy readings of 27, 7.8 SOL in, RugCheck sees 30 holders (0.26 SOL a holder)."""
        wl = npm.Watchlist(cfg(np_min_age_s=45))
        c, acts = coin_on(wl, "tickz", TICKZ, step=45.0 / (len(TICKZ) - 1), dev_sol=0.0)     # the last one at 46 s
        fired = [i for i, (a, _) in enumerate(acts) if a == "fire"]
        self.assertEqual(fired, [len(TICKZ) - 1])                               # at 45 s, like TICKZ
        self.assertEqual((c.buys, c.sells, round(c.sol_in, 1), round(c.rise_pct), round(c.mcap_sol)),
                         (21, 6, 7.8, 59, 44))
        self.assertEqual(npm.rug_check_np(report(c.mint, c.curve, 30, (1.7, 1.5, 1.4, 1.2)), c, wl.cfg),
                         (True, [], 30))
        wl2 = npm.Watchlist(cfg(np_min_age_s=45))
        s_, acts = coin_on(wl2, "sprayz", crowd_path(18, 4, 0.49, 0.255, dev=0.0), step=45.0 / 21, dev_sol=0.0)
        self.assertEqual(acts[-1][0], "fire")
        self.assertEqual((s_.buys, s_.sells, round(s_.sol_in, 1)), (18, 4, 7.8))
        self.assertTrue(npm.rug_check_np(report(s_.mint, s_.curve, 31, (1.4,)), s_, wl2.cfg)[0])

    def test_the_verdict_does_not_depend_on_the_poll_rate(self):
        """The adversarial review's finding: SOL per buy READING measured the poll rate, not the buyers. The same
        trades (a 0.15 SOL buy every 0.2 s from 38 s: a crowd) read every 1 s or every 0.5 s (the bot polls at 0.5 s
        while it holds a coin) once read "concentrated: 0.75 SOL a buy" at 1 s and fired at 0.5 s. Now the trigger
        fires at both rates, and the same RugCheck report gives the same verdict whatever the rate."""
        c_json = {k: v for k, v in json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                                               "memebot", "config.json"))).items()
                  if k.startswith("np_") and not k.startswith("np_snipe")}   # the momentum trigger (no snipe)
        trades = [(38.1 + 0.2 * i, 0.15) for i in range(60)]
        verdicts = {}
        for step in (1.0, 0.5):
            wl = npm.Watchlist(dict(D, **c_json))
            c = wl.on_create(TN.create("crowdflow", "CROWD", "crowd coin", dev_sol=0.3), T0)
            wl.on_meta(c.mint, {"telegram": "https://t.me/x"})
            got = fired = None
            for i, (t, sol) in enumerate(flow_readings(trades, step, 60)):
                if wl.on_curve(c.mint, TN.reading(0.3 + sol, slot=1000 + i), T0 + t) is None:
                    continue
                act, why = wl.judge(c, T0 + t)
                if act in ("fire", "reject"):
                    got, fired = act, why
                    break
            self.assertEqual(got, "fire", step)
            self.assertTrue(str(fired).startswith("momentum ·"), fired)      # the trigger, never a snipe
            verdicts[step] = [npm.rug_check_np(report(c.mint, c.curve, n), npm.Coin.from_dict(c.to_dict()), wl.cfg)[0]
                              for n in (8, 26)]
            self.assertEqual(verdicts[step], [None, True], step)             # 8 wallets wait, 26 pass

    def test_the_probes_uzim_waits_for_its_wallets(self):
        """The probe's UZIM: 9.2 SOL in at its trigger (4.5 SOL of snipers in its first reading), then +163 %. It is
        no longer rejected for its buy readings; RugCheck decides: 13 wallets wait (19 needed), 19 pass."""
        wl = npm.Watchlist(dict(D))
        c = TN.uzim(wl)
        for i, rs in enumerate(TN.UZIM_RSOL[:TN.UZIM_FIRE + 1]):
            wl.on_curve(c.mint, TN.reading(rs), TN.t_of(i))
            act, why = wl.judge(c, TN.t_of(i))
        self.assertEqual(act, "fire")
        self.assertAlmostEqual(c.buy_max, 4.5)
        self.assertEqual(npm.rug_check_np(report(c.mint, c.curve, 13), c, D)[1],
                         ["waiting: 13 holders for 9.2 SOL in (0.71 SOL a holder, max 0.5)"])
        self.assertTrue(npm.rug_check_np(report(c.mint, c.curve, 19), c, D)[0])

    def test_the_biggest_buy_reading(self):
        """buy_max: the biggest single rise of the real SOL between two readings (the first one holds the launch's
        snipers); np_max_buy_sol is off by default and rejects above it when set (inclusive)."""
        path = [2.3] + [round(2.3 + 0.15 * k, 6) for k in range(1, 15)]      # 2.0 SOL of snipers, 14 buys of 0.15
        c, acts = coin_on(npm.Watchlist(cfg()), "sniped", path)
        self.assertAlmostEqual(c.buy_max, 2.0)
        first = [a for a, _ in acts].index("fire")
        self.assertEqual(first, 5)                                             # 2.75 SOL from 6 buy readings
        self.assertEqual(acts[-1][0], "fire")                                  # off by default: it fires
        c, acts = coin_on(npm.Watchlist(cfg(np_max_buy_sol=2.0)), "sniped", path)
        self.assertEqual(acts[-1][0], "fire")                                  # 2.0 is still in
        c, acts = coin_on(npm.Watchlist(cfg(np_max_buy_sol=1.5)), "sniped", path)
        self.assertEqual(acts[first], ("reject", "concentrated: one buy of 2.0 SOL (max 1.5)"))
        self.assertEqual(c.status, "rejected: concentrated: one buy of 2.0 SOL (max 1.5)")
        # sells never count, and buy_max survives state.json (bounded)
        d = npm.Coin.from_dict(dict(c.to_dict(), buy_max=1e99))
        self.assertEqual(d.buy_max, npm.MAX_VSOL)
        self.assertEqual(npm.Coin.from_dict(dict(c.to_dict(), buy_max="x")).buy_max, 0.0)
        wl = npm.Watchlist(cfg())
        e = wl.on_create(TN.create("sells", dev_sol=0.3), T0)
        for i, rs in enumerate([1.0, 0.4, 3.0, 2.0]):
            wl.on_curve(e.mint, TN.reading(rs), T0 + i)
        self.assertAlmostEqual(e.buy_max, 2.6)
        self.assertEqual(npm.concentration(None, D), (False, "no coin"))
        self.assertFalse(hasattr(npm.Coin, "sol_per_buy"))                    # the per-reading measure is gone

    def test_garbage_never_raises(self):
        for x in (None, "junk", float("nan"), -1, 1e99):
            c = npm.Coin(mint="g", rsol=0.3 + (x if isinstance(x, float) and x == x and x < 1e9 else 0), buys=0)
            npm.concentration(c, {"np_max_buy_sol": x})
            npm.rug_cache_s({"mint": "g", "totalHolders": x}, c, {"np_min_holders": x, "np_rug_recheck_s": x,
                                                                   "np_max_sol_per_holder": x})
            npm.rug_check_np({"mint": "g", "totalHolders": x, "topHolders": x}, c,
                             {"np_min_holders": x, "np_max_sol_per_holder": x})
            npm.rug_check_np({"mint": "g", "totalHolders": 3, "topHolders": [x, {"pct": x}, {"address": x}]}, c,
                             {"np_max_sol_per_holder": x})
            npm.holder_counts({"topHolders": [x, {"pct": x, "owner": x}], "totalHolders": x}, x)
            npm.labels(c, None, None)


class Labels(unittest.TestCase):
    """5. The LEARNER's concentration labels, from RugCheck's PASS at the entry: the SOL in for each wallet holding the
    coin, and how many wallets hold it."""

    NEW = [lab for _, lab in npm.NP_SOLHOLD + npm.NP_HOLDERS]

    def test_the_buckets(self):
        def lab(sol, holders=None):
            c = npm.Coin(mint="m", rsol0=0.3, rsol=0.3 + sol, buys=8)
            if holders is not None:
                c.rug = {"ok": True, "reasons": [], "holders": holders, "top1_pct": 1.0, "top10_pct": 5.0}
            return [x for x in npm.labels(c) if x.startswith(("np:solh:", "np:holders:"))]
        self.assertEqual(lab(7.8, 30), ["np:solh:.25-.5", "np:holders:20-40"])     # TICKZ
        self.assertEqual(lab(5.0, 10), ["np:solh:.25-.5", "np:holders:10-20"])     # 0.5: inside the rule and the bucket
        self.assertEqual(lab(2.5, 12), ["np:solh:<.25", "np:holders:10-20"])
        self.assertEqual(lab(3.3, 6), ["np:solh:.5-1", "np:holders:<10"])          # STOREB under a looser config
        self.assertEqual(lab(9.2, 8), ["np:solh:1+", "np:holders:<10"])
        self.assertEqual(lab(9.2, 40), ["np:solh:<.25", "np:holders:40+"])
        self.assertEqual(lab(3.3), [])                                          # no PASS: no labels
        self.assertEqual(lab(3.3, 0), ["np:holders:<10"])                       # no wallet seen: no SOL a holder
        c = npm.Coin(mint="m", rsol0=0.3, rsol=3.3, buys=6)
        c.rug = {"ok": None, "wait": True, "holders": 3}                        # no PASS: no holder labels
        self.assertEqual([x for x in npm.labels(c) if x.startswith(("np:holders", "np:solh"))], [])

    def test_the_labels_fit_the_learners_format(self):
        """learner.py keeps only the labels in its KNOWN set, every one at most 16 characters and an "np:" one with
        two colons (tests/test_learner.py)."""
        for x in self.NEW:
            self.assertLessEqual(len(x), 16, x)
            self.assertEqual(x.count(":"), 2, x)
        self.assertEqual(len(set(self.NEW)), 8)
        learner = load("memebot", "learner")
        self.assertEqual(set(learner._labels(self.NEW)), set(self.NEW))       # all kept


# ---------------------------------------------------------------- the engine (bot.py, FakeFeeds, CurveMarket)

class Engine(unittest.TestCase):
    """bot.py's New Pairs engine on the test clock: the rules above decide its entries. np_rug_cache_s is set to
    np_rug_recheck_s here so a report is asked for again after 4 s whether or not bot.py's np_rug_get reads
    newpairs.rug_cache_s() yet."""

    setUp = E.Engine.setUp
    night = E.Engine.night
    turn = E.Engine.turn
    read = E.Engine.read
    NP = E.mb.newpairs                          # the engine's own newpairs (its Coin class, not this file's npm)

    def cfg(self, **kw):
        c = E.np_cfg(np_min_buys=6, np_min_sol=2.5, np_min_buy_share=0.65, np_min_rise_pct=12.0, np_need_social=False,
                     np_max_sol_per_holder=D["np_max_sol_per_holder"], np_min_holders=D["np_min_holders"],
                     np_rug_recheck_s=4.0, np_rug_cache_s=4.0, np_rug_prefetch=False, learn_skip_pct=-1000.0)
        c.update(kw)
        return c

    def start(self, n, mint, rep, dev=0.3):
        self.m.reports[mint] = rep
        self.turn(n, E.create(mint, mint.upper(), dev_sol=dev))
        self.turn(n, E.meta(mint, False, True, False), sec=0.3)

    def path(self, n, mint, path, sec=1.0):
        for rsol in path:
            self.read(n, mint, rsol, sec=sec)
            if mint in n.state["positions"]:
                return True
        return False

    def empty(self, mint):
        r = E.rug_report(mint, top=())
        r["totalHolders"] = 0
        return r

    def holders(self, mint, n, top=(3.0, 2.0, 1.5), empty=0):
        """A clean report listing the curve's account, n wallets holding the coin and `empty` sold-out accounts."""
        top = list(top)[:n] + [0.4] * max(0, n - len(top))
        r = E.rug_report(mint, top=top)
        r["topHolders"] += [{"address": "e%d" % i, "owner": "eo%d" % i, "pct": 0, "amount": 0} for i in range(empty)]
        r["totalHolders"] = len(r["topHolders"])
        return r

    def test_storeb_is_not_bought(self):
        """STOREB with RugCheck seeing its six big buyers (the concentration rule alone, np_min_holders 0): its 3.3 SOL
        over 6 wallets is 0.55 a holder, it waits; RugCheck was asked at its fire (the report is on the tape for the
        backtest), and the 2.9 SOL dump rejects it for good before any PASS."""
        n = self.night(self.cfg(np_min_holders=0))
        self.start(n, "storeb", self.holders("storeb", 6, (2.0,) * 6))
        self.assertFalse(self.path(n, "storeb", STOREB))
        coin = n.wl.get("storeb")
        self.read(n, "storeb", 3.65, sec=0.5)                                   # RugCheck's answer is in by now
        self.assertRegex(self.NP.rug_text(coin), r"^waiting: 6 holders for 3\.3 SOL in \(0\.5\d SOL a holder, max 0\.5\)$")
        self.assertEqual(self.m.rug_calls, ["storeb"])
        self.read(n, "storeb", STOREB_DUMP + 0.05, sec=2.5)
        self.assertEqual(coin.status, "rejected: dump: 2.9 SOL sold")
        self.assertEqual(n.state["positions"], {})

    def test_storeb_waits_for_its_holders_and_the_dump_rejects_it(self):
        """With only the holders rule: the fire finds RugCheck seeing 0 holders, no entry; 3 s later the 2.9 SOL
        dump rejects it for good (live it was bought, and sold at -25 %). The old rules buy it."""
        n = self.night(self.cfg(np_max_sol_per_holder=0))
        self.start(n, "storeb", self.empty("storeb"))
        self.assertFalse(self.path(n, "storeb", STOREB))
        coin = n.wl.get("storeb")
        self.read(n, "storeb", 3.65, sec=0.5)                                   # RugCheck's answer is in by now
        self.assertTrue(self.NP.rug_waiting(coin), coin.rug)
        self.assertEqual(self.NP.rug_text(coin), "waiting: RugCheck sees 0 holders (min 10)")
        self.assertEqual((coin.status, n.state["positions"]), ("watching", {}))
        self.read(n, "storeb", STOREB_DUMP + 0.05, sec=2.5)
        self.assertEqual(coin.status, "rejected: dump: 2.9 SOL sold")
        self.assertEqual(n.state["positions"], {})

    def test_the_old_rules_buy_storeb(self):
        n = self.night(self.cfg(np_max_sol_per_holder=0, np_min_holders=0))
        self.start(n, "storeb", self.empty("storeb"))
        self.assertTrue(self.path(n, "storeb", STOREB + [3.65]))
        self.assertEqual(n.state["positions"]["storeb"]["holders"], 0)          # "PASS · 0 holders", as live

    def test_tickz_is_bought(self):
        n = self.night(self.cfg(np_min_age_s=45))
        self.start(n, "tickz", self.holders("tickz", 30, (1.7, 1.5, 1.4, 1.2)), dev=0.0)
        self.assertTrue(self.path(n, "tickz", TICKZ + [TICKZ[-1] + 0.3], sec=45.0 / len(TICKZ)))
        self.assertGreaterEqual(n.wl.get("tickz").entry_t - n.wl.get("tickz").t0, 45.0)
        pos = n.state["positions"]["tickz"]
        self.assertEqual(pos["holders"], 30)
        self.assertIn("np:solh:.25-.5", pos.get("learn") or self.NP.labels(n.wl.get("tickz")))

    def test_a_waiting_coin_is_bought_once_rugcheck_sees_its_holders(self):
        n = self.night(self.cfg())
        crowd = crowd_path(14, 3, 0.35, 0.2)                                    # 4.3 SOL from 14 buy readings
        self.start(n, "slow", self.holders("slow", 3, (3.0,)))
        self.assertFalse(self.path(n, "slow", crowd))
        coin = n.wl.get("slow")
        self.assertEqual(coin.status, "watching")
        self.assertTrue(self.NP.rug_waiting(coin), coin.rug)
        self.assertEqual(self.NP.rug_text(coin), "waiting: RugCheck sees 3 holders (min 10)")
        first = len(self.m.rug_calls)
        self.m.reports["slow"] = self.holders("slow", 14)                       # RugCheck's index caught up
        r = crowd[-1]
        bought_after = None
        for k in range(12):
            r += 0.12
            self.read(n, "slow", r, sec=0.5)
            if "slow" in n.state["positions"]:
                bought_after = 0.5 * (k + 1)
                break
        self.assertIsNotNone(bought_after)
        self.assertLessEqual(bought_after, 5.5)                                 # within a recheck (4 s) and a reading
        self.assertLessEqual(len(self.m.rug_calls) - first, 2)                  # one more call, not one a reading
        self.assertEqual(n.state["positions"]["slow"]["holders"], 14)


# ---------------------------------------------------------------- the backtest's levers, judged by the replay

bt = load("memebot", "backtest")
R = bt.replay
START = R.parse_time("2026-11-26T08:00:00Z")


def tape_coin(mint, t0, path, holders=14, top=(3.0, 2.0, 1.5), rugs=None):
    """A recorded coin: the recorder's lines (replay.synth_coin) with RugCheck seeing `holders` wallets holding it (its
    "rug" line as the recorder writes it now: the accounts it listed, the curve's among them), or the rugs given:
    [(seconds after t0, holders)] as the live bot asked (its ask 0.35 s before each line)."""
    recs = R.synth_coin(mint, t0, path, social=(False, True, False))
    rug = next(r for r in recs if r["k"] == "rug")
    recs.remove(rug)
    for at, n in rugs or [(2.0, holders)]:
        top_n = list(top)[:n]
        recs.append(dict(rug, t=round(t0 + at, 3), holders=n + 1, held=n, empty=0, curve=1, top=top_n,
                         top1=top_n[0] if top_n else 0.0, top10=sum(top_n)))
    return recs


def replay_cfg(**kw):
    c = R.bot.load_config(R.bot.CONFIG)       # the momentum entry (np_snipe off): its RugCheck rules are the subject
    c.update(learn_skip_pct=-1000.0, learn_mult_min=1.0, learn_mult_max=1.0, np_min_age_s=0, np_snipe=False,
             np_max_sol_per_holder=D["np_max_sol_per_holder"], np_min_holders=D["np_min_holders"],
             np_max_buy_sol=D["np_max_buy_sol"], np_rug_recheck_s=4.0, np_rug_cache_s=4.0)
    c.update(kw)
    return c


def mints():
    return {k: (k + "x" * 40)[:40] + "pump" for k in ("conc", "crowd", "thin", "sniped", "later")}


# 8 buy readings of 1 SOL (6 SOL in when the trigger fires, 8 at the top), then one wallet sells 3.3 SOL: RugCheck sees
# 10 wallets (0.6-0.8 SOL a holder)
CONC = [round(0.3 + 1.0 * k, 2) for k in range(1, 9)]


def lever_tape():
    """Five coins a minute apart, each with ONE recorded RugCheck report (a tape that never asked again): a few big
    wallets, then their dump (conc: 6-8 SOL over 10 wallets), many small buyers (crowd: 6 SOL over 14), a crowd whose
    only report sees 3 wallets (thin), a crowd whose first reading is 2.0 SOL of snipers (sniped), and a thin one the
    live bot asked about again and found 14 wallets (later)."""
    m = mints()
    up = R.path_to(0.3, 6.3, 0.3)                                        # 20 buys of 0.3 SOL
    hold = R.path_to(6.3, 4.0, 0.4)
    recs = tape_coin(m["conc"], START + 10, CONC + R.path_to(8.3, 5.0, 3.3) + R.path_to(5.0, 2.0, 0.3), holders=10)
    recs += tape_coin(m["crowd"], START + 70, up + hold)
    recs += tape_coin(m["thin"], START + 130, up + hold, holders=3)
    recs += tape_coin(m["sniped"], START + 190, [2.3] + R.path_to(2.3, 6.5, 0.15) + hold)
    recs += tape_coin(m["later"], START + 250, up + hold, rugs=[(2.0, 3), (12.0, 14)])
    recs += [{"k": "hb", "t": START + 10 * i, "sol": 200.0} for i in range(0, 101)]
    return R.tape_of(sorted(recs, key=lambda r: r["t"]))


class Levers(unittest.TestCase):
    def test_the_levers_are_in_the_default_grid(self):
        for k in ("np_min_holders", "np_max_sol_per_holder", "np_max_buy_sol"):
            self.assertIn(k, bt.DEFAULT_GRID)
            self.assertNotIn(D[k], bt.DEFAULT_GRID[k], k)                       # never the default: no replay twice
            self.assertNotIn(k, bt.SIZE_LEVERS)
        self.assertIn(0, bt.DEFAULT_GRID["np_min_holders"])                    # off: the morning's rule
        self.assertIn(0, bt.DEFAULT_GRID["np_max_sol_per_holder"])
        base = replay_cfg()
        g = bt.build_grid("default", base, 200, seed=7)
        for lv in ({"np_min_holders": 0}, {"np_max_sol_per_holder": 0}, {"np_max_buy_sol": 1.5}):
            self.assertIn(lv, g)
        self.assertLessEqual(len(g), 200)                                       # still time-boxed
        self.assertEqual(len({str(sorted(x.items())) for x in g}), len(g))
        self.assertEqual(len([x for x in g if len(x) == 1]), sum(len(v) for v in bt.DEFAULT_GRID.values()))

    def test_the_replay_judges_each_lever(self):
        tape = lever_tape()
        m = mints()
        until = START + 1000                    # a job buys nothing in its last 5 minutes: the coins are early in it

        def traded(**kw):
            res = R.replay(tape, replay_cfg(**kw), START, until)
            got = {t["mint"] for t in res["trades"]} | {p["mint"] for p in res["open"]}
            return sorted(k for k, v in m.items() if v in got), res["stats"]

        base, st = traded()
        self.assertEqual(base, ["crowd", "later", "sniped"])
        # thin (3 wallets) waited and the tape had no fresher report for its recheck: unknown, not "no trade"; conc
        # (0.6 SOL a wallet) waited too, but its dump rejected it before a recheck was due: judged, not bought
        self.assertEqual((st["rug_stale"], st["rug_missing"], st["unseen"]), (1, 0, 0))
        self.assertEqual(st["unjudged"], 1)
        # thin's 6 SOL over 3 wallets waits on the SOL a holder too: both rules off buy it
        loose, st = traded(np_min_holders=0)
        self.assertEqual((loose, st["rug_stale"]), (["crowd", "later", "sniped"], 1))
        loose, st = traded(np_min_holders=0, np_max_sol_per_holder=0)
        self.assertEqual(loose, ["conc", "crowd", "later", "sniped", "thin"])
        self.assertEqual((st["rug_stale"], st["unjudged"]), (0, 0))
        self.assertEqual(traded(np_max_sol_per_holder=0)[0], ["conc", "crowd", "later", "sniped"])
        self.assertEqual(traded(np_max_buy_sol=1.5)[0], ["crowd", "later"])
        res = R.replay(tape, replay_cfg(np_max_sol_per_holder=0), START, until)
        conc = [t for t in res["trades"] if t["mint"] == m["conc"]][0]
        self.assertTrue(conc["why"].startswith("dump: "), conc)
        self.assertLess(conc["pnl_pct"], -15)                                   # what the rule spares: the dump

    def test_a_report_handed_out_again_is_stale_only_when_it_leaves_the_coin_waiting(self):
        """The adversarial review's finding: every report handed out twice made its coin "rug_stale", so an ordinary
        ask after the cache counted the coin as one the tape cannot judge, and
        the backtest's keep rule turned the lever down for a false reason (a SIZER retry here, asked again after the
        replay's 4 s cache for a minute). Only a recheck whose answer would leave the
        coin WAITING is stale: a passing report handed out again is the replay's "latest report by now"."""
        m = mints()
        tape = R.tape_of(tape_coin(m["crowd"], START + 5, R.path_to(0.3, 6.3, 0.3) + R.path_to(6.3, 7.5, 0.05)) +
                         [{"k": "hb", "t": START + 10 * i, "sol": 200.0} for i in range(0, 71)])
        mk = {}
        orig = R.ReplayMarket.__init__

        def init(market, *a, **kw):
            orig(market, *a, **kw)
            mk["m"] = market
        with unittest.mock.patch.object(R.ReplayMarket, "__init__", init):   # the SIZER has no order to place:
            res = R.replay(tape, replay_cfg(min_order_usd=1000.0), START, START + 700)   # every fire tried again
        self.assertGreater(mk["m"].stats["rug_asks"], 2)                        # asked again and again (4 s cache)
        self.assertEqual(len(mk["m"].rug_given[m["crowd"]]), 1)                 # always the one report on the tape
        self.assertEqual((res["stats"]["rug_stale"], res["stats"]["unjudged"]), (0, 0))
        # the same tape with RugCheck seeing 3 wallets: every recheck gets that report again while it waits: stale
        tape = R.tape_of(tape_coin(m["thin"], START + 5, R.path_to(0.3, 6.3, 0.3) + R.path_to(6.3, 7.5, 0.05),
                                   holders=3) + [{"k": "hb", "t": START + 10 * i, "sol": 200.0} for i in range(0, 71)])
        res = R.replay(tape, replay_cfg(), START, START + 700)
        self.assertEqual((res["stats"]["rug_stale"], res["stats"]["unjudged"]), (1, 1))
        # no holder rule at all: nothing can wait, nothing is stale
        res = R.replay(tape, replay_cfg(np_min_holders=0, np_max_sol_per_holder=0), START, START + 700)
        self.assertEqual((res["stats"]["rug_stale"], res["stats"]["unjudged"]), (0, 0))

    def test_a_tape_with_the_recheck_judges_the_wait(self):
        """A tape recorded with the recheck: the live bot asked at 2, 6 and 10 s and RugCheck saw 3, 3, then 14
        holders. The replay's rechecks find each of those reports in turn (none handed out twice: nothing stale)
        and it buys once the 14 are in, not before."""
        tape = R.tape_of(tape_coin(mints()["later"], START + 5, R.path_to(0.3, 6.3, 0.3) + R.path_to(6.3, 4.0, 0.4),
                                   rugs=[(2.0, 3), (6.0, 3), (10.0, 14)]) +
                         [{"k": "hb", "t": START + 10 * i, "sol": 200.0} for i in range(0, 71)])
        res = R.replay(tape, replay_cfg(), START, START + 700)
        self.assertEqual(len(res["trades"]) + len(res["open"]), 1)
        self.assertEqual((res["stats"]["rug_stale"], res["stats"]["unjudged"]), (0, 0))
        opened = R.ts_epoch((res["trades"] or res["open"])[0]["opened"])
        self.assertGreaterEqual(opened - START - 5, 10.0)                       # not before RugCheck saw 14

    def test_an_old_tape_line_never_buys_a_coin_the_live_rule_waits_for(self):
        """The post-fix review's finding, through the replay: an f16ee6e "rug" line (holders 15 = RugCheck's
        totalHolders: 4 wallets holding, 11 empty accounts; no "held") was bought by the base config as "15 holders"
        and counted judged (unjudged 0). Now it waits on its 4 wallets, and with no fresher report on the tape for its
        recheck it is unjudged (rug_stale), as the same report recorded by today's recorder is."""
        m = mints()["thin"]
        recs = tape_coin(m, START + 5, R.path_to(0.3, 6.3, 0.3) + R.path_to(6.3, 7.5, 0.05), holders=4,
                         top=(3.0, 2.0, 1.5, 1.0))
        for r in recs:
            if r["k"] == "rug":
                for k in ("held", "empty", "curve"):
                    r.pop(k)
                r["holders"] = 15
        tape = R.tape_of(recs + [{"k": "hb", "t": START + 10 * i, "sol": 200.0} for i in range(0, 71)])
        res = R.replay(tape, replay_cfg(), START, START + 700)
        self.assertEqual(len(res["trades"]) + len(res["open"]), 0)
        self.assertEqual((res["stats"]["rug_stale"], res["stats"]["unjudged"]), (1, 1))
        self.assertEqual(tape.summary()["rug_lines_old"], 1)
        # the holder rules off: judged on what the line says, as before
        res = R.replay(tape, replay_cfg(np_min_holders=0, np_max_sol_per_holder=0), START, START + 700)
        self.assertEqual(len(res["trades"]) + len(res["open"]), 1)


if __name__ == "__main__":
    unittest.main()
