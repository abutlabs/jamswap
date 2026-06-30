#!/usr/bin/env python3
"""Marmalade end-to-end demo — a narrated multi-market trading scenario.

Two markets (TOKA/USD and TOKB/USD) clear INDEPENDENTLY in JAM's Refine while
sharing one balance ledger — the parallelism JAM uniquely enables. Shows: a SEALED
(commit/reveal) round, a plaintext round, uniform-price clearing, settlement, a
shared cross-market balance, the resting book, cancel, and MEV-resistance.

  LASAIR_RPC=http://localhost:19900 JAM=service/marmalade-service.jam python3 sim/demo.py

Stdlib only (urllib, struct, hashlib).
"""
import hashlib, json, os, struct, sys, urllib.request

RPC = os.environ.get("LASAIR_RPC", "http://localhost:19900").rstrip("/")
JAM = os.environ.get("JAM", "service/marmalade-service.jam")
SID = 1729
BUY, SELL = 0, 1
USD, TOKA, TOKB = 0, 1, 2          # asset ids
M_A, M_B = 1, 2                     # market ids (TOKA/USD, TOKB/USD)

def order(acct, oid, side, price, qty):                 # 17 bytes
    return struct.pack("<IIBII", acct, oid, side, price, qty)
def deposit(acct, asset, amount):                       # [1][acct][asset][amount]
    return bytes([1]) + struct.pack("<II", acct, asset) + struct.pack("<Q", amount)
def match_hdr(tag, market, base, quote):
    return bytes([tag]) + struct.pack("<III", market, base, quote)

def rpc(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(RPC + path, data=data,
        headers={"content-type": "application/json"}, method="POST" if data else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())
def submit(payload): return rpc(f"/v1/service/{SID}/item", {"payload_hex": payload.hex()})
def storage(key):
    r = rpc(f"/v1/service/{SID}/storage/{key.hex()}")
    return bytes.fromhex(r["value_hex"]) if r.get("value_hex") else b""
def bal(asset, acct):
    return int.from_bytes(storage(b"b" + struct.pack("<II", asset, acct)) or b"\0", "little")
def mstate(prefix, market):
    v = storage(prefix + struct.pack("<I", market)); return int.from_bytes(v, "little") if v else 0
def h(s): print("\n\033[1;35m== " + s + "\033[0m")
def line(s): print("   " + s)

def main():
    h("Deploy the Marmalade DEX service")
    jam = open(JAM, "rb").read()
    line(f"deployed ({len(jam)} bytes) -> service_id {rpc('/v1/service', {'jam_hex': jam.hex()})['service_id']}")

    h("List markets (canonical assets — trading an unlisted market is rejected)")
    submit(bytes([6]) + struct.pack("<III", M_A, TOKA, USD)); line("listed TOKA/USD")
    submit(bytes([6]) + struct.pack("<III", M_B, TOKB, USD)); line("listed TOKB/USD")

    h("Fund traders (one ledger, shared across all markets)")
    submit(deposit(1, USD, 100000));  line("Alice: +100000 USD  (will buy in BOTH markets)")
    submit(deposit(2, TOKA, 1000));   line("Bob:   +1000 TOKA")
    submit(deposit(4, TOKB, 1000));   line("Dave:  +1000 TOKB")

    h("Market A = TOKA/USD — a SEALED round (commit, then reveal+match)")
    a_orders = [(order(1, 1, BUY, 100, 10), b"alice-A-nonce-padding-32bytes-aa"),
                (order(2, 2, SELL, 100, 6), b"bob---A-nonce-padding-32bytes-bb")]
    for o, n in a_orders:
        submit(bytes([2]) + struct.pack("<II", M_A, struct.unpack_from("<I", o)[0]) + hashlib.blake2s(o + n).digest())
    line("committed Alice buy + Bob sell (hidden — only hashes on-chain)")
    commits = storage(b"commits" + struct.pack("<I", M_A))
    reveals = b"".join(o + n for o, n in a_orders)
    submit(match_hdr(3, M_A, TOKA, USD) + struct.pack("<I", len(commits)) + commits + reveals)
    line(f"revealed+matched -> clearing price {mstate(b'lp', M_A)}, volume {mstate(b'cv', M_A)}")

    h("Market B = TOKB/USD — a plaintext round (different price, clears independently)")
    b_orders = order(1, 3, BUY, 50, 5) + order(4, 4, SELL, 50, 5)
    submit(match_hdr(0, M_B, TOKB, USD) + b_orders)
    line(f"matched -> clearing price {mstate(b'lp', M_B)}, volume {mstate(b'cv', M_B)}")

    h("Two markets, two prices, ONE shared ledger")
    line(f"Market A price = {mstate(b'lp', M_A)}   Market B price = {mstate(b'lp', M_B)}   (independent)")
    line(f"Alice  USD={bal(USD,1)}  TOKA={bal(TOKA,1)}  TOKB={bal(TOKB,1)}")
    line("       (USD 100000 − 600 [6 TOKA @100] − 250 [5 TOKB @50] = 99150, shared across both markets)")
    line(f"Bob    USD={bal(USD,2)}  TOKA={bal(TOKA,2)}")
    line(f"Dave   USD={bal(USD,4)}  TOKB={bal(TOKB,4)}")

    h("Resting book (Market A) + cancel")
    def show_book(m):
        bk = storage(b"book" + struct.pack("<I", m))
        for i in range(len(bk) // 17):
            a, oid, side, p, q = struct.unpack_from("<IIBII", bk, i * 17)
            line(f"resting: acct{a} {'BUY ' if side==BUY else 'SELL'} {q} @ {p} (order {oid})")
        return len(bk) // 17
    show_book(M_A)
    submit(bytes([4]) + struct.pack("<III", M_A, 1, 1))   # Alice cancels her resting buy (order 1)
    line(f"after Alice cancels order 1 -> {show_book(M_A)} resting orders")

    h("MEV-resistance — an UNCOMMITTED order is rejected")
    before = bal(USD, 1)
    submit(match_hdr(3, M_A, TOKA, USD) + struct.pack("<I", 0) + order(1, 99, BUY, 100, 5) + bytes(32))
    line(f"Alice USD {before} -> {bal(USD,1)}  ->  {'REJECTED ✓' if before == bal(USD,1) else 'LEAKED ✗'}")

    print("\n\033[1;32mMarmalade: parallel, trustless, MEV-resistant order-book auctions on JAM.\033[0m")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("demo failed:", e); sys.exit(1)
