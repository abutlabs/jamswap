#!/usr/bin/env python3
"""E2E smoke test for a RUNNING jamswap stack (default compose). Run via:

    make verify        # -> docker compose exec -T dex python3 /app/verify.py

Exercises the full QUIC pipeline with a FRESH random account each run, so it is
rerunnable against a long-lived chain:

  1. register  -> a handle is assigned on-chain        (accumulate + ed25519 verify)
  2. duplicate -> the SAME register payload is force-submitted twice straight to the
                  builder (bypassing the dex's idempotency guard): GP rejects the
                  duplicate work-package and the chain must keep authoring (the
                  pre-1.6.2 wedge regression test)
  3. deposit   -> faucet credit lands                  (unsigned accumulate)
  4. withdraw  -> signed debit lands                   (ed25519 verify in accumulate)

Exit code 0 = all good; non-zero with a FAIL line otherwise.
"""
import json, os, struct, sys, time, urllib.request

from nacl.signing import SigningKey

DEX = "http://localhost:8080"
BUILDER = os.environ["BUILDER_URL"]
SID = int(os.environ["SERVICE_ID"])
SCALE = 10000


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def get(path):
    return json.load(urllib.request.urlopen(DEX + path, timeout=30))


def wait(pred, what, tries=None, delay=0.5):
    # On a CONTESTED mixed chain (equal 3:3 split) settlement needs ~2 lasair-
    # authored canonical blocks (guarantee, then assure-any-pending) ≈ 4 slots
    # expected at 6 s/slot, plus re-guarantee retries after lost fork races —
    # so the window must comfortably exceed one U=5-slot cycle. 90 s covers
    # several retry rounds; the fast path still returns on first success.
    if tries is None:
        tries = int(os.environ.get("VERIFY_WAIT_TRIES", "180"))
    for _ in range(tries):
        v = pred()
        if v is not None:
            return v
        time.sleep(delay)
    sys.exit("FAIL: timed out waiting for " + what)


sk = SigningKey.generate()
pub = sk.verify_key.encode()
print("account pubkey:", pub.hex())

# 1. register -> handle
sig = sk.sign(b"jamswap:v1:register" + pub).signature
post(DEX + "/api/register", {"pubkey": pub.hex(), "sig": sig.hex()})
handle = wait(lambda: get("/api/handle?pubkey=" + pub.hex())["handle"],
              "register to accumulate")
print("PASS register: handle", handle)

# 2. duplicate work-packages must not wedge the chain
payload = bytes([7]) + pub + sig
for i in range(2):
    r = post(BUILDER + "/submit", {"service_id": SID, "payload_hex": payload.hex()})
    if not r.get("accepted"):
        sys.exit("FAIL: builder refused duplicate submit %d: %r" % (i, r))
print("PASS duplicate: 2 identical packages submitted (chain must survive)")

# 3. faucet deposit lands (this also proves the chain still accumulates AFTER the
#    duplicates from step 2 — i.e. authoring never wedged)
amount = 12345
before = float(get("/api/balance?asset=0&account=%d" % handle)["balance"])
post(DEX + "/api/deposit", {"account": handle, "asset": 0, "amount": amount})
wait(lambda: (lambda b: b if b >= before + amount else None)(
        float(get("/api/balance?asset=0&account=%d" % handle)["balance"])),
     "deposit to accumulate")
print("PASS deposit: +%d USDC" % amount)

# 4. signed withdraw lands
wd = 2345 * SCALE
nonce = get("/api/nonce?handle=%d" % handle)["nonce"]
msg = (b"jamswap:v1:withdraw" + struct.pack("<I", handle) + struct.pack("<I", 0)
       + struct.pack("<Q", wd) + struct.pack("<Q", nonce))
wsig = sk.sign(msg).signature
post(DEX + "/api/withdraw", {"account": handle, "asset": 0, "amount_atomic": wd,
                             "nonce": nonce, "sig": wsig.hex()})
wait(lambda: (lambda b: b if b <= before + amount - wd / SCALE else None)(
        float(get("/api/balance?asset=0&account=%d" % handle)["balance"])),
     "withdraw to accumulate")
print("PASS withdraw: -%d USDC (signed)" % (wd // SCALE))

# 5. THE POINT OF A DEX: two crossing public orders MATCH and the trade SETTLES
#    on-chain (cumulative volume grows). This is the coverage gap that let the
#    round pipeline break invisibly for a full day (2026-07-09): every earlier
#    ALL PASS proved register/deposit/withdraw but never a single matched trade.
sk2 = SigningKey.generate()
pub2 = sk2.verify_key.encode()
sig2 = sk2.sign(b"jamswap:v1:register" + pub2).signature
post(DEX + "/api/register", {"pubkey": pub2.hex(), "sig": sig2.hex()})
handle2 = wait(lambda: get("/api/handle?pubkey=" + pub2.hex())["handle"],
               "counterparty register to accumulate")
post(DEX + "/api/deposit", {"account": handle2, "asset": 1, "amount": 100})   # seller needs DOT
wait(lambda: (float(get("/api/balance?asset=1&account=%d" % handle2)["balance"]) >= 100) or None,
     "counterparty DOT deposit")
cv0 = float(get("/api/state?market=1")["volume"])

def place(signer, h, side, qty_d, price_d, seq):
    qty, price = int(qty_d * SCALE), int(price_d * SCALE)
    msg = (b"jamswap:v1:order" + struct.pack("<I", h) + struct.pack("<I", 1)
           + bytes([0 if side == "buy" else 1]) + struct.pack("<I", qty)
           + bytes([0]) + bytes([0]) + struct.pack("<I", price) + struct.pack("<Q", seq))
    post(DEX + "/api/order", {"market": 1, "base": 1, "quote": 0, "account": h,
                              "side": side, "qty": qty_d, "price": price_d,
                              "seq": seq, "sig": signer.sign(msg).signature.hex()})

place(sk2, handle2, "sell", 5, 1.0, 1)
place(sk,  handle,  "buy",  5, 1.0, 1)
wait(lambda: (lambda v: v if v > cv0 else None)(float(get("/api/state?market=1")["volume"])),
     "the crossing orders to MATCH and the round to SETTLE on-chain",
     tries=360)   # settle = queue wait + guarantee/assure/accumulate: minutes on the shared chain
print("PASS trade: %s DOT matched and settled on-chain" %
      (float(get("/api/state?market=1")["volume"]) - cv0))

print("ALL PASS: register / duplicate-survival / deposit / withdraw / MATCHED TRADE")
