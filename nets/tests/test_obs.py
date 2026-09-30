"""jamswap's side of the observability stack: the labels on the nets' containers
(nets/netgen.py and the hand-written compose files), the net glue (nets/obsnet.py), the
soak's pushed metrics (nets/soak_metrics.py) and jamswap's dashboards
(observability/gen_dashboards.py). The stack itself is the abutlabs/observability repo,
tested there. Hermetic: no stack, no Docker (the compose check skips without it).

    python3 -m unittest discover -s nets/tests
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "observability"))
import netgen  # noqa: E402
import obsnet  # noqa: E402
import soak_metrics  # noqa: E402
import gen_dashboards  # noqa: E402

O = "org.abutlabs.obs."


def labels(name):
    return {svc: {k[len(O):]: v for k, v in (spec.get("labels") or {}).items()}
            for svc, spec in netgen.compose(name)["services"].items()}


class Labels(unittest.TestCase):
    def test_lasair_pj(self):
        lb = labels("lasair-pj")
        for svc, spec in lb.items():
            self.assertEqual((spec["net"], spec["run_id"], spec["logs"]),
                             ("lasair-pj", "${OBS_RUN_ID:-}", "true"), svc)
        scraped = {s: (v["job"], v["client"], v["port"]) for s, v in lb.items() if v.get("scrape")}
        self.assertEqual(scraped, {
            "lm0": ("lasair", "lasair", "9615"), "lm1": ("lasair", "lasair", "9615"),
            "lm2": ("lasair", "lasair", "9615"), "builder": ("builder", "lasair", "19980"),
            "dex": ("dex", "dex", "8080"), "loadgen": ("loadgen", "loadgen", "9111"),
            "netwatch": ("netwatch", "netwatch", "9106")})
        # PolkaJam exports no metrics: its own JIP-2 RPC, and lasair's through its reader
        jip2 = {s: (v["jip2"], v.get("jip2.node")) for s, v in lb.items() if v.get("jip2")}
        self.assertEqual(jip2, {"pj3": ("42603", None), "pj4": ("42604", None), "pj5": ("42605", None),
                                "reader": ("19800", "lm0"), "reader1": ("19800", "lm1"),
                                "reader2": ("19800", "lm2")})
        self.assertEqual(lb["pj3"]["client"], "polkajam")

    def test_pj6_gateway_node_is_polled(self):
        lb = labels("pj6")
        self.assertEqual(lb["rpc"]["jip2"], str(netgen.gateway(netgen.profile("pj6"))["rpc"]))
        self.assertNotIn("scrape", lb["pj0"])

    def test_polkajam_gets_the_telemetry_endpoint(self):
        env = netgen.compose("lasair-pj")["services"]["pj3"]["environment"]
        self.assertEqual(env["TELEMETRY"], "${OBS_JIP3:-}")

    @unittest.skipUnless(shutil.which("docker"), "needs the docker CLI")
    def test_hand_written_nets(self):
        expect = {"docker-compose.lasair6.yml": ("lasair6", {"lm0", "lm5", "builder", "dex", "loadgen",
                                                             "netwatch"}),
                  "docker-compose.mixed.yml": ("mixed", {"lm3", "lm5", "builder", "dex"})}
        for f, (net, scraped) in expect.items():
            r = subprocess.run(["docker", "compose", "-p", "obs-test", "-f", f, "config", "--format",
                                "json"], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=dict(os.environ, OBS_RUN_ID="r1"))
            self.assertEqual(r.returncode, 0, r.stderr)
            svcs = json.loads(r.stdout)["services"]
            for svc, spec in svcs.items():
                lb = spec.get("labels") or {}
                self.assertEqual((lb.get(O + "net"), lb.get(O + "run_id")), (net, "r1"), (f, svc))
            got = {s for s, spec in svcs.items() if (spec.get("labels") or {}).get(O + "scrape") == "true"}
            self.assertTrue(scraped <= got, (f, scraped - got))


class Glue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = obsnet.OBS, obsnet.OBS_HOME
        obsnet.OBS_HOME = self.tmp.name
        obsnet.OBS = os.path.join(self.tmp.name, "obs")

    def tearDown(self):
        obsnet.OBS, obsnet.OBS_HOME = self.saved
        self.tmp.cleanup()

    def test_without_the_stack_nothing_fails(self):
        self.assertEqual(obsnet.obs("ping").returncode, 127)
        self.assertFalse(obsnet.available())
        self.assertEqual(obsnet.links("lasair-pj-x"), [])
        self.assertFalse(obsnet.push("lasair-pj", "lasair-pj-x", "soak_pass 1\n"))

    def test_push_and_links_go_through_the_cli(self):
        # a stand-in CLI that records its arguments and stdin
        log = os.path.join(self.tmp.name, "calls")
        with open(obsnet.OBS, "w") as fh:
            fh.write("import sys, json\nopen(%r, 'a').write(json.dumps([sys.argv[1:], sys.stdin.read() "
                     "if sys.argv[1] == 'push' else '']) + '\\n')\n"
                     "if sys.argv[1] == 'link':\n"
                     "    print('platform/Chain health       http://g/d/obs-chain?x=1')\n"
                     "    print('jamswap/Soak runs           http://g/d/obs-soak-runs?x=1')\n" % log)
        self.assertTrue(obsnet.push("lasair-pj", "lasair-pj-x", "soak_pass 1\n"))
        self.assertEqual(obsnet.links("lasair-pj-x"),
                         [("platform/Chain health", "http://g/d/obs-chain?x=1"),
                          ("jamswap/Soak runs", "http://g/d/obs-soak-runs?x=1")])
        calls = [json.loads(ln) for ln in open(log)]
        self.assertEqual(calls[0], [["push", "soak", "--group", "run_id=lasair-pj-x", "--group",
                                     "net=lasair-pj"], "soak_pass 1\n"])


# a soak that ended like lasair6-20260928T154026Z: chain PASS, orders FAIL
DONE = {"net": "lasair6", "secs": 3600, "drain": 180, "started_ts": 1000.0, "poll": 0, "parity": 0,
        "verdict": 1, "pass": False,
        "load": {"offered": 1440, "refused": 519, "busy": 0, "turned_away": 519, "pass": False}}
VERDICT = {
    "orders_seen": 921, "slo": 0.664344, "target": 0.9999, "cleared": 572, "missed": 289,
    "clear_latency_p50_s": 160.12, "clear_latency_p99_s": 1623.54,
    "sealed": {"seen": 101, "terminal": 62, "stuck_open": 33, "zero_loss": False},
    "chain": {"pass": True,
              "verdict": {"one_head": {"pass": True, "ok_samples": 630, "episodes": 0,
                                       "longest_episode_slots": 0, "epoch_slots": 12},
                          "liveness": {"pass": True, "advance_slots": {"lm0": 1055, "lm1": 1050}},
                          "finality": {"pass": True, "conflicts": [], "stall_limit_slots": 12,
                                       "hash_checked_slots": 659,
                                       "nodes": {"lm0": {"longest_stall_slots": 2}}},
                          "authoring": {"pass": True, "blocks": {"0": 181, "1": 179}, "idle": []}},
              "parity": {"pass": True, "nodes": {"lm0": {"digest": "05ed"}, "lm1": {"digest": "05ed"}}}}}


def samples(text):
    """{(name, frozenset(labels)): value}, checking the text format as the Pushgateway would:
    one TYPE line per family, a family's samples together."""
    out, families, current = {}, [], None
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            name = line.split()[2]
            assert name not in families, "family %s declared twice" % name
            families.append(name)
            current = name
            continue
        m = re.match(r'^([a-z_]+)(?:\{(.*)\})? (\S+)$', line)
        assert m, line
        assert m.group(1) == current, "%s outside its family" % m.group(1)
        lb = frozenset(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', m.group(2) or ""))
        out[(m.group(1), lb)] = float(m.group(3))
    return out


class SoakMetrics(unittest.TestCase):
    def test_start_carries_the_configuration(self):
        cfg = {"clients": "lasair,lasair", "lasair_image": "ghcr.io/abutlabs/lasair:2.1.2",
               "load_profile": "trading", "load_rate": "12", "sealed_ratio": "0.2", "jamswap_commit": "abc"}
        s = samples(soak_metrics.start(cfg, 300, 180, 1000.0))
        info = [lb for (n, lb) in s if n == "soak_info"][0]
        self.assertIn(("lasair_image", "ghcr.io/abutlabs/lasair:2.1.2"), info)
        self.assertIn(("secs", "300"), info)
        self.assertEqual(s[("soak_phase", frozenset({("phase", "starting")}))], 1)
        self.assertEqual(s[("soak_load_seconds_left", frozenset())], 300)

    def test_result_judges_every_check(self):
        s = samples(soak_metrics.result(DONE, VERDICT, 0.9999, 5000.0))
        checks = {dict(lb)["check"]: (v, dict(lb)) for (n, lb), v in s.items() if n == "soak_check"}
        self.assertEqual(set(checks), {c for c, _, _ in soak_metrics.CHECKS})
        verdicts = {c: v for c, (v, _) in checks.items()}
        self.assertEqual(verdicts, {"offered_load": 0, "clearing_slo": 0, "sealed_zero_loss": 0,
                                    "one_head": 1, "liveness": 1, "finality": 1, "authoring": 1,
                                    "state_parity": 1})
        self.assertEqual(checks["clearing_slo"][1]["threshold"], "≥ 0.9999")
        self.assertEqual(checks["sealed_zero_loss"][1]["measured"], "33 stuck of 101")
        self.assertEqual(s[("soak_check_value", frozenset({("check", "clearing_slo")}))], 0.664344)
        self.assertEqual(s[("soak_pass", frozenset())], 0)
        self.assertEqual(s[("soak_duration_seconds", frozenset())], 4000)
        self.assertEqual(s[("soak_clear_latency_seconds", frozenset({("quantile", "0.99")}))], 1623.54)
        self.assertEqual(s[("soak_orders_refused", frozenset())], 519)
        self.assertEqual(s[("soak_phase", frozenset({("phase", "done")}))], 1)

    def test_a_soak_that_ended_early_pushes_what_it_has(self):
        done = {"pass": False, "load": {"offered": 0, "refused": 0, "busy": 0, "pass": False},
                "poll": "timeout"}
        s = samples(soak_metrics.result(done, None, 0.9999, 5000.0))
        self.assertEqual([dict(lb)["check"] for (n, lb) in s if n == "soak_check"], ["offered_load"])
        self.assertEqual(s[("soak_step_exit_code", frozenset({("step", "poll")}))], -1)


@unittest.skipIf(gen_dashboards.obsdash_missing, "needs the observability repo (OBS_HOME)")
class Dashboards(unittest.TestCase):
    def test_generated_json_is_current(self):
        for name, d in gen_dashboards.render_all().items():
            with open(os.path.join(gen_dashboards.OUT, name)) as fh:
                self.assertEqual(fh.read(), json.dumps(d, indent=1) + "\n",
                                 "%s is stale: python3 observability/gen_dashboards.py" % name)

    def test_soak_runs_dashboard(self):
        d = gen_dashboards.soak_runs()
        self.assertEqual(d["uid"], "obs-soak-runs")
        self.assertEqual([v["name"] for v in d["templating"]["list"]], ["net", "run_id", "at", "run_from", "run_to"])
        self.assertEqual(d["links"][0]["title"], "whole run")
        exprs = [t["expr"] for p in d["panels"] for t in p.get("targets", [])]
        for c, _, _ in soak_metrics.CHECKS:
            self.assertTrue(any('check="%s"' % c in e for e in exprs), c)
        for e in exprs:
            self.assertRegex(e, r"soak_|push_time_seconds", e)
        tables = [p for p in d["panels"] if p["type"] == "table"]
        self.assertEqual([t["title"] for t in tables], ["Configuration", "Soak runs, newest first"])

    def test_dex_dashboard_is_scoped_by_run(self):
        d = gen_dashboards.dex()
        for p in d["panels"]:
            for t in p.get("targets", []):
                self.assertIn('run_id="$run_id"', t["expr"], p["title"])


if __name__ == "__main__":
    unittest.main()
