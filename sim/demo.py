#!/usr/bin/env python3
"""Jamswap end-to-end demo — a narrated multi-market trading scenario.

Two markets (TOKA/USD and TOKB/USD) clear INDEPENDENTLY in JAM's Refine while
sharing one balance ledger — the parallelism JAM uniquely enables. Shows: a SEALED
(commit/reveal) round, a plaintext round, uniform-price clearing, settlement, a
shared cross-market balance, the resting book, cancel, and MEV-resistance.

  LASAIR_RPC=http://localhost:19900 JAM=service/jamswap-service.jam python3 sim/demo.py

Stdlib only (urllib, struct, hashlib).
"""
import hashlib, json, os, struct, sys, urllib.request

RPC = os.environ.get("LASAIR_RPC", "http://localhost:19900").rstrip("/")
JAM = os.environ.get("JAM", "service/jamswap-service.jam")
# Set at deploy time from the id the node actually assigns (see main). Node service
# ids are handed out sequentially, so a node reused across runs drifts 1729 -> 1730
# -> …; hardcoding an id makes the demo read/write a *different* live service. An
# explicit SERVICE_ID overrides (e.g. to drive an already-deployed service).
SID = int(os.environ["SERVICE_ID"]) if os.environ.get("SERVICE_ID") else None
BUY, SELL = 0, 1
USD, TOKA, TOKB = 0, 1, 2          # asset ids
M_A, M_B = 1, 2                     # market ids (TOKA/USD, TOKB/USD)
SCALE = 10_000                     # fixed-point scale (match service SCALE): atomic = display × SCALE
def d(v): return round(v / SCALE, 4)                    # atomic -> display

# optional ed25519 signer (PyNaCl) — used only for the signed-cancel showcase. Without it the
# demo still runs; the cancel step is skipped (matching + settlement need no signatures).
try:
    from nacl.signing import SigningKey
    HAVE_NACL = True
    ALICE = SigningKey((b"jamswap-demo-alice-key" + b"\0" * 32)[:32])
except Exception:
    HAVE_NACL = False
def canon(action, *parts):                              # must match canon() in the service
    return b"jamswap:v1:" + action + b"".join(parts)
def order(acct, oid, side, price, qty):                 # 17 bytes; price/qty scaled to atomic
    return struct.pack("<IIBII", acct, oid, side, price * SCALE, qty * SCALE)
def deposit(acct, asset, amount):                       # [1][acct][asset][amount] (atomic)
    return bytes([1]) + struct.pack("<II", acct, asset) + struct.pack("<Q", amount * SCALE)
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
    global SID
    h("Deploy the Jamswap DEX service")
    jam = open(JAM, "rb").read()
    if SID is None:
        SID = int(rpc("/v1/service", {"jam_hex": jam.hex()})["service_id"])
        line(f"deployed ({len(jam)} bytes) -> service_id {SID}")
    else:
        line(f"using existing service_id {SID} (SERVICE_ID set)")

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
    # unified sealed round: commits ‖ reveals ‖ (resting book + public orders — none here)
    rest = storage(b"book" + struct.pack("<I", M_A))
    submit(match_hdr(3, M_A, TOKA, USD)
           + struct.pack("<I", len(commits)) + commits
           + struct.pack("<I", len(reveals)) + reveals
           + rest)
    line(f"revealed+matched -> clearing price {d(mstate(b'lp', M_A))}, volume {d(mstate(b'cv', M_A))}")

    h("Market B = TOKB/USD — a plaintext round (different price, clears independently)")
    b_orders = order(1, 3, BUY, 50, 5) + order(4, 4, SELL, 50, 5)
    submit(match_hdr(0, M_B, TOKB, USD) + b_orders)
    line(f"matched -> clearing price {d(mstate(b'lp', M_B))}, volume {d(mstate(b'cv', M_B))}")

    h("Two markets, two prices, ONE shared ledger")
    line(f"Market A price = {d(mstate(b'lp', M_A))}   Market B price = {d(mstate(b'lp', M_B))}   (independent)")
    line(f"Alice  USD={d(bal(USD,1))}  TOKA={d(bal(TOKA,1))}  TOKB={d(bal(TOKB,1))}")
    line("       (USD 100000 − 600 [6 TOKA @100] − 250 [5 TOKB @50] − fees, shared across both markets)")
    line(f"Bob    USD={d(bal(USD,2))}  TOKA={d(bal(TOKA,2))}")
    line(f"Dave   USD={d(bal(USD,4))}  TOKB={d(bal(TOKB,4))}")

    h("Resting book (Market A) + owner-authenticated cancel")
    def show_book(m):
        bk = storage(b"book" + struct.pack("<I", m))
        for i in range(len(bk) // 17):
            a, oid, side, p, q = struct.unpack_from("<IIBII", bk, i * 17)
            line(f"resting: acct{a} {'BUY ' if side==BUY else 'SELL'} {d(q)} @ {d(p)} (order {oid})")
        return len(bk) // 17
    # Alice registers her key (first registration -> handle 1), then rests a plaintext buy that
    # doesn't cross. Cancelling a resting order is now signature-authenticated (only the owner).
    if HAVE_NACL:
        apk = bytes(ALICE.verify_key)
        submit(bytes([7]) + apk + ALICE.sign(canon(b"register", apk)).signature)
        handle = int.from_bytes(storage(b"h" + apk) or b"\0", "little")
    else:
        handle = 1
    submit(match_hdr(0, M_A, TOKA, USD) + order(handle, 10, BUY, 80, 5))   # rests (no crossing sell)
    line(f"Alice rests a buy 5 @ 80 (order 10) -> {show_book(M_A)} resting order(s)")
    if HAVE_NACL:
        nonce = int.from_bytes(storage(b"nc" + struct.pack("<I", handle)) or b"\0", "little")
        msg = canon(b"cancel", struct.pack("<I", handle), struct.pack("<I", M_A),
                    struct.pack("<I", 10), struct.pack("<Q", nonce))
        submit(bytes([4]) + struct.pack("<IIIQ", handle, M_A, 10, nonce) + ALICE.sign(msg).signature)
        line(f"Alice (handle {handle}) SIGNS a cancel of order 10 -> {show_book(M_A)} resting orders")
        # a cancel signed by the WRONG key is rejected
        n2 = int.from_bytes(storage(b"nc" + struct.pack("<I", handle)) or b"\0", "little")
        submit(match_hdr(0, M_A, TOKA, USD) + order(handle, 11, BUY, 80, 5))   # rest another
        bad = SigningKey((b"not-alice" + b"\0" * 32)[:32])
        bmsg = canon(b"cancel", struct.pack("<I", handle), struct.pack("<I", M_A),
                     struct.pack("<I", 11), struct.pack("<Q", n2))
        submit(bytes([4]) + struct.pack("<IIIQ", handle, M_A, 11, n2) + bad.sign(bmsg).signature)
        line(f"a cancel signed by the WRONG key -> order 11 still rests: {show_book(M_A)} (rejected ✓)")
    else:
        line("signed cancel needs an ed25519 signer (pip install pynacl) — order 10 left resting")

    h("MEV-resistance — an UNCOMMITTED order is rejected")
    before = bal(USD, 1)
    rev = order(1, 99, BUY, 100, 5) + bytes(32)   # reveal with NO matching commitment
    submit(match_hdr(3, M_A, TOKA, USD) + struct.pack("<I", 0) + struct.pack("<I", len(rev)) + rev)
    line(f"Alice USD {d(before)} -> {d(bal(USD,1))}  ->  {'REJECTED ✓' if before == bal(USD,1) else 'LEAKED ✗'}")

    print("\n\033[1;32mJamswap: parallel, trustless, MEV-resistant order-book auctions on JAM.\033[0m")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("demo failed:", e); sys.exit(1)
