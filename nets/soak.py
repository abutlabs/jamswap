#!/usr/bin/env python3
"""The epic's shared acceptance (A1-A4, jamswap#22) as one command, on a running net
whose DEX runs on JIP-2 (the dex, loadgen and netwatch services nets/netgen.py adds):

    python3 nets/soak.py NET [--secs 600] [--drain 180] [--out DIR]
    ./dex soak NET=<name> [SECS]

  1. waits for the DEX API and reads the service id the dex deployed (its deploy state);
  2. starts the load generator, and `netwatch poll` over every node for SECS + DRAIN
     seconds, with --require-finality on a GRANDPA net: A1 one head, A2 finality;
  3. after SECS stops the load, so the last rounds settle within DRAIN;
  4. `netwatch parity` at the common finalized head: A3, the service state (books,
     balances, custody, registry, landed-round markers) identical on every node;
  5. `soak_verdict.py` over the dex's order event log with --chain and --parity: A4,
     clearing SLO >= 0.9999, sealed zero-loss, and the chain half folded in.

Run it on a freshly started net (`./dex up`): the verdict judges the dex's whole order
event log. Everything lands in DIR (default ~/.cache/jamswap/soak/<net>-<UTC time>):
poll.txt, chain.jsonl, parity.txt, parity.json, order_events.jsonl, verdict.txt,
verdict.json, soak.log, and DONE (written last: each step's exit status). Exit 0 iff
the poll, the parity probe and the soak verdict all pass.
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
            s.dc("stop", "loadgen", timeout=60, check=False)
            s.log("loadgen off; draining")
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

    done["pass"] = done["poll"] == 0 and done["parity"] == 0 and done["verdict"] == 0
    done["finished"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for f in ("poll.txt", "parity.txt", "verdict.txt"):
        print("\n==> %s" % f)
        with open(s.path(f)) as fh:
            lines = fh.read().splitlines()
        print("\n".join(lines[-40:] if f == "poll.txt" else lines))
    with open(s.path("DONE"), "w") as fh:
        json.dump(done, fh, indent=2)
    s.log("DONE %s: %s" % ("PASS" if done["pass"] else "FAIL", json.dumps(done)))
    return 0 if done["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
