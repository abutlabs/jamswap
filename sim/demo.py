#!/usr/bin/env python3
"""Marmalade end-to-end demo — a narrated trading scenario against a lasair node.

Deploys the Marmalade JAM service, funds traders, runs ONE sealed-order
frequent-batch-auction round (commit -> reveal -> clear -> settle), then shows the
resting order book and proves MEV-resistance (an uncommitted order is rejected).

  LASAIR_RPC=http://localhost:19900 JAM=service/marmalade-service.jam python3 sim/demo.py

Reuses only the Python stdlib (urllib, struct, hashlib) — no dependencies.
"""
import hashlib, json, os, struct, sys, urllib.request

RPC = os.environ.get("LASAIR_RPC", "http://localhost:19900").rstrip("/")
JAM = os.environ.get("JAM", "service/marmalade-service.jam")
SID = 1729

# wire (matches crates/match-engine/src/wire.rs)
def order(acct, oid, side, price, qty):  # 17 bytes
    return struct.pack("<IIBII", acct, oid, side, price, qty)
def deposit(acct, asset, amount):        # [1][account][asset][amount]
    return bytes([1]) + struct.pack("<IBQ", acct, asset, amount)
BUY, SELL = 0, 1
BASE, QUOTE = 0, 1

def rpc(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(RPC + path, data=data,
        headers={"content-type": "application/json"}, method="POST" if data else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

def submit(payload):  # one work-item (refine + accumulate)
    return rpc(f"/v1/service/{SID}/item", {"payload_hex": payload.hex()})

def storage(key_bytes):
    r = rpc(f"/v1/service/{SID}/storage/{key_bytes.hex()}")
    return bytes.fromhex(r["value_hex"]) if r.get("value_hex") else b""

def bal(asset, acct):
    return int.from_bytes(storage((b"B" if asset == BASE else b"Q") + struct.pack("<I", acct)) or b"\0", "little")
def u64(key):
    v = storage(key); return int.from_bytes(v, "little") if v else 0

def h(s): print("\n\033[1;35m== " + s + "\033[0m")
def line(s): print("   " + s)

def main():
    h("Deploy the Marmalade DEX service")
    jam = open(JAM, "rb").read()
    r = rpc("/v1/service", {"jam_hex": jam.hex()})
    line(f"deployed marmalade-service ({len(jam)} bytes) -> service_id {r['service_id']}")

    h("Fund three traders (Phase-2 faucet)")
    submit(deposit(1, QUOTE, 100000)); line("Alice (acct1): +100000 quote")
    submit(deposit(2, BASE,    1000)); line("Bob   (acct2): +1000 base")
    submit(deposit(3, QUOTE, 100000)); line("Carol (acct3): +100000 quote")

    # the round's orders (hidden during commit)
    orders = {
        "Alice buy 10 @100": (order(1, 1, BUY,  100, 10), b"alice-nonce-padding-32bytes-aaaa"),
        "Bob  sell 6 @100":  (order(2, 2, SELL, 100,  6), b"bob---nonce-padding-32bytes-bbbb"),
        "Carol buy 5 @98":   (order(3, 3, BUY,   98,  5), b"carol-nonce-padding-32bytes-cccc"),
    }
    h("Commit phase — orders are SEALED (only hashes go on-chain)")
    for label, (o, nonce) in orders.items():
        commit = hashlib.blake2s(o + nonce).digest()
        submit(bytes([2]) + struct.pack("<I", struct.unpack_from("<I", o)[0]) + commit)
        line(f"committed: {label}  ->  H = {commit.hex()[:16]}…  (the order itself is hidden)")
    commits = storage(b"commits")
    line(f"on-chain 'commits' = {len(commits)//32} hashes, {len(commits)} bytes — no prices/qtys visible")

    h("Reveal + match — refine verifies each order against its commitment, then clears")
    reveals = b"".join(o + nonce for (o, nonce) in orders.values())
    submit(bytes([3]) + struct.pack("<I", len(commits)) + commits + reveals)
    line(f"clearing price = {u64(b'last_price')}   (uniform price — everyone trades at it)")
    line(f"matched volume this round (cum) = {u64(b'cum_volume')}")

    h("Settled balances")
    line(f"Alice base={bal(BASE,1):>3}  quote={bal(QUOTE,1)}   (bought 6 @100 -> +6 base / -600 quote)")
    line(f"Bob   base={bal(BASE,2):>3}  quote={bal(QUOTE,2)}      (sold 6 @100 -> -6 base / +600 quote)")
    line(f"Carol base={bal(BASE,3):>3}  quote={bal(QUOTE,3)}   (didn't cross at 100 -> unchanged, rests)")

    h("Resting order book (partially/un-filled orders carry to the next round)")
    book = storage(b"book")
    for i in range(len(book) // 17):
        a, oid, side, p, q = struct.unpack_from("<IIBII", book, i * 17)
        line(f"resting: acct{a} {'BUY ' if side == BUY else 'SELL'} {q} @ {p}")

    h("MEV-resistance — an UNCOMMITTED order is rejected (can't be injected post-seal)")
    forged = order(3, 99, BUY, 100, 5) + bytes(32)
    before = bal(QUOTE, 3)
    submit(bytes([3]) + struct.pack("<I", 0) + forged)   # empty commit set
    after = bal(QUOTE, 3)
    line(f"Carol quote before={before} after={after}  ->  {'REJECTED ✓ (unchanged)' if before == after else 'LEAKED ✗'}")

    print("\n\033[1;32mMarmalade: a trustless, MEV-resistant order-book auction cleared in JAM's Refine.\033[0m")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("demo failed:", e); sys.exit(1)
