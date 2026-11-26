#!/usr/bin/env python3
"""A live probe on GitHub Actions: where can the bot read a brand-new pump.fun coin's socials (X, Telegram, website)?

The live bot found ~43 % of metadata fetches failing (dweb.link, pinata, cloudflare-ipfs, then ipfs.io's 403), so
NEWS scored most coins 0-2 and every trade was a scalp. For N seconds this listens to PumpPortal's new coins, then
for each coin's metadata uri asks every candidate source at once and prints, per source: answers, JSON documents
with socials, median time. It never trades. python3 memebot/probe_meta.py --seconds 120
"""

import argparse
import asyncio
import concurrent.futures
import json
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

from websockets.asyncio.client import connect

PUMPPORTAL = "wss://pumpportal.fun/api/data"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

# "{cid}" is the CID plus any path; path gateways and subdomain gateways
GATEWAYS = [
    "https://dweb.link/ipfs/{cid}",
    "https://{cid1}.ipfs.dweb.link/{rest}",
    "https://gateway.pinata.cloud/ipfs/{cid}",
    "https://pump.mypinata.cloud/ipfs/{cid}",
    "https://cloudflare-ipfs.com/ipfs/{cid}",
    "https://ipfs.io/ipfs/{cid}",
    "https://w3s.link/ipfs/{cid}",
    "https://{cid1}.ipfs.w3s.link/{rest}",
    "https://nftstorage.link/ipfs/{cid}",
    "https://4everland.io/ipfs/{cid}",
    "https://ipfs.filebase.io/ipfs/{cid}",
    "https://gateway.lighthouse.storage/ipfs/{cid}",
    "https://flk-ipfs.xyz/ipfs/{cid}",
    "https://ipfs.runfission.com/ipfs/{cid}",
    "https://hardbin.com/ipfs/{cid}",
    "https://cf-ipfs.com/ipfs/{cid}",
]
# whole-coin sources keyed by mint
BY_MINT = [
    "https://frontend-api-v3.pump.fun/coins/{mint}",
    "https://frontend-api.pump.fun/coins/{mint}",
    "https://api.rugcheck.xyz/v1/tokens/{mint}/report",
]


def ipfs_path(uri):
    u = (uri or "").strip()
    if u.lower().startswith("ipfs://"):
        rest = u[7:]
        return (rest[5:] if rest.lower().startswith("ipfs/") else rest).split("?")[0] or None
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return None
    if "/ipfs/" in p.path:
        return p.path.split("/ipfs/", 1)[1] or None
    host = p.hostname or ""
    if ".ipfs." in host:
        return host.split(".ipfs.")[0] + (p.path if p.path not in ("", "/") else "")
    return None


def socials(doc):
    if not isinstance(doc, dict):
        return {}
    places = [doc] + [doc[k] for k in ("extensions", "properties", "links", "socials", "fileMeta", "tokenMeta")
                      if isinstance(doc.get(k), dict)]
    out = {}
    for field, keys in (("twitter", ("twitter", "x", "twitter_url")), ("telegram", ("telegram", "tg")),
                        ("website", ("website", "web", "site", "external_url"))):
        for place in places:
            v = next((place[k] for k in keys if isinstance(place.get(k), str) and place[k].strip()), "")
            if v:
                out[field] = v.strip()[:80]
                break
    return out


def get(url, timeout):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/plain,*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(400000).decode("utf-8", "replace")
            ms = int((time.time() - t0) * 1000)
            try:
                doc = json.loads(body)
            except ValueError:
                return {"st": r.status, "ms": ms, "json": False, "err": "not json: " + body[:60].replace("\n", " ")}
            return {"st": r.status, "ms": ms, "json": isinstance(doc, dict), "soc": socials(doc),
                    "keys": sorted(doc)[:30] if isinstance(doc, dict) else None}
    except urllib.error.HTTPError as e:
        return {"st": e.code, "ms": int((time.time() - t0) * 1000), "json": False, "err": "HTTP %s" % e.code}
    except Exception as e:  # noqa: BLE001 - a probe reports every failure
        return {"st": None, "ms": int((time.time() - t0) * 1000), "json": False,
                "err": "%s: %s" % (type(e).__name__, str(e)[:80])}


async def collect(seconds):
    creates = []
    t_end = time.time() + seconds
    async with connect(PUMPPORTAL, open_timeout=10, ping_interval=20, max_queue=4096) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        while time.time() < t_end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, t_end - time.time()))
            except asyncio.TimeoutError:
                break
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if isinstance(m, dict) and m.get("txType") == "create":
                m["_rx"] = time.time()
                creates.append(m)
    return creates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--max-coins", type=int, default=25)
    ap.add_argument("--timeout", type=float, default=4.0)
    a = ap.parse_args()
    creates = asyncio.run(collect(a.seconds))
    print("creates: %d in %d s" % (len(creates), a.seconds))
    hosts = {}
    for c in creates:
        u = c.get("uri") or ""
        h = "ipfs" if ipfs_path(u) else (urllib.parse.urlsplit(u).hostname or "?")
        hosts[h] = hosts.get(h, 0) + 1
    print("uri kinds:", json.dumps(hosts, sort_keys=True))
    coins = [c for c in creates if not c.get("is_mayhem_mode")][-a.max_coins:]
    stats = {}
    samples = {}
    jobs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        for c in coins:
            uri, mint = (c.get("uri") or "").strip(), c.get("mint") or ""
            path = ipfs_path(uri)
            if path:
                cid1, _, rest = path.partition("/")
                for g in GATEWAYS:
                    jobs.append((g, pool.submit(get, g.format(cid=path, cid1=cid1, rest=rest), a.timeout), mint))
            elif uri.startswith("http"):
                jobs.append(("direct:" + (urllib.parse.urlsplit(uri).hostname or "?"),
                             pool.submit(get, uri, a.timeout), mint))
            for g in BY_MINT:
                jobs.append((g, pool.submit(get, g.format(mint=mint), a.timeout), mint))
        for name, fut, mint in jobs:
            r = fut.result()
            s = stats.setdefault(name, {"n": 0, "json": 0, "soc": 0, "ms": [], "errs": {}})
            s["n"] += 1
            if r.get("json"):
                s["json"] += 1
                s["ms"].append(r["ms"])
                if r.get("soc"):
                    s["soc"] += 1
                samples.setdefault(name, (mint, r.get("soc"), r.get("keys")))
            else:
                e = str(r.get("err"))[:50]
                s["errs"][e] = s["errs"].get(e, 0) + 1
    print("%-48s %4s %5s %4s %7s  errors" % ("source", "n", "json", "soc", "med ms"))
    for name, s in sorted(stats.items(), key=lambda kv: (-kv[1]["json"] / max(1, kv[1]["n"]), kv[0])):
        med = int(statistics.median(s["ms"])) if s["ms"] else None
        top = sorted(s["errs"].items(), key=lambda kv: -kv[1])[:3]
        print("%-48s %4d %5d %4d %7s  %s" % (name[:48], s["n"], s["json"], s["soc"], med, top))
    print("=== a sample answer per source (mint, socials, keys)")
    for name, smp in samples.items():
        print(name[:48], json.dumps(smp)[:400])


if __name__ == "__main__":
    main()
