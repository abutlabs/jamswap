#!/usr/bin/env python3
"""E2E: sealed orders rest hidden across auctions and cross a later counterparty.

This is the regression test, on a LIVE node, for the reported bug: a sealed sell placed
in one auction and a sealed buy placed in a later auction never matched (sealed orders
were immediate-or-cancel and drained every tick). It drives the off-chain **builder**
(`server.py`) over its HTTP API — the layer where the fix lives — against a real
lasair-node, and asserts:

  Round 1: a sealed SELL is placed, an auction runs -> NO settlement, the order RESTS
           (still pending, hidden).
  Round 2: a sealed BUY that crosses is placed, an auction runs -> BOTH settle (seller
           receives quote, buyer receives base).

Unlike `test_enc_round.py` (which talks raw node RPC with pre-baked committee payloads),
this must go through the builder, because carry-forward is builder-side logic.

## Run it

Bring up the stack, then point this at the running UI server:

    docker compose up -d                 # or docker-compose.testnet.yml
    # the `dex` service serves the builder API on :8080
    JAMSWAP_URL=http://127.0.0.1:8080 python3 offchain/test_sealed_resting_e2e.py

Works in either sealing mode (encrypt-until-batch or ENC_MODE=0 commit-reveal) — the
carry-forward logic is the same. Run the server with REQUIRE_ORDER_SIG=0 so this script
doesn't need to manage account keys (it uses the deposit faucet + bare account handles).

If the server isn't reachable it SKIPS (exit 0) so it never breaks CI, which has no node.
"""
import json
import os
import secrets
import struct
import sys
import time
import urllib.error
import urllib.request

try:
    from nacl.signing import SigningKey
except Exception:
    print("SKIP — this test needs PyNaCl (orders and sealed commits are owner-signed now): "
          "pip install pynacl")
    sys.exit(0)

URL = os.environ.get("JAMSWAP_URL", "http://127.0.0.1:8080").rstrip("/")
MARKET, DOT, USDC = 1, 1, 0        # market 1 is DOT/USDC (base=DOT, quote=USDC)
QTY, PRICE = 10, 1                 # sell/buy 10 DOT @ 1 USDC
SCALE = 10_000                     # atomic fixed-point scale (matches the service)
KEYS = {}                          # handle -> SigningKey


def canon(action, *parts):         # must match canon() in server.py / the service
    return b"jamswap:v1:" + action + b"".join(parts)


def p32(x): return struct.pack("<I", x)
def p64(x): return struct.pack("<Q", x)


def call(method, path, body=None):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read() or "null")


def get(path):  return call("GET", path)
def post(path, body):  return call("POST", path, body)


def deposit(account, asset, amount):
    post("/api/deposit", {"account": account, "asset": asset, "amount": amount})


def balance(account, asset):
    return get(f"/api/balance?account={account}&asset={asset}")["balance"]


def register():
    # a FRESH random key per run → a fresh handle with clean balances and seq floors
    sk = SigningKey(secrets.token_bytes(32))
    pk = bytes(sk.verify_key)
    post("/api/register", {"pubkey": pk.hex(), "sig": sk.sign(canon(b"register", pk)).signature.hex()})
    for _ in range(30):                          # registration lands with the next block
        h = get(f"/api/handle?pubkey={pk.hex()}").get("handle")
        if h:
            KEYS[h] = sk
            return h
        time.sleep(1)
    raise RuntimeError("registration didn't land on-chain")


def next_seq():
    # ms wall clock: strictly rising per account across runs (the on-chain floor persists)
    return int(time.time() * 1000)


def place(account, side, sealed):
    # orders (and, for sealed, the on-chain commitment) are OWNER-SIGNED — the service
    # verifies both, so this test manages real keys like the UI does.
    sk = KEYS[account]
    seq = next_seq()
    msg = canon(b"order", p32(account), p32(MARKET), bytes([0 if side == "buy" else 1]),
                p32(QTY * SCALE), b"\0", bytes([1 if sealed else 0]),
                p32(PRICE * SCALE), p64(seq))
    body = {"market": MARKET, "base": DOT, "quote": USDC, "account": account,
            "side": side, "qty": QTY, "price": PRICE, "type": "limit",
            "sealed": sealed, "seq": seq, "sig": sk.sign(msg).signature.hex()}
    if sealed:
        prep = post("/api/seal_prepare", {"market": MARKET, "account": account, "side": side,
                                          "qty": QTY, "price": PRICE, "type": "limit"})
        if prep.get("error"):
            raise RuntimeError(f"seal_prepare rejected: {prep['error']}")
        cseq = max(next_seq(), seq + 1)
        cmsg = canon(b"commit", p32(MARKET), p32(account), bytes.fromhex(prep["commit"]), p64(cseq))
        body.update({"oid": prep["oid"], "commit_seq": cseq,
                     "commit_sig": sk.sign(cmsg).signature.hex()})
    r = post("/api/order", body)
    if r.get("error"):
        raise RuntimeError(f"order rejected: {r['error']}")
    return r


def run_auction():
    return post("/api/round", {"market": MARKET, "base": DOT, "quote": USDC})


def pending_count(account):
    return len(get(f"/api/mine?account={account}").get("orders", []))


def main():
    try:
        st = get("/api/state?market=1")
    except (urllib.error.URLError, ConnectionError) as e:
        print(f"SKIP — no jamswap server at {URL} ({e}). "
              f"Bring up `docker compose up` and set JAMSWAP_URL. This test needs a live node.")
        return 0
    print(f"sealing mode: {st.get('seal_mode')}")

    # fresh keys → fresh handles (orders + sealed commits are owner-signed now)
    SELLER = register()
    BUYER = register()
    print(f"registered seller -> handle {SELLER}, buyer -> handle {BUYER}")

    # fund the two sides (faucet; no signing needed)
    deposit(SELLER, DOT, QTY)      # seller needs base to sell
    deposit(BUYER, USDC, QTY * PRICE)   # buyer needs quote to buy
    assert balance(SELLER, DOT) >= QTY and balance(BUYER, USDC) >= QTY * PRICE, "funding failed"
    seller_usdc_before = balance(SELLER, USDC)
    buyer_dot_before = balance(BUYER, DOT)

    # ── Round 1: a lone sealed SELL — nothing crosses, it must REST (not expire) ──
    place(SELLER, "sell", sealed=True)
    assert pending_count(SELLER) == 1, "sealed sell should be queued"
    run_auction()
    assert balance(SELLER, USDC) == seller_usdc_before, "R1: no settlement expected (nothing crossed)"
    assert pending_count(SELLER) == 1, \
        "R1 REGRESSION: the sealed sell must REST hidden, not be immediate-or-cancel"
    print("round 1: sealed SELL placed, auction ran -> no match, order RESTS hidden ✓")

    # ── Round 2: a sealed BUY that crosses -> both settle ──
    place(BUYER, "buy", sealed=True)
    run_auction()
    seller_usdc_after = balance(SELLER, USDC)
    buyer_dot_after = balance(BUYER, DOT)
    FEE = 0.03  # flat per-filled-order fee in the base asset (FEE_FLAT=300 atomic in the service)
    assert abs((buyer_dot_after - buyer_dot_before) - (QTY - FEE)) < 1e-6, \
        f"R2: buyer must receive {QTY} DOT − {FEE} fee, got {buyer_dot_after - buyer_dot_before}"
    assert seller_usdc_after > seller_usdc_before, \
        f"R2: seller must receive USDC proceeds, got {seller_usdc_after - seller_usdc_before}"
    assert pending_count(SELLER) == 0 and pending_count(BUYER) == 0, "both orders should have cleared"
    print(f"round 2: sealed BUY crosses the resting SELL -> SETTLED "
          f"(buyer +{QTY} DOT, seller +{seller_usdc_after - seller_usdc_before} USDC) ✓")
    print("\nALL ASSERTIONS PASSED — sealed orders rest hidden across auctions and cross "
          "a later counterparty (the reported bug is fixed, verified e2e on lasair).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
