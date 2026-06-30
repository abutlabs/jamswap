#!/usr/bin/env python3
"""Marmalade off-chain layer — the round builder + a trading API + the UI.

This is the operating layer the plan calls Phase 6: it collects orders into a
pending batch per market, and on `/api/round` reads the market's resting book from
chain, assembles the work-package (book + pending), submits it to the JAM node
(TAG_MATCH), and clears the pending queue. It also serves the trading UI and proxies
balance/state reads. Stdlib only (http.server, urllib, struct).

  LASAIR_RPC=http://localhost:19900 PORT=8080 python3 offchain/server.py
"""
import json, os, struct, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RPC = os.environ.get("LASAIR_RPC", "http://localhost:19900").rstrip("/")
SID = int(os.environ.get("SERVICE_ID", "1729"))
PORT = int(os.environ.get("PORT", "8080"))
WEB = os.path.join(os.path.dirname(__file__), "web")

BUY, SELL = 0, 1
pending = {}      # market_id -> list of (account, oid, side, price, qty)
next_oid = [1000]

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
def api_order(b):
    m = int(b["market"]); oid = next_oid[0]; next_oid[0] += 1
    side = BUY if b["side"] == "buy" else SELL
    pending.setdefault(m, []).append((int(b["account"]), oid, side, int(b["price"]), int(b["qty"])))
    return {"ok": True, "order_id": oid, "pending": len(pending[m])}
def api_round(b):
    m, base, quote = int(b["market"]), int(b["base"]), int(b["quote"])
    # the builder: resting book (from chain) + this round's pending orders
    rest = storage(b"book" + struct.pack("<I", m))
    body = b"".join(order_bytes(*o) for o in pending.get(m, []))
    submit(bytes([0]) + struct.pack("<III", m, base, quote) + rest + body)
    pending[m] = []
    return {"ok": True, "price": mstate(b"lp", m), "volume": mstate(b"cv", m), "book": book_of(m)}
def api_state(q):
    m = int(q.get("market", "1"))
    return {"price": mstate(b"lp", m), "volume": mstate(b"cv", m),
            "book": book_of(m), "pending": len(pending.get(m, []))}
def api_balance(q):
    return {"balance": bal(int(q["asset"]), int(q["account"]))}

ROUTES_POST = {"/api/deposit": api_deposit, "/api/order": api_order, "/api/round": api_round}
ROUTES_GET = {"/api/state": api_state, "/api/balance": api_balance}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        path = self.path.split("?")[0]
        q = dict(p.split("=") for p in self.path.split("?")[1].split("&")) if "?" in self.path else {}
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

if __name__ == "__main__":
    print(f"marmalade off-chain API + UI on :{PORT} (node {RPC}, service {SID})")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
