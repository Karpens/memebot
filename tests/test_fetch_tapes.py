"""memebot/fetch_tapes.py and .github/workflows/backtest.yml: the tapes-<run id>-<attempt> artifacts of the bot's
workflow found through a FAKE GitHub REST API (made-up JSON: pagination, names, dates, expired artifacts, other
workflows, redirects to a storage host, failures), unzipped where memebot/backtest.py --data reads them, and the
report's summary page. Offline: nothing here talks to GitHub."""

import io
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock
import urllib.parse
import zipfile

from _load import ROOT, load

os.environ["MEMEBOT_LIVE"] = "0"
os.environ["MEMEBOT_FEEDS"] = "0"
ft = load("memebot", "fetch_tapes")

API = "https://api.github.com"
REPO = "owner/memebot"
NOW = ft.parse_ts("2026-11-26T12:00:00Z")
WF_ID, OTHER_WF = 1001, 2002


def stamp(hours_ago):
    return ft.iso(NOW - hours_ago * 3600)


def zip_of(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def artifact(aid, name, hours_ago, run_id=None, expired=False, size=1000):
    return {"id": aid, "name": name, "size_in_bytes": size, "expired": expired,
            "created_at": stamp(hours_ago) if hours_ago is not None else None,
            "updated_at": stamp(hours_ago) if hours_ago is not None else None,
            "archive_download_url": "%s/repos/%s/actions/artifacts/%d/zip" % (API, REPO, aid),
            "workflow_run": {"id": run_id} if run_id else {}}


class FakeGitHub:
    """GitHub's REST API, made up: the repository's artifact list in pages (Link headers, or bare page numbers),
    the bot's workflow, its runs, and each artifact's zip behind a redirect to a storage host."""

    def __init__(self, artifacts, runs, zips, link=True, per_page=None, fail=None, gone=(), list_status=200):
        self.artifacts, self.runs, self.zips = artifacts, runs, zips
        self.link, self.per_page, self.fail, self.gone = link, per_page, dict(fail or {}), set(gone)
        self.list_status = list_status
        self.log = []                    # (url, headers)

    def __call__(self, url, headers):
        self.log.append((url, dict(headers)))
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.netloc == "blob.example.net":
            if "Authorization" in headers:
                return 400, {}, b"two auth methods"            # the storage host refuses the API's token
            aid = int(u.path.rsplit("/", 1)[-1])
            if self.fail.get(aid):
                self.fail[aid] -= 1
                return 503, {}, b"busy"
            return 200, {"content-type": "application/zip"}, self.zips[aid]
        assert u.netloc == "api.github.com", url
        assert headers.get("Authorization") == "Bearer tok", headers
        path = u.path
        if path == "/repos/%s" % REPO:
            return 200, {}, json.dumps({"full_name": REPO, "default_branch": "main"}).encode()
        if path == "/repos/%s/actions/workflows/memebot.yml" % REPO:
            return 200, {}, json.dumps({"id": WF_ID, "path": ".github/workflows/memebot.yml"}).encode()
        if path == "/repos/%s/actions/artifacts" % REPO:
            if self.list_status != 200:
                return self.list_status, {}, json.dumps({"message": "Resource not accessible by integration"}).encode()
            per = self.per_page or int(q.get("per_page", 30))
            page = int(q.get("page", 1))
            items = self.artifacts[(page - 1) * per: page * per]
            hd = {}
            if self.link and page * per < len(self.artifacts):
                nxt = "%s%s?per_page=%d&page=%d" % (API, path, per, page + 1)
                hd["link"] = '<%s>; rel="next", <%s%s?per_page=%d&page=9>; rel="last"' % (nxt, API, path, per)
            return 200, hd, json.dumps({"total_count": len(self.artifacts), "artifacts": items}).encode()
        if path.startswith("/repos/%s/actions/runs/" % REPO):
            rid = int(path.rsplit("/", 1)[-1])
            if rid not in self.runs:
                return 404, {}, b'{"message": "Not Found"}'
            return 200, {}, json.dumps(self.runs[rid]).encode()
        if path.startswith("/repos/%s/actions/artifacts/" % REPO) and path.endswith("/zip"):
            aid = int(path.split("/")[-2])
            if aid in self.gone:
                return 410, {}, b'{"message": "Artifact has expired"}'
            return 302, {"location": "https://blob.example.net/x/%d?sig=SECRET" % aid}, b""
        return 404, {}, b'{"message": "Not Found"}'


def run(rid, wf=WF_ID, path=".github/workflows/memebot.yml@refs/heads/main", conclusion="success", repo=REPO,
        event="workflow_dispatch", branch="main"):
    """A run as GitHub's API shows it (the fields fetch_tapes reads): the bot's chain dispatches its next job
    (workflow_dispatch), the hourly net is a schedule run, both of this repository on its default branch."""
    return {"id": rid, "workflow_id": wf, "path": path, "conclusion": conclusion, "run_started_at": stamp(8),
            "event": event, "head_branch": branch, "head_repository": {"full_name": repo}}


TAPE = '{"k":"start","t":1795680000,"v":2,"cfg":{}}\n{"k":"hb","t":1795680001,"sol":210.0}\n'


class Pages(unittest.TestCase):
    def test_every_page_by_the_link_header(self):
        arts = [artifact(i, "other-%d" % i, 1) for i in range(1, 251)]
        gh = FakeGitHub(arts, {}, {})
        api = ft.Api("tok", API, gh, sleep=lambda s: None)
        got = list(api.pages("/repos/%s/actions/artifacts" % REPO, "artifacts"))
        self.assertEqual([a["id"] for a in got], list(range(1, 251)))
        self.assertEqual(len(gh.log), 3)
        self.assertIn("per_page=100", gh.log[0][0])

    def test_page_numbers_when_there_is_no_link_header(self):
        arts = [artifact(i, "other-%d" % i, 1) for i in range(1, 201)]          # exactly two full pages
        gh = FakeGitHub(arts, {}, {}, link=False)
        api = ft.Api("tok", API, gh, sleep=lambda s: None)
        got = list(api.pages("/repos/%s/actions/artifacts" % REPO, "artifacts"))
        self.assertEqual(len(got), 200)
        self.assertEqual(len(gh.log), 2)          # total_count says nothing is left: no empty third page asked

    def test_a_page_cap_warns_and_stops(self):
        arts = [artifact(i, "other-%d" % i, 1) for i in range(1, 251)]
        gh = FakeGitHub(arts, {}, {})
        said = []
        api = ft.Api("tok", API, gh, sleep=lambda s: None)
        got = list(api.pages("/repos/%s/actions/artifacts" % REPO, "artifacts", max_pages=2, warn=said.append))
        self.assertEqual(len(got), 200)
        self.assertTrue(said and "stopped after 2 pages" in said[0])

    def test_next_link(self):
        self.assertEqual(ft.next_link('<https://a/x?page=2>; rel="next", <https://a/x?page=5>; rel="last"'),
                         "https://a/x?page=2")
        self.assertEqual(ft.next_link('<https://a/x?page=1>; rel="prev", <https://a/x?page=5>; rel="last"'), None)
        self.assertIsNone(ft.next_link(None))


class Names(unittest.TestCase):
    def test_names_dates_and_duplicates(self):
        cutoff = NOW - 24 * 3600
        arts = [artifact(1, "tapes-500-1", 2, run_id=500),
                artifact(2, "tapes-501-2", 23.9),                 # no workflow_run: the run id from the name
                artifact(3, "tapes-502-1", 24.1),                 # ended before the window
                artifact(4, "tapes-503-1", 24.0),                 # exactly at the cutoff: in
                artifact(5, "backtest-9", 1), artifact(6, "tapes-x-1", 1), artifact(7, "tapes-504", 1),
                artifact(8, "tapes-505-1-extra", 1), artifact(9, "pagecheck", 1),
                artifact(10, "tapes-506-1", None),               # no date at all: not judged in the window
                artifact(11, "tapes-507-1", 3, expired=True),
                artifact(12, "tapes-508-1", 5, expired=True), artifact(13, "tapes-508-1", 4)]  # the live copy wins
        got, n = ft.candidates(arts, cutoff)
        self.assertEqual([c["name"] for c in got], ["tapes-503-1", "tapes-501-2", "tapes-508-1", "tapes-507-1",
                                                    "tapes-500-1"])                       # oldest job end first
        by = {c["name"]: c for c in got}
        self.assertEqual((by["tapes-501-2"]["run_id"], by["tapes-501-2"]["attempt"], by["tapes-501-2"]["dir"]),
                         (501, 2, "501-2"))
        self.assertEqual(by["tapes-508-1"]["id"], 13)
        self.assertFalse(by["tapes-508-1"]["expired"])
        self.assertTrue(by["tapes-507-1"]["expired"])
        self.assertEqual(n, {"listed": 13, "other_names": 5, "older": 2})

    def test_github_times(self):
        self.assertEqual(ft.parse_ts("2026-11-26T12:00:00Z"), NOW)
        self.assertEqual(ft.parse_ts("2026-11-26T13:00:00+01:00"), NOW)
        self.assertEqual(ft.parse_ts("2026-11-26T12:00:00"), NOW)
        self.assertIsNone(ft.parse_ts("yesterday"))
        self.assertIsNone(ft.parse_ts(None))
        self.assertEqual(ft.iso(NOW), "2026-11-26T12:00:00Z")


class Fetch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.out = os.path.join(self.dir, "tapes")
        self.env = {k: os.environ.pop(k) for k in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_ACTIONS") if k in os.environ}
        os.environ["GH_TOKEN"] = "tok"
        self.addCleanup(self.restore)

    def restore(self):
        os.environ.pop("GH_TOKEN", None)
        os.environ.update(self.env)

    def world(self, **kw):
        arts = [artifact(i, "other-%d" % i, 1) for i in range(1, 121)]                  # filler: two pages
        arts += [artifact(201, "tapes-700-1", 30, run_id=700),                           # too old
                 artifact(202, "tapes-701-1", 20, run_id=701, size=2_500_000),
                 artifact(203, "tapes-702-2", 6, run_id=702),                            # a second attempt
                 artifact(204, "tapes-703-1", 3, run_id=703, expired=True),
                 artifact(205, "tapes-704-1", 2, run_id=704),                            # another workflow
                 artifact(206, "tapes-705-1", 1, run_id=705),                            # 410 at download
                 artifact(207, "backtest-706", 1, run_id=706)]
        runs = {700: run(700), 701: run(701), 702: run(702, conclusion="cancelled"), 703: run(703),
                704: run(704, wf=OTHER_WF, path=".github/workflows/probe.yml"), 705: run(705)}
        zips = {202: zip_of({"2026-11-25T16.jsonl": TAPE, "2026-11-25T17.jsonl": TAPE, "2026-11-25T18.jsonl": TAPE}),
                203: zip_of({"2026-11-26T05.jsonl": TAPE, "../../evil.jsonl": TAPE, "/abs.jsonl": TAPE,
                             "sub/2026-11-26T06.jsonl.gz": "x"})}
        return FakeGitHub(arts, runs, zips, gone={206}, **kw)

    def test_the_tapes_of_the_bots_jobs_that_ended_in_the_window(self):
        gh = self.world(fail={202: 1})            # the storage host is busy once: tried again
        said, md, gho, man = [], os.path.join(self.dir, "summary.md"), os.path.join(self.dir, "out.txt"), \
            os.path.join(self.dir, "fetched.json")
        rc = ft.main(["fetch", "--repo", REPO, "--hours", "24", "--out", self.out, "--manifest", man, "--md", md,
                      "--github-output", gho], transport=gh, now=NOW, say=said.append, sleep=lambda s: None)
        self.assertEqual(rc, 0, "\n".join(said))
        self.assertEqual(sorted(os.listdir(self.out)), ["701-1", "702-2"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.out, "701-1"))),
                         ["2026-11-25T16.jsonl", "2026-11-25T17.jsonl", "2026-11-25T18.jsonl"])
        # a zip member outside its folder is never written
        self.assertEqual(sorted(os.listdir(os.path.join(self.out, "702-2"))), ["2026-11-26T05.jsonl", "sub"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "evil.jsonl")))
        self.assertFalse(os.path.exists("/abs.jsonl"))
        with open(os.path.join(self.out, "701-1", "2026-11-25T16.jsonl")) as fh:
            self.assertEqual(fh.read(), TAPE)
        with open(gho) as fh:
            self.assertEqual(fh.read(), "count=2\n")
        with open(man) as fh:
            m = json.load(fh)
        self.assertEqual([a["name"] for a in m["artifacts"]], ["tapes-701-1", "tapes-702-2"])
        self.assertEqual(m["artifacts"][0]["hours"], ["2026-11-25T16", "2026-11-25T18"])
        self.assertEqual(m["artifacts"][1]["conclusion"], "cancelled")
        self.assertEqual(sorted(m["skipped"]["expired"]), ["tapes-703-1", "tapes-705-1"])
        self.assertEqual(m["skipped"]["other_workflow"], ["tapes-704-1"])
        self.assertEqual(m["skipped"]["failed"], [])
        self.assertEqual(m["listed"]["older"], 1)
        # the token went to the API only: never to the storage host, whose signed URL stays out of the log
        blob = [h for u, h in gh.log if "blob.example.net" in u]
        self.assertTrue(blob)
        self.assertTrue(all("Authorization" not in h for h in blob))
        self.assertNotIn("SECRET", "\n".join(said))
        with open(md) as fh:
            page = fh.read()
        self.assertIn("| tapes-701-1 | 2026-11-25T16:00:00Z |", page)
        self.assertIn("2 artifacts from 2 runs", page)
        self.assertIn("expired", page)
        # each run looked up once, the artifact list read on every page
        runs = [u for u, _ in gh.log if "/actions/runs/" in u]
        self.assertEqual(len(runs), len(set(runs)))
        self.assertEqual(sum(1 for u, _ in gh.log if u.split("?")[0].endswith("/actions/artifacts")), 2)

    def test_the_folders_read_as_one_tape_for_the_backtester(self):
        R = load("memebot", "replay")
        gh = self.world()
        ft.main(["fetch", "--repo", REPO, "--out", self.out], transport=gh, now=NOW, say=lambda s: None)
        tape = R.read_tape(self.out)
        self.assertEqual(len(tape.files), 5)           # 3 + 2 tape files (the .gz one is junk: one bad read)
        self.assertEqual(tape.n.get("start"), 4)

    def test_none_found_is_a_clear_message_and_a_success(self):
        gh = FakeGitHub([artifact(1, "tapes-1-1", 30, run_id=1), artifact(2, "pagecheck", 1)], {1: run(1)}, {})
        said, md, gho = [], os.path.join(self.dir, "s.md"), os.path.join(self.dir, "o.txt")
        rc = ft.main(["fetch", "--repo", REPO, "--hours", "6", "--out", self.out, "--md", md, "--github-output", gho],
                     transport=gh, now=NOW, say=said.append)
        self.assertEqual(rc, 0)
        self.assertIn("no tapes to backtest", said[-1])
        self.assertIn("the last 6 h", said[-1])
        self.assertIn("2 artifacts in the repository, 1 of them older tapes", said[-1])
        self.assertIn("this run ends here, green", said[-1])
        with open(gho) as fh:
            self.assertEqual(fh.read(), "count=0\n")
        with open(md) as fh:
            self.assertIn("no tapes to backtest", fh.read())
        self.assertEqual(os.listdir(self.out), [])

    def test_an_empty_repository_and_an_empty_hours_input(self):
        gh = FakeGitHub([], {}, {})
        said = []
        rc = ft.main(["fetch", "--repo", REPO, "--hours", "", "--out", self.out], transport=gh, now=NOW,
                     say=said.append)
        self.assertEqual(rc, 0)
        self.assertIn("the last 24 h", said[-1])

    def test_a_refused_list_fails_and_says_which_permission(self):
        gh = FakeGitHub([], {}, {}, list_status=403)
        said, md = [], os.path.join(self.dir, "s.md")
        rc = ft.main(["fetch", "--repo", REPO, "--out", self.out, "--md", md], transport=gh, now=NOW,
                     say=said.append)
        self.assertEqual(rc, 1)
        self.assertIn("actions: read", said[-1])
        self.assertIn("HTTP 403", said[-1])

    def test_every_download_failing_is_a_failure_not_none_found(self):
        gh = FakeGitHub([artifact(1, "tapes-1-1", 2, run_id=1)], {1: run(1)}, {1: b""}, fail={1: 99})
        said = []
        api = ft.Api("tok", API, gh, sleep=lambda s: None)
        man = ft.fetch(api, REPO, self.out, 24, now=NOW, say=said.append)
        self.assertEqual(man["count"], 0)
        self.assertEqual(len(man["skipped"]["failed"]), 1)
        self.assertIn("HTTP 503", man["skipped"]["failed"][0])
        self.assertIn("Every download failed: this run fails.", said[-1])
        self.assertNotIn("green", said[-1])
        self.assertEqual(sum(1 for u, _ in gh.log if "blob.example.net" in u), ft.TRIES)      # tried 3 times
        gh.fail[1] = 99
        said, gho = [], os.path.join(self.dir, "o.txt")
        self.assertEqual(ft.main(["fetch", "--repo", REPO, "--out", self.out, "--github-output", gho], transport=gh,
                                 now=NOW, say=said.append, sleep=lambda s: None), 1)
        self.assertTrue(said[-1].startswith("error: every tapes artifact found failed to download"))
        with open(gho) as fh:
            self.assertEqual(fh.read(), "count=0\n")

    def test_a_broken_zip_is_skipped_and_the_rest_downloaded(self):
        gh = FakeGitHub([artifact(1, "tapes-1-1", 2, run_id=1), artifact(2, "tapes-2-1", 1, run_id=2)],
                        {1: run(1), 2: run(2)}, {1: b"not a zip", 2: zip_of({"2026-11-26T11.jsonl": TAPE})})
        api = ft.Api("tok", API, gh, sleep=lambda s: None)
        man = ft.fetch(api, REPO, self.out, 24, now=NOW, say=lambda s: None, warn=lambda s: None)
        self.assertEqual([a["name"] for a in man["artifacts"]], ["tapes-2-1"])
        self.assertIn("tapes-1-1", man["skipped"]["failed"][0])
        self.assertEqual(os.listdir(self.out), ["2-1"])          # no empty folder for the broken one

    def test_a_run_matched_by_its_path_when_the_workflow_lookup_404s(self):
        class NoWorkflow(FakeGitHub):
            def __call__(self, url, headers):
                if url.endswith("/actions/workflows/memebot.yml"):
                    self.log.append((url, headers))
                    return 404, {}, b'{"message": "Not Found"}'
                return super().__call__(url, headers)
        gh = NoWorkflow([artifact(1, "tapes-1-1", 2, run_id=1), artifact(2, "tapes-2-1", 1, run_id=2)],
                        {1: run(1, wf=None), 2: run(2, wf=None, path=".github/workflows/probe.yml")},
                        {1: zip_of({"2026-11-26T10.jsonl": TAPE})})
        api = ft.Api("tok", API, gh, sleep=lambda s: None)
        man = ft.fetch(api, REPO, self.out, 24, now=NOW, say=lambda s: None, warn=lambda s: None)
        self.assertEqual([a["name"] for a in man["artifacts"]], ["tapes-1-1"])
        self.assertEqual(man["skipped"]["other_workflow"], ["tapes-2-1"])

    def test_only_the_live_bots_own_runs(self):
        """A public repository runs a pull request's own copy of memebot.yml under the same workflow id (a fork's
        too, once approved): its artifact named tapes-<run>-<attempt> is made-up data, never the bot's tape. Only
        schedule and workflow_dispatch runs of this repository on its default branch count; the rest is listed."""
        arts = [artifact(i, "tapes-%d-1" % (900 + i), 10 - i, run_id=900 + i) for i in range(1, 7)]
        runs = {901: run(901, event="pull_request", branch="evil", repo="mallory/memebot"),     # a fork's PR
                902: run(902, event="pull_request", branch="feature"),                          # a PR in the repo
                903: run(903, event="workflow_dispatch", branch="experiment"),                  # another branch
                904: run(904, event="push"),
                905: run(905, event="schedule"),                                                # the hourly net
                906: run(906)}                                                                  # the chain
        zips = {i: zip_of({"2026-11-26T%02d.jsonl" % i: TAPE}) for i in range(1, 7)}
        gh = FakeGitHub(arts, runs, zips)
        said = []
        man = ft.fetch(ft.Api("tok", API, gh, sleep=lambda s: None), REPO, self.out, 24, now=NOW, say=said.append)
        self.assertEqual([a["name"] for a in man["artifacts"]], ["tapes-905-1", "tapes-906-1"])
        self.assertEqual(man["branch"], "main")
        self.assertEqual(man["skipped"]["not_the_bot"], [
            "tapes-901-1: a pull_request run", "tapes-902-1: a pull_request run",
            "tapes-903-1: a run on the branch experiment (the bot's is main)", "tapes-904-1: a push run"])
        self.assertEqual(sorted(os.listdir(self.out)), ["905-1", "906-1"])
        self.assertFalse([u for u, _ in gh.log if "/artifacts/1/zip" in u or "/artifacts/2/zip" in u])  # never fetched
        self.assertTrue(any("4 not the live bot's runs" in x for x in said), said)
        # a dispatch of the same repository from a fork's name is no different: the head repository must be this one
        gh = FakeGitHub([artifact(1, "tapes-1-1", 2, run_id=1)], {1: run(1, repo="mallory/memebot")},
                        {1: zip_of({"2026-11-26T10.jsonl": TAPE})})
        man = ft.fetch(ft.Api("tok", API, gh, sleep=lambda s: None), REPO, self.out, 24, now=NOW, say=lambda s: None)
        self.assertEqual((man["count"], man["skipped"]["not_the_bot"]), (0, ["tapes-1-1: a run from mallory/memebot"]))
        # --branch "*": any branch of this repository (still never a pull request's run)
        gh = FakeGitHub(arts, runs, zips)
        man = ft.fetch(ft.Api("tok", API, gh, sleep=lambda s: None), REPO, self.out, 24, now=NOW, say=lambda s: None,
                       branch="*")
        self.assertEqual([a["name"] for a in man["artifacts"]], ["tapes-903-1", "tapes-905-1", "tapes-906-1"])

    def test_the_newest_tapes_up_to_the_size_cap(self):
        """backtest.py holds the whole tape in memory (about 23x its size with 4 workers): --max-mb keeps the newest
        artifacts until the next one would pass the cap, and leaves that one and every older one out (said, listed),
        so the tapes are the latest contiguous hours."""
        big = "x" * 400_000                                          # 0.4 MB unzipped a tape file
        arts = [artifact(i, "tapes-%d-1" % (800 + i), 10 - i, run_id=800 + i) for i in range(1, 6)]
        zips = {i: zip_of({"2026-11-26T%02d.jsonl" % i: big}) for i in range(1, 6)}
        gh = FakeGitHub(arts, {800 + i: run(800 + i) for i in range(1, 6)}, zips)
        said = []
        man = ft.fetch(ft.Api("tok", API, gh, sleep=lambda s: None), REPO, self.out, 24, now=NOW, say=said.append,
                       max_mb=1.0)
        self.assertEqual([a["name"] for a in man["artifacts"]], ["tapes-804-1", "tapes-805-1"])     # 0.8 MB of 1
        self.assertEqual(man["skipped"]["over_cap"], ["tapes-801-1", "tapes-802-1", "tapes-803-1"])
        self.assertEqual(man["unzipped"], 800_000)
        self.assertEqual(sorted(os.listdir(self.out)), ["804-1", "805-1"])
        self.assertEqual(sum(1 for u, _ in gh.log if "blob.example.net" in u), 3)      # the older two never fetched
        self.assertTrue(any("tapes-803-1 and every older tape left out" in x for x in said), said)
        self.assertIn("3 older ones over the size cap", ft.skipped_text(man["skipped"]))
        # the workflow passes a cap; the CLI takes one (0: none) and refuses nonsense
        gho = os.path.join(self.dir, "o.txt")
        self.assertEqual(ft.main(["fetch", "--repo", REPO, "--out", self.out, "--max-mb", "0", "--github-output", gho],
                                 transport=FakeGitHub(arts, {800 + i: run(800 + i) for i in range(1, 6)}, zips),
                                 now=NOW, say=lambda s: None), 0)
        with open(gho) as fh:
            self.assertEqual(fh.read(), "count=5\n")
        with self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr", io.StringIO()):
            ft.main(["fetch", "--repo", REPO, "--max-mb", "-1"], transport=gh, now=NOW, say=lambda s: None)

    def test_any_failure_of_one_artifact_skips_it_and_the_rest_go_on(self):
        """A transfer cut off before its Content-Length (http.client.IncompleteRead: not an OSError) is tried again
        like a network error, then skipped; a damaged member (a CRC error while unzipping) is skipped too: one
        artifact never stops the others, and the count is still written."""
        import http.client
        good = zip_of({"2026-11-26T11.jsonl": TAPE})
        bad = bytearray(zip_of({"2026-11-26T09.jsonl": TAPE * 50}))
        i = bad.find(b"start")                                       # flip a byte of the deflated data: a CRC error
        bad[i if i > 0 else 60] ^= 0xFF
        gh = FakeGitHub([artifact(1, "tapes-1-1", 3, run_id=1), artifact(2, "tapes-2-1", 2, run_id=2),
                         artifact(3, "tapes-3-1", 1, run_id=3)], {1: run(1), 2: run(2), 3: run(3)},
                        {1: bytes(bad), 3: good})
        cut = [0]

        def transport(url, headers):
            if url.startswith("https://blob.example.net/x/2"):
                cut[0] += 1
                raise http.client.IncompleteRead(b"x" * 100, 999900)
            return gh(url, headers)
        said, gho = [], os.path.join(self.dir, "o.txt")
        rc = ft.main(["fetch", "--repo", REPO, "--out", self.out, "--github-output", gho], transport=transport,
                     now=NOW, say=said.append, sleep=lambda s: None)
        self.assertEqual(rc, 0, said)
        self.assertEqual(cut[0], ft.TRIES)                           # tried again, as a network error
        with open(gho) as fh:
            self.assertEqual(fh.read(), "count=1\n")
        self.assertEqual(os.listdir(self.out), ["3-1"])               # no half folder of the broken ones
        failed = "\n".join(said)
        self.assertIn("tapes-2-1 not downloaded", failed)
        self.assertIn("IncompleteRead", failed)
        self.assertIn("tapes-1-1 not downloaded", failed)

    def test_bad_arguments(self):
        for argv in (["fetch", "--repo", REPO, "--hours", "a day"], ["fetch", "--repo", REPO, "--hours", "-3"],
                     ["fetch", "--repo", "not a repo"], ["fetch", "--repo", REPO, "--max-mb", "lots"]):
            with self.assertRaises(SystemExit) as e, unittest.mock.patch("sys.stderr", io.StringIO()):
                ft.main(argv, transport=FakeGitHub([], {}, {}), now=NOW, say=lambda s: None)
            self.assertEqual(e.exception.code, 2)
        os.environ.pop("GH_TOKEN")
        with self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr", io.StringIO()) as err:
            ft.main(["fetch", "--repo", REPO], transport=FakeGitHub([], {}, {}), now=NOW, say=lambda s: None)
        self.assertIn("GH_TOKEN", err.getvalue())


class RealTransport(unittest.TestCase):
    """urllib_transport against a local HTTP server (127.0.0.1 plays the API, "localhost" the storage host): no
    redirect followed by urllib itself, the token never sent to the storage host, the Link header read."""

    def setUp(self):
        import http.server
        import threading
        zipped = zip_of({"2026-11-26T10.jsonl": TAPE})
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                host = self.headers.get("Host", "")
                auth = self.headers.get("Authorization")
                seen.append((host, self.path, auth))
                port = self.server.server_address[1]
                if self.path.startswith("/blob/"):
                    return self.reply(400 if auth else 200, b"auth" if auth else zipped)
                if auth != "Bearer tok":
                    return self.reply(401, b'{"message": "Bad credentials"}')
                p = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(p.query))
                if p.path == "/api/repos/o/r/actions/workflows/memebot.yml":
                    return self.reply(200, json.dumps({"id": 9, "path": ".github/workflows/memebot.yml"}).encode())
                if p.path == "/api/repos/o/r/actions/artifacts":
                    if q.get("page") == "2":
                        doc = {"total_count": 2, "artifacts": [artifact(2, "pagecheck", 1)]}
                        return self.reply(200, json.dumps(doc).encode())
                    doc = {"total_count": 2, "artifacts": [artifact(1, "tapes-77-1", 1, run_id=77)]}
                    nxt = "http://127.0.0.1:%d/api/repos/o/r/actions/artifacts?per_page=100&page=2" % port
                    return self.reply(200, json.dumps(doc).encode(), {"Link": '<%s>; rel="next"' % nxt})
                if p.path == "/api/repos/o/r/actions/runs/77":
                    return self.reply(200, json.dumps(run(77, wf=9, repo="o/r")).encode())
                if p.path == "/api/repos/o/r/actions/artifacts/1/zip":
                    return self.reply(302, b"", {"Location": "http://localhost:%d/blob/1?sig=SECRET" % port})
                return self.reply(404, b'{"message": "Not Found"}')

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_fetch_over_http(self):
        no_proxy = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        with unittest.mock.patch.dict(os.environ, no_proxy, clear=True):
            api = ft.Api("tok", "http://127.0.0.1:%d/api" % self.port, sleep=lambda s: None)
            # the artifacts' own URLs point at the fake API's host
            arts = list(api.pages("/repos/o/r/actions/artifacts", "artifacts"))
            self.assertEqual([a["name"] for a in arts], ["tapes-77-1", "pagecheck"])
            orig = api.pages

            def pages(path, key, **kw):
                for a in orig(path, key, **kw):
                    a["archive_download_url"] = "http://127.0.0.1:%d/api/repos/o/r/actions/artifacts/%d/zip" % (
                        self.port, a["id"])
                    yield a
            api.pages = pages
            out = os.path.join(self.dir, "tapes")
            man = ft.fetch(api, "o/r", out, 24, now=NOW, say=lambda s: None)
        self.assertEqual([a["name"] for a in man["artifacts"]], ["tapes-77-1"])
        with open(os.path.join(out, "77-1", "2026-11-26T10.jsonl")) as fh:
            self.assertEqual(fh.read(), TAPE)
        blob = [x for x in self.seen if x[1].startswith("/blob/")]
        self.assertEqual(len(blob), 1)
        self.assertTrue(blob[0][0].startswith("localhost:"))
        self.assertIsNone(blob[0][2])                               # no token to the storage host
        api_calls = [x for x in self.seen if x[1].startswith("/api/")]
        self.assertTrue(all(a == "Bearer tok" for _, _, a in api_calls))

    def test_an_http_error_comes_back_as_a_status(self):
        no_proxy = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        with unittest.mock.patch.dict(os.environ, no_proxy, clear=True):
            st, hd, body = ft.urllib_transport("http://127.0.0.1:%d/api/nothing" % self.port, {})
        self.assertEqual(st, 401)
        self.assertIn(b"Bad credentials", body)


class CutOffDownload(unittest.TestCase):
    """The real urllib transport against a storage host that declares 1 MB, sends 100 bytes and closes: the
    http.client.IncompleteRead it raises is tried again like any network error, then that artifact is skipped and
    the next one downloaded; main() writes count= and returns 0 (it once crashed with a traceback here)."""

    def test_a_cut_download_is_skipped_and_the_rest_fetched(self):
        import http.server
        import threading
        good = zip_of({"2026-11-26T10.jsonl": TAPE})
        blobs = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def json(self, doc, code=200):
                body = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                p = self.path.split("?")[0]
                base = "http://127.0.0.1:%d" % self.server.server_address[1]
                if p == "/repos/%s" % REPO:
                    return self.json({"default_branch": "main"})
                if p == "/repos/%s/actions/workflows/memebot.yml" % REPO:
                    return self.json({"id": 1, "path": ".github/workflows/memebot.yml"})
                if p == "/repos/%s/actions/artifacts" % REPO:
                    arts = [dict(artifact(i, "tapes-%d-1" % (100 + i), i, run_id=100 + i),
                                 archive_download_url=base + "/repos/%s/actions/artifacts/%d/zip" % (REPO, i))
                            for i in (1, 2)]
                    return self.json({"total_count": 2, "artifacts": arts})
                if p.startswith("/repos/%s/actions/runs/" % REPO):
                    return self.json(run(int(p.rsplit("/", 1)[1]), wf=1))
                if p.endswith("/zip"):
                    self.send_response(302)
                    self.send_header("Location", base + "/blob/" + p.split("/")[-2])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if p.startswith("/blob/"):
                    aid = int(p.rsplit("/", 1)[1])
                    blobs.append(aid)
                    self.send_response(200)
                    if aid == 1:                                  # the newest: cut off after 100 bytes
                        self.send_header("Content-Length", "1000000")
                        self.end_headers()
                        self.wfile.write(good[:100])
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    self.send_header("Content-Length", str(len(good)))
                    self.end_headers()
                    self.wfile.write(good)
                    return
                self.json({"message": "Not Found"}, 404)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        gho = os.path.join(d, "gho")
        no_proxy = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        no_proxy["GH_TOKEN"] = "tok"
        no_proxy.pop("GITHUB_ACTIONS", None)
        said = []
        with unittest.mock.patch.dict(os.environ, no_proxy, clear=True):
            rc = ft.main(["fetch", "--repo", REPO, "--api", "http://127.0.0.1:%d" % srv.server_address[1],
                          "--out", os.path.join(d, "tapes"), "--github-output", gho], now=NOW, say=said.append,
                         sleep=lambda s: None)
        self.assertEqual(rc, 0, said)
        with open(gho) as fh:
            self.assertEqual(fh.read(), "count=1\n")
        self.assertEqual(blobs.count(1), ft.TRIES)
        self.assertEqual(os.listdir(os.path.join(d, "tapes")), ["102-1"])
        self.assertTrue(any("tapes-101-1 not downloaded" in x and "IncompleteRead" in x for x in said), said)


def report(action="keep"):
    """A made-up report.json with the fields memebot/backtest.py writes (numbers invented for the test)."""
    m = lambda n, tot, mean, **kw: dict({"trades": n, "win_rate": 0.4, "total_pnl": tot, "mean_pnl_pct": mean,
                                         "sum_pnl_pct": mean * n, "max_drawdown": 3.2, "profit_factor": 1.1,
                                         "blind": 0}, **kw)
    rep = {"tape": {"from": "2026-11-25T12:00:00Z", "to": "2026-11-26T11:00:00Z", "hours": 23.0, "files": 24,
                    "coins": 21000, "coins_with_readings": 900, "coins_with_rugcheck": 300, "runs": [[1, 2], [3, 4]],
                    "lost_lines": 12, "lost_hours": ["2026-11-25T20"], "capped_coins": 0, "unknown_s": 30.0,
                    "bad_lines": 1, "unknown_kinds": {"x": 2}},
           "range": {"split_at": "2026-11-26T04:06:00Z", "first_part_h": 16.1, "held_out_h": 6.9},
           "settings": {"split": 0.7, "min_trades": 30, "margin_usd": 2.0, "t_needed": 2.58},
           "base": {"first_part": m(80, -4.0, -0.5), "held_out": m(31, -1.5, -0.4, profit_factor="inf")},
           "top": [{"rank": 1, "text": "np_min_holders=20|x", "levers": {"np_min_holders": 20},
                    "first_part": m(60, 3.0, 0.5), "held_out": m(31, 2.0, 0.6, blind=1),
                    "vs_base": {"coins": 40, "mean": 1.2, "t": 2.7}, "passes": action == "change",
                    "why_not": [] if action == "change" else ["only 12 held-out trades (needs 30)"]}],
           "risk_levers": [{"text": "position_pct=5.0", "first_part": m(80, -2.0, -0.5)}],
           "ranking": [{"rank": 1, "levers": {"np_min_holders": 20}, "first_part": m(60, 3.0, 0.5)}],
           "live": {"trades": m(11, -14.06, -9.0), "replay_base_trades": 111, "live_config_differs": {"max_open": 8}},
           "live_check": {"live": 11, "replay": 11, "same_entry": 10, "same_exit": 9, "pnl_pct_gap_mean": 0.3,
                          "pnl_pct_gap_max": -2.1, "live_pnl": -14.06, "replay_pnl": -13.5},
           "grid": {"configs": 120, "replayed": 120, "ranked": 70, "thin": 49, "errors": {}},
           "costs": {"example": "a 10 $ order at SOL 210 $: about 3.2 % each way"}, "wall_s": 1234.5,
           "assumptions": ["Paper on paper."],
           "lines": ["tape ...", "live check: 11 live trades, 11 replayed", "DECISION: KEEP · thin"]}
    if action == "change":
        rep["decision"] = {"action": "change", "why": "rank 1 ...", "config_diff": {"np_min_holders": 20}}
    else:
        rep["decision"] = {"action": "keep", "why": "no top config passed on the held-out part"}
    return rep


class Summary(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def page(self, rep):
        path, md = os.path.join(self.dir, "report.json"), os.path.join(self.dir, "s.md")
        with open(path, "w") as fh:
            json.dump(rep, fh)
        said = []
        self.assertEqual(ft.main(["summary", path, "--md", md], say=said.append), 0)
        with open(md) as fh:
            return fh.read(), said

    def test_keep(self):
        page, said = self.page(report("keep"))
        self.assertTrue(page.startswith("## Backtest: KEEP"))
        self.assertIn("**KEEP** config.json as it is: no top config passed", page)
        self.assertIn("| base | config.json as it is | 80 trades · win 40 % · -4.00 $ · mean -0.50 % · Σ -40.0 % | "
                      "31 trades · win 40 % · -1.50 $ · mean -0.40 % · DD 3.20 $ · PF ∞ |", page)
        self.assertIn("| #1 | np_min_holders=20\\|x |", page)                  # a pipe cannot break the table
        self.assertIn("+1.20 % of an order a coin over 40 coins · t 2.70 (needs 2.58)", page)
        self.assertIn("no: only 12 held-out trades (needs 30)", page)
        self.assertIn("12 lines the recorder lost (hours 2026-11-25T20)", page)
        self.assertIn("30 s unknown for every coin", page)
        self.assertIn("> live check: 11 live trades", page)                   # backtest.py's own words
        rep = report("keep")
        rep["lines"] = [x for x in rep["lines"] if not x.startswith("live check:")]
        page2, _ = self.page(rep)
        self.assertIn("11 live trades, 11 replayed, 10 with the same entry, 9 with the same exit", page2)
        self.assertIn('`{"max_open": 8}`', page)
        self.assertIn("position_pct=5.0", page)
        self.assertIn("a hint, not a promise", page)
        self.assertEqual(said[1:], ["  tape ...", "  live check: 11 live trades, 11 replayed", "  DECISION: KEEP · thin"])

    def test_the_coins_the_tape_cannot_judge_and_old_rug_lines_are_on_the_page(self):
        """The post-fix review's finding: report.json kept the base's unjudged coins, but the page never showed them,
        and its list of what a change needs left the unjudged rules out."""
        rep = report("keep")
        rep["tape"]["rug_lines_old"] = 120
        rep["base"]["unjudged"] = {"first_part": 70, "held_out": 34,
                                   "held_out_by_cause": {"stale": 30, "missing": 3, "unseen": 1}}
        rep["top"][0]["unjudged"] = {"first_part": 2, "held_out": 1, "held_out_by_cause": {"stale": 0, "missing": 1,
                                                                                          "unseen": 0}}
        page, _ = self.page(rep)
        self.assertIn("PF ∞ · 34 coins it cannot judge (30 waiting for holders, no fresher report, 3 no RugCheck "
                      "report, 1 no readings) |", page)
        self.assertIn("· 1 blind · 1 coin it cannot judge (1 no RugCheck report) |", page)
        self.assertIn("at most 10 % as many coins the tape cannot judge for it beyond the base's as it has trades, and "
                      "at most 10 % of its trades on coins the tape cannot judge for the base", page)
        self.assertIn("120 RugCheck lines recorded before the tape counted the wallets holding a coin", page)

    def test_change_shows_the_diff_and_never_applies_it(self):
        page, _ = self.page(report("change"))
        self.assertTrue(page.startswith("## Backtest: CHANGE"))
        self.assertIn("**CHANGE (a hint, not a promise)**", page)
        self.assertIn('"np_min_holders": 20', page)
        self.assertIn("The workflow changes nothing", page)
        self.assertIn("**passes**", page)

    def test_a_report_with_no_tape(self):
        page, _ = self.page({"decision": {"action": "keep", "why": "no tape to replay (tapes)"}, "top": [],
                             "ranking": [], "base": None, "tape": {"files": 0}, "lines": ["keep: no tape to replay"]})
        self.assertIn("no tape to replay", page)
        self.assertIn("**Tape gaps**: none recorded.", page)
        self.assertIn("No live trade on the tape", page)

    def test_a_missing_report(self):
        md, said = os.path.join(self.dir, "s.md"), []
        self.assertEqual(ft.main(["summary", os.path.join(self.dir, "none.json"), "--md", md], say=said.append), 0)
        self.assertIn("no backtest report", said[0])
        with open(md) as fh:
            self.assertIn("see the backtest step's log", fh.read())


class EndToEnd(unittest.TestCase):
    # backtest.main() sets MEMEBOT_LIVE / MEMEBOT_FEEDS / MEMEBOT_REC = 0 for its process: restored after the test,
    # or the recorder's tests that run later in the same process would find the recorder off
    @unittest.mock.patch.dict(os.environ)
    def test_fetched_tapes_through_the_backtester_to_the_summary_page(self):
        """Synthetic tapes (made-up coins on the pump.fun curve maths) as two job artifacts behind the fake API,
        fetched, backtested with a tiny grid by backtest.py's own command line, and summarized."""
        R = load("memebot", "replay")
        bt = load("memebot", "backtest")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        src = os.path.join(d, "src")
        recs = R.synth_tape(R.parse_time("2026-11-26T08:00:00Z"), R.demo_coins(40, seed=3))
        cut = recs[len(recs) // 2]["t"]
        zips = {}
        for aid, part in ((1, [r for r in recs if r["t"] < cut]), (2, [r for r in recs if r["t"] >= cut])):
            R.write_tape(os.path.join(src, str(aid)), part)
            folder = os.path.join(src, str(aid), "tapes")
            files = {}
            for n in os.listdir(folder):
                with open(os.path.join(folder, n), encoding="utf-8") as fh:
                    files[n] = fh.read()
            zips[aid] = zip_of(files)
        gh = FakeGitHub([artifact(1, "tapes-801-1", 7, run_id=801), artifact(2, "tapes-802-1", 1, run_id=802)],
                        {801: run(801), 802: run(802)}, zips)
        out = os.path.join(d, "tapes")
        os.environ["GH_TOKEN"] = "tok"
        self.assertEqual(ft.main(["fetch", "--repo", REPO, "--out", out], transport=gh, now=NOW, say=lambda s: None),
                         0)
        self.assertEqual(sorted(os.listdir(out)), ["801-1", "802-1"])
        grid = os.path.join(d, "grid.json")
        with open(grid, "w") as fh:
            json.dump({"combos": [{"stop_pct": -10.0}, {"tp_x": 1.5}]}, fh)
        # config.json's rules with a LEARNER that neither skips nor resizes and the entry filters these made-up
        # coins (a trigger 5 s after launch, no holders on their report) cannot pass, so that configs trade here
        cfg = bt.bot.load_config(bt.bot.CONFIG)
        cfg.update(learn_skip_pct=-1000.0, learn_mult_min=1.0, learn_mult_max=1.0, np_min_age_s=0, np_min_holders=0,
                   np_max_sol_per_holder=0)
        cfg_path = os.path.join(d, "config.json")
        with open(cfg_path, "w") as fh:
            json.dump(cfg, fh)
        rep_path = os.path.join(d, "report.json")
        said = []
        self.assertEqual(bt.main(["--data", out, "--grid", grid, "--split", "0.6", "--out", rep_path, "--jobs", "1",
                                  "--min-trades", "3", "--config", cfg_path], out=said.append), 0)
        md = os.path.join(d, "s.md")
        self.assertEqual(ft.main(["summary", rep_path, "--md", md], say=lambda s: None), 0)
        with open(md) as fh:
            page = fh.read()
        with open(rep_path) as fh:
            rep = json.load(fh)
        self.assertEqual(rep["tape"]["files"], len(os.listdir(os.path.join(out, "801-1"))) +
                         len(os.listdir(os.path.join(out, "802-1"))))
        self.assertIn("## Backtest: %s" % rep["decision"]["action"].upper(), page)
        self.assertIn("| base | config.json as it is |", page)
        for c in rep["top"]:
            self.assertIn("| #%d | %s |" % (c["rank"], c["text"]), page)
        for line in rep["lines"]:
            self.assertIn(line.replace("```", "'''"), page)
        self.assertEqual(os.environ["MEMEBOT_REC"], "0")        # what the decorator puts back afterwards


class Workflow(unittest.TestCase):
    """.github/workflows/backtest.yml: read-only, on demand, and wired to the helper's flags."""

    def setUp(self):
        with open(os.path.join(ROOT, ".github", "workflows", "backtest.yml"), encoding="utf-8") as fh:
            self.text = fh.read()

    def test_the_yaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        doc = yaml.safe_load(self.text)
        on = doc.get("on", doc.get(True))               # YAML 1.1 reads a bare `on` as true
        self.assertEqual(list(on), ["workflow_dispatch"])
        inputs = on["workflow_dispatch"]["inputs"]
        self.assertEqual({k: v["default"] for k, v in inputs.items()},
                         {"hours": "24", "grid": "default", "split": "0.7"})
        self.assertEqual(doc["permissions"], {"contents": "read", "actions": "read"})
        self.assertEqual(doc["concurrency"]["group"], "backtest")
        job = doc["jobs"]["backtest"]
        self.assertEqual((job["env"]["MEMEBOT_LIVE"], job["env"]["MEMEBOT_REC"]), ("0", "0"))
        self.assertLessEqual(job["timeout-minutes"], 360)
        runs = "\n".join(s.get("run", "") for s in job["steps"])
        self.assertIn("memebot/fetch_tapes.py fetch", runs)
        self.assertIn("python3 memebot/backtest.py --data tapes", runs)
        self.assertIn("--out report.json", runs)
        self.assertIn("memebot/fetch_tapes.py summary report.json", runs)
        up = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact"))
        self.assertEqual(up["with"]["name"], "backtest-${{ github.run_id }}")
        self.assertEqual(up["with"]["retention-days"], 30)
        py = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/setup-python"))
        self.assertEqual(str(py["with"]["python-version"]), "3.12")

    def test_it_never_touches_the_bots_data_or_live_channel(self):
        for word in ("meme-data", "git push", "git commit", "gh workflow run", "contents: write", "actions: write"):
            body = "\n".join(l for l in self.text.splitlines() if not l.lstrip().startswith("#"))
            self.assertNotIn(word, body)
        # the inputs reach the shell through env, never pasted into the script (no injection through an input)
        for line in self.text.splitlines():
            if "${{ inputs." in line:
                self.assertRegex(line.strip(), r"^[A-Z_]+: \$\{\{ inputs\.\w+ \}\}$")


if __name__ == "__main__":
    unittest.main()
