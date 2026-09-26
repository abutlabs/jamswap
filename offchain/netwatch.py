#!/usr/bin/env python3
"""netwatch: a client-neutral watch over every node of a JAM test net.

One tool for any net (all-lasair, all-PolkaJam, mixed): it polls each node, lines the
nodes' heads up, and judges the net's shared acceptance A1-A3 (issue #22):

  * one head       every node's best block agrees within N slots, and no divergence
                   (a fork, a lagging or unreachable node) lasts longer than one epoch;
  * liveness       every node's best block advances;
  * finality       where the net finalizes, every node's finalized head advances, never
                   goes back, never stalls longer than one epoch, and holds one hash per slot;
  * state parity   the service's keys read at the common finalized head digest the same
                   on every node (`parity`).

Nodes (--node NAME,CLIENT,URL[,READER], repeatable; or NETWATCH_NODES, specs separated
by spaces or ';'). The URL's scheme picks how the node is read, through the chain
adapter (chain.py) either way:

  ws:// wss://     JIP-2 node RPC (chain.Jip2Chain): bestBlock, finalizedBlock, parent,
                   syncState, statistics, parameters, serviceValue. Heads carry hashes.
  http:// https:// a node without JIP-2, read through its Prometheus endpoint
                   (chain.JamnpChain: lasair_slot, lasair_block_height,
                   lasair_finalized_slot, lasair_finalized_height). Heads carry a slot and
                   a height but no hash, so these nodes are compared by slot and height
                   only; READER is its CE-129 reader bridge, which reads state at the head
                   it follows (lasair until lasair#68 / lasair#70).

CLIENT is a label only ("polkajam", "lasair", "javajam" ...). --validators names the node
behind each validator index (NAME[:CLIENT],...) so the on-chain validator statistics
(GP pi: blocks each validator authored into the chain) are labelled by node and client.

Commands:

  poll     sample every --interval seconds for --duration seconds (or --count samples),
           print one line per sample, append samples to --samples, then judge the run;
           exit 0 iff the verdict passes
  serve    the same sampling as a Prometheus exporter on --port (/metrics, /verdict)
  verdict  judge a samples file (JSONL, as poll/serve write it)
  parity   digest the service's keys on every node at the common finalized head; exit 0
           iff at least two nodes digested and all digests agree

Metric names (serve): jam_best_slot, jam_finalized_slot, jam_best_height,
jam_finalized_height, jam_node_up, jam_peers, jam_head_lag_slots,
jam_finality_lag_slots, jam_head_agree, jam_final_agree, jam_best_hash48,
jam_finalized_hash48 (all {node, client}); jam_net_* for the net as a whole; jam_pi_*
for the validator statistics. `offchain/soak_verdict.py --chain/--parity` folds the
verdict and the parity result into the soak's exit code.

Stdlib only (plus the chain adapter). Clean-room: every client is read through its
public RPC or metrics endpoint only.
"""
import argparse
import collections
import concurrent.futures
import hashlib
import http.server
import json
import os
import struct
import sys
import threading
import time
import urllib.request
from typing import NamedTuple, Optional
from urllib.parse import urlsplit

import chain
import jip2

DEFAULT_MAX_LAG = 3          # slots a node's best block may trail the net's newest
DEFAULT_EPOCH_SLOTS = 12     # used when no node serves JIP-2 parameters() (tiny, as lasair6)
DEFAULT_SLOT_SECS = 6
PI_FIELDS = ("blocks", "tickets", "preimages", "preimages_size", "guarantees", "assurances")


# ---- nodes -------------------------------------------------------------------
class Node(NamedTuple):
    name: str
    client: str
    url: str
    reader: Optional[str] = None

    @property
    def kind(self):
        return "jip2" if urlsplit(self.url).scheme in ("ws", "wss") else "metrics"


def parse_node(spec):
    """NAME,CLIENT,URL[,READER_URL] -> Node."""
    parts = [p.strip() for p in spec.strip().split(",")]
    if len(parts) not in (3, 4) or not all(parts[:3]):
        raise ValueError(f"node {spec!r}: expected NAME,CLIENT,URL[,READER_URL]")
    name, client, url = parts[:3]
    if urlsplit(url).scheme not in ("ws", "wss", "http", "https"):
        raise ValueError(f"node {name}: {url!r} is neither ws(s):// (JIP-2) nor http(s):// (metrics)")
    reader = parts[3] if len(parts) == 4 and parts[3] else None
    if reader and urlsplit(reader).scheme not in ("http", "https"):
        raise ValueError(f"node {name}: reader {reader!r} is not an http(s):// URL")
    return Node(name, client, url, reader.rstrip("/") if reader else None)


def parse_nodes(specs):
    nodes = [parse_node(s) for s in specs]
    names = [n.name for n in nodes]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise ValueError(f"duplicate node names: {', '.join(dup)}")
    return nodes


def split_specs(text):
    return [s for s in text.replace(";", " ").split() if s]


def nodes_from_env(env=None):
    """NETWATCH_NODES, else the one node the DEX's own chain env names (CHAIN_BACKEND=jip2:
    CHAIN_RPC; jamnp: NODE_METRICS_URL with READER_URL), labelled NODE_CLIENT."""
    env = os.environ if env is None else env
    if env.get("NETWATCH_NODES", "").strip():
        return parse_nodes(split_specs(env["NETWATCH_NODES"]))
    backend = (env.get("CHAIN_BACKEND") or "jamnp").strip().lower()
    if backend == "jip2":
        url = env.get("CHAIN_RPC") or "ws://localhost:19800"
        return [Node("node0", env.get("NODE_CLIENT") or "jip2", url)]
    if env.get("NODE_METRICS_URL", "").strip():
        return [Node("node0", env.get("NODE_CLIENT") or "lasair", env["NODE_METRICS_URL"].strip(),
                     (env.get("READER_URL") or "").rstrip("/") or None)]
    return []


def parse_validators(text, nodes=()):
    """'pj0,pj1:polkajam,...' -> [(node, client)] by validator index; a bare name takes the
    client of the node of that name."""
    clients = {n.name: n.client for n in nodes}
    out = []
    for item in [s.strip() for s in (text or "").split(",") if s.strip()]:
        name, _, client = item.partition(":")
        out.append((name, client or clients.get(name, "unknown")))
    return out


# ---- one node, behind the chain adapter ---------------------------------------
def _block(b):
    if b is None:
        return None
    return {"slot": b.slot, "hash": b.hash.hex() if b.hash is not None else None, "height": b.height}


class Probe:
    """Reads one node: a JIP-2 node through chain.Jip2Chain, a node without JIP-2 through
    chain.JamnpChain (its Prometheus gauges for heads, its reader bridge for state)."""

    def __init__(self, node, timeout=5.0):
        self.node = node
        self.timeout = timeout
        if node.kind == "jip2":
            self.chain = chain.Jip2Chain(None, node.url, timeout=timeout)
        else:
            self.chain = chain.JamnpChain(None, reader_url=node.reader or "", metrics_url=node.url)

    def observe(self):
        """The node's best and finalized blocks (and peers, where JIP-2 serves them). Any
        failure marks the node down for this sample; it never stops the sampler."""
        out = {"client": self.node.client, "kind": self.node.kind, "up": False,
               "best": None, "final": None, "peers": None, "sync": None, "error": None}
        try:
            out["best"] = _block(self.chain.head())
            out["final"] = _block(self.chain.finalized())
        except Exception as e:                  # noqa: BLE001 - a monitor records, never crashes
            out["error"] = f"{type(e).__name__}: {e}"[:300]
            return out
        out["up"] = out["best"] is not None
        if self.node.kind == "jip2":
            try:
                s = self.chain.rpc.call("syncState")
                out["peers"], out["sync"] = int(s["num_peers"]), s.get("status")
            except Exception:                   # noqa: BLE001 - optional in JIP-2
                pass
        return out

    def ancestor_at(self, block, slot, max_hops):
        """The newest block at or below `slot` on the chain ending at `block` ({"slot",
        "hash"}), found by walking JIP-2 parent(); None past max_hops or on any error."""
        try:
            cur_slot, cur_hash = block["slot"], bytes.fromhex(block["hash"])
            for _ in range(max_hops + 1):
                if cur_slot <= slot:
                    return {"slot": cur_slot, "hash": cur_hash.hex()}
                d = self.chain.rpc.parent(cur_hash)
                cur_slot, cur_hash = int(d["slot"]), jip2.unb64(d["header_hash"])
        except Exception:                       # noqa: BLE001
            return None
        return None

    def parameters(self):
        p = self.chain.rpc.call("parameters")
        return p["V1"] if isinstance(p, dict) and isinstance(p.get("V1"), dict) else None

    def statistics(self, header_hash):
        """(pi_V, pi_L) at the block, or None if the node does not serve statistics."""
        try:
            r = self.chain.rpc.call("statistics", jip2.b64(bytes.fromhex(header_hash)))
            return decode_validator_stats(jip2.unb64(r)) if r else None
        except Exception:                       # noqa: BLE001 - optional in JIP-2
            return None

    # ---- state reads for the parity probe ----
    def read_keys_at(self, service_id, keys, header_hash):
        """JIP-2 serviceValue of every key at one block: [(key, value or None)]."""
        h = bytes.fromhex(header_hash)
        return [(k, self.chain.rpc.service_value(h, service_id, k)) for k in keys]

    def read_keys_at_reader_head(self, service_id, keys, attempts=3):
        """Every key through the reader bridge, all at ONE reader head: (pairs, head_hex).
        Re-read when the head moved in the middle of the read."""
        if not self.node.reader:
            raise chain.ChainUnsupported(f"{self.node.name}: no reader bridge to read state through")
        for _ in range(attempts):
            pairs, heads = [], set()
            for k in keys:
                url = f"{self.node.reader}/read?service={service_id}&key={k.hex()}"
                r = json.loads(urllib.request.urlopen(url, timeout=self.timeout).read())
                found = r.get("found", bool(r.get("value_hex")))
                pairs.append((k, bytes.fromhex(r.get("value_hex") or "") if found else None))
                heads.add(r.get("head_hex") or "")
            if len(heads) == 1:
                return pairs, heads.pop()
        raise chain.ChainError(f"{self.node.name}: the reader's head moved during every read")

    def close(self):
        rpc = getattr(self.chain, "rpc", None)
        if rpc is not None:
            rpc.close()


# ---- GP 0.8.0 validator statistics (pi_V, pi_L) --------------------------------
def decode_nat(buf, i=0):
    """GP general natural-number decoding (serialization.tex): (value, next offset)."""
    b = buf[i]
    n = 8 - (b ^ 0xFF).bit_length()            # leading one bits: how many bytes follow
    if n == 8:
        return int.from_bytes(buf[i + 1:i + 9], "little"), i + 9
    if i + 1 + n > len(buf):
        raise ValueError("truncated natural")
    hi = b & ((1 << (7 - n)) - 1)
    return (hi << (8 * n)) + int.from_bytes(buf[i + 1:i + 1 + n], "little"), i + 1 + n


def decode_validator_stats(raw):
    """C(13) = E(var(pi_V), var(pi_L), pi_C, pi_S) (GP 0.8.0 merklization.tex): each of
    pi_V, pi_L a length-prefixed sequence of E4(blocks, tickets, preimage count, preimage
    octets, guarantees, assurances). Returns (pi_V, pi_L) as lists of 6-tuples; the core
    and service statistics after them are not decoded."""
    out, i = [], 0
    for _ in range(2):
        n, i = decode_nat(raw, i)
        if i + 24 * n > len(raw):
            raise ValueError("truncated validator statistics")
        out.append([struct.unpack_from("<6I", raw, i + 24 * v) for v in range(n)])
        i += 24 * n
    return out[0], out[1]


class PiTracker:
    """Blocks (and the other pi fields) credited to each validator over a run: each
    finished epoch's final record (pi_L, read once when the epoch turns) folded into a
    running total. Epochs that pass between two samples are lost beyond the last one."""

    def __init__(self):
        self.epoch = None
        self.total = collections.defaultdict(lambda: [0] * len(PI_FIELDS))
        self.current = []

    def update(self, slot, epoch_slots, current, last):
        e = slot // epoch_slots
        if self.epoch is not None and e > self.epoch:
            for i, rec in enumerate(last):
                self.total[i] = [a + b for a, b in zip(self.total[i], rec)]
        if self.epoch is None or e >= self.epoch:
            self.epoch, self.current = e, list(current)

    def credited(self):
        """Per validator: the folded totals plus the current epoch so far."""
        n = max(len(self.current), max(self.total, default=-1) + 1)
        cur = {i: rec for i, rec in enumerate(self.current)}
        return {i: [a + b for a, b in zip(self.total[i], cur.get(i, (0,) * len(PI_FIELDS)))]
                for i in range(n)}


# ---- sampling -----------------------------------------------------------------
class Sampler:
    """Samples every node at once and lines their heads up: for the JIP-2 nodes, the hash
    of each one's newest block at or below the lowest best (and finalized) slot among them,
    so nodes polled a slot apart still compare like for like."""

    def __init__(self, nodes, timeout=5.0, max_lag=DEFAULT_MAX_LAG, epoch_slots=None,
                 slot_secs=None, validators=(), statistics=True):
        if not nodes:
            raise ValueError("no nodes to watch: pass --node or set NETWATCH_NODES")
        self.nodes = list(nodes)
        self.probes = [Probe(n, timeout) for n in self.nodes]
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(2, len(self.nodes)))
        self.max_lag = max_lag
        self._epoch_slots, self._slot_secs = epoch_slots, slot_secs
        self._params_from = None
        self.validators = list(validators)
        self.statistics = statistics
        self.pi = PiTracker()

    def close(self):
        for p in self.probes:
            p.close()
        self.pool.shutdown(wait=False)

    def _params(self, up):
        # the epoch and slot length: flags first, else the first JIP-2 node's parameters()
        if self._epoch_slots and self._slot_secs:
            return self._epoch_slots, self._slot_secs
        if self._params_from is None:
            for p in up:
                try:
                    v1 = p.parameters()
                except Exception:               # noqa: BLE001
                    continue
                if v1 and v1.get("epoch_period"):
                    self._epoch_slots = self._epoch_slots or int(v1["epoch_period"])
                    self._slot_secs = self._slot_secs or int(v1.get("slot_period_sec") or DEFAULT_SLOT_SECS)
                    self._params_from = p.node.name
                    break
        return self._epoch_slots or DEFAULT_EPOCH_SLOTS, self._slot_secs or DEFAULT_SLOT_SECS

    def _align(self, probes, obs, which, max_hops):
        if not probes:
            return None
        slot = min(obs[p.node.name][which]["slot"] for p in probes)

        def walk(p):
            b = obs[p.node.name][which]
            return p.ancestor_at(b, slot, max_hops) if b.get("hash") else None
        found = self.pool.map(walk, probes)
        return {"slot": slot, "blocks": {p.node.name: b for p, b in zip(probes, found)}}

    def sample(self):
        t = time.time()
        obs = dict(zip((n.name for n in self.nodes), self.pool.map(lambda p: p.observe(), self.probes)))
        up = [p for p in self.probes if p.node.kind == "jip2" and obs[p.node.name]["up"]
              and obs[p.node.name]["best"] and obs[p.node.name]["final"]]
        epoch_slots, slot_secs = self._params(up)
        s = {"t": round(t, 3), "epoch_slots": epoch_slots, "slot_secs": slot_secs, "nodes": obs,
             "head": self._align(up, obs, "best", 2 * self.max_lag + 4),
             "final": self._align(up, obs, "final", min(max(epoch_slots, 16), 64))}
        if self.statistics and up:
            for p in up:
                st = p.statistics(obs[p.node.name]["best"]["hash"])
                if st is not None:
                    s["pi"] = {"node": p.node.name, "slot": obs[p.node.name]["best"]["slot"],
                               "current": [list(r) for r in st[0]], "last": [list(r) for r in st[1]]}
                    self.pi.update(s["pi"]["slot"], epoch_slots, st[0], st[1])
                    break
        return s


# ---- judging --------------------------------------------------------------------
def _modal(values):
    """The value most nodes share, or None when the top count is tied."""
    c = collections.Counter(values).most_common()
    if not c or (len(c) > 1 and c[0][1] == c[1][1]):
        return None
    return c[0][0]


def judge_sample(s, max_lag=DEFAULT_MAX_LAG):
    """One sample's one-head picture: which nodes are down, how far each trails the newest
    best slot, whether the JIP-2 nodes hold one hash at the common slot (and nodes read
    by slot and height one height at a shared slot), and whether all of that is ok."""
    nodes = s.get("nodes") or {}
    up = {n: o for n, o in nodes.items() if o.get("up") and o.get("best")}
    down = sorted(set(nodes) - set(up))
    top = max((o["best"]["slot"] for o in up.values()), default=None)
    lag = {n: top - o["best"]["slot"] for n, o in up.items()}
    blocks = (s.get("head") or {}).get("blocks") or {}
    hashes = {n: b["hash"] for n, b in blocks.items() if b}
    unaligned = sorted(n for n, b in blocks.items() if not b)
    heights = collections.defaultdict(set)
    for o in up.values():
        if o["best"].get("hash") is None and o["best"].get("height") is not None:
            heights[o["best"]["slot"]].add(o["best"]["height"])
    height_split = sorted(slot for slot, hs in heights.items() if len(hs) > 1)
    heads = len(set(hashes.values()))
    worst = max(lag.values(), default=0)
    lagging = sorted(n for n, v in lag.items() if v > max_lag)
    split = heads > 1 or bool(height_split)
    fb = (s.get("final") or {}).get("blocks") or {}
    fhashes = {n: b["hash"] for n, b in fb.items() if b}
    return {"ok": bool(up) and not down and not split and not unaligned and not lagging,
            "top": top, "down": down, "lag": lag, "max_lag": worst, "lagging": lagging,
            "heads": heads, "split": split, "height_split": height_split, "unaligned": unaligned,
            "head_hash": _modal(list(hashes.values())), "hashes": hashes,
            "final_heads": len(set(fhashes.values())),
            "final_hash": _modal(list(fhashes.values())), "final_hashes": fhashes}


def _problems(j):
    kinds = []
    if j["down"]:
        kinds.append("down")
    if j["split"]:
        kinds.append("split")
    if j["lagging"] or j["unaligned"]:
        kinds.append("lag")
    return kinds


FINALITY_MODES = ("auto", "require", "report")


def verdict(samples, max_lag=DEFAULT_MAX_LAG, epoch_slots=None, slot_secs=None,
            finality="auto", final_stall_slots=None, require_authoring=False,
            min_peers=None):
    """Judge a run of samples (see Sampler.sample) against A1/A2: the report dict, with
    "pass" and one section per check. `finality`: "auto" judges finality where the net
    finalizes (a net that does not passes), "require" fails a net that does not, and
    "report" reports it without judging (a net whose clients do not share finality)."""
    if finality not in FINALITY_MODES:
        raise ValueError(f"finality must be one of {FINALITY_MODES}, not {finality!r}")
    samples = [s for s in samples if s.get("nodes")]
    if not samples:
        return {"pass": False, "reason": "no samples", "samples": 0}
    E = int(epoch_slots or samples[-1].get("epoch_slots") or DEFAULT_EPOCH_SLOTS)
    S = float(slot_secs or samples[-1].get("slot_secs") or DEFAULT_SLOT_SECS)
    stall_limit = final_stall_slots or E
    names = sorted({n for s in samples for n in s["nodes"]})
    clients = {n: o.get("client") for s in samples for n, o in s["nodes"].items()}
    kinds = {n: o.get("kind") for s in samples for n, o in s["nodes"].items()}
    judged = [judge_sample(s, max_lag) for s in samples]
    span_s = samples[-1]["t"] - samples[0]["t"]

    # -- one head: divergence episodes (runs of not-ok samples), measured first-bad to
    # last-bad sample, in slots of chain progress or of wall time, whichever is longer
    episodes, cur = [], None
    for s, j in zip(samples, judged):
        if j["ok"]:
            if cur:
                episodes.append(cur)
                cur = None
            continue
        top = j["top"] if j["top"] is not None else (cur["end_slot"] if cur else 0)
        if cur is None:
            cur = {"start_t": s["t"], "start_slot": top, "samples": 0, "kinds": [], "nodes": []}
        cur["end_t"], cur["end_slot"] = s["t"], top
        cur["samples"] += 1
        cur["kinds"] = sorted(set(cur["kinds"]) | set(_problems(j)))
        cur["nodes"] = sorted(set(cur["nodes"]) | set(j["down"]) | set(j["lagging"])
                              | set(j["unaligned"])
                              | ({n for n, h in j["hashes"].items() if h != j["head_hash"]}
                                 if j["split"] else set()))
    open_episode = cur is not None
    if cur:
        episodes.append(cur)
    for ep in episodes:
        ep["slots"] = round(max(ep["end_slot"] - ep["start_slot"], (ep["end_t"] - ep["start_t"]) / S), 1)
    too_long = [ep for ep in episodes if ep["slots"] > E]
    ok_samples = sum(j["ok"] for j in judged)
    hashed = any(k == "jip2" for k in kinds.values())
    one_head = {
        "pass": ok_samples > 0 and not too_long,
        "method": ("hash (JIP-2 nodes)" + (", slot/height (others)" if len(set(kinds.values())) > 1 else "")
                   if hashed else "slot/height only (no node serves hashes)"),
        "samples": len(samples), "ok_samples": ok_samples, "max_lag_param": max_lag,
        "max_lag_slots": max((j["max_lag"] for j in judged), default=0),
        "max_heads": max((j["heads"] for j in judged), default=0),
        "episodes": len(episodes), "longest_episode_slots": max((ep["slots"] for ep in episodes), default=0),
        "open_episode": open_episode, "epoch_slots": E,
        "failed_episodes": too_long[:10], "sample_episodes": episodes[:10],
    }

    # -- liveness: every node's best block advanced over the run
    first, last = {}, {}
    for s in samples:
        for n, o in s["nodes"].items():
            if o.get("up") and o.get("best"):
                first.setdefault(n, o["best"]["slot"])
                last[n] = o["best"]["slot"]
    advance = {n: (last[n] - first[n]) if n in first else None for n in names}
    long_enough = span_s >= 2 * S
    liveness = {"pass": (not long_enough) or all(a is not None and a > 0 for a in advance.values()),
                "advance_slots": advance, "span_s": round(span_s, 1)}
    if not long_enough:
        liveness["note"] = "run shorter than two slots: not judged"

    # -- finality: per node, monotone and advancing; one hash per finalized slot
    fin = {n: [] for n in names}               # (t, final slot, best slot, was down before)
    by_slot_hash = collections.defaultdict(lambda: collections.defaultdict(set))
    by_slot_height = collections.defaultdict(lambda: collections.defaultdict(set))
    was_down = {n: False for n in names}
    for s in samples:
        for n in names:
            o = s["nodes"].get(n)
            if not (o and o.get("up") and o.get("final") and o.get("best")):
                was_down[n] = True
                continue
            f = o["final"]
            fin[n].append((s["t"], f["slot"], o["best"]["slot"], was_down[n]))
            was_down[n] = False
            if f.get("hash"):
                by_slot_hash[f["slot"]][f["hash"]].add(n)
            elif f.get("height") is not None:
                by_slot_height[f["slot"]][f["height"]].add(n)
        for n, b in ((s.get("final") or {}).get("blocks") or {}).items():
            if b:
                by_slot_hash[b["slot"]][b["hash"]].add(n)
    per_node, regressions = {}, []
    for n, obs in fin.items():
        if not obs:
            per_node[n] = {"advanced": None, "observations": 0}
            continue
        stall, run_start = 0.0, obs[0]
        for prev, cur_ in zip(obs, obs[1:]):
            if cur_[1] < prev[1] and not cur_[3]:
                regressions.append({"node": n, "from": prev[1], "to": cur_[1], "t": cur_[0]})
            if cur_[1] != run_start[1]:
                run_start = cur_
            else:
                stall = max(stall, cur_[2] - run_start[2], (cur_[0] - run_start[0]) / S)
        per_node[n] = {"first": obs[0][1], "last": obs[-1][1], "advanced": obs[-1][1] > obs[0][1],
                       "longest_stall_slots": round(stall, 1), "lag_slots": obs[-1][2] - obs[-1][1],
                       "observations": len(obs)}
    conflicts = [{"slot": slot, "hashes": {h: sorted(ns) for h, ns in hs.items()}}
                 for slot, hs in sorted(by_slot_hash.items()) if len(hs) > 1]
    conflicts += [{"slot": slot, "heights": {str(h): sorted(ns) for h, ns in hs.items()}}
                  for slot, hs in sorted(by_slot_height.items()) if len(hs) > 1]
    runs = any(v.get("advanced") for v in per_node.values())
    stalled = sorted(n for n, v in per_node.items() if v.get("longest_stall_slots", 0) > stall_limit)
    not_advanced = sorted(n for n, v in per_node.items() if not v.get("advanced"))
    if runs:
        fpass = not not_advanced and not conflicts and not regressions and not stalled
        status = "finalizing"
    else:
        # required finality cannot be judged on a run shorter than two slots
        fpass = not (finality == "require" and long_enough) and not conflicts and not regressions
        status = "not finalizing"
    judged_finality = finality != "report"
    finality = {"pass": fpass, "status": status, "mode": finality,
                "required": finality == "require", "judged": judged_finality,
                "stall_limit_slots": stall_limit, "not_advanced": not_advanced if runs else [],
                "stalled": stalled, "conflicts": conflicts[:10], "regressions": regressions[:10],
                "nodes": per_node,
                "hash_checked_slots": len(by_slot_hash)}

    out = {"pass": one_head["pass"] and liveness["pass"] and (finality["pass"] or not judged_finality),
           "samples": len(samples), "span_s": round(span_s, 1), "epoch_slots": E, "slot_secs": S,
           "nodes": {n: {"client": clients[n], "kind": kinds[n]} for n in names},
           "one_head": one_head, "liveness": liveness, "finality": finality}

    # -- authoring: blocks credited by consensus (GP pi) to every validator over the run
    pi_samples = [s for s in samples if s.get("pi")]
    if pi_samples:
        tr = PiTracker()
        for s in pi_samples:
            tr.update(s["pi"]["slot"], s.get("epoch_slots") or E, s["pi"]["current"], s["pi"]["last"])
        credited = tr.credited()
        blocks = {str(i): rec[0] for i, rec in sorted(credited.items())}
        idle = sorted(i for i, b in blocks.items() if b == 0)
        out["authoring"] = {"pass": not (require_authoring and (idle or not blocks)),
                            "required": require_authoring, "blocks": blocks, "idle": idle,
                            "from": pi_samples[-1]["pi"].get("node")}
        out["pass"] = out["pass"] and out["authoring"]["pass"]
    elif require_authoring:
        out["authoring"] = {"pass": False, "required": True,
                            "reason": "no node served validator statistics"}
        out["pass"] = False

    # -- peers (JIP-2 syncState) at the last sample
    if min_peers is not None:
        peers = {n: o.get("peers") for n, o in samples[-1]["nodes"].items() if o.get("kind") == "jip2"}
        few = sorted(n for n, p in peers.items() if p is None or p < min_peers)
        out["peers"] = {"pass": not few, "min": min_peers, "peers": peers, "below": few}
        out["pass"] = out["pass"] and out["peers"]["pass"]
    return out


def load_samples(path):
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                continue                       # a torn last line of a live file
            if isinstance(s, dict) and s.get("nodes"):
                out.append(s)
    return out


def load_chain_report(path, **opts):
    """A chain verdict from `path`: a verdict JSON (netwatch verdict --json, /verdict) as
    is, or a samples JSONL judged here."""
    with open(path) as fh:
        text = fh.read()
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        doc = None
    if isinstance(doc, dict) and "one_head" in doc:
        return doc
    return verdict(load_samples(path), **opts)


# ---- state parity --------------------------------------------------------------
TREASURY = 0xFFFFFFFF          # the service's fee account handle (lib.rs)


def _p32(x):
    return struct.pack("<I", x)


def service_keys(markets=(1, 2, 3), accounts=range(1, 7), assets=(0, 1, 2), rounds=(), extra=()):
    """The jamswap service keys the parity probe reads (layout: service/src/lib.rs): the
    registry counters, per market its listing, book, cumulative volume, last price and
    sealed-order sets, per asset custody, per account its balances, key, nonce and seq
    floors (and the fee treasury's balances), the landed-round markers named, and any
    extra keys. Absent keys are part of the digest too."""
    keys = [b"markets", b"nexthandle", b"govnonce", b"comnonce", b"committee"]
    for m in markets:
        keys += [p + _p32(m) for p in (b"mkt", b"book", b"cv", b"lp", b"commits", b"cage", b"encset")]
    keys += [b"cust" + _p32(a) for a in assets]
    for h in list(accounts) + [TREASURY]:
        keys += [b"b" + _p32(a) + _p32(h) for a in assets]
        if h != TREASURY:
            keys += [p + _p32(h) for p in (b"pk", b"nc", b"sq", b"sc")]
    keys += [b"rl" + bytes(r) for r in rounds]
    keys += [bytes(k) for k in extra]
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def state_digest(pairs):
    """blake2b-256 over the (key, value-or-absent) pairs in order."""
    h = hashlib.blake2b(digest_size=32)
    h.update(b"jamswap:v1:parity")
    for k, v in pairs:
        h.update(_p32(len(k)) + k)
        h.update(b"\x00" if v is None else b"\x01" + _p32(len(v)) + v)
    return h.hexdigest()


def rounds_from_dex(dex_url, accounts, timeout=5.0):
    """Round ids the DEX's fill receipts name (/api/executions): their landed markers."""
    rids = []
    for a in accounts:
        try:
            r = json.loads(urllib.request.urlopen(f"{dex_url.rstrip('/')}/api/executions?account={a}",
                                                  timeout=timeout).read())
        except Exception:                       # noqa: BLE001 - best effort
            continue
        for e in r.get("executions", []):
            try:
                rids.append(bytes.fromhex(e["round"]))
            except (KeyError, TypeError, ValueError):
                continue                         # a receipt without a round (jamnp backend)
    return list(dict.fromkeys(rids))


def _common_block(probes, at):
    """The block every JIP-2 node reads at — the lowest finalized (or best) head among the
    nodes that answer, so it is final (or known) on all; or an explicit header hash — and
    {node: error} for the nodes that did not answer."""
    if at not in ("final", "best"):
        return {"mode": "hash", "slot": None, "hash": bytes.fromhex(at.removeprefix("0x")).hex(),
                "from": None}, {}
    obs, errs = [], {}
    for p in probes:
        try:
            b = p.chain.finalized() if at == "final" else p.chain.head()
            obs.append((b.slot, b.hash.hex(), p.node.name))
        except Exception as e:                  # noqa: BLE001
            errs[p.node.name] = f"{type(e).__name__}: {e}"[:300]
    if not obs:
        return None, errs
    slot, h, name = min(obs)
    return {"mode": at, "slot": slot, "hash": h, "from": name}, errs


def parity(probes, service_id, keys, at="final", attempts=3, pause=6.0, diff_keys=20):
    """Digest `keys` of service `service_id` on every node: JIP-2 nodes at the common
    block (pinned), the others at their reader's head (not pinned). A mismatch among
    pinned nodes is final; one involving an unpinned node is retried (its head may just
    have moved), up to `attempts` times. A node without JIP-2 and without a reader of its
    own (none given, or one another node already names: one reader cannot stand for two
    nodes) is skipped and listed, not read."""
    jips = [p for p in probes if p.node.kind == "jip2"]
    rest, skipped, readers = [], {}, {}
    for p in probes:
        if p.node.kind == "jip2":
            continue
        if not p.node.reader:
            skipped[p.node.name] = "no reader bridge to read its state through (lasair#70)"
        elif p.node.reader in readers:
            skipped[p.node.name] = f"its reader is {readers[p.node.reader]}'s: not an independent read"
        else:
            readers[p.node.reader] = p.node.name
            rest.append(p)
    report = None
    for attempt in range(1, attempts + 1):
        target, terrs = _common_block(jips, at) if jips else (None, {})
        nodes, values = {}, {}

        def read(p):
            try:
                if p.node.kind == "jip2":
                    if p.node.name in terrs or target is None:
                        raise chain.ChainError(terrs.get(p.node.name) or "no JIP-2 node answered")
                    return p.node.name, p.read_keys_at(service_id, keys, target["hash"]), target["hash"], True, None
                pairs, head = p.read_keys_at_reader_head(service_id, keys)
                return p.node.name, pairs, head, False, None
            except Exception as e:              # noqa: BLE001
                return p.node.name, None, None, p.node.kind == "jip2", f"{type(e).__name__}: {e}"[:300]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(2, len(jips + rest))) as ex:
            results = list(ex.map(read, jips + rest))
        for (name, pairs, at_hash, pinned, err), p in zip(results, jips + rest):
            nodes[name] = {"client": p.node.client, "kind": p.node.kind, "pinned": pinned,
                           "at": at_hash, "digest": None, "present": None, "error": err}
            if pairs is not None:
                nodes[name]["digest"] = state_digest(pairs)
                nodes[name]["present"] = sum(v is not None for _, v in pairs)
                values[name] = dict(pairs)
        groups = collections.defaultdict(list)
        for name, v in nodes.items():
            if v["digest"]:
                groups[v["digest"]].append(name)
        mismatched = {}
        if len(groups) > 1:
            for k in keys:
                vals = {n: vs.get(k) for n, vs in values.items()}
                if len({v for v in vals.values()}) > 1:
                    mismatched[k.hex()] = {n: (v.hex()[:256] if v is not None else None)
                                           for n, v in vals.items()}
                    if len(mismatched) >= diff_keys:
                        break
        errors = sorted(n for n, v in nodes.items() if v["error"])
        digested = sum(1 for v in nodes.values() if v["digest"])
        ok = not errors and len(groups) == 1 and digested >= 2
        reason = ("all digests agree" if ok else
                  f"errors on {', '.join(errors)}" if errors else
                  f"only {digested} node(s) could be read: nothing to compare" if digested < 2 else
                  f"{len(groups)} different digests")
        report = {"pass": ok, "reason": reason, "service": service_id, "at": target,
                  "keys": len(keys), "attempt": attempt, "nodes": nodes, "skipped": skipped,
                  "groups": {d: sorted(ns) for d, ns in groups.items()},
                  "mismatched_keys": mismatched}
        if ok:
            break
        # only an unpinned read (or a node error) can change on a retry; a pinned mismatch
        # at one block hash is a divergence already
        pinned_split = len({nodes[n]["digest"] for n in nodes if nodes[n]["pinned"] and nodes[n]["digest"]}) > 1
        if pinned_split or len(jips + rest) < 2 or attempt == attempts:
            break
        time.sleep(pause)
    return report


# ---- Prometheus exposition ------------------------------------------------------
def _lbl(**kv):
    return "{" + ",".join('%s="%s"' % (k, str(v).replace("\\", r"\\").replace('"', r'\"'))
                          for k, v in kv.items()) + "}" if kv else ""


def _hash48(h):
    # the first 6 bytes of a hex header hash as an integer (exact in a float64), or None
    try:
        return int(h[:12], 16) if h else None
    except ValueError:
        return None


def _num(v):
    return str(int(v)) if isinstance(v, bool) or float(v).is_integer() else repr(float(v))


HELP = {
    "jam_node_up": "1 if the node answered this sample's head reads",
    "jam_best_slot": "slot of the node's best block",
    "jam_finalized_slot": "slot of the node's finalized block",
    "jam_best_height": "height of the node's best block (nodes that report heights)",
    "jam_finalized_height": "height of the node's finalized block (nodes that report heights)",
    "jam_best_hash48": "first 48 bits of the best block's header hash (JIP-2 nodes): equal = same block",
    "jam_finalized_hash48": "first 48 bits of the finalized block's header hash (JIP-2 nodes)",
    "jam_peers": "peers the node reports (JIP-2 syncState)",
    "jam_head_lag_slots": "slots the node's best block trails the net's newest best block",
    "jam_finality_lag_slots": "best slot minus finalized slot on the node",
    "jam_head_agree": "1 if the node's block at the common slot is the one most nodes hold (JIP-2 nodes)",
    "jam_final_agree": "1 if the node's block at the common finalized slot is the one most nodes hold",
    "jam_net_nodes": "nodes watched",
    "jam_net_nodes_up": "nodes that answered this sample",
    "jam_net_head_slot": "newest best slot among the nodes",
    "jam_net_finalized_slot": "lowest finalized slot among the nodes (final everywhere)",
    "jam_net_heads": "distinct blocks the JIP-2 nodes hold at the common slot (1 = one head)",
    "jam_net_final_heads": "distinct blocks the JIP-2 nodes hold at the common finalized slot",
    "jam_net_one_head": "1 if this sample is one head (all up, one block, lag within the bound)",
    "jam_net_divergence_slots": "length of the current divergence episode in slots (0 = none)",
    "jam_net_samples_total": "samples taken",
    "jam_net_diverged_samples_total": "samples that were not one head",
    "jam_finality_conflicts_total": "finalized slots seen with two different hashes (a safety failure)",
    "jam_netwatch_last_sample_time": "unix time of the last sample",
}


class Exporter:
    """The latest sample and running tallies, rendered as Prometheus text."""

    def __init__(self, sampler, window=8640, samples_path=None, verdict_opts=None):
        self.sampler = sampler
        self.samples = collections.deque(maxlen=window)
        self.samples_path = samples_path
        self.verdict_opts = verdict_opts or {}
        self.lock = threading.Lock()
        self.last, self.judged = None, None
        self.n = self.diverged = 0
        self.episode_start = None
        self.final_seen = collections.defaultdict(set)   # final slot -> hashes seen
        self.conflicts = 0

    def step(self):
        s = self.sampler.sample()
        j = judge_sample(s, self.sampler.max_lag)
        with self.lock:
            self.last, self.judged = s, j
            self.samples.append(s)
            self.n += 1
            if not j["ok"]:
                self.diverged += 1
                self.episode_start = self.episode_start or (s["t"], j["top"] or 0)
            else:
                self.episode_start = None
            blocks = [o["final"] for o in s["nodes"].values() if o.get("up") and o.get("final")]
            blocks += [b for b in ((s.get("final") or {}).get("blocks") or {}).values() if b]
            for b in blocks:
                if b.get("hash"):
                    seen = self.final_seen[b["slot"]]
                    if b["hash"] not in seen:
                        seen.add(b["hash"])
                        self.conflicts += len(seen) > 1
            if len(self.final_seen) > 4096:      # keep the newest slots only
                for k in sorted(self.final_seen)[:1024]:
                    del self.final_seen[k]
        if self.samples_path:
            append_sample(self.samples_path, s)
        return s, j

    def verdict(self):
        with self.lock:
            samples = list(self.samples)
        return verdict(samples, **self.verdict_opts)

    def render(self):
        with self.lock:
            s, j = self.last, self.judged
            n, diverged, conflicts, ep = self.n, self.diverged, self.conflicts, self.episode_start
            pi_total = {i: list(v) for i, v in self.sampler.pi.total.items()}
        lines, typed = [], set()

        def put(name, value, kind="gauge", **labels):
            if value is None:
                return
            if name not in typed:
                typed.add(name)
                if name in HELP:
                    lines.append(f"# HELP {name} {HELP[name]}")
                lines.append(f"# TYPE {name} {kind}")
            lines.append(f"{name}{_lbl(**labels)} {_num(value)}")

        put("jam_net_nodes", len(self.sampler.nodes))
        put("jam_net_samples_total", n, "counter")
        put("jam_net_diverged_samples_total", diverged, "counter")
        put("jam_finality_conflicts_total", conflicts, "counter")
        if s is None:
            return "\n".join(lines) + "\n"
        put("jam_netwatch_last_sample_time", s["t"])
        nodes = s["nodes"]
        put("jam_net_nodes_up", sum(1 for o in nodes.values() if o.get("up")))
        put("jam_net_head_slot", j["top"])
        finals = [o["final"]["slot"] for o in nodes.values() if o.get("up") and o.get("final")]
        put("jam_net_finalized_slot", min(finals) if finals else None)
        if s.get("head"):
            put("jam_net_heads", j["heads"])
        if s.get("final"):
            put("jam_net_final_heads", j["final_heads"])
        put("jam_net_one_head", int(j["ok"]))
        div = 0
        if ep is not None:
            div = max((j["top"] or 0) - ep[1], (s["t"] - ep[0]) / (s.get("slot_secs") or DEFAULT_SLOT_SECS))
        put("jam_net_divergence_slots", round(div, 1))
        for name in sorted(nodes):
            o = nodes[name]
            lb = {"node": name, "client": o.get("client")}
            put("jam_node_up", int(bool(o.get("up"))), **lb)
            if not o.get("up"):
                continue
            b, f = o.get("best") or {}, o.get("final") or {}
            put("jam_best_slot", b.get("slot"), **lb)
            put("jam_finalized_slot", f.get("slot"), **lb)
            put("jam_best_height", b.get("height"), **lb)
            put("jam_finalized_height", f.get("height"), **lb)
            put("jam_best_hash48", _hash48(b.get("hash")), **lb)
            put("jam_finalized_hash48", _hash48(f.get("hash")), **lb)
            put("jam_peers", o.get("peers"), **lb)
            put("jam_head_lag_slots", j["lag"].get(name), **lb)
            if b.get("slot") is not None and f.get("slot") is not None:
                put("jam_finality_lag_slots", b["slot"] - f["slot"], **lb)
            if name in j["hashes"]:
                put("jam_head_agree", int(j["hashes"][name] == j["head_hash"]), **lb)
            elif name in j["unaligned"]:
                put("jam_head_agree", 0, **lb)
            if name in j["final_hashes"]:
                put("jam_final_agree", int(j["final_hashes"][name] == j["final_hash"]), **lb)
        pi = s.get("pi")
        vals = self.sampler.validators
        if pi:
            for epoch, recs in (("current", pi["current"]), ("last", pi["last"])):
                for i, rec in enumerate(recs):
                    node, client = vals[i] if i < len(vals) else (f"v{i}", "unknown")
                    for fld, v in zip(PI_FIELDS, rec):
                        put("jam_pi_" + fld, v, validator=str(i), node=node, client=client, epoch=epoch)
        # every validator's counter exists from the first statistics read (zero until an
        # epoch turns), so a panel draws a baseline rather than "No data"
        n_val = max(len(pi["current"]) if pi else 0, max(pi_total, default=-1) + 1)
        for fld_i, fld in enumerate(PI_FIELDS):
            for i in range(n_val):
                node, client = vals[i] if i < len(vals) else (f"v{i}", "unknown")
                put(f"jam_pi_{fld}_cumulative_total", pi_total.get(i, [0] * len(PI_FIELDS))[fld_i],
                    "counter", validator=str(i), node=node, client=client)
        return "\n".join(lines) + "\n"


def serve(exporter, port, interval):
    def loop():
        while True:
            started = time.time()
            try:
                exporter.step()
            except Exception as e:              # noqa: BLE001 - keep serving
                print(f"netwatch: sample failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(max(0.0, interval - (time.time() - started)))
    threading.Thread(target=loop, daemon=True).start()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                body, ctype = exporter.render().encode(), "text/plain; version=0.0.4"
            elif self.path == "/verdict":
                body, ctype = json.dumps(exporter.verdict(), indent=2).encode(), "application/json"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    return srv


# ---- output ----------------------------------------------------------------------
def append_sample(path, s):
    try:
        with open(path, "a") as fh:
            fh.write(json.dumps(s, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"netwatch: cannot append to {path}: {e}", file=sys.stderr)


def _short(h):
    return (h or "?")[:8]


def sample_line(i, s, j):
    ts = time.strftime("%H:%M:%S", time.localtime(s["t"]))
    nodes = s["nodes"]
    up = sum(1 for o in nodes.values() if o.get("up"))
    finals = [o["final"]["slot"] for o in nodes.values() if o.get("up") and o.get("final")]
    head = f"best {j['top']}" if j["top"] is not None else "best ?"
    if s.get("head"):
        head += f" ({j['heads']} head{'s' if j['heads'] != 1 else ''} @{s['head']['slot']}: {_short(j['head_hash'])})"
    fin = f"final {min(finals)}..{max(finals)}" if finals else "final ?"
    if s.get("final"):
        fin += f" ({j['final_heads']} hash @{s['final']['slot']}: {_short(j['final_hash'])})"
    line = f"{ts} #{i} {head} lag<={j['max_lag']} {fin} up {up}/{len(nodes)}"
    if j["ok"]:
        return line + " ok"
    bits = []
    if j["down"]:
        bits.append("DOWN " + ",".join(j["down"]))
    if j["split"]:
        bits.append("SPLIT " + " ".join(f"{n}={_short(h)}" for n, h in sorted(j["hashes"].items()))
                    + (f" heights@{j['height_split']}" if j["height_split"] else ""))
    if j["lagging"] or j["unaligned"]:
        bits.append("LAG " + " ".join(f"{n}={j['lag'].get(n, '?')}" for n in sorted(set(j["lagging"]) | set(j["unaligned"]))))
    return line + " " + "; ".join(bits)


def print_verdict(v, out=None):
    out = out or sys.stdout

    def pf(ok):
        return "PASS" if ok else "FAIL"
    if "one_head" not in v:
        print(f"chain verdict       : FAIL ({v.get('reason')})", file=out)
        return
    oh, lv, fn = v["one_head"], v["liveness"], v["finality"]
    print(f"one head            : {pf(oh['pass'])}  ({oh['method']}; {oh['ok_samples']}/{oh['samples']} "
          f"samples ok, max lag {oh['max_lag_slots']} slots, {oh['episodes']} divergence episode(s), "
          f"longest {oh['longest_episode_slots']} slots vs epoch {oh['epoch_slots']})", file=out)
    for ep in oh["failed_episodes"][:3]:
        print(f"  too long          : {ep['slots']} slots {ep['kinds']} on {ep['nodes']}", file=out)
    adv = [a for a in lv["advance_slots"].values() if a is not None]
    still = sorted(n for n, a in lv["advance_slots"].items() if not a)
    print(f"liveness            : {pf(lv['pass'])}  (best advanced "
          f"{min(adv) if adv else '?'}..{max(adv) if adv else '?'} slots per node over {lv['span_s']} s"
          + (f"; {lv['note']}" if lv.get("note") else "")
          + (f"; not advancing: {still}" if still and not lv.get("note") else "") + ")", file=out)
    stalls = [x.get("longest_stall_slots", 0) for x in fn["nodes"].values()]
    print(f"finality            : {pf(fn['pass']) if fn.get('judged', True) else 'report only: ' + pf(fn['pass'])}"
          f"  ({fn['status']}" + (", required" if fn["required"] else "")
          + f"; {len(fn['conflicts'])} conflict(s), {len(fn['regressions'])} regression(s), "
          f"longest stall {max(stalls, default=0)} slots vs {fn['stall_limit_slots']}, "
          f"{fn['hash_checked_slots']} finalized slots hash-checked)", file=out)
    if fn["not_advanced"]:
        print(f"  not advancing     : {fn['not_advanced']}", file=out)
    for c in fn["conflicts"][:3]:
        print(f"  CONFLICT          : {c}", file=out)
    if "authoring" in v:
        a = v["authoring"]
        print(f"authoring (pi)      : {pf(a['pass'])}  (blocks per validator {a.get('blocks')}"
              + (f"; idle {a['idle']}" if a.get("idle") else "") + ")", file=out)
    if "peers" in v:
        p = v["peers"]
        print(f"peers               : {pf(p['pass'])}  ({p['peers']}, want >= {p['min']})", file=out)
    print(f"CHAIN VERDICT       : {pf(v['pass'])}", file=out)


def print_parity(r, out=None):
    out = out or sys.stdout
    at = r.get("at") or {}
    where = (f"{at.get('mode')} slot {at.get('slot')} 0x{_short(at.get('hash'))} (from {at.get('from')})"
             if at else "each node's reader head")
    print(f"state parity        : {'PASS' if r['pass'] else 'FAIL'}  ({r['reason']}; service "
          f"{r['service']}, {r['keys']} keys at {where}, attempt {r['attempt']})", file=out)
    for n, v in sorted(r["nodes"].items()):
        pin = "pinned" if v["pinned"] else "reader head 0x" + _short(v["at"])
        print(f"  {n:<10} {v['client']:<10} {(v['digest'] or '-')[:16]}  present {v['present']}  "
              f"({pin}){'  ERROR ' + v['error'] if v['error'] else ''}", file=out)
    for n, why in sorted((r.get("skipped") or {}).items()):
        print(f"  {n:<10} skipped: {why}", file=out)
    for k, vals in list(r["mismatched_keys"].items())[:5]:
        print(f"  DIFF key {k}: " + ", ".join(f"{n}={(x or 'absent')[:24]}" for n, x in vals.items()), file=out)


# ---- CLI ---------------------------------------------------------------------------
def _add_verdict_opts(ap):
    ap.add_argument("--max-lag", type=int, default=DEFAULT_MAX_LAG,
                    help=f"slots a node's best may trail the newest (default {DEFAULT_MAX_LAG})")
    ap.add_argument("--epoch-slots", type=int, default=None,
                    help="epoch length in slots (default: JIP-2 parameters(), else "
                         f"{DEFAULT_EPOCH_SLOTS})")
    ap.add_argument("--slot-secs", type=float, default=None, help="slot length in seconds")
    ap.add_argument("--finality", choices=FINALITY_MODES, default="auto",
                    help="auto: judge finality where the net finalizes (default); require: "
                         "fail a net whose finalized head does not advance; report: report "
                         "it without judging (clients that do not share finality)")
    ap.add_argument("--require-finality", dest="finality", action="store_const", const="require",
                    help="same as --finality require")
    ap.add_argument("--final-stall-slots", type=int, default=None,
                    help="longest a node's finalized head may stand still (default one epoch)")
    ap.add_argument("--require-authoring", action="store_true",
                    help="fail unless every validator was credited a block (GP pi statistics)")
    ap.add_argument("--min-peers", type=int, default=None,
                    help="fail if a JIP-2 node reports fewer peers at the last sample")


def verdict_opts(args):
    return {"max_lag": args.max_lag, "epoch_slots": args.epoch_slots, "slot_secs": args.slot_secs,
            "finality": args.finality, "final_stall_slots": args.final_stall_slots,
            "require_authoring": args.require_authoring, "min_peers": args.min_peers}


def _add_node_opts(ap):
    ap.add_argument("--node", action="append", default=[], metavar="NAME,CLIENT,URL[,READER]",
                    help="a node to watch (repeatable); default NETWATCH_NODES")
    ap.add_argument("--validators", default=os.environ.get("NETWATCH_VALIDATORS", ""),
                    help="node behind each validator index, NAME[:CLIENT],... (labels pi)")
    ap.add_argument("--timeout", type=float, default=5.0, help="seconds per RPC call")


def _nodes(args):
    return parse_nodes(args.node) if args.node else nodes_from_env()


def _parse_list(text, default):
    if not text:
        return list(default)
    out = []
    for part in text.split(","):
        a, _, b = part.strip().partition("-")
        out += list(range(int(a), int(b) + 1)) if b else [int(a)]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="netwatch", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("poll", help="sample for a while, then judge the run")
    _add_node_opts(p)
    _add_verdict_opts(p)
    p.add_argument("--interval", type=float, default=6.0)
    p.add_argument("--duration", type=float, default=None, help="seconds to sample")
    p.add_argument("--count", type=int, default=None, help="samples to take")
    p.add_argument("--samples", default=None, help="append every sample to this JSONL file")
    p.add_argument("--json", action="store_true", help="print the verdict as JSON")
    p.add_argument("--quiet", action="store_true", help="no per-sample lines")

    p = sub.add_parser("serve", help="Prometheus exporter (/metrics, /verdict)")
    _add_node_opts(p)
    _add_verdict_opts(p)
    p.add_argument("--port", type=int, default=int(os.environ.get("NETWATCH_PORT", "9106")))
    p.add_argument("--interval", type=float, default=float(os.environ.get("NETWATCH_INTERVAL", "5")))
    p.add_argument("--samples", default=os.environ.get("NETWATCH_SAMPLES") or None)
    p.add_argument("--window", type=int, default=8640, help="samples kept for /verdict (12 h at 5 s)")

    p = sub.add_parser("verdict", help="judge a samples file")
    p.add_argument("samples")
    _add_verdict_opts(p)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("parity", help="service state digest on every node")
    _add_node_opts(p)
    p.add_argument("--service", type=int, default=int(os.environ["SERVICE_ID"]) if os.environ.get("SERVICE_ID") else None)
    p.add_argument("--at", default="final", help="final (default), best, or a header hash")
    p.add_argument("--markets", default="1,2,3")
    p.add_argument("--accounts", default="1-6")
    p.add_argument("--assets", default="0,1,2")
    p.add_argument("--round", action="append", default=[], metavar="HEX", help="landed-round id")
    p.add_argument("--dex-url", default=None, help="take landed-round ids from the DEX's fill receipts")
    p.add_argument("--key", action="append", default=[], metavar="HEX", help="an extra key")
    p.add_argument("--attempts", type=int, default=3)
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", default=None, help="also write the result JSON here")
    args = ap.parse_args(argv)

    if args.cmd == "verdict":
        v = verdict(load_samples(args.samples), **verdict_opts(args))
        print(json.dumps(v, indent=2)) if args.json else print_verdict(v)
        return 0 if v["pass"] else 1

    nodes = _nodes(args)
    if not nodes:
        ap.error("no nodes: pass --node NAME,CLIENT,URL[,READER] or set NETWATCH_NODES")
    validators = parse_validators(args.validators, nodes)

    if args.cmd == "parity":
        if args.service is None:
            ap.error("parity needs --service (or SERVICE_ID)")
        accounts = _parse_list(args.accounts, range(1, 7))
        rounds = [bytes.fromhex(r.removeprefix("0x")) for r in args.round]
        if args.dex_url:
            rounds += rounds_from_dex(args.dex_url, accounts)
        keys = service_keys(_parse_list(args.markets, (1, 2, 3)), accounts,
                            _parse_list(args.assets, (0, 1, 2)), rounds,
                            [bytes.fromhex(k.removeprefix("0x")) for k in args.key])
        probes = [Probe(n, args.timeout) for n in nodes]
        try:
            r = parity(probes, args.service, keys, at=args.at, attempts=args.attempts)
        finally:
            for pr in probes:
                pr.close()
        if args.out:
            with open(args.out, "w") as fh:
                json.dump(r, fh, indent=2)
        print(json.dumps(r, indent=2)) if args.json else print_parity(r)
        return 0 if r["pass"] else 1

    sampler = Sampler(nodes, timeout=args.timeout, max_lag=args.max_lag, epoch_slots=args.epoch_slots,
                      slot_secs=args.slot_secs, validators=validators)
    if args.cmd == "serve":
        ex = Exporter(sampler, window=args.window, samples_path=args.samples, verdict_opts=verdict_opts(args))
        srv = serve(ex, args.port, args.interval)
        print(f"netwatch: {len(nodes)} node(s) on :{args.port} every {args.interval}s: "
              + ", ".join(f"{n.name}({n.client},{n.kind})" for n in nodes), flush=True)
        srv.serve_forever()
        return 0

    # poll
    samples, i, start = [], 0, time.time()
    try:
        while True:
            t0 = time.time()
            s = sampler.sample()
            samples.append(s)
            if args.samples:
                append_sample(args.samples, s)
            if not args.quiet:
                print(sample_line(i, s, judge_sample(s, args.max_lag)), file=sys.stderr if args.json else sys.stdout,
                      flush=True)
            i += 1
            if args.count is not None and i >= args.count:
                break
            if args.duration is not None and time.time() - start + args.interval > args.duration:
                break
            if args.count is None and args.duration is None and i >= 1:
                break
            time.sleep(max(0.0, args.interval - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        sampler.close()
    v = verdict(samples, **verdict_opts(args))
    print(json.dumps(v, indent=2)) if args.json else print_verdict(v)
    return 0 if v["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
