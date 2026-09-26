#!/usr/bin/env python3
"""One head? Poll every validator of a running net and say whether they agree.

    python3 nets/onehead.py NET [--secs 300] [--every 6] [--jsonl FILE]
    ./dex heads NET=<name> [SECS]

Each sample reads every node's best block (and finalized block) and classifies it:

  SAME   every node reports the same best block hash;
  LAG    the heads differ, but every lower head is an ancestor of the highest one
         (checked by walking JIP-2 `parent` from the highest head), i.e. one chain
         with some nodes a block or two behind;
  FORK   some head is NOT on the highest head's chain (different blocks);
  DOWN   a node did not answer.

A FORK that is gone by the next sample is a re-org at the tip; one that persists is a
split. Finality: every node's finalized block must lie on one chain and advance.

How each client is read (black box, public interfaces only):
  PolkaJam / JavaJAM / pbnjam   JIP-2 RPC over WebSocket on the host port the net
                                publishes (offchain/jip2.py); the hand-written mixed net
                                publishes only pj0, so its PolkaJam nodes are asked from
                                inside their containers (JSON-RPC over HTTP, curl)
  lasair                        no JIP-2 RPC yet (lasair#68): its `STATUS height= head=
                                root= slot=` log line (best block) and its
                                lasair_finalized_* metrics (no finalized hash)
Exit 0 when no fork persisted, no node was down at the end, every node's head advanced
and no two finalized blocks conflict; whether finality advanced is reported beside it.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "offchain"))
import netgen  # noqa: E402
import jip2  # noqa: E402

STATUS_RE = re.compile(r"STATUS height=(\d+) head=0x([0-9a-f]+) root=0x[0-9a-f]+ slot=(\d+)")
# the hand-written nets: which RPC port a PolkaJam node serves inside its container
LEGACY_RPC_BASE = {"mixed": 19890}


class Jip2Probe:
    """JIP-2 over WebSocket (a host port)."""
    can_walk = True

    def __init__(self, url):
        self.url, self.c = url, jip2.Jip2Client(url, timeout=5)

    def _call(self, method, *params):
        return self.c.call(method, *params)

    @staticmethod
    def _desc(r):
        return None if r is None else (r["slot"], base64.b64decode(r["header_hash"]).hex())

    def best(self):
        return self._desc(self._call("bestBlock"))

    def finalized(self):
        return self._desc(self._call("finalizedBlock"))

    def parent(self, h):
        return self._desc(self._call("parent", base64.b64encode(bytes.fromhex(h)).decode()))


class ExecProbe(Jip2Probe):
    """JSON-RPC over HTTP from inside the node's container (RPC not published)."""

    def __init__(self, container, port):
        self.container, self.port = container, port

    def _call(self, method, *params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)})
        out = subprocess.run(["docker", "exec", self.container, "curl", "-s", "-m", "4", "-X",
                              "POST", "-H", "content-type: application/json", "-d", body,
                              "http://127.0.0.1:%d" % self.port],
                             capture_output=True, text=True, timeout=10).stdout
        msg = json.loads(out)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg["result"]


class LasairProbe:
    """lasair: the STATUS log line (best block) and the finality metrics."""
    can_walk = False

    def __init__(self, container):
        self.container, self.seen = container, {}      # slot -> hash, from every STATUS line

    def best(self):
        out = subprocess.run(["docker", "logs", "--since", "90s", self.container],
                             capture_output=True, text=True, timeout=20)
        last = None
        for m in STATUS_RE.finditer(out.stdout + out.stderr):
            _, head, slot = m.groups()
            self.seen[int(slot)] = head
            last = (int(slot), head)
        if last is None:
            raise RuntimeError("no STATUS line in the last 90 s")
        return last

    def finalized(self):
        out = subprocess.run(["docker", "exec", self.container, "curl", "-s", "-m", "4",
                              "http://127.0.0.1:9615/metrics"],
                             capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"^lasair_finalized_slot (\S+)", out, re.M)
        return None if m is None else (int(float(m.group(1))), None)

    def ancestor_at(self, slot):
        return self.seen.get(slot)


def probes(net):
    proj = netgen.project(net)
    out = []
    for n in netgen.nodes(net):
        name = n["service"]
        container = "%s-%s-1" % (proj, name)
        if n["client"] == "lasair":
            out.append((name, LasairProbe(container)))
        elif netgen.generated(net):
            out.append((name, Jip2Probe("ws://127.0.0.1:%d" % n["rpc"])))
        else:
            out.append((name, ExecProbe(container, LEGACY_RPC_BASE[net] + n["index"])))
    return out


def on_chain(tip_probe, tip, other, cache):
    """Is block `other` (slot, hash) on the chain ending at `tip`? True / False / None."""
    slot, h = other
    if tip[1] == h:
        return True
    if tip[0] <= slot:
        return False                       # same or higher slot, different block
    if not tip_probe.can_walk:
        seen = tip_probe.ancestor_at(slot)
        return None if seen is None else seen == h
    cur = tip
    for _ in range(64):
        if cur[0] <= slot:
            return cur == (slot, h)
        nxt = cache.get(cur[1])
        if nxt is None:
            nxt = tip_probe.parent(cur[1])
            if nxt is None:
                return False
            cache[cur[1]] = nxt
        cur = nxt
    return None


def sample(ps, cache):
    heads, fins, down = {}, {}, []
    for name, p in ps:
        try:
            heads[name] = p.best()
            fins[name] = p.finalized()
        except Exception as e:               # noqa: BLE001 - any failure = node down
            down.append("%s(%s)" % (name, str(e)[:60]))
    verdict, forked = "SAME", []
    if heads:
        tip_name = max(heads, key=lambda k: heads[k][0])
        tip_probe = dict(ps)[tip_name]
        tip = heads[tip_name]
        for name, h in heads.items():
            if h == tip:
                continue
            ok = on_chain(tip_probe, tip, h, cache)
            if ok is False:
                forked.append(name)
            elif verdict == "SAME":
                verdict = "LAG"
        if forked:
            verdict = "FORK"
    if down:
        verdict = "DOWN"
    # finality: every finalized block (with a hash) on the chain of the highest one
    fin_conflict = []
    with_hash = {k: v for k, v in fins.items() if v and v[1]}
    if with_hash:
        top = max(with_hash, key=lambda k: with_hash[k][0])
        for name, f in with_hash.items():
            if on_chain(dict(ps)[top], with_hash[top], f, cache) is False:
                fin_conflict.append(name)
    return {"t": time.time(), "verdict": verdict, "heads": heads, "fins": fins,
            "down": down, "forked": forked, "fin_conflict": fin_conflict}


def fmt(s, t0):
    slots = sorted({h[0] for h in s["heads"].values()})
    hashes = sorted({h[1][:12] for h in s["heads"].values()})
    fin = sorted({f[0] for f in s["fins"].values() if f})
    line = "+%4ds %-4s best=%s %s fin=%s" % (
        s["t"] - t0, s["verdict"], "/".join(map(str, slots)) or "-", ",".join(hashes),
        "/".join(map(str, fin)) or "-")
    if s["forked"]:
        line += " FORKED=" + ",".join("%s@%d:%s" % (k, s["heads"][k][0], s["heads"][k][1][:12])
                                      for k in s["forked"])
    if s["down"]:
        line += " DOWN=" + ",".join(s["down"])
    if s["fin_conflict"]:
        line += " FINALITY-CONFLICT=" + ",".join(s["fin_conflict"])
    return line


def summarize(samples, names):
    """Print the verdict; True when the heads were one chain throughout. A node counts
    from its first answer (a node still starting is DOWN, not a failure), but must be
    up at the end."""
    n = len(samples)
    count = {v: sum(1 for s in samples if s["verdict"] == v) for v in ("SAME", "LAG", "FORK", "DOWN")}
    run, worst = 0, 0
    for s in samples:
        run = run + 1 if s["verdict"] == "FORK" else 0
        worst = max(worst, run)
    last = samples[-1]

    def span(key, k):
        seen = [s[key][k][0] for s in samples if s[key].get(k)]
        return (seen[0], seen[-1]) if seen else (None, None)
    advanced = {k: span("heads", k) for k in names}
    fin_adv = {k: span("fins", k) for k in names}
    fin_conf = sum(1 for s in samples if s["fin_conflict"])
    # one sample (./dex status) can't show progress: judge agreement only
    heads_ok = n == 1 or all(b is not None and b > a for a, b in advanced.values())
    finalizing = any(b for _, b in fin_adv.values())
    fin_all = all(b is not None and a is not None and b > a for a, b in fin_adv.values())
    ok = worst <= 2 and last["verdict"] in ("SAME", "LAG") and heads_ok and fin_conf == 0
    print("---")
    print("samples %d over %ds: SAME %d, LAG %d, FORK %d (longest run %d), DOWN %d"
          % (n, last["t"] - samples[0]["t"], count["SAME"], count["LAG"], count["FORK"], worst,
             count["DOWN"]))
    print("best slot  per node (first answer -> last): " + ", ".join(
        "%s %s->%s" % (k, a, b) for k, (a, b) in advanced.items()))
    print("finalized  per node (first answer -> last): " + ", ".join(
        "%s %s->%s" % (k, a, b) for k, (a, b) in fin_adv.items()))
    print("HEADS:    %s" % ("ONE HEAD" if ok else "NOT ONE HEAD"))
    print("FINALITY: %s" % (
        "CONFLICT in %d samples" % fin_conf if fin_conf else
        "advancing on every node, one chain" if finalizing and fin_all else
        "advancing on some nodes only" if finalizing else
        "not advancing (no node finalized past its first reading)"))
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("net")
    ap.add_argument("--secs", type=int, default=300)
    ap.add_argument("--every", type=float, default=6.0)
    ap.add_argument("--jsonl", help="also append every sample to this file")
    a = ap.parse_args()
    ps = probes(a.net)
    names = [k for k, _ in ps]
    print("probing %s: %s" % (a.net, ", ".join(names)))
    samples, cache, t0 = [], {}, time.time()
    while True:
        s = sample(ps, cache)
        samples.append(s)
        print(fmt(s, t0), flush=True)
        if a.jsonl:
            with open(a.jsonl, "a") as f:
                f.write(json.dumps(s) + "\n")
        if time.time() - t0 >= a.secs:
            break
        time.sleep(max(0.0, a.every - (time.time() - s["t"])))
    sys.exit(0 if summarize(samples, names) else 1)


if __name__ == "__main__":
    main()
