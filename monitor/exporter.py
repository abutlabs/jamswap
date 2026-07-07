#!/usr/bin/env python3
"""Prometheus exporter for the mixed lasair+PolkaJam network.

Neither client exposes metrics natively (lasair logs to stdout; PolkaJam is a
black box with a JSON-RPC), so this exporter derives them from what IS
observable, with no dependencies beyond the Python stdlib:

  - the Docker Engine API (unix socket, read-only) — incremental log tails of
    every compose service in NODES, parsed into counters/gauges;
  - pj0's JSON-RPC `bestBlock` — the canonical head slot.

Serves /metrics on :9105. Counters are accumulated in-process from log lines
strictly newer than the last processed timestamp (RFC3339Nano is lexically
ordered), so restarts of the exporter reset counters — which Prometheus's
rate()/increase() handle fine.
"""
import http.client
import http.server
import json
import os
import re
import socket
import threading
import time
import urllib.request

DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT", "jamswap")
PJ_RPC = os.environ.get("PJ_RPC", "http://172.28.0.10:19890")
PORT = int(os.environ.get("EXPORTER_PORT", "9105"))
NODES = {  # compose service -> client
    "lm3": "lasair", "lm4": "lasair", "lm5": "lasair",
    "pj0": "polkajam", "pj1": "polkajam", "pj2": "polkajam",
}

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


# what we recognise in each client's log lines
RE_LASAIR_STATUS = re.compile(r"^STATUS height=(\d+) .*slot=(\d+)")
RE_LASAIR_AUTHORED = re.compile(r"authored slot (\d+) \(val (\d+)\) height (\d+)")
RE_LASAIR_REJECT = re.compile(r"import rejected slot \d+ .*: (\w+)")
RE_LASAIR_POOL = re.compile(r"pool=(\d+)")
RE_PJ_FINALIZED = re.compile(r"Finalized 0x[0-9a-f]+\.* \(#(\d+)\)")
RE_PJ_NET = re.compile(r"Net status: (\d+) peers \((\d+) vals\)")


def parse_line(svc, client, line):
    if client == "lasair":
        m = RE_LASAIR_STATUS.search(line)
        if m:
            setg("jam_node_height", {"node": svc}, int(m.group(1)))
            setg("jam_node_slot", {"node": svc}, int(m.group(2)))
            return
        m = RE_LASAIR_AUTHORED.search(line)
        if m:
            bump("jam_authored_total", {"node": svc, "client": client})
            return
        m = RE_LASAIR_REJECT.search(line)
        if m:
            bump("jam_import_rejected_total", {"node": svc, "reason": m.group(1)})
            return
        if "accept error" in line:
            bump("jam_accept_errors_total", {"node": svc})
            return
        if "does not verify against on-chain gamma_z" in line:
            bump("jam_ring_failures_total", {"node": svc})
            return
        if "rejected on self-import" in line:
            bump("jam_selfimport_dropped_total", {"node": svc})
            return
        m = RE_LASAIR_POOL.search(line)
        if m:
            setg("jam_ticket_pool", {"node": svc}, int(m.group(1)))
    else:  # polkajam
        if "Authored block" in line:
            bump("jam_authored_total", {"node": svc, "client": client})
            return
        if "Imported 0x" in line:
            bump("jam_pj_imported_total", {"node": svc})
            return
        m = RE_PJ_FINALIZED.search(line)
        if m:
            setg("jam_finalized_slot", {"node": svc}, int(m.group(1)))
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


def poll_rpc():
    try:
        req = urllib.request.Request(
            PJ_RPC, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "bestBlock",
                                "params": []}).encode(),
            {"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=3))["result"]
        setg("jam_head_slot", {}, int(d["slot"]))
        setg("jam_rpc_up", {}, 1)
    except Exception:
        setg("jam_rpc_up", {}, 0)


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
    # Zero-seed the fault counters so a HEALTHY chain graphs flat zero lines
    # instead of "No data" (a series that only appears once something breaks
    # makes the faults panel unreadable as a baseline).
    for svc, client in NODES.items():
        if client == "lasair":
            for m in ("jam_accept_errors_total", "jam_ring_failures_total",
                      "jam_selfimport_dropped_total"):
                bump(m, {"node": svc}, 0)
            bump("jam_import_rejected_total", {"node": svc, "reason": "duplicate_package"}, 0)
    print("jam mixed-net exporter on :%d (project=%s, rpc=%s)"
          % (PORT, COMPOSE_PROJECT, PJ_RPC), flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
