#!/usr/bin/env python3
"""End-to-end canary for the jamswap stack (docs/OBSERVABILITY_PLAN.md phase 1).

Every CANARY_INTERVAL seconds: create a FRESH ed25519 account, register it through
the public DEX API, then faucet-deposit — polling on-chain state until each step
settles. This exercises the whole pipeline (API -> builder -> CE-133 -> guarantee ->
assurance -> accumulate -> CE-129 read) exactly the way a user does, so it catches
what per-component metrics can't: the system not working END TO END.

Exports on :9110/metrics (scraped by the monitor overlay's `jamswap` job):
  jamswap_canary_pass_total / jamswap_canary_fail_total{stage}
  jamswap_canary_duration_seconds (histogram, per full pass)
  jamswap_canary_last_pass_age_seconds (gauge; alert when it grows past ~2 intervals)

Env: DEX_URL (default http://dex:8080), CANARY_INTERVAL (300 s),
     CANARY_STEP_TIMEOUT (240 s per step).
"""
import json, os, struct, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nacl.signing import SigningKey
import metrics

DEX = os.environ.get("DEX_URL", "http://dex:8080").rstrip("/")
INTERVAL = int(os.environ.get("CANARY_INTERVAL", "300"))
STEP_TIMEOUT = int(os.environ.get("CANARY_STEP_TIMEOUT", "240"))
PORT = int(os.environ.get("CANARY_PORT", "9110"))

metrics.describe("jamswap_canary_pass_total", "full canary cycles that settled end-to-end")
metrics.describe("jamswap_canary_fail_total", "canary cycles that failed, by stage")
metrics.describe("jamswap_canary_duration_seconds", "wall time of a full passing canary cycle")

_last_pass = [0.0]
metrics.gauge_fn("jamswap_canary_last_pass_age_seconds",
                 "seconds since the last fully-passing canary cycle (0 before the first)",
                 lambda: (time.time() - _last_pass[0]) if _last_pass[0] else 0.0)


def _req(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(DEX + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if data else "GET")
    return json.load(urllib.request.urlopen(req, timeout=30))


def _wait(pred, budget):
    t0 = time.time()
    while time.time() - t0 < budget:
        try:
            if pred():
                return True
        except Exception:
            pass                       # dex restarting / reader hiccup: keep polling
        time.sleep(3)
    return False


def cycle():
    sk = SigningKey.generate()
    pub = sk.verify_key.encode()
    stage = "register"
    try:
        sig = sk.sign(b"jamswap:v1:register" + pub).signature
        _req("/api/register", {"pubkey": pub.hex(), "sig": sig.hex()})
        if not _wait(lambda: _req("/api/handle?pubkey=" + pub.hex())["handle"] is not None,
                     STEP_TIMEOUT):
            raise TimeoutError("register did not settle")
        handle = _req("/api/handle?pubkey=" + pub.hex())["handle"]

        stage = "deposit"
        amount = 7
        _req("/api/deposit", {"account": handle, "asset": 0, "amount": amount})
        if not _wait(lambda: float(_req("/api/balance?asset=0&account=%d" % handle)["balance"]) >= amount,
                     STEP_TIMEOUT):
            raise TimeoutError("deposit did not settle")
        return None
    except Exception as e:
        return (stage, str(e))


def run():
    while True:
        t0 = time.time()
        failed = cycle()
        took = time.time() - t0
        if failed is None:
            metrics.inc("jamswap_canary_pass_total")
            metrics.observe("jamswap_canary_duration_seconds", None, took)
            _last_pass[0] = time.time()
            print("canary PASS in %.1fs" % took)
        else:
            stage, err = failed
            metrics.inc("jamswap_canary_fail_total", {"stage": stage})
            print("canary FAIL at %s after %.1fs: %s" % (stage, took, err))
        time.sleep(max(0.0, INTERVAL - took))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = metrics.render().encode() if self.path == "/metrics" else b"jamswap canary\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    threading.Thread(target=run, daemon=True).start()
    print("jamswap canary: probing %s every %ds; /metrics on :%d" % (DEX, INTERVAL, PORT))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
