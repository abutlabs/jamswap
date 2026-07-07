#!/usr/bin/env python3
"""Prometheus exporter for the PolkaJam side of the mixed network.

lasair >=1.6.4 serves /metrics natively (prometheus.yml's `lasair` job);
PolkaJam is a black box with no Prometheus endpoint (probed: no --prometheus
flag, only the RPC listens). This exporter derives its metrics from what IS
observable, stdlib-only:

  - the Docker Engine API (unix socket, read-only) — incremental log tails of
    the pj containers (authored / imported / net status);
  - the JSON-RPC — bestBlock, finalizedBlock, syncState, and `statistics`:
    the on-chain GP validator statistics (pi), per-validator counters recorded
    by CONSENSUS for both clients' validators — the apples-to-apples baseline
    every client-side counter can be compared against.

Serves /metrics on :9105. Log-derived counters reset when the exporter
restarts — which Prometheus's rate()/increase() handle fine.
"""
import base64
import http.client
import http.server
import json
import os
import re
import socket
import struct
import threading
import time
import urllib.request

DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT", "jamswap")
PJ_RPC = os.environ.get("PJ_RPC", "http://172.28.0.10:19890")
PORT = int(os.environ.get("EXPORTER_PORT", "9105"))
# PolkaJam only: lasair >=1.6.4 serves Prometheus /metrics natively
# (--metrics-port; see prometheus.yml's `lasair` job), so parsing its logs here
# would double-count. This exporter covers what stays a black box.
NODES = {"pj0": "polkajam", "pj1": "polkajam", "pj2": "polkajam"}

# ---- docker engine API over the unix socket (stdlib http.client) -----------
class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.unix_path)
        self.sock = s


def docker_get(path):
    conn = UnixHTTPConnection(DOCKER_SOCK)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        body = r.read()
        if r.status != 200:
            return None
        return body
    finally:
        conn.close()


def demux_logs(raw):
    """Docker multiplexes non-tty logs into 8-byte-header frames; tty logs are raw."""
    if not raw:
        return ""
    if raw[:1] in (b"\x00", b"\x01", b"\x02") and len(raw) >= 8:
        out, i = [], 0
        while i + 8 <= len(raw):
            n = int.from_bytes(raw[i + 4:i + 8], "big")
            out.append(raw[i + 8:i + 8 + n])
            i += 8 + n
        return b"".join(out).decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


def find_containers():
    filters = json.dumps({"label": ["com.docker.compose.project=" + COMPOSE_PROJECT]})
    body = docker_get("/containers/json?filters=" + urllib.request.quote(filters))
    if body is None:
        return {}
    out = {}
    for c in json.loads(body):
        svc = c.get("Labels", {}).get("com.docker.compose.service", "")
        if svc in NODES:
            out[svc] = c["Id"]
    return out


# ---- metric state -----------------------------------------------------------
LOCK = threading.Lock()
counters = {}   # (name, labels-tuple) -> float
gauges = {}     # (name, labels-tuple) -> float
last_ts = {}    # service -> last processed RFC3339Nano timestamp string
last_since = {} # service -> unix seconds used for the next `since`


def bump(name, labels, n=1):
    counters[(name, tuple(sorted(labels.items())))] = \
        counters.get((name, tuple(sorted(labels.items()))), 0) + n


def setg(name, labels, v):
    gauges[(name, tuple(sorted(labels.items())))] = v


# what we recognise in PolkaJam's log lines
RE_PJ_NET = re.compile(r"Net status: (\d+) peers \((\d+) vals\)")


def parse_line(svc, client, line):
    if "Authored block" in line:
        bump("jam_authored_total", {"node": svc, "client": client})
        return
    if "Imported 0x" in line:
        bump("jam_pj_imported_total", {"node": svc})
        return
    m = RE_PJ_NET.search(line)
    if m:
        setg("jam_pj_peers", {"node": svc}, int(m.group(1)))
        setg("jam_pj_vals", {"node": svc}, int(m.group(2)))


def poll_logs():
    containers = find_containers()
    setg("jam_monitor_nodes", {}, len(containers))
    for svc, cid in containers.items():
        client = NODES[svc]
        since = last_since.get(svc, 0)
        raw = docker_get(
            "/containers/%s/logs?stdout=1&stderr=1&timestamps=1&since=%d" % (cid, since))
        if raw is None:
            continue
        floor = last_ts.get(svc, "")
        newest = floor
        for line in demux_logs(raw).splitlines():
            # "2026-07-07T18:40:42.007092000Z <payload>" — RFC3339Nano sorts lexically
            ts, _, payload = line.partition(" ")
            if not payload or ts <= floor:
                continue
            if ts > newest:
                newest = ts
            parse_line(svc, client, payload)
        last_ts[svc] = newest
        last_since[svc] = max(0, int(time.time()) - 2)


def rpc(method, params=[]):
    req = urllib.request.Request(
        PJ_RPC, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                            "params": params}).encode(),
        {"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=3))
    if "error" in r:
        raise RuntimeError(r["error"])
    return r["result"]


# validator index -> node name for the on-chain pi statistics (matches LAYOUT)
VALIDATOR_NODES = os.environ.get(
    "VALIDATOR_NODES", "pj0,pj1,pj2,lm3,lm4,lm5").split(",")
PI_FIELDS = ["blocks", "tickets", "preimages", "preimages_size",
             "guarantees", "assurances"]


def poll_rpc():
    try:
        best = rpc("bestBlock")
        setg("jam_head_slot", {}, int(best["slot"]))
        fin = rpc("finalizedBlock")
        setg("jam_finalized_slot", {"node": "pj0"}, int(fin["slot"]))
        sync = rpc("syncState")
        setg("jam_pj_peers", {"node": "pj0"}, int(sync["num_peers"]))
        setg("jam_rpc_up", {}, 1)
    except Exception:
        setg("jam_rpc_up", {}, 0)
        return
    # On-chain validator statistics (GP pi): per-validator counters recorded by
    # CONSENSUS — the same numbers from any node, for BOTH clients' validators.
    # This is the apples-to-apples baseline; a client's own counters can then be
    # compared against what the chain actually credited it with.
    # Encoding: 2 epochs (current, last) x V records of 6 u32 LE fields.
    try:
        raw = base64.b64decode(rpc("statistics", [best["header_hash"]]))
        v = len(VALIDATOR_NODES)
        if len(raw) >= 2 * v * 24:
            for half, epoch in ((0, "current"), (v * 24, "last")):
                for i, node in enumerate(VALIDATOR_NODES):
                    rec = struct.unpack_from("<6I", raw, half + i * 24)
                    for f, val in zip(PI_FIELDS, rec):
                        setg("jam_pi_" + f,
                             {"validator": str(i), "node": node, "epoch": epoch},
                             val)
    except Exception:
        pass


def render():
    lines = []
    for store, kind in ((counters, "counter"), (gauges, "gauge")):
        for (name, labels) in sorted(store):
            if not any(l[0] == name for l in lines):
                lines.append((name, "# TYPE %s %s" % (name, kind)))
            lab = ",".join('%s="%s"' % kv for kv in labels)
            lines.append((name, "%s%s %s" % (name, "{" + lab + "}" if lab else "", store[(name, labels)])))
    return "\n".join(l for _, l in lines) + "\n"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404); self.end_headers(); return
        with LOCK:
            try:
                poll_logs()
                poll_rpc()
                body = render().encode()
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(str(e).encode()); return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    # Zero-seed the counters so pj series exist from the first scrape (a series
    # that only appears once something happens graphs as "No data" baselines).
    for svc, client in NODES.items():
        bump("jam_authored_total", {"node": svc, "client": client}, 0)
        bump("jam_pj_imported_total", {"node": svc}, 0)
    print("jam mixed-net exporter on :%d (project=%s, rpc=%s)"
          % (PORT, COMPOSE_PROJECT, PJ_RPC), flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
