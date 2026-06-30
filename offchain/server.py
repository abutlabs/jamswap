#!/usr/bin/env python3
"""Jamswap off-chain layer — the round builder + a trading API + the UI.

This is the operating layer the plan calls Phase 6: it collects orders into a
pending batch per market, and on `/api/round` reads the market's resting book from
chain, assembles the work-package (book + pending), submits it to the JAM node
(TAG_MATCH), and clears the pending queue. It also serves the trading UI and proxies
balance/state reads. Stdlib only (http.server, urllib, struct).

  LASAIR_RPC=http://localhost:19900 PORT=8080 python3 offchain/server.py
"""
import hashlib, json, os, secrets, struct, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# service payload tags (must match service/src/lib.rs)
TAG_MATCH, TAG_DEPOSIT, TAG_COMMIT, TAG_REVEAL, TAG_CANCEL, TAG_WITHDRAW, TAG_LIST = range(7)

RPC = os.environ.get("LASAIR_RPC", "http://localhost:19900").rstrip("/")
# Service id: an explicit SERVICE_ID wins; otherwise we DEPLOY the blob ($JAM) at
# startup and use whatever id the node assigns. Deploying here (rather than trusting a
# hardcoded id) is what keeps the UI pointed at THIS service — node service ids are
# assigned sequentially, so a node reused across runs drifts 1729 -> 1730 -> ...
SID = int(os.environ["SERVICE_ID"]) if os.environ.get("SERVICE_ID") else None
PORT = int(os.environ.get("PORT", "8080"))
WEB = os.path.join(os.path.dirname(__file__), "web")

BUY, SELL = 0, 1
# market_id -> list of dicts {account, oid, side, price, qty, sealed, address, reveal?}
# A SEALED order's price/qty are never exposed in the public mempool (/api/state) —
# only its on-chain commitment hash (Blake2s256(order17 ‖ nonce32)) is, exactly as a
# front-runner watching the chain would see it. The terms are revealed at round time.
pending = {}
next_oid = [1000]

def commitment(reveal_bytes):     # must match service commitment(): Blake2s256, 32B
    return hashlib.blake2s(reveal_bytes, digest_size=32).digest()

# ---- node RPC + wire ------------------------------------------------------
def node(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(RPC + path, data=data,
        headers={"content-type": "application/json"}, method="POST" if data else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())
def order_bytes(a, oid, side, p, q): return struct.pack("<IIBII", a, oid, side, p, q)
def submit(payload): return node(f"/v1/service/{SID}/item", {"payload_hex": payload.hex()})
def storage(key):
    r = node(f"/v1/service/{SID}/storage/{key.hex()}")
    return bytes.fromhex(r["value_hex"]) if r.get("value_hex") else b""
def bal(asset, acct):
    return int.from_bytes(storage(b"b" + struct.pack("<II", asset, acct)) or b"\0", "little")
def mstate(prefix, m):
    v = storage(prefix + struct.pack("<I", m)); return int.from_bytes(v, "little") if v else 0
def book_of(m):
    bk = storage(b"book" + struct.pack("<I", m)); out = []
    for i in range(len(bk) // 17):
        a, oid, side, p, q = struct.unpack_from("<IIBII", bk, i * 17)
        out.append({"account": a, "id": oid, "side": "buy" if side == BUY else "sell", "price": p, "qty": q})
    return out

# ---- API handlers ---------------------------------------------------------
def api_deposit(b):
    submit(bytes([1]) + struct.pack("<IIQ", int(b["account"]), int(b["asset"]), int(b["amount"])))
    return {"ok": True}
def api_withdraw(b):
    submit(bytes([5]) + struct.pack("<IIQ", int(b["account"]), int(b["asset"]), int(b["amount"])))
    return {"ok": True, "balance": bal(int(b["asset"]), int(b["account"]))}
def api_list(b):
    submit(bytes([6]) + struct.pack("<III", int(b["market"]), int(b["base"]), int(b["quote"])))
    return {"ok": True}
def api_order(b):
    m = int(b["market"]); oid = next_oid[0]; next_oid[0] += 1
    side = BUY if b["side"] == "buy" else SELL
    acct, price, qty = int(b["account"]), int(b["price"]), int(b["qty"])
    o = {"account": acct, "oid": oid, "side": side, "price": price, "qty": qty,
         "sealed": bool(b.get("sealed")), "address": b.get("address", "")}
    if o["sealed"]:
        # commit-reveal: publish ONLY the hash now (orders hidden on-chain), reveal at round
        nonce = secrets.token_bytes(32)
        o["reveal"] = order_bytes(acct, oid, side, price, qty) + nonce
        submit(bytes([TAG_COMMIT]) + struct.pack("<II", m, acct) + commitment(o["reveal"]))
    pending.setdefault(m, []).append(o)
    return {"ok": True, "order_id": oid, "sealed": o["sealed"], "pending": len(pending[m])}
def api_round(b):
    m, base, quote = int(b["market"]), int(b["base"]), int(b["quote"])
    pend = pending.get(m, [])
    sealed = [o for o in pend if o["sealed"]]
    public = [o for o in pend if not o["sealed"]]
    hdr = struct.pack("<III", m, base, quote)
    # sealed batch: REVEAL the committed orders (the node re-checks each hash ∈ commits)
    if sealed:
        commits = b"".join(commitment(o["reveal"]) for o in sealed)
        reveals = b"".join(o["reveal"] for o in sealed)
        submit(bytes([TAG_REVEAL]) + hdr + struct.pack("<I", len(commits)) + commits + reveals)
    # public batch: resting book (from chain) + this round's public orders -> MATCH
    if public or not sealed:
        rest = storage(b"book" + struct.pack("<I", m))
        body = b"".join(order_bytes(o["account"], o["oid"], o["side"], o["price"], o["qty"]) for o in public)
        submit(bytes([TAG_MATCH]) + hdr + rest + body)
    pending[m] = []
    return {"ok": True, "price": mstate(b"lp", m), "volume": mstate(b"cv", m),
            "book": book_of(m), "cleared": {"sealed": len(sealed), "public": len(public)}}
def short(a):
    return (a[:6] + "…" + a[-4:]) if a and len(a) > 12 else a
def api_state(q):
    m = int(q.get("market", "1"))
    mempool = []
    for o in pending.get(m, []):
        e = {"side": "buy" if o["side"] == BUY else "sell", "sealed": o["sealed"],
             "who": short(o.get("address", "")) or f"acct {o['account']}"}
        # a sealed order's terms are NOT revealed in the public mempool — only that it exists
        e["price"], e["qty"] = (None, None) if o["sealed"] else (o["price"], o["qty"])
        mempool.append(e)
    onchain_commits = len(storage(b"commits" + struct.pack("<I", m))) // 32
    return {"price": mstate(b"lp", m), "volume": mstate(b"cv", m), "book": book_of(m),
            "pending": len(pending.get(m, [])), "mempool": mempool, "sealed_onchain": onchain_commits}
def api_balance(q):
    return {"balance": bal(int(q["asset"]), int(q["account"]))}

ROUTES_POST = {"/api/deposit": api_deposit, "/api/withdraw": api_withdraw,
               "/api/list": api_list, "/api/order": api_order, "/api/round": api_round}

# the markets the UI shows; listed once at startup so they're tradable.
DEFAULT_MARKETS = [(1, 1, 0), (2, 2, 0)]  # (market, base=TOKA/TOKB, quote=USD)
def ensure_markets():
    for m, base, quote in DEFAULT_MARKETS:
        try: api_list({"market": m, "base": base, "quote": quote})
        except Exception as e: print("list failed", m, e)
ROUTES_GET = {"/api/state": api_state, "/api/balance": api_balance}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        path = self.path.split("?")[0]
        q = dict(p.split("=") for p in self.path.split("?")[1].split("&")) if "?" in self.path else {}
        if path == "/api/stream":            # live order-book feed (Server-Sent Events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    body = json.dumps(api_state(q)).encode()
                    self.wfile.write(b"data: " + body + b"\n\n")
                    self.wfile.flush()
                    time.sleep(1.5)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        if path in ROUTES_GET:
            try: self._send(200, json.dumps(ROUTES_GET[path](q)).encode())
            except Exception as e: self._send(500, json.dumps({"error": str(e)}).encode())
        else:
            fn = "index.html" if path == "/" else path.lstrip("/")
            try:
                data = open(os.path.join(WEB, fn), "rb").read()
                ctype = "text/html" if fn.endswith(".html") else "application/javascript"
                self._send(200, data, ctype)
            except FileNotFoundError:
                self._send(404, b"not found", "text/plain")
    def do_POST(self):
        path = self.path.split("?")[0]
        ln = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(ln) or b"{}")
        if path in ROUTES_POST:
            try: self._send(200, json.dumps(ROUTES_POST[path](body)).encode())
            except Exception as e: self._send(500, json.dumps({"error": str(e)}).encode())
        else:
            self._send(404, json.dumps({"error": "no route"}).encode())

def wait_for_node():
    for _ in range(60):
        try:
            if "ok" in str(node("/v1/healthz")): return
        except Exception: pass
        time.sleep(1)

def deploy_jam():
    jam = open(os.environ["JAM"], "rb").read()
    r = node("/v1/service", {"jam_hex": jam.hex()})
    return int(r["service_id"])

if __name__ == "__main__":
    if SID is None and os.environ.get("JAM"):
        wait_for_node()
        SID = deploy_jam()                      # use the id THIS deploy was assigned
        print(f"deployed jamswap-service -> service id {SID}")
    elif SID is None:
        SID = 1729                              # last-resort default (first deploy on a fresh node)
    print(f"jamswap off-chain API + UI on :{PORT} (node {RPC}, service {SID})")
    try: ensure_markets(); print("listed default markets:", DEFAULT_MARKETS)
    except Exception as e: print("market listing skipped:", e)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
