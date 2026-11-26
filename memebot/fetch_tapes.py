#!/usr/bin/env python3
"""Meme Bot: the live bot's recorded tapes from GitHub Actions for the backtester, and its report for people.
Used by the Backtest workflow (.github/workflows/backtest.yml); standard library only.

    python3 memebot/fetch_tapes.py fetch [--hours 24] [--out tapes] [--repo OWNER/NAME] [--workflow memebot.yml]
                                         [--max-mb 500] [--branch NAME] [--manifest FILE] [--md FILE]
                                         [--github-output FILE] [--api URL]
    python3 memebot/fetch_tapes.py summary report.json [--md FILE]

fetch: every artifact named tapes-<run id>-<attempt> (each memebot.yml job uploads its own tapes when it ends:
memebot/recorder.py's UTC hours, kept 14 days) whose job ended in the last --hours hours, unzipped into
OUT/<run id>-<attempt>/ (memebot/backtest.py --data OUT reads such a folder: one subfolder per job).
  - GitHub's REST API (GITHUB_API_URL, default https://api.github.com) with the token in GH_TOKEN or GITHUB_TOKEN
    (in the workflow: the job's own token, permission actions: read). The token goes to the API host only, never
    to the storage host an artifact download is redirected to.
  - the repository's artifact list, every page (the Link header's rel="next"; page numbers when it has none).
  - "ended in the last N hours": the artifact's created_at (the upload is the job's last step), at or after
    now - N hours. Only runs of --workflow (each run looked up once: its workflow_id, or its path) that the live
    bot's chain makes: a "schedule" or "workflow_dispatch" run of this repository (not a pull request's run, which
    GitHub runs under the same workflow id with the PR's own copy of the file, a fork's included) on --branch (the
    repository's default branch; "*": any). Others are listed under skipped "not_the_bot", with why.
  - the newest first, up to --max-mb MB unzipped in all (default 500): backtest.py keeps the whole tape in memory,
    about 6x its size in the main process and 4x more in each of its workers (measured: about 23x with 4 workers),
    so 500 MB stays under a 16 GB runner. The first artifact that would pass the cap and every older one are listed
    under skipped "over_cap": the tapes used are the newest contiguous hours, never a window with a hole.
  - expired artifacts (past their 14 days, or a download that answers 410) are skipped and listed; a download that
    still fails after 3 tries (a refused or cut-off transfer, a broken zip: anything) is skipped with a warning and
    its folder removed (the backtest runs on the rest).
  - none found: a clear message, exit 0, count=0 in --github-output (the workflow then skips the backtest).
    Exit 1 when the API refuses the list (a missing permission) or every download failed; 2 on bad arguments.
summary: report.json's own lines (backtest.py's text_lines) into the log, and a Markdown page (--md, e.g. the
job's $GITHUB_STEP_SUMMARY): the decision, the top configs on the first part and on the held-out part, the live
check, the tape's gaps.

Read-only: it never runs git, never touches the meme-data branch, the live bot, its recorder or the live channel.
The report is paper on paper: a "change" there is a hint for a person to judge, never applied by this code.
"""

import argparse
import datetime
import http.client
import io
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

NAME_RE = re.compile(r"^tapes-(\d+)-(\d+)$")
PER_PAGE = 100
MAX_PAGES = 100            # 10 000 artifacts: far more than 14 days of jobs; a warning when it is reached
TRIES = 3
REDIRECTS = 5
MAX_MB = 500.0             # unzipped tape a backtest may hold (see the module's doc: memory, not time, is the limit)
BOT_EVENTS = ("schedule", "workflow_dispatch")     # how memebot.yml's jobs start (the hourly net, the chain, a person)
USER_AGENT = "memebot-backtest"          # GitHub's API wants one; a generic name, nothing about the user
API = "https://api.github.com"
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


class ApiError(Exception):
    def __init__(self, status, url, text=""):
        self.status, self.url = status, url
        super().__init__("HTTP %s from %s%s" % (status, _strip_query(url), (": " + text) if text else ""))


class Expired(ApiError):
    pass


def _strip_query(url):
    """A URL for the log without its query (a storage URL's signature stays out of the log)."""
    p = urllib.parse.urlsplit(str(url))
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def parse_ts(s):
    """GitHub's ISO time ("2026-11-26T10:00:00Z") -> epoch seconds, or None."""
    if not s or not isinstance(s, str):
        return None
    try:
        d = datetime.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return (d - EPOCH).total_seconds()


def iso(t):
    if t is None:
        return "-"
    return (EPOCH + datetime.timedelta(seconds=float(t))).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_link(link):
    """The rel="next" URL of a Link header, or None."""
    for part in (link or "").split(","):
        m = re.match(r'\s*<([^>]+)>\s*;(.*)', part)
        if m and re.search(r'rel\s*=\s*"?next"?', m.group(2)):
            return m.group(1)
    return None


# ---------------------------------------------------------------- the transport

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                      # Api.download follows it itself, without the token


def urllib_transport(url, headers, timeout=120):
    """One GET, no redirect followed -> (status, {lower-case header: value}, body bytes). Network errors raise."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read() or b""
        except Exception:
            body = b""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body


class Api:
    """GitHub's REST API over a transport (urllib_transport, or a fake in the tests)."""

    def __init__(self, token, base=API, transport=None, tries=TRIES, sleep=time.sleep, backoff_s=2.0):
        self.token, self.base = token, (base or API).rstrip("/")
        self.host = urllib.parse.urlsplit(self.base).netloc
        self.transport = transport or urllib_transport
        self.tries, self.sleep, self.backoff_s = max(1, int(tries)), sleep, backoff_s
        self.calls = 0

    def url(self, path):
        return path if re.match(r"^https?://", path) else self.base + "/" + path.lstrip("/")

    def headers(self, url):
        h = {"User-Agent": USER_AGENT}
        if urllib.parse.urlsplit(url).netloc == self.host:          # the token to the API only
            h.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
            if self.token:
                h["Authorization"] = "Bearer " + self.token
        return h

    def send(self, url):
        """GET with retries on network errors, 5xx and 429 -> (status, headers, body)."""
        err = None
        for k in range(self.tries):
            self.calls += 1
            try:
                st, hd, body = self.transport(url, self.headers(url))
            except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
                # http.client.HTTPException: a transfer cut off before Content-Length (IncompleteRead), a bad
                # status line ... a network error like any other, tried again
                err, st, hd, body = e, None, {}, b""
            if st is not None and st < 500 and st != 429:
                return st, hd, body
            if k + 1 < self.tries:
                self.sleep(self.backoff_s * (2 ** k))
        if st is None:
            raise ApiError("network", url, str(err))
        return st, hd, body

    def get_json(self, path):
        url = self.url(path)
        st, hd, body = self.send(url)
        if st != 200:
            raise ApiError(st, url, _message(body))
        try:
            return json.loads(body.decode("utf-8") if isinstance(body, bytes) else body), hd
        except ValueError:
            raise ApiError(st, url, "not JSON")

    def pages(self, path, key, max_pages=MAX_PAGES, warn=None):
        """Every item under `key` of a listing, page by page (Link rel="next"; else page numbers while full pages
        come and total_count says more are left)."""
        sep = "&" if "?" in path else "?"
        first = "%s%sper_page=%d" % (path, sep, PER_PAGE)
        url, page, seen = first, 1, 0
        while url:
            doc, hd = self.get_json(url)
            items = doc.get(key) or [] if isinstance(doc, dict) else []
            for it in items:
                yield it
            seen += len(items)
            total = doc.get("total_count") if isinstance(doc, dict) else None
            nxt = next_link(hd.get("link"))
            if nxt is None and len(items) >= PER_PAGE and (not isinstance(total, int) or seen < total):
                nxt = "%s&page=%d" % (first, page + 1)
            if nxt and page >= max_pages:
                if warn:
                    warn("stopped after %d pages of %s (%d items): the rest is not read" % (page, path, seen))
                return
            url, page = nxt, page + 1

    def download(self, url):
        """An artifact's zip: the API answers with a redirect to the storage host, followed here without the
        token. -> bytes; Expired on 410; ApiError otherwise."""
        url = self.url(url)
        for _ in range(REDIRECTS + 1):
            st, hd, body = self.send(url)
            if st in (301, 302, 303, 307, 308) and hd.get("location"):
                url = urllib.parse.urljoin(url, hd["location"])
                continue
            if st == 200:
                return body
            if st == 410:
                raise Expired(st, url, _message(body) or "expired")
            raise ApiError(st, url, _message(body))
        raise ApiError("redirects", url, "more than %d redirects" % REDIRECTS)


def _message(body):
    try:
        doc = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        return str(doc.get("message") or "")[:200] if isinstance(doc, dict) else ""
    except (ValueError, AttributeError, UnicodeDecodeError):
        return ""


# ---------------------------------------------------------------- the artifacts

def candidates(artifacts, cutoff):
    """The repository's artifacts -> ([tapes artifacts whose job ended at or after cutoff, oldest first], counts).
    Each: {"name", "run_id", "attempt", "ended", "expired", "size", "id", "url", "run"}. A name twice (should
    not happen): the newest one that has not expired."""
    best, n = {}, {"listed": 0, "other_names": 0, "older": 0}
    for a in artifacts:
        n["listed"] += 1
        name = str(a.get("name") or "")
        m = NAME_RE.match(name)
        if not m:
            n["other_names"] += 1
            continue
        ended = parse_ts(a.get("created_at")) or parse_ts(a.get("updated_at"))
        if ended is None or ended < cutoff:
            n["older"] += 1
            continue
        run = a.get("workflow_run") if isinstance(a.get("workflow_run"), dict) else {}
        c = {"name": name, "run_id": int(run.get("id") or m.group(1)), "attempt": int(m.group(2)),
             "dir": "%s-%s" % (m.group(1), m.group(2)), "ended": ended, "expired": bool(a.get("expired")),
             "size": int(a.get("size_in_bytes") or 0), "id": a.get("id"), "url": a.get("archive_download_url")}
        old = best.get(name)
        if old is None or (old["expired"], -old["ended"]) > (c["expired"], -c["ended"]):
            best[name] = c
    return sorted(best.values(), key=lambda c: (c["ended"], c["name"])), n


def extract(data, dest):
    """An artifact's zip into dest (emptied first) -> [relative paths written]. A member that would land outside
    dest (an absolute path, "..") is skipped; nothing is executed or linked."""
    z = zipfile.ZipFile(io.BytesIO(data))            # not a zip: BadZipFile before dest is touched
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    root = os.path.realpath(dest)
    files = []
    with z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if not parts or name.startswith("/") or ".." in parts or ":" in parts[0]:
                continue
            target = os.path.join(root, *parts)
            if os.path.commonpath([root, os.path.realpath(target)]) != root:
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            files.append("/".join(parts))
    return sorted(files)


def unzipped_size(data):
    """The bytes an artifact's zip unpacks to (its members' declared sizes). BadZipFile when it is no zip."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return sum(max(0, int(i.file_size)) for i in z.infolist() if not i.is_dir())


def _mb(n):
    return "%.1f MB" % (n / 1e6)


def not_the_bot(run, repo, branch):
    """None for a run of the live bot's chain, else why not: not a "schedule" or "workflow_dispatch" run (a pull
    request's run, a fork's too, uses the PR's own copy of the workflow under the same workflow id), started from
    another repository, or on another branch than `branch` (None: any)."""
    ev = str(run.get("event") or "?")
    if ev not in BOT_EVENTS:
        return "a %s run" % ev
    head = run.get("head_repository") if isinstance(run.get("head_repository"), dict) else {}
    name = str(head.get("full_name") or "")
    if name.lower() != str(repo).lower():
        return "a run from %s" % (name or "an unknown repository")
    if branch and run.get("head_branch") != branch:
        return "a run on the branch %s (the bot's is %s)" % (run.get("head_branch") or "?", branch)
    return None


def fetch(api, repo, out, hours=24.0, workflow="memebot.yml", now=None, say=print, warn=None, max_mb=MAX_MB,
          branch=None):
    """Download the tapes (see the module's doc) -> the manifest dict ({"count", "artifacts", "skipped", ...}).
    branch: the runs' branch (None: the repository's default branch, looked up; "*": any)."""
    warn = warn or (lambda msg: say("warning: " + msg))
    now = time.time() if now is None else float(now)
    cutoff = now - float(hours) * 3600.0
    cap = float(max_mb) * 1e6 if max_mb is not None and float(max_mb) > 0 else float("inf")
    wf_path = ".github/workflows/" + workflow
    say("tapes: the artifacts tapes-<run id>-<attempt> of %s in %s whose job ended after %s (the last %g h), the "
        "newest first up to %s unzipped" % (workflow, repo, iso(cutoff), hours,
                                            _mb(cap) if math.isfinite(cap) else "any size"))
    if branch is None:
        try:
            doc, _ = api.get_json("/repos/%s" % repo)
            branch = str(doc.get("default_branch") or "") or None
        except ApiError as e:
            warn("the repository's default branch could not be read (%s): runs on any branch are taken" % e)
    elif branch == "*":
        branch = None
    wf_id = None
    try:
        wf, _ = api.get_json("/repos/%s/actions/workflows/%s" % (repo, urllib.parse.quote(workflow)))
        wf_id = wf.get("id")
        wf_path = str(wf.get("path") or wf_path).split("@")[0]
    except ApiError as e:
        if e.status != 404:
            raise
        warn("no workflow %s in %s (HTTP 404): its runs are matched by path only" % (workflow, repo))
    cands, n = candidates(api.pages("/repos/%s/actions/artifacts" % repo, "artifacts", warn=warn), cutoff)
    man = {"repo": repo, "workflow": workflow, "hours": hours, "now": iso(now), "cutoff": iso(cutoff), "out": out,
           "branch": branch, "max_mb": max_mb, "listed": n, "artifacts": [],
           "skipped": {"expired": [], "other_workflow": [], "not_the_bot": [], "failed": [], "over_cap": []}}
    runs = {}
    used = 0                                            # bytes unzipped so far
    full = False                                        # the cap was reached: every older artifact is left out
    os.makedirs(out, exist_ok=True)
    for c in reversed(cands):                           # the newest first: the cap keeps the latest hours
        if c["expired"]:
            man["skipped"]["expired"].append(c["name"])
            continue
        if c["run_id"] not in runs:
            try:
                runs[c["run_id"]], _ = api.get_json("/repos/%s/actions/runs/%d" % (repo, c["run_id"]))
            except ApiError as e:
                runs[c["run_id"]] = {"_error": str(e)}
        run = runs[c["run_id"]]
        if run.get("_error"):
            why = "%s: its run %d could not be read (%s)" % (c["name"], c["run_id"], run["_error"])
            man["skipped"]["failed"].append(why)
            warn(why)
            continue
        path = str(run.get("path") or "").split("@")[0]
        if not ((wf_id is not None and run.get("workflow_id") == wf_id) or path == wf_path):
            man["skipped"]["other_workflow"].append(c["name"])
            continue
        why = not_the_bot(run, repo, branch)
        if why is not None:
            man["skipped"]["not_the_bot"].append("%s: %s" % (c["name"], why))
            warn("%s left out: %s, not the live bot's (only its schedule and workflow_dispatch runs count)" % (
                c["name"], why))
            continue
        if full:
            man["skipped"]["over_cap"].append(c["name"])
            continue
        url = c["url"] or "/repos/%s/actions/artifacts/%s/zip" % (repo, c["id"])
        dest = os.path.join(out, c["dir"])
        try:
            data = api.download(url)
            size = unzipped_size(data)
            if used + size > cap:
                full = True
                man["skipped"]["over_cap"].append(c["name"])
                warn("%s and every older tape left out: %s unzipped would pass the %s cap (--max-mb; a backtest "
                     "keeps the whole tape in memory)" % (c["name"], _mb(used + size), _mb(cap)))
                continue
            files = extract(data, dest)
        except Expired:
            man["skipped"]["expired"].append(c["name"])
            continue
        except Exception as e:                              # noqa: BLE001 - one artifact never stops the others
            shutil.rmtree(dest, ignore_errors=True)          # never half a job's tape
            man["skipped"]["failed"].append("%s: %s: %s" % (c["name"], type(e).__name__, e))
            warn("%s not downloaded: %s: %s" % (c["name"], type(e).__name__, e))
            continue
        used += size
        tapes = [f for f in files if f.endswith((".jsonl", ".jsonl.gz"))]
        hrs = sorted({os.path.basename(f)[:13] for f in tapes})
        row = {"name": c["name"], "run_id": c["run_id"], "attempt": c["attempt"], "ended": iso(c["ended"]),
               "size": c["size"], "unzipped": size, "dir": os.path.join(out, c["dir"]), "files": len(files),
               "tape_files": len(tapes),
               "hours": [hrs[0], hrs[-1]] if len(hrs) > 1 else hrs,          # the first and the last UTC hour
               "run_started": run.get("run_started_at"), "conclusion": run.get("conclusion")}
        man["artifacts"].append(row)
        say("  %s: job ended %s, %s zipped (%s unzipped), %d tape files (%s) -> %s" % (
            c["name"], row["ended"], _mb(c["size"]), _mb(size), len(tapes), " → ".join(row["hours"]) or "none",
            row["dir"]))
    man["artifacts"].sort(key=lambda a: (a["ended"], a["name"]))     # oldest first, as listed
    for k in ("expired", "other_workflow", "not_the_bot", "over_cap"):
        man["skipped"][k].reverse()
    man["count"] = len(man["artifacts"])
    man["runs"] = len({a["run_id"] for a in man["artifacts"]})
    man["size"] = sum(a["size"] for a in man["artifacts"])
    man["unzipped"] = used
    man["api_calls"] = api.calls
    sk = man["skipped"]
    if man["count"]:
        say("tapes: %d artifacts from %d runs (%s zipped, %s unzipped), jobs ended %s → %s%s" % (
            man["count"], man["runs"], _mb(man["size"]), _mb(used), man["artifacts"][0]["ended"],
            man["artifacts"][-1]["ended"], skipped_text(sk)))
    else:
        say(none_found_text(man))
    return man


def skipped_text(sk):
    parts = []
    if sk["expired"]:
        parts.append("%d expired (GitHub keeps them 14 days): %s" % (len(sk["expired"]), ", ".join(sk["expired"][:10])))
    if sk["other_workflow"]:
        parts.append("%d not from the bot's workflow: %s" % (len(sk["other_workflow"]),
                                                              ", ".join(sk["other_workflow"][:10])))
    if sk.get("not_the_bot"):
        parts.append("%d not the live bot's runs (only its schedule and workflow_dispatch runs on its branch): %s" % (
            len(sk["not_the_bot"]), "; ".join(sk["not_the_bot"][:5])))
    if sk.get("over_cap"):
        parts.append("%d older ones over the size cap (a backtest holds the whole tape in memory): %s" % (
            len(sk["over_cap"]), ", ".join(sk["over_cap"][:10])))
    if sk["failed"]:
        parts.append("%d failed to download: %s" % (len(sk["failed"]), "; ".join(sk["failed"][:5])))
    return (" · skipped " + " · ".join(parts)) if parts else ""


def none_found_text(man):
    return ("no tapes to backtest: no artifact tapes-<run id>-<attempt> of %s whose job ended after %s (the last %g h; "
            "%d artifacts in the repository, %d of them older tapes)%s. A job uploads its tapes only when it ends "
            "(about every 5 h 40 min), so the job running now is not here yet. %s" % (
                man["workflow"], man["cutoff"], man["hours"], man["listed"]["listed"], man["listed"]["older"],
                skipped_text(man["skipped"]),
                "Every download failed: this run fails." if man["skipped"]["failed"] else
                "Nothing to replay: this run ends here, green."))


def fetch_markdown(man):
    out = ["## The tapes", ""]
    if not man["count"]:
        out += ["**%s**" % _md(none_found_text(man)), ""]
        return "\n".join(out) + "\n"
    out.append("%d artifacts from %d runs of `%s` (%s zipped, %s unzipped), jobs that ended after %s (the last %g "
               "h)%s." % (man["count"], man["runs"], man["workflow"], _mb(man["size"]), _mb(man.get("unzipped") or 0),
                          man["cutoff"], man["hours"], _md(skipped_text(man["skipped"]))))
    out += ["", "| artifact | job ended (UTC) | conclusion | tape hours (UTC) | files | zipped |",
            "|---|---|---|---|---|---|"]
    for a in man["artifacts"]:
        out.append("| %s | %s | %s | %s | %d | %s |" % (a["name"], a["ended"], a.get("conclusion") or "-",
                                                      " → ".join(a["hours"]) or "-", a["tape_files"], _mb(a["size"])))
    return "\n".join(out) + "\n\n"


# ---------------------------------------------------------------- the report for people

def _num(x):
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _md(s, most=None):
    s = str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")
    return s if most is None or len(s) <= most else s[:most - 1] + "…"


def _pf(x):
    if x in ("inf", float("inf")):
        return "∞"
    v = _num(x)
    return "-" if v is None else "%.2f" % v


def _win(m):
    v = _num(m.get("win_rate"))
    return "-" if v is None else "%.0f %%" % (v * 100)


def first_text(m):
    """A config's first part (the ranking): trades, win rate, total $, mean % and the sum of % it is ranked by."""
    if not m:
        return "no result"
    return "%d trades · win %s · %+.2f $ · mean %+.2f %% · Σ %+.1f %%" % (
        _num(m.get("trades")) or 0, _win(m), _num(m.get("total_pnl")) or 0, _num(m.get("mean_pnl_pct")) or 0,
        _num(m.get("sum_pnl_pct")) or 0)


def held_text(m):
    """A config's held-out part: trades, win rate, total $, mean %, max drawdown, profit factor, blind exits."""
    if not m:
        return "no result"
    return "%d trades · win %s · %+.2f $ · mean %+.2f %% · DD %.2f $ · PF %s%s" % (
        _num(m.get("trades")) or 0, _win(m), _num(m.get("total_pnl")) or 0, _num(m.get("mean_pnl_pct")) or 0,
        _num(m.get("max_drawdown")) or 0, _pf(m.get("profit_factor")),
        " · %d blind" % m["blind"] if _num(m.get("blind")) else "")


def unjudged_text(c):
    """A config's held-out coins the tape cannot judge for it, by cause ("" when none): left out of its comparison
    with the base (backtest.py)."""
    u = (c or {}).get("unjudged") or {}
    if not _num(u.get("held_out")):
        return ""
    by = u.get("held_out_by_cause") or {}
    parts = ["%d %s" % (by[k], w) for k, w in (("stale", "waiting for holders, no fresher report"),
                                                ("missing", "no RugCheck report"), ("unseen", "no readings"))
             if _num(by.get(k))]
    return " · %d coin%s it cannot judge%s" % (u["held_out"], "" if u["held_out"] == 1 else "s",
                                                " (%s)" % ", ".join(parts) if parts else "")


def vs_text(v, need):
    if not v:
        return "-"
    return "%+.2f %% of an order a coin over %d coins · t %.2f%s" % (
        _num(v.get("mean")) or 0, _num(v.get("coins")) or 0, _num(v.get("t")) or 0,
        " (needs %.2f)" % need if _num(need) is not None else "")


def gaps_lines(tp):
    """The tape's gaps, in words ([] when it recorded everything it says it did)."""
    out = []
    if _num(tp.get("lost_lines")):
        out.append("%d lines the recorder lost (hours %s)" % (
            tp["lost_lines"], ", ".join(tp.get("lost_hours") or []) or "-"))
    if _num(tp.get("capped_coins")):
        out.append("%d coins' readings left out at a full hour's cap (hours %s)" % (
            tp["capped_coins"], ", ".join(tp.get("capped_hours") or []) or "-"))
    if _num(tp.get("unknown_s")):
        out.append("%.0f s unknown for every coin (exits there are counted blind)" % tp["unknown_s"])
    if _num(tp.get("bad_lines")):
        out.append("%d lines that were not JSON" % tp["bad_lines"])
    if tp.get("unknown_kinds"):
        out.append("unknown kinds ignored: %s" % json.dumps(tp["unknown_kinds"], sort_keys=True))
    if _num(tp.get("rugcheck_failed_asks")):
        out.append("%d RugCheck asks that failed live" % tp["rugcheck_failed_asks"])
    if _num(tp.get("rug_lines_old")):
        out.append("%d RugCheck lines recorded before the tape counted the wallets holding a coin (their holder rules "
                   "judged on the wallets their top lists: a lower bound)" % tp["rug_lines_old"])
    return out


def summary_markdown(rep):
    """report.json -> a Markdown page (the decision first, then the configs, the live check, the tape)."""
    d = rep.get("decision") or {}
    act = str(d.get("action") or "keep").upper()
    st = rep.get("settings") or {}
    out = ["## Backtest: %s" % act, ""]
    if act == "CHANGE":
        out += ["**CHANGE (a hint, not a promise)**: %s" % _md(d.get("why")), "",
                "The workflow changes nothing: the config.json keys below are for a person to judge and apply.", "",
                "```json", json.dumps(d.get("config_diff"), indent=1, sort_keys=True, ensure_ascii=False), "```", ""]
    else:
        out += ["**KEEP** config.json as it is: %s" % _md(d.get("why")), ""]
    tp = rep.get("tape") or {}
    rg = rep.get("range") or {}
    out.append("**Tape** %s → %s (%s h, %s files, runs recorded: %s): %s coins, %s with readings, %s with "
               "RugCheck." % (
        tp.get("from", "-"), tp.get("to", "-"), tp.get("hours", "-"), tp.get("files", "-"), len(tp.get("runs") or []),
        tp.get("coins", "-"), tp.get("coins_with_readings", "-"), tp.get("coins_with_rugcheck", "-")))
    if rg:
        out.append("**Walk-forward**: ranked on the first %s h, judged on the held-out %s h after %s (split %s)." % (
            rg.get("first_part_h", "-"), rg.get("held_out_h", "-"), rg.get("split_at", "-"), st.get("split", "-")))
    gaps = gaps_lines(tp)
    out.append("**Tape gaps**: %s." % ("; ".join(gaps) if gaps else "none recorded"))
    out.append("")
    base = rep.get("base") or {}
    top = rep.get("top") or []
    need = st.get("t_needed")
    if base or top:
        out += ["### The top configs (ranked on the first part, judged on the held-out part)", "",
                "| | config | first part | held-out | vs the base on the same coins | verdict |",
                "|---|---|---|---|---|---|"]
        if base:
            out.append("| base | config.json as it is | %s | %s | | %s |" % (
                first_text(base.get("first_part")), held_text(base.get("held_out")) + unjudged_text(base),
                _md(base.get("error")) if base.get("error") else ""))
        for c in top:
            verdict = "**passes**" if c.get("passes") else "no: " + "; ".join(c.get("why_not") or [])
            out.append("| #%s | %s | %s | %s | %s | %s |" % (
                c.get("rank"), _md(c.get("text") or c.get("levers"), 160), first_text(c.get("first_part")),
                held_text(c.get("held_out")) + unjudged_text(c), vs_text(c.get("vs_base"), need), _md(verdict, 400)))
        out.append("")
        out.append("A change needs, on the held-out part: at least %s trades, a mean above 0 after costs, a paired t "
                   "of %s or more against the base on the same coins, %s $ more than the base in all, at most 10 %% "
                   "blind exits, at most 10 %% as many coins the tape cannot judge for it beyond the base's as it has "
                   "trades, and at most 10 %% of its trades on coins the tape cannot judge for the base. The coins "
                   "either cannot judge are left out of the comparison on the same coins (a result there is unknown, "
                   "not 0 %%)." % (st.get("min_trades", "-"), need if need is not None else "-",
                                   "%.2f" % st["margin_usd"] if _num(st.get("margin_usd")) is not None else "-"))
        out.append("")
    risk = rep.get("risk_levers") or []
    if risk:
        out += ["<details><summary>Risk choices: the same rules in other dollars (first part; never a "
                "recommendation)</summary>", ""]
        out += ["- %s: %s" % (_md(c.get("text")), first_text(c.get("first_part"))) for c in risk[:10]]
        out += ["", "</details>", ""]
    rk = rep.get("ranking") or []
    if rk:
        out += ["<details><summary>The first part's ranking (top %d)</summary>" % min(20, len(rk)), "",
                "| rank | config | first part |", "|---|---|---|"]
        out += ["| %s | %s | %s |" % (r.get("rank"), _md(json.dumps(r.get("levers"), sort_keys=True), 160),
                                      first_text(r.get("first_part"))) for r in rk[:20]]
        out += ["", "</details>", ""]
    live = rep.get("live") or {}
    lc = rep.get("live_check")
    out.append("### The live bot and the replay")
    out.append("")
    if live.get("trades"):
        out.append("Live bot, same range: %s; the base config's replay: %s trades." % (
            held_text(live["trades"]), live.get("replay_base_trades", "-")))
    else:
        out.append("No live trade on the tape in this range.")
    if live.get("live_config_differs"):
        out.append("The live run's settings differ from config.json: `%s`." % _md(json.dumps(
            live["live_config_differs"], sort_keys=True), 600))
    check = next((x for x in rep.get("lines") or [] if str(x).startswith("live check:")), None)
    if check:                     # backtest.py's own words (replay.live_check_text): the counts and where to look
        out += ["", "> %s" % _md(check)]
    elif lc:
        out.append("")
        out.append("Live check (the live run replayed with its own settings, trade by trade): %s live trades, %s "
                   "replayed, %s with the same entry, %s with the same exit; P&L gap a trade (replay - live) mean %s "
                   "%%, worst %s %%; live %s $ against replay %s $." % (
                       lc.get("live"), lc.get("replay"), lc.get("same_entry"), lc.get("same_exit"),
                       lc.get("pnl_pct_gap_mean"), lc.get("pnl_pct_gap_max"), lc.get("live_pnl"), lc.get("replay_pnl")))
    gr = rep.get("grid") or {}
    out += ["", "**Grid**: %s configs, %s replayed, %s ranked, %s with too few trades, %d failed · %s s." % (
        gr.get("configs", "-"), gr.get("replayed", "-"), gr.get("ranked", "-"), gr.get("thin", "-"),
        len(gr.get("errors") or {}), rep.get("wall_s", "-"))]
    cost = (rep.get("costs") or {}).get("example")
    if cost:
        out.append("**Costs**: %s." % _md(cost))
    lines = rep.get("lines") or []
    if lines:
        out += ["", "<details><summary>The report's own lines</summary>", "", "```text"]
        out += [str(x).replace("```", "'''") for x in lines]
        out += ["```", "", "</details>"]
    if rep.get("assumptions"):
        out += ["", "<details><summary>Assumptions and honest limits</summary>", ""]
        out += ["- %s" % _md(a) for a in rep["assumptions"]]
        out += ["", "</details>"]
    out += ["", "_Paper on paper: the bot's own engine replayed on what the live bot recorded. A pass on the "
                "held-out part is a hint, not a promise. Not financial advice._", ""]
    return "\n".join(out) + "\n"


def summary(path, md=None, say=print):
    """report.json -> its lines into the log, its Markdown page appended to md. -> 0 (1 when it cannot be read)."""
    try:
        with open(path, encoding="utf-8") as fh:
            rep = json.load(fh)
    except (OSError, ValueError) as e:
        msg = "no backtest report (%s: %s): see the backtest step's log" % (path, e)
        say(msg)
        if md:
            _append(md, "## Backtest\n\n**%s**\n" % _md(msg))
        return 1
    say("backtest report (%s):" % path)
    for line in rep.get("lines") or []:
        say("  " + str(line))
    if md:
        _append(md, summary_markdown(rep))
    return 0


def _append(path, text):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


# ---------------------------------------------------------------- command line

def main(argv=None, transport=None, now=None, say=print, sleep=time.sleep):
    ap = argparse.ArgumentParser(description="Meme Bot: the tapes from GitHub Actions for the backtester, and its "
                                             "report in Markdown (read-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download the tapes-* artifacts of the bot's workflow runs")
    f.add_argument("--hours", default="24", help="the jobs that ended in the last N hours (default 24)")
    f.add_argument("--out", default="tapes", help="one subfolder <run id>-<attempt> per artifact (default tapes)")
    f.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"),
                   help="OWNER/NAME (default $GITHUB_REPOSITORY)")
    f.add_argument("--workflow", default="memebot.yml", help="the bot's workflow file (default memebot.yml)")
    f.add_argument("--max-mb", default=str(MAX_MB), help="unzipped tape at most, the newest first (default %g; 0: "
                                                         "no cap)" % MAX_MB)
    f.add_argument("--branch", help="the runs' branch (default: the repository's default branch; *: any)")
    f.add_argument("--api", default=os.environ.get("GITHUB_API_URL") or API, help="the REST API's base URL")
    f.add_argument("--manifest", help="write what was downloaded and skipped here (JSON)")
    f.add_argument("--md", help="append a Markdown section here (e.g. $GITHUB_STEP_SUMMARY)")
    f.add_argument("--github-output", help="append count=N here (e.g. $GITHUB_OUTPUT)")
    s = sub.add_parser("summary", help="report.json into the log and Markdown")
    s.add_argument("report")
    s.add_argument("--md", help="append the Markdown page here (e.g. $GITHUB_STEP_SUMMARY)")
    a = ap.parse_args(argv)
    if a.cmd == "summary":
        summary(a.report, a.md, say)
        return 0                  # a missing report is the backtest step's failure, already red; said here too
    try:
        hours = float(str(a.hours).strip() or "24")
    except ValueError:
        ap.error("--hours: a number of hours, e.g. 24 (got %r)" % a.hours)
    if not (hours > 0 and math.isfinite(hours)):
        ap.error("--hours must be above 0 (got %r)" % a.hours)
    try:
        max_mb = float(str(a.max_mb).strip() or MAX_MB)
    except ValueError:
        ap.error("--max-mb: a number of MB, e.g. 500 (got %r)" % a.max_mb)
    if not (max_mb >= 0 and math.isfinite(max_mb)):
        ap.error("--max-mb must be 0 or more (got %r)" % a.max_mb)
    if not a.repo or not re.match(r"^[\w.-]+/[\w.-]+$", a.repo):
        ap.error("--repo OWNER/NAME (or $GITHUB_REPOSITORY) is needed (got %r)" % a.repo)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        ap.error("no token: set GH_TOKEN (in the workflow: ${{ github.token }} with permissions actions: read)")
    gha = os.environ.get("GITHUB_ACTIONS") == "true"

    def warn(msg):
        say(("::warning::" if gha else "warning: ") + msg)

    api = Api(token, a.api, transport, sleep=sleep)
    try:
        man = fetch(api, a.repo, a.out, hours, a.workflow, now=now, say=say, warn=warn, max_mb=max_mb or None,
                    branch=a.branch)
    except ApiError as e:
        msg = ("the tapes could not be listed: %s. The job's token needs the permission actions: read "
               "(permissions: in .github/workflows/backtest.yml)" % e)
        say(("::error::" if gha else "error: ") + msg)
        if a.md:
            _append(a.md, "## The tapes\n\n**%s**\n\n" % _md(msg))
        return 1
    status = 0
    if not man["count"] and man["skipped"]["failed"]:
        say(("::error::" if gha else "error: ") + "every tapes artifact found failed to download: %s" % "; ".join(
            man["skipped"]["failed"][:5]))
        status = 1
    elif not man["count"] and gha:
        say("::notice::no tapes in the last %g h: nothing to backtest" % hours)
    if a.manifest:
        with open(a.manifest, "w", encoding="utf-8") as fh:
            json.dump(man, fh, indent=1, ensure_ascii=False)
    if a.md:
        _append(a.md, fetch_markdown(man))
    if a.github_output:
        _append(a.github_output, "count=%d\n" % man["count"])
    return status


if __name__ == "__main__":
    sys.exit(main())
