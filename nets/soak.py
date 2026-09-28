#!/usr/bin/env python3
"""The epic's shared acceptance (A1-A4, jamswap#22) as one command, on a running net
with the dex, loadgen and netwatch services (every generated DEX net, and lasair6):

    python3 nets/soak.py NET [--secs 600] [--drain 180] [--out DIR]
    ./dex soak NET=<name> [SECS]

  1. waits for the DEX API and reads its service id: the dex's SERVICE_ID (seeded into
     genesis on a lasair net), else the one it deployed (its deploy state);
  2. starts the load generator, and `netwatch poll` over every node for SECS + DRAIN
     seconds, with --require-finality on a GRANDPA net: A1 one head, A2 finality;
  3. after SECS stops the load, so the last rounds settle within DRAIN;
  4. `netwatch parity` at the common finalized head: A3, the service state (books,
     balances, custody, registry, landed-round markers) identical on every node;
  5. `soak_verdict.py` over the dex's order event log with --chain and --parity: A4,
     clearing SLO >= 0.9999, sealed zero-loss, and the chain half folded in;
  6. the offered load, as the load generator counted it just before it stopped: the SLO
     judges only orders the DEX accepted, so a DEX that turned the load away (a 4xx/5xx
     to every order) would otherwise pass. At most 1 - target of it may be refused.

Run it on a freshly started net (`./dex up`): the verdict judges the dex's whole order
event log. With the observability stack running (monitor/README.md), the soak's start,
load off, drain, every PASS/FAIL line of the verdict (with its threshold) and the overall
result (a region over the soak) are annotated on the net's run in Grafana, and the
configuration, the progress (every minute) and the results go to its Pushgateway under
the net's run id (nets/soak_metrics.py): the "Soak runs" dashboard shows them.

Everything lands in DIR (default ~/.cache/jamswap/soak/<net>-<UTC time>): config.json
(what ran, written first), poll.txt, chain.jsonl, parity.txt, parity.json,
order_events.jsonl, dex.log, loadgen.log, verdict.txt, verdict.json, loadgen.txt,
soak.log, and DONE (written last: each step's result). Exit 0 iff the poll, the parity
probe, the soak verdict and the offered load all pass.
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
import obsnet  # noqa: E402
import profiles  # noqa: E402
import soak_metrics  # noqa: E402

NETWATCH = ["python3", "/netwatch/netwatch.py"]


class Soak:
    def __init__(self, name, out):
        self.name, self.out = name, out
        self.compose = ["docker", "compose", "-p", netgen.project(name),
                        "-f", netgen.compose_path(name)]
        os.makedirs(out, exist_ok=True)
        self._log = open(os.path.join(out, "soak.log"), "a")
        self.run_id = None                      # the obs run the results are pushed under

    def log(self, msg):
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        self._log.write(line + "\n")
        self._log.flush()

    def note(self, text, tags, start=None, end=None):
        """Annotate the net's obs run (nets/obsnet.py); never fails the soak."""
        try:
            obsnet.note(self.name, text, tags, start=start, end=end)
        except Exception as e:                  # noqa: BLE001 - observability is optional
            self.log("obs annotate failed: %s" % e)

    def push(self, text):
        """Push soak metrics to the obs Pushgateway under the run; never fails the soak."""
        if not self.run_id:
            return
        try:
            if not obsnet.push(self.name, self.run_id, text):
                self.log("obs push failed (Pushgateway not reachable?)")
        except Exception as e:                  # noqa: BLE001 - observability is optional
            self.log("obs push failed: %s" % e)

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


def soak_config(name, out, run_id):
    """What this soak runs, as soak/report.py and the Soak runs dashboard show it."""
    p = netgen.profile(name)
    clients = p["clients"]
    lasair = "lasair" in clients
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()
    except OSError:
        commit = ""
    env = os.environ.get
    return {
        "net": name, "clients": clients,
        "validators": "%d (one key each)" % len(clients.split(",")),
        "lasair_image": env("LASAIR_IMAGE", profiles.LASAIR_IMAGE) if lasair else "none (no lasair node)",
        "data_dir": env("LASAIR_DATA_DIR", ""),
        "dex_backend": (env("LASAIR_DEX_BACKEND") or "jip2 through lasair-reader") if lasair
        else "jip2 (a PolkaJam node)",
        "load_profile": env("PROFILE", "trading"), "load_rate": env("RATE", "12"),
        "sealed_ratio": env("SEALED_RATIO", "0.2"),
        "load": "PROFILE=%s, RATE=%s pairs/min, SEALED_RATIO=%s" % (
            env("PROFILE", "trading"), env("RATE", "12"), env("SEALED_RATIO", "0.2")),
        "jamswap_commit": commit, "run_id": run_id or "", "out_dir": out,
    }


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
    a = ap.parse_args(argv)

    if not netgen.soakable(a.net):
        ap.error("%s has no DEX stack (dex, loadgen, netwatch); see nets/profiles.py" % a.net)
    p = netgen.profile(a.net)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = a.out or os.path.join(os.environ.get("JAMSWAP_CACHE") or
                                os.path.expanduser("~/.cache/jamswap"), "soak",
                                "%s-%s" % (a.net, stamp))
    s = Soak(a.net, out)
    finality = ["--require-finality"] if p.get("finality") == "grandpa" else []
    t_soak = time.time()
    done = {"net": a.net, "secs": a.secs, "drain": a.drain, "started": stamp, "started_ts": t_soak}
    s.log("soak %s: %d s load + %d s drain -> %s" % (a.net, a.secs, a.drain, out))
    if obsnet.available():
        s.run_id = obsnet.run_id(a.net)
        s.log("obs run %s: %s" % (s.run_id, obsnet.obs("link", s.run_id, "-d", "soak-runs").stdout.strip()))
    cfg = soak_config(a.net, out, s.run_id)
    with open(s.path("config.json"), "w") as fh:
        json.dump(cfg, fh, indent=1)
    s.push(soak_metrics.start(cfg, a.secs, a.drain, t_soak))
    s.note("soak start: %d s load + %d s drain (%s)" % (a.secs, a.drain, out), "soak")

    url = netgen.dex_url_of(a.net)
    fin = wait_dex(url, a.ready_timeout)
    s.log("dex %s answers: %s" % (url, json.dumps(fin)))
    sid = a.service
    if sid is None:
        env = s.dc("exec", "-T", "dex", "printenv", "SERVICE_ID", timeout=30, check=False)
        if env.stdout.strip():
            sid = int(env.stdout.strip())
        else:
            st = s.dc("exec", "-T", "dex", "cat", "/shared/jamswap_deploy.json", timeout=30)
            sid = int(json.loads(st.stdout)["service_id"])
    done["service"] = sid
    s.log("service %d" % sid)

    chain_tmp, parity_tmp = "/tmp/soak-%s-chain.jsonl" % stamp, "/tmp/soak-%s-parity.json" % stamp
    poll_cmd = s.compose + ["exec", "-T", "netwatch"] + NETWATCH + [
        "poll", "--duration", str(a.secs + a.drain), "--interval", "6",
        "--samples", chain_tmp] + finality
    with open(s.path("poll.txt"), "w") as poll_out:
        poll = subprocess.Popen(poll_cmd, cwd=REPO, stdout=poll_out, stderr=subprocess.STDOUT)
        try:
            s.dc("start", "loadgen", timeout=60)
            s.log("loadgen on; netwatch poll running (%s)" % (" ".join(finality) or "finality auto"))
            s.note("soak: load on (netwatch poll %s)" % (" ".join(finality) or "finality auto"),
                   "soak,load")
            t_end = time.time() + a.secs
            s.push(soak_metrics.progress("load", a.secs))
            while time.time() < t_end:
                time.sleep(min(60, max(0, t_end - time.time())))
                if poll.poll() is not None:
                    s.log("netwatch poll ended early (exit %s)" % poll.returncode)
                    break
                s.log("  %d s of load left" % max(0, t_end - time.time()))
                s.push(soak_metrics.progress("load", t_end - time.time()))
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
            s.note("soak: load off, draining %d s (offered %d, refused %d, busy %d)"
                   % (a.drain, load["offered"], load["refused"], load["busy"]), "soak,load")
            s.push(soak_metrics.progress("drain", 0))
        try:
            done["poll"] = poll.wait(timeout=a.drain + 300)
        except subprocess.TimeoutExpired:
            poll.kill()
            done["poll"] = "timeout"
    s.log("netwatch poll: exit %s" % done["poll"])
    s.note("soak: drain done (netwatch poll exit %s); parity and verdict next" % done["poll"], "soak")
    s.dc("cp", "netwatch:" + chain_tmp, s.path("chain.jsonl"), timeout=60)
    s.push(soak_metrics.progress("parity", 0))

    done["parity"] = s.run_to("parity.txt", s.compose + ["exec", "-T", "netwatch"] + NETWATCH + [
        "parity", "--service", str(sid), "--dex-url", "http://dex:8080", "--out", parity_tmp],
        timeout=600)
    s.log("netwatch parity: exit %s" % done["parity"])
    s.dc("cp", "netwatch:" + parity_tmp, s.path("parity.json"), timeout=60, check=False)

    s.dc("cp", "dex:/shared/order_events.jsonl", s.path("order_events.jsonl"), timeout=60)
    # the dex's and the load generator's own logs: an op the dex refused says why only
    # there ("op buy failed: ..."), and a net torn down afterwards takes them with it
    for svc in ("dex", "loadgen"):
        s.run_to("%s.log" % svc, s.compose + ["logs", "--no-color", svc], timeout=120)
    verdict = [sys.executable, os.path.join(REPO, "offchain", "soak_verdict.py"),
               s.path("order_events.jsonl"), "--target", a.target, "--chain", s.path("chain.jsonl")]
    if os.path.exists(s.path("parity.json")):
        verdict += ["--parity", s.path("parity.json")]
    verdict += finality
    s.push(soak_metrics.progress("verdict", 0))
    done["verdict"] = s.run_to("verdict.txt", verdict, timeout=300)
    s.run_to("verdict.json", verdict + ["--json"], timeout=300)
    s.log("soak_verdict: exit %s" % done["verdict"])

    done["pass"] = (done["poll"] == 0 and done["parity"] == 0 and done["verdict"] == 0
                    and done["load"]["pass"])
    done["finished"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for f in ("poll.txt", "parity.txt", "verdict.txt"):
        print("\n==> %s" % f)
        with open(s.path(f)) as fh:
            lines = fh.read().splitlines()
        print("\n".join(lines[-40:] if f == "poll.txt" else lines))
    ld = done["load"]
    load_line = ("offered load   : %s  (%d orders offered, %d refused, %d busy; at most %g%% may "
                 "be turned away)" % ("PASS" if ld["pass"] else "FAIL", ld["offered"],
                                      ld["refused"], ld["busy"], 100 * (1 - float(a.target))))
    print("\n==> " + load_line)
    # every verdict line, each with the threshold it was judged against
    with open(s.path("verdict.txt")) as fh:
        lines = [ln.strip() for ln in fh if " PASS" in ln or " FAIL" in ln]
    for ln in lines + [load_line]:
        s.note(ln, "soak,verdict," + ("fail" if " FAIL" in ln else "pass"))
    with open(s.path("DONE"), "w") as fh:
        json.dump(done, fh, indent=2)
    try:
        with open(s.path("verdict.json")) as fh:
            verdict_json = json.load(fh)
    except (OSError, ValueError):
        verdict_json = None
    s.push(soak_metrics.result(done, verdict_json, float(a.target), time.time()))
    s.log("DONE %s: %s" % ("PASS" if done["pass"] else "FAIL", json.dumps(done)))
    s.note("soak %s: poll %s, parity %s, verdict %s, offered load %s (%s)" % (
        "PASS" if done["pass"] else "FAIL", done["poll"], done["parity"], done["verdict"],
        "PASS" if done["load"]["pass"] else "FAIL", out),
        "soak,verdict," + ("pass" if done["pass"] else "fail"), start=t_soak, end=time.time())
    return 0 if done["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
