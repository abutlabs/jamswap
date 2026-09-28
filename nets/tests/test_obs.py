"""Tests for the obs stack's CLI (monitor/obs/obs), the net glue (nets/obsnet.py) and the
generated dashboards (monitor/obs/gen_dashboards.py). Hermetic: a temporary OBS_STATE, a
Prometheus container name that does not exist, and a fake Grafana for annotations.

    python3 -m unittest discover -s nets/tests
"""
import contextlib
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "monitor", "obs"))
import obsnet  # noqa: E402
import gen_dashboards  # noqa: E402


def load_cli(state, grafana_port="1"):
    """A fresh import of the obs CLI bound to this state dir and Grafana port."""
    os.environ.update(OBS_STATE=state, OBS_PROMETHEUS_CONTAINER="obs-test-no-such-container",
                      OBS_GRAFANA_PORT=grafana_port, OBS_PROMETHEUS_PORT="1")
    path = os.path.join(REPO, "monitor", "obs", "obs")
    loader = importlib.machinery.SourceFileLoader("obs_cli", path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("obs_cli", loader))
    loader.exec_module(mod)
    return mod


def run(cli, *argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.main(list(argv))
    return rc, out.getvalue()


class FakeGrafana(http.server.BaseHTTPRequestHandler):
    posted = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGrafana.posted.append((self.path, self.headers.get("Authorization"), body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"id": 7, "message": "Annotation added"}')

    def log_message(self, *a):
        pass


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.env = dict(os.environ)
        self.cli = load_cli(self.state)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        self.tmp.cleanup()

    def targets(self, net, job):
        with open(os.path.join(self.state, "targets", "%s--%s.json" % (net, job))) as fh:
            return json.load(fh)

    def test_parse_target(self):
        t = self.cli.parse_target("v0@host.docker.internal:41100/stats")
        self.assertEqual(t, {"node": "v0", "addr": "host.docker.internal:41100", "path": "/stats"})
        self.assertEqual(self.cli.parse_target("lm0:9615"),
                         {"node": "lm0", "addr": "lm0:9615", "path": "/metrics"})

    def test_register_writes_file_sd_with_every_label(self):
        rc, _ = run(self.cli, "register", "n1", "n1-r1", "lasair", "lm0:9615", "v1@h:1/x",
                    "--label", "client=lasair", "--label", "extra=1")
        self.assertEqual(rc, 0)
        g = self.targets("n1", "lasair")
        self.assertEqual([x["targets"] for x in g], [["lm0:9615"], ["h:1"]])
        self.assertEqual(g[0]["labels"], {"net": "n1", "run_id": "n1-r1", "job": "lasair",
                                          "node": "lm0", "client": "lasair", "extra": "1"})
        self.assertEqual(g[1]["labels"]["node"], "v1")
        self.assertEqual(g[1]["labels"]["__metrics_path__"], "/x")
        rc, out = run(self.cli, "current", "n1")
        self.assertEqual((rc, out.strip()), (0, "n1-r1"))

    def test_client_defaults_to_the_job(self):
        run(self.cli, "register", "n1", "n1-r1", "dex", "dex:8080")
        self.assertEqual(self.targets("n1", "dex")[0]["labels"]["client"], "dex")

    def test_a_new_run_replaces_the_nets_targets_and_ends_the_old_run(self):
        run(self.cli, "register", "n1", "n1-r1", "lasair", "lm0:9615")
        run(self.cli, "register", "n1", "n1-r1", "dex", "dex:8080")
        run(self.cli, "register", "n1", "n1-r2", "dex", "dex:8080")
        self.assertEqual(sorted(os.listdir(os.path.join(self.state, "targets"))), ["n1--dex.json"])
        self.assertEqual(self.targets("n1", "dex")[0]["labels"]["run_id"], "n1-r2")
        self.assertIsNotNone(self.cli._read(self.cli.run_file("n1-r1"))["end"])
        self.assertIsNone(self.cli._read(self.cli.run_file("n1-r2"))["end"])

    def test_unregister_drops_targets_and_ends_the_run(self):
        run(self.cli, "register", "n1", "n1-r1", "lasair", "lm0:9615")
        run(self.cli, "register", "n2", "n2-r1", "lasair", "lm0:9615")
        run(self.cli, "unregister", "n1")
        self.assertEqual(os.listdir(os.path.join(self.state, "targets")), ["n2--lasair.json"])
        self.assertEqual(run(self.cli, "current", "n1")[0], 1)
        self.assertIsNotNone(self.cli._read(self.cli.run_file("n1-r1"))["end"])

    def test_bad_names_are_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run(self.cli, "register", "../x", "r", "lasair", "lm0:9615")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run(self.cli, "register", "n", "r", "lasair", "lm0")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run(self.cli, "register", "n", "r", "lasair", "lm0:1", "--label", "net=x")

    def test_link_carries_the_runs_variables_and_range(self):
        run(self.cli, "register", "n1", "n1-r1", "lasair", "lm0:9615")
        start = self.cli._read(self.cli.run_file("n1-r1"))["start"]
        _, out = run(self.cli, "link", "n1-r1", "-d", "memory")
        self.assertIn("/d/obs-memory?", out)
        for part in ("var-net=n1", "var-run_id=n1-r1", "to=now", "from=%d" % (int(start * 1000) - 60000)):
            self.assertIn(part, out)
        run(self.cli, "unregister", "n1")
        _, out = run(self.cli, "link", "n1-r1")
        self.assertNotIn("to=now", out)
        _, out = run(self.cli, "link", "n1-r1", "--all")
        self.assertEqual(len(out.splitlines()), 4)

    def test_annotate_tags_the_run_and_its_net(self):
        srv = http.server.HTTPServer(("127.0.0.1", 0), FakeGrafana)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            cli = load_cli(self.state, str(srv.server_port))
            run(cli, "register", "n1", "n1-r1", "lasair", "lm0:9615")
            FakeGrafana.posted.clear()
            self.assertEqual(run(cli, "annotate", "n1-r1", "soak start", "--tags", "soak")[0], 0)
            self.assertEqual(run(cli, "annotate", "n1-r1", "verdict", "--tags", "verdict,fail",
                                 "--start", "start", "--end", "1790000000")[0], 0)
        finally:
            srv.shutdown()
            srv.server_close()
        (path, auth, point), (_, _, region) = FakeGrafana.posted
        self.assertEqual(path, "/api/annotations")
        self.assertTrue(auth.startswith("Basic "))
        self.assertEqual(point["tags"], ["obs", "n1-r1", "n1", "soak", "event"])
        self.assertNotIn("timeEnd", point)
        self.assertEqual(region["tags"], ["obs", "n1-r1", "n1", "verdict", "fail"])
        self.assertEqual(region["timeEnd"], 1790000000000)
        self.assertLessEqual(region["time"], point["time"])


class NetGlue(unittest.TestCase):
    def test_lasair_pj_targets(self):
        services = ["spec-init", "lm0", "lm1", "lm2", "pj3", "pj4", "pj5", "builder", "reader",
                    "reader1", "reader2", "dex", "loadgen", "netwatch"]
        t = obsnet.targets("lasair-pj", services)
        self.assertEqual(t["lasair"], ("lasair", ["lm0@lm0:9615", "lm1@lm1:9615", "lm2@lm2:9615"]))
        self.assertEqual(t["dex"], ("dex", ["dex@dex:8080"]))
        self.assertEqual(t["loadgen"], ("loadgen", ["loadgen@loadgen:9111"]))
        self.assertEqual(t["netwatch"], ("netwatch", ["netwatch@netwatch:9106"]))
        self.assertEqual(t["builder"], ("lasair", ["builder@builder:19980"]))
        self.assertEqual(set(t), {"lasair", "dex", "loadgen", "netwatch", "builder"})

    def test_polkajam_nodes_are_not_scraped(self):
        # PolkaJam exports no Prometheus metrics: netwatch is its view
        self.assertEqual(obsnet.targets("pj6", ["pj0", "pj1", "netwatch"]),
                         {"netwatch": ("netwatch", ["netwatch@netwatch:9106"])})

    def test_lasair6_nodes(self):
        t = obsnet.targets("lasair6", ["lm%d" % i for i in range(6)])
        self.assertEqual(len(t["lasair"][1]), 6)


class Dashboards(unittest.TestCase):
    def test_generated_json_is_current(self):
        for name, d in gen_dashboards.render_all().items():
            with open(os.path.join(gen_dashboards.OUT, name)) as fh:
                self.assertEqual(fh.read(), json.dumps(d, indent=1) + "\n",
                                 "%s is stale: python3 monitor/obs/gen_dashboards.py" % name)

    def test_every_dashboard_is_scoped_explained_and_states_its_thresholds(self):
        for name, d in gen_dashboards.render_all().items():
            self.assertEqual([v["name"] for v in d["templating"]["list"]], ["net", "run_id"], name)
            self.assertEqual(d["panels"][0]["type"], "text", name)
            for p in d["panels"]:
                for t in p.get("targets", []):
                    self.assertIn('run_id="$run_id"', t["expr"], (name, p["title"]))
                if p["type"] == "stat" and p["fieldConfig"]["defaults"]["color"]["mode"] == "thresholds":
                    self.assertIn("PASS", p["title"], (name, p["title"]))
            tags = [a["target"]["tags"] for a in d["annotations"]["list"] if "target" in a]
            self.assertEqual(tags, [["$run_id", "event"], ["$run_id", "pass"], ["$run_id", "fail"]])


if __name__ == "__main__":
    unittest.main()
