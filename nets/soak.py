#!/usr/bin/env python3
"""The epic's shared acceptance (A1-A4, jamswap#22) as one command, on a running net
whose DEX runs on JIP-2 (the dex, loadgen and netwatch services nets/netgen.py adds):

    python3 nets/soak.py NET [--secs 600] [--drain 180] [--out DIR]
    ./dex soak NET=<name> [SECS]

  1. waits for the DEX API and reads the service id the dex deployed (its deploy state);
     then a short `netwatch poll` (--precheck, 60 s) must pass before any load: a net that
     did not form (a node stuck at genesis, finality not advancing everywhere) is reported
     as such, not soaked for an hour and failed on A1/A2;
  2. starts the load generator, and `netwatch poll` over every node for SECS + DRAIN
     seconds, with --require-finality on a GRANDPA net: A1 one head, A2 finality;
  3. after SECS stops the load, so the last rounds settle within DRAIN;
  4. `netwatch parity` at the common finalized head: A3, the service state (books,
     balances, custody, registry, landed-round markers) identical on every node;
  5. `soak_verdict.py` over the dex's order event log with --chain and --parity: A4,
     clearing SLO >= 0.9999, sealed zero-loss, and the chain half folded in;
  6. the offered load, as the load generator counted it just before it stopped: the SLO
     judges only orders the DEX accepted, so a DEX that turned the load away (a 4xx/5xx
     to every order) would otherwise pass. At most 1 - target of it may be refused;
  7. the submission nodes (the dex's jamswap_relays_total / jamswap_settled_via_total):
     every node the dex submitted through (its gateway, and on a net whose clients'
     validators take work-packages, one of those: CHAIN_SUBMIT_RPC) must have carried at
     least one round that settled, or the net's claim to route rounds through it fails.

Run it on a freshly started net (`./dex up`): the verdict judges the dex's whole order
event log. Everything lands in DIR (default ~/.cache/jamswap/soak/<net>-<UTC time>):
precheck.txt, poll.txt, chain.jsonl, parity.txt, parity.json, order_events.jsonl,
verdict.txt, verdict.json, loadgen.txt, dex_metrics.txt, rounds.txt (every settled round
and the node it went through), soak.log, and DONE (written last: each step's result).
Exit 0 iff the poll, the parity probe, the soak verdict, the offered load and the
submission nodes all pass; 2 if the precheck failed (nothing was soaked).
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import netgen  # noqa: E402

NETWATCH = ["python3", "/netwatch/netwatch.py"]


class Soak:
    def __init__(self, name, out):
        self.name, self.out = name, out
        self.compose = ["docker", "compose", "-p", netgen.project(name),
                        "-f", netgen.compose_path(name)]
        os.makedirs(out, exist_ok=True)
        self._log = open(os.path.join(out, "soak.log"), "a")

    def log(self, msg):
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        self._log.write(line + "\n")
        self._log.flush()

    def path(self, name):
        return os.path.join(self.out, name)

    def dc(self, *args, stdout=None, timeout=None, check=True):
        """docker compose <args> in the repo (the compose file's relative paths)."""
        r = subprocess.run(self.compose + list(args), cwd=REPO, stdout=stdout or subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError("docker compose %s: exit %d: %s" % (" ".join(args[:3]), r.returncode,
                                                                    r.stderr.strip()[-500:]))
        return r

    def run_to(self, fname, cmd, timeout=None):
        """Run cmd with stdout+stderr to out/fname; return its exit status."""
        with open(self.path(fname), "w") as fh:
            r = subprocess.run(cmd, cwd=REPO, stdout=fh, stderr=subprocess.STDOUT, text=True,
                               timeout=timeout)
        return r.returncode


LOADGEN_METRICS = ("import urllib.request; print(urllib.request.urlopen("
                   "'http://localhost:9111/metrics', timeout=10).read().decode())")
DEX_METRICS = LOADGEN_METRICS.replace("9111", "8080")
ROUND_OPS = ("round", "reveal", "enc_round")      # the work-items that settle a round


def via_counts(metrics_text):
    """{node: {"relayed": {op: n}, "settled": {op: n}}} from the dex's /metrics:
    jamswap_relays_total and jamswap_settled_via_total, labelled {op, via}."""
    out = {}
    series = {"jamswap_relays_total": "relayed", "jamswap_settled_via_total": "settled"}
    for line in metrics_text.splitlines():
        name = line.split("{")[0]
        if name not in series or line.startswith("#") or "{" not in line:
            continue
        labels = dict(kv.split("=", 1) for kv in line[line.index("{") + 1:line.rindex("}")].split(","))
        labels = {k: v.strip('"') for k, v in labels.items()}
        node = out.setdefault(labels.get("via", "?"), {"relayed": {}, "settled": {}})
        node[series[name]][labels.get("op", "?")] = int(float(line.rsplit(" ", 1)[1]))
    return out


def judge_routes(counts):
    """Every node the dex submitted through settled at least one round (ROUND_OPS)."""
    rounds = {n: sum(c["settled"].get(op, 0) for op in ROUND_OPS) for n, c in counts.items()}
    return {"pass": bool(rounds) and all(v > 0 for v in rounds.values()),
            "rounds_settled": rounds, "nodes": counts}


def load_counts(metrics_text):
    """{offered, refused, busy} from the load generator's /metrics: ops it offered, API
    calls the DEX refused or failed (loadgen_op_errors_total), and 503s (the chain was
    busy: loadgen_ops_busy_total). Every one of them is an order that never entered."""
    tot = {"loadgen_ops_total": 0, "loadgen_op_errors_total": 0, "loadgen_ops_busy_total": 0}
    for line in metrics_text.splitlines():
        name = line.split("{")[0].split(" ")[0]
        if name in tot and not line.startswith("#"):
            tot[name] += float(line.rsplit(" ", 1)[1])
    return {"offered": int(tot["loadgen_ops_total"]), "refused": int(tot["loadgen_op_errors_total"]),
            "busy": int(tot["loadgen_ops_busy_total"])}


def judge_load(counts, target):
    """The offered load passes iff it was offered and at most 1 - target of it was turned
    away (refused or busy)."""
    turned = counts["refused"] + counts["busy"]
    out = dict(counts, turned_away=turned)
    out["pass"] = counts["offered"] > 0 and turned <= (1.0 - target) * counts["offered"]
    return out


def wait_dex(url, timeout):
    """The DEX API answers /api/finality once the service is deployed and set up."""
    deadline = time.time() + timeout
    while True:
        try:
            with urllib.request.urlopen(url + "/api/finality", timeout=5) as r:
                return json.load(r)
        except (OSError, ValueError):
            if time.time() > deadline:
                raise
            time.sleep(5)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("net")
    ap.add_argument("--secs", type=int, default=600, help="seconds of load (default 600)")
    ap.add_argument("--drain", type=int, default=180,
                    help="seconds after the load stops for the last rounds to settle (180)")
    ap.add_argument("--out", default=None, help="result directory")
    ap.add_argument("--service", type=int, default=None,
                    help="the service id (default: the dex's deploy state)")
    ap.add_argument("--target", default="0.9999", help="clearing SLO target (0.9999)")
    ap.add_argument("--ready-timeout", type=float, default=900,
                    help="seconds to wait for the DEX API (900)")
    ap.add_argument("--precheck", type=int, default=60,
                    help="seconds of netwatch poll that must pass before the load (60; 0: none)")
    a = ap.parse_args(argv)

    if not netgen.generated(a.net) or netgen.dex_backend(a.net) != "jip2":
        ap.error("%s has no JIP-2 DEX stack (dex, loadgen, netwatch); see nets/profiles.py" % a.net)
    p = netgen.profile(a.net)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = a.out or os.path.join(os.environ.get("JAMSWAP_CACHE") or
                                os.path.expanduser("~/.cache/jamswap"), "soak",
                                "%s-%s" % (a.net, stamp))
    s = Soak(a.net, out)
    finality = ["--require-finality"] if p.get("finality") == "grandpa" else []
    done = {"net": a.net, "secs": a.secs, "drain": a.drain, "started": stamp}
    s.log("soak %s: %d s load + %d s drain -> %s" % (a.net, a.secs, a.drain, out))

    url = netgen.dex_url(p)
    fin = wait_dex(url, a.ready_timeout)
    s.log("dex %s answers: %s" % (url, json.dumps(fin)))
    sid = a.service
    if sid is None:
        st = s.dc("exec", "-T", "dex", "cat", "/shared/jamswap_deploy.json", timeout=30)
        sid = int(json.loads(st.stdout)["service_id"])
    done["service"] = sid
    s.log("service %d" % sid)

    if a.precheck > 0:
        done["precheck"] = s.run_to("precheck.txt", s.compose + ["exec", "-T", "netwatch"] + NETWATCH + [
            "poll", "--duration", str(a.precheck), "--interval", "6"] + finality,
            timeout=a.precheck + 300)
        s.log("precheck (%d s netwatch poll, no load): exit %s" % (a.precheck, done["precheck"]))
        if done["precheck"] != 0:
            with open(s.path("precheck.txt")) as fh:
                print("\n==> precheck.txt\n" + "\n".join(fh.read().splitlines()[-25:]))
            done["pass"] = False
            done["finished"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            with open(s.path("DONE"), "w") as fh:
                json.dump(done, fh, indent=2)
            s.log("DONE FAIL: the net failed its precheck before any load (one head / "
                  "finality on every node): start a fresh net (./dex down; ./dex up)")
            return 2

    chain_tmp, parity_tmp = "/tmp/soak-%s-chain.jsonl" % stamp, "/tmp/soak-%s-parity.json" % stamp
    poll_cmd = s.compose + ["exec", "-T", "netwatch"] + NETWATCH + [
        "poll", "--duration", str(a.secs + a.drain), "--interval", "6",
        "--samples", chain_tmp] + finality
    with open(s.path("poll.txt"), "w") as poll_out:
        poll = subprocess.Popen(poll_cmd, cwd=REPO, stdout=poll_out, stderr=subprocess.STDOUT)
        try:
            s.dc("start", "loadgen", timeout=60)
            s.log("loadgen on; netwatch poll running (%s)" % (" ".join(finality) or "finality auto"))
            t_end = time.time() + a.secs
            while time.time() < t_end:
                time.sleep(min(60, max(0, t_end - time.time())))
                if poll.poll() is not None:
                    s.log("netwatch poll ended early (exit %s)" % poll.returncode)
                    break
                s.log("  %d s of load left" % max(0, t_end - time.time()))
        finally:
            r = s.dc("exec", "-T", "loadgen", "python3", "-c", LOADGEN_METRICS, timeout=60,
                     check=False)
            with open(s.path("loadgen.txt"), "w") as fh:
                fh.write(r.stdout)
            load = judge_load(load_counts(r.stdout), float(a.target))
            done["load"] = load
            s.dc("stop", "loadgen", timeout=60, check=False)
            s.log("loadgen off; draining. offered %(offered)d, refused %(refused)d, busy %(busy)d"
                  % load)
        try:
            done["poll"] = poll.wait(timeout=a.drain + 300)
        except subprocess.TimeoutExpired:
            poll.kill()
            done["poll"] = "timeout"
    s.log("netwatch poll: exit %s" % done["poll"])
    s.dc("cp", "netwatch:" + chain_tmp, s.path("chain.jsonl"), timeout=60)

    done["parity"] = s.run_to("parity.txt", s.compose + ["exec", "-T", "netwatch"] + NETWATCH + [
        "parity", "--service", str(sid), "--dex-url", "http://dex:8080", "--out", parity_tmp],
        timeout=600)
    s.log("netwatch parity: exit %s" % done["parity"])
    s.dc("cp", "netwatch:" + parity_tmp, s.path("parity.json"), timeout=60, check=False)

    s.dc("cp", "dex:/shared/order_events.jsonl", s.path("order_events.jsonl"), timeout=60)
    verdict = [sys.executable, os.path.join(REPO, "offchain", "soak_verdict.py"),
               s.path("order_events.jsonl"), "--target", a.target, "--chain", s.path("chain.jsonl")]
    if os.path.exists(s.path("parity.json")):
        verdict += ["--parity", s.path("parity.json")]
    verdict += finality
    done["verdict"] = s.run_to("verdict.txt", verdict, timeout=300)
    s.run_to("verdict.json", verdict + ["--json"], timeout=300)
    s.log("soak_verdict: exit %s" % done["verdict"])

    r = s.dc("exec", "-T", "dex", "python3", "-c", DEX_METRICS, timeout=60, check=False)
    with open(s.path("dex_metrics.txt"), "w") as fh:
        fh.write(r.stdout)
    done["routes"] = judge_routes(via_counts(r.stdout))
    r = s.dc("logs", "--no-log-prefix", "dex", timeout=120, check=False)   # a fresh net: all of it
    with open(s.path("rounds.txt"), "w") as fh:
        fh.writelines(l + "\n" for l in r.stdout.splitlines() if "settled on-chain" in l)

    done["pass"] = (done["poll"] == 0 and done["parity"] == 0 and done["verdict"] == 0
                    and done["load"]["pass"] and done["routes"]["pass"])
    done["finished"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for f in ("poll.txt", "parity.txt", "verdict.txt"):
        print("\n==> %s" % f)
        with open(s.path(f)) as fh:
            lines = fh.read().splitlines()
        print("\n".join(lines[-40:] if f == "poll.txt" else lines))
    ld = done["load"]
    print("\n==> offered load   : %s  (%d orders offered, %d refused, %d busy; at most %g%% may "
          "be turned away)" % ("PASS" if ld["pass"] else "FAIL", ld["offered"], ld["refused"],
                               ld["busy"], 100 * (1 - float(a.target))))
    rt = done["routes"]
    print("==> submission nodes: %s  (rounds settled per node: %s)" % (
        "PASS" if rt["pass"] else "FAIL",
        ", ".join("%s %d" % kv for kv in sorted(rt["rounds_settled"].items())) or "none"))
    for node, c in sorted(rt["nodes"].items()):
        print("      %-10s relayed %s | settled %s" % (node, c["relayed"], c["settled"]))
    with open(s.path("DONE"), "w") as fh:
        json.dump(done, fh, indent=2)
    s.log("DONE %s: %s" % ("PASS" if done["pass"] else "FAIL", json.dumps(done)))
    return 0 if done["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
