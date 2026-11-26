#!/usr/bin/env python3
"""A live probe of the real-time sources for New Pairs (brand-new pump.fun coins), run on GitHub Actions.

It does not trade. For N seconds it:
  1. listens to PumpPortal's free stream (new coins and migrations) and prints the real event fields;
  2. follows the bonding curves of the first new coins on the public Solana RPC WebSocket (accountSubscribe) and
     checks the decoded reserves against the coin's create event;
  3. asks, for a few new coins after ~20 s, what Jupiter quotes, what RugCheck knows, what DexScreener has indexed
     and what the coin's metadata (X, Telegram, website) says, with the time each answer took.
The summary at the end says what works and how fast. python3 memebot/probe.py --seconds 90
"""

import argparse
import asyncio
import base64
import json
import statistics
import struct
import time
import urllib.parse
import urllib.request

from websockets.asyncio.client import connect

PUMPPORTAL = "wss://pumpportal.fun/api/data"
RPC_WS = "wss://api.mainnet-beta.solana.com"
RPC_HTTP = "https://api.mainnet-beta.solana.com"
SOL = "So11111111111111111111111111111111111111112"
UA = "Mozilla/5.0 (meme-bot probe)"


def http_json(url, timeout=6):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            hdr = {k.lower(): v for k, v in r.headers.items() if k.lower().startswith(("x-ratelimit", "cache-control", "age"))}
            return {"ok": True, "status": r.status, "ms": int((time.time() - t0) * 1000), "json": json.loads(body), "hdr": hdr}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "ms": int((time.time() - t0) * 1000), "err": e.read()[:200].decode("utf-8", "replace")}
    except Exception as e:  # noqa: BLE001 - a probe reports every failure
        return {"ok": False, "status": None, "ms": int((time.time() - t0) * 1000), "err": "%s: %s" % (type(e).__name__, e)}


def decode_curve(b64):
    raw = base64.b64decode(b64)
    vtok, vquote, rtok, rquote, supply = struct.unpack_from("<5Q", raw, 8)
    return {"len": len(raw), "vtok": vtok, "vsol": vquote, "rtok": rtok, "rsol": rquote, "supply": supply,
            "complete": raw[48] != 0, "price_sol": (vquote / 1e9) / (vtok / 1e6) if vtok else None}


def curve_buy_tokens(vsol_lamports, vtok_raw, sol_in, fee_pct):
    """Tokens (whole) a buy of sol_in SOL gets on a constant-product curve after the fee."""
    x = vsol_lamports / 1e9
    y = vtok_raw / 1e6
    s = sol_in * (1 - fee_pct / 100.0)
    return y - x * y / (x + s)


async def main(seconds, follow, enrich):
    out = {"creates": [], "migrates": [], "notices": [], "curve_updates": {}, "decoded": [], "enrich": [],
           "errors": [], "rx_lag_ms": []}
    t_end = time.time() + seconds
    sub_ids = {}          # rpc request id -> mint
    subs = {}             # subscription id -> mint
    created = {}          # mint -> create event
    rpc = None
    rpc_q = asyncio.Queue()

    async def pumpportal():
        try:
            async with connect(PUMPPORTAL, open_timeout=10, ping_interval=20, max_queue=4096) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                while time.time() < t_end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, t_end - time.time()))
                    except asyncio.TimeoutError:
                        break
                    rx = time.time()
                    try:
                        m = json.loads(raw)
                    except ValueError:
                        out["errors"].append("pumpportal non-json: %s" % str(raw)[:120])
                        continue
                    if not isinstance(m, dict) or "txType" not in m:
                        out["notices"].append(str(m)[:300])
                        continue
                    m["_rx"] = rx
                    if m["txType"] == "create":
                        out["creates"].append(m)
                        created[m.get("mint")] = m
                        if len(created) <= follow and m.get("bondingCurveKey"):
                            await rpc_q.put(m)
                    elif m["txType"] == "migrate":
                        out["migrates"].append(m)
                    else:
                        out["notices"].append("other txType: %s" % str(m)[:300])
        except Exception as e:  # noqa: BLE001
            out["errors"].append("pumpportal: %s: %s" % (type(e).__name__, e))

    async def chain():
        nonlocal rpc
        rid = 0
        try:
            async with connect(RPC_WS, open_timeout=10, ping_interval=20, max_queue=4096, max_size=2 ** 22) as ws:
                rpc = ws

                async def subscriber():
                    nonlocal rid
                    while time.time() < t_end:
                        try:
                            ev = await asyncio.wait_for(rpc_q.get(), timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        rid += 1
                        sub_ids[rid] = ev["mint"]
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "accountSubscribe",
                                                  "params": [ev["bondingCurveKey"], {"encoding": "base64",
                                                                                     "commitment": "processed"}]}))

                task = asyncio.create_task(subscriber())
                while time.time() < t_end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, t_end - time.time()))
                    except asyncio.TimeoutError:
                        break
                    m = json.loads(raw)
                    if "result" in m and "id" in m:
                        subs[m["result"]] = sub_ids.get(m["id"])
                        continue
                    if "error" in m:
                        out["errors"].append("rpc: %s" % str(m)[:300])
                        continue
                    if m.get("method") == "accountNotification":
                        p = m["params"]
                        mint = subs.get(p["subscription"])
                        val = p["result"]["value"]
                        d = decode_curve(val["data"][0])
                        d["mint"] = mint
                        d["slot"] = p["result"]["context"]["slot"]
                        d["t"] = time.time()
                        out["curve_updates"].setdefault(mint, []).append(d)
                task.cancel()
        except Exception as e:  # noqa: BLE001
            out["errors"].append("rpc ws at +%.0fs: %s: %s" % (time.time() - (t_end - seconds), type(e).__name__, e))

    async def chain_retry():
        for i in range(5):
            if time.time() >= t_end:
                return
            await chain()
            for ev in list(created.values())[:follow]:        # resubscribe after a drop
                if ev.get("bondingCurveKey"):
                    await rpc_q.put(ev)
            await asyncio.sleep(2 + 2 * i)

    async def poller():
        """getMultipleAccounts over HTTP for every followed curve, every 0.5 s: latency, errors, changes seen."""
        last = {}
        while time.time() < t_end:
            await asyncio.sleep(0.5)
            keys = [(m, ev["bondingCurveKey"]) for m, ev in list(created.items())[:follow] if ev.get("bondingCurveKey")]
            if not keys:
                continue
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                               "params": [[k for _, k in keys], {"encoding": "base64", "commitment": "processed"}]}).encode()
            t0 = time.time()

            def call():
                req = urllib.request.Request(RPC_HTTP, data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, json.loads(r.read().decode()), dict(r.headers)
                except urllib.error.HTTPError as e:
                    return e.code, None, dict(e.headers)
                except Exception as e:  # noqa: BLE001
                    return "%s: %s" % (type(e).__name__, e), None, {}

            st, j, hdr = await asyncio.to_thread(call)
            ms = int((time.time() - t0) * 1000)
            pl = out.setdefault("poll", {"ok": 0, "err": {}, "ms": [], "changes": 0, "nulls": 0})
            if st == 200 and isinstance(j, dict) and "result" in j:
                pl["ok"] += 1
                pl["ms"].append(ms)
                for (m, _), v in zip(keys, j["result"]["value"]):
                    if not v:
                        pl["nulls"] += 1
                        continue
                    d = decode_curve(v["data"][0])
                    if last.get(m) and (last[m]["vsol"], last[m]["vtok"]) != (d["vsol"], d["vtok"]):
                        pl["changes"] += 1
                    last[m] = d
            else:
                k = str(st) if j is None else "rpc-error %s" % str(j.get("error"))[:80]
                pl["err"][k] = pl["err"].get(k, 0) + 1
                if "retry-after" in {x.lower() for x in hdr}:
                    pl.setdefault("retry_after", []).append(hdr.get("Retry-After") or hdr.get("retry-after"))

    async def enricher():
        done = set()
        while time.time() < t_end - 5:
            await asyncio.sleep(2)
            for mint, ev in list(created.items()):
                if mint in done or len(done) >= enrich or time.time() - ev["_rx"] < 20:
                    continue
                done.add(mint)
                row = {"mint": mint, "symbol": ev.get("symbol"), "age_s": round(time.time() - ev["_rx"], 1)}
                lamports = int(0.05 * 1e9)
                for name, url in (
                        ("jup_lite_quote", "https://lite-api.jup.ag/swap/v1/quote?" + urllib.parse.urlencode(
                            {"inputMint": SOL, "outputMint": mint, "amount": lamports, "slippageBps": 500})),
                        ("jup_api_quote", "https://api.jup.ag/swap/v1/quote?" + urllib.parse.urlencode(
                            {"inputMint": SOL, "outputMint": mint, "amount": lamports, "slippageBps": 500})),
                        ("jup_price", "https://lite-api.jup.ag/price/v3?ids=" + mint),
                        ("rugcheck", "https://api.rugcheck.xyz/v1/tokens/%s/report" % mint),
                        ("dexscreener", "https://api.dexscreener.com/tokens/v1/solana/" + mint),
                        ("metadata", ev.get("uri") or "")):
                    if not url:
                        row[name] = {"ok": False, "err": "no uri"}
                        continue
                    r = await asyncio.to_thread(http_json, url)
                    j = r.pop("json", None)
                    if name.startswith("jup_") and name.endswith("quote") and isinstance(j, dict):
                        r["outAmount"] = j.get("outAmount")
                        r["route"] = [((s or {}).get("swapInfo") or {}).get("label") for s in j.get("routePlan") or []]
                        r["impact"] = j.get("priceImpactPct")
                    elif name == "jup_price" and isinstance(j, dict):
                        r["price"] = j.get(mint)
                    elif name == "rugcheck" and isinstance(j, dict):
                        r["keys"] = sorted(j)[:40]
                        r["totalHolders"] = j.get("totalHolders")
                        r["topHolders"] = len(j.get("topHolders") or [])
                        r["risks"] = [x.get("name") for x in j.get("risks") or []][:8]
                        r["creatorBalance"] = j.get("creatorBalance")
                    elif name == "dexscreener":
                        r["pairs"] = len(j) if isinstance(j, list) else None
                        if isinstance(j, list) and j:
                            r["dex"] = j[0].get("dexId")
                    elif name == "metadata" and isinstance(j, dict):
                        r["keys"] = sorted(j)
                        r["social"] = {k: (j.get(k) or "")[:60] for k in ("twitter", "telegram", "website")}
                    row[name] = r
                ups = out["curve_updates"].get(mint) or []
                if ups and ev.get("vSolInBondingCurve") is not None:
                    last = ups[-1]
                    row["curve_buy_0.05sol_tokens_fee1.25"] = round(curve_buy_tokens(last["vsol"], last["vtok"], 0.05, 1.25), 2)
                out["enrich"].append(row)

    await asyncio.gather(pumpportal(), chain_retry(), poller(), enricher())

    # ---- the report
    c, mg = out["creates"], out["migrates"]
    print("=== PUMPPORTAL ===")
    print("creates: %d (%.1f/min) · migrates: %d (%.1f/min) · notices: %s" % (
        len(c), len(c) / seconds * 60, len(mg), len(mg) / seconds * 60, out["notices"][:4]))
    for ev in c[:2]:
        print("create sample:", json.dumps({k: v for k, v in ev.items() if k != "_rx"})[:900])
    for ev in mg[:2]:
        print("migrate sample:", json.dumps({k: v for k, v in ev.items() if k != "_rx"})[:600])
    keys = {}
    for ev in c:
        for k in ev:
            keys[k] = keys.get(k, 0) + 1
    print("create keys:", keys)
    pools = {}
    for ev in c:
        pools[ev.get("pool")] = pools.get(ev.get("pool"), 0) + 1
    print("create pools:", pools)
    print("=== CHAIN (accountSubscribe on bonding curves) ===")
    print("followed curves:", len(subs), "· with updates:", sum(1 for v in out["curve_updates"].values() if v))
    for mint, ups in list(out["curve_updates"].items())[:follow]:
        ev = created.get(mint) or {}
        first, last = ups[0], ups[-1]
        exp = (ev.get("vSolInBondingCurve") or 0) / (ev.get("vTokensInBondingCurve") or 1)
        gaps = [round(b["t"] - a["t"], 2) for a, b in zip(ups, ups[1:])]
        print("%s %-10s updates %3d · len %d · price create %.3e vs first decode %.3e · last %.3e (%+.0f%%) · rsol %.2f → %.2f · "
              "complete %s · median gap %ss" % (
                  mint[:8], (ev.get("symbol") or "")[:10], len(ups), first["len"], exp, first["price_sol"] or 0,
                  last["price_sol"] or 0, ((last["price_sol"] or 0) / (first["price_sol"] or 1) - 1) * 100,
                  first["rsol"] / 1e9, last["rsol"] / 1e9, last["complete"], statistics.median(gaps) if gaps else "-"))
    print("=== ENRICH (after ~20 s) ===")
    for row in out["enrich"]:
        rc = row.get("rugcheck") or {}
        md = row.get("metadata") or {}
        ds = row.get("dexscreener") or {}
        jq = row.get("jup_lite_quote") or {}
        print("%s %-8s age %ss | jup %s %sms %s out %s | rug %s %sms holders %s top %s risks %s | dex %s pairs %s %s | meta %s %sms %s %s" % (
            row["mint"][:8], (row.get("symbol") or "")[:8], row.get("age_s"), jq.get("status"), jq.get("ms"), jq.get("route"),
            jq.get("outAmount"), rc.get("status"), rc.get("ms"), rc.get("totalHolders"), rc.get("topHolders"), rc.get("risks"),
            ds.get("status"), ds.get("pairs"), ds.get("dex"), md.get("status"), md.get("ms"), md.get("social"), md.get("err", "")[:80]))
        print("   curve estimate 0.05 SOL:", row.get("curve_buy_0.05sol_tokens_fee1.25"), "tokens · jupiter:",
              int(jq.get("outAmount") or 0) / 1e6)
    pl = out.get("poll") or {}
    print("=== HTTP POLL (getMultipleAccounts every 0.5 s) ===")
    print("ok %s · errors %s · median %s ms · p90 %s ms · changes seen %s · null accounts %s · retry-after %s" % (
        pl.get("ok"), pl.get("err"), statistics.median(pl["ms"]) if pl.get("ms") else "-",
        sorted(pl["ms"])[int(len(pl["ms"]) * 0.9)] if pl.get("ms") else "-", pl.get("changes"), pl.get("nulls"),
        (pl.get("retry_after") or [])[:3]))
    print("=== ERRORS ===")
    for e in out["errors"][:20]:
        print(e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=90)
    ap.add_argument("--follow", type=int, default=8)
    ap.add_argument("--enrich", type=int, default=4)
    a = ap.parse_args()
    asyncio.run(main(a.seconds, a.follow, a.enrich))
