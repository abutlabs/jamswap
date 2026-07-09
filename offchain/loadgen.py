#!/usr/bin/env python3
"""Trading load generator for the jamswap stack (docs/OBSERVABILITY_PLAN.md phase 3).

Drives the PUBLIC DEX API with signed orders from the six standard JAM dev accounts
(Alice..Fergie — pre-registered and funded in genesis via mixed/gen-spec.py
GENESIS_BALANCE), so the matching engine + the full JAM settlement pipeline are
exercised exactly the way real traders exercise them. Runs anywhere it can reach
the dex (compose service, `make load`; or the k8s Deployment in k8s/loadgen.yaml
for scale-out) and exports its own view on :9111/metrics — offered load measured
from OUTSIDE the system under test.

Profiles (PROFILE):
  trading       random crossing buy/sell pairs between random dev accounts around
                the last price; SEALED_RATIO of orders go through the sealed
                commit->reveal path (default 0.2)
  faucet-storm  register+deposit hammering with FRESH accounts (the incident
                scenario; needs no genesis accounts)
  steady        deposits/withdraws only — settlement pipeline without matching

Env: DEX_URL (http://dex:8080), PROFILE (trading), RATE (ops/min, 12), MARKET (1),
     BASE (1), QUOTE (0), SEALED_RATIO (0.2), LOADGEN_PORT (9111).
"""
import json, os, random, struct, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nacl.signing import SigningKey
import metrics

DEX = os.environ.get("DEX_URL", "http://dex:8080").rstrip("/")
PROFILE = os.environ.get("PROFILE", "trading")
RATE = float(os.environ.get("RATE", "12"))            # ops per minute
MARKET = int(os.environ.get("MARKET", "1"))
BASE = int(os.environ.get("BASE", "1"))
QUOTE = int(os.environ.get("QUOTE", "0"))
SEALED_RATIO = float(os.environ.get("SEALED_RATIO", "0.2"))
PORT = int(os.environ.get("LOADGEN_PORT", "9111"))
SCALE = 10_000

# the standard JAM dev accounts (docs.jamcha.in/basics/dev-accounts) — the same
# public seeds the trading UI holds; genesis registers them as handles 1..6.
DEV_SEEDS = [
    "996542becdf1e78278dc795679c825faca2e9ed2bf101bf3c4a236d3ed79cf59",  # Alice
    "b81e308145d97464d2bc92d35d227a9e62241a16451af6da5053e309be4f91d7",  # Bob
    "0093c8c10a88ebbc99b35b72897a26d259313ee9bad97436a437d2e43aaafa0f",  # Carol
    "69b3a7031787e12bfbdcac1b7a737b3e5a9f9450c37e215f6d3b57730e21001a",  # David
    "b4de9ebf8db5428930baa5a98d26679ab2a03eae7c791d582e6b75b7f018d0d4",  # Eve
    "4a6482f8f479e3ba2b845f8cef284f4b3208ba3241ed82caa1b5ce9fc6281730",  # Fergie
]
KEYS = [SigningKey(bytes.fromhex(s)) for s in DEV_SEEDS]

metrics.describe("loadgen_ops_total", "operations offered to the dex API, by op")
metrics.describe("loadgen_op_errors_total", "API calls the dex refused/failed, by op")

_seq = [int(time.time() * 1000)]
def next_seq():
    _seq[0] += 1
    return _seq[0]

def req(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(DEX + path, data=data,
                               headers={"Content-Type": "application/json"},
                               method="POST" if data else "GET")
    return json.load(urllib.request.urlopen(r, timeout=30))

def canon(action, *parts):
    return b"jamswap:v1:" + action + b"".join(parts)

def handle_of(key):
    return req("/api/handle?pubkey=" + key.verify_key.encode().hex())["handle"]

def place_public(key, handle, side, qty_d, price_d):
    qty, price = int(qty_d * SCALE), int(round(price_d * SCALE))
    seq = next_seq()
    msg = canon(b"order", struct.pack("<I", handle), struct.pack("<I", MARKET),
                bytes([side]), struct.pack("<I", qty), bytes([0]), bytes([0]),
                struct.pack("<I", price), struct.pack("<Q", seq))
    return req("/api/order", {"market": MARKET, "base": BASE, "quote": QUOTE,
                              "account": handle, "side": "buy" if side == 0 else "sell",
                              "qty": qty_d, "price": price_d, "seq": seq,
                              "sig": key.sign(msg).signature.hex()})

def place_sealed(key, handle, side, qty_d, price_d):
    prep = req("/api/seal_prepare", {"market": MARKET, "account": handle,
                                     "side": "buy" if side == 0 else "sell",
                                     "qty": qty_d, "price": price_d})
    cseq = next_seq()
    cmsg = canon(b"commit", struct.pack("<I", MARKET), struct.pack("<I", handle),
                 bytes.fromhex(prep["commit"]), struct.pack("<Q", cseq))
    return req("/api/order", {"market": MARKET, "base": BASE, "quote": QUOTE,
                              "account": handle, "side": "buy" if side == 0 else "sell",
                              "qty": qty_d, "price": price_d, "sealed": True,
                              "oid": prep["oid"], "commit_seq": cseq,
                              "commit_sig": key.sign(cmsg).signature.hex()})

def op(name, fn):
    metrics.inc("loadgen_ops_total", {"op": name})
    try:
        fn()
    except Exception as e:
        metrics.inc("loadgen_op_errors_total", {"op": name})
        print("op %s failed: %s" % (name, e))

def tick_trading(handles):
    # a crossing pair between two random accounts around the last price (or 1.0)
    lp = 0.0
    try:
        lp = float(req("/api/state?market=%d" % MARKET).get("price") or 0)
    except Exception:
        pass
    mid = lp if lp > 0 else 1.0
    qty = random.randint(1, 20)
    buyer, seller = random.sample(range(len(KEYS)), 2)
    sealed = random.random() < SEALED_RATIO
    px_sell = round(mid * random.uniform(0.98, 1.0), 4)
    px_buy = round(mid * random.uniform(1.0, 1.02), 4)
    if sealed:
        op("sealed_sell", lambda: place_sealed(KEYS[seller], handles[seller], 1, qty, px_sell))
    else:
        op("sell", lambda: place_public(KEYS[seller], handles[seller], 1, qty, px_sell))
    op("buy", lambda: place_public(KEYS[buyer], handles[buyer], 0, qty, px_buy))

def tick_steady(handles):
    h = random.choice(handles)
    op("deposit", lambda: req("/api/deposit", {"account": h, "asset": QUOTE,
                                               "amount": random.randint(1, 50)}))

def tick_faucet_storm(_handles):
    sk = SigningKey.generate()
    pub = sk.verify_key.encode()
    sig = sk.sign(b"jamswap:v1:register" + pub).signature
    op("register", lambda: req("/api/register", {"pubkey": pub.hex(), "sig": sig.hex()}))

def run():
    # resolve dev-account handles (genesis-seeded => instant; else wait for on-chain)
    handles = []
    for k in KEYS:
        h = None
        for _ in range(60):
            try:
                h = handle_of(k)
            except Exception:
                pass
            if h is not None:
                break
            time.sleep(3)
        handles.append(h)
    print("loadgen: dev-account handles:", handles)
    if PROFILE == "trading" and any(h is None for h in handles):
        print("loadgen: some dev accounts unregistered — is GENESIS_BALANCE seeding on?")
    tick = {"trading": tick_trading, "steady": tick_steady,
            "faucet-storm": tick_faucet_storm}.get(PROFILE, tick_trading)
    interval = 60.0 / max(RATE, 0.01)
    print("loadgen: profile=%s rate=%.1f ops/min (every %.1fs) -> %s" %
          (PROFILE, RATE, interval, DEX))
    while True:
        t0 = time.time()
        tick(handles)
        time.sleep(max(0.0, interval - (time.time() - t0)))

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = metrics.render().encode() if self.path == "/metrics" else b"jamswap loadgen\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

if __name__ == "__main__":
    threading.Thread(target=run, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
