#!/usr/bin/env python3
"""E2E: account-signed operations on a live net: withdraw and cancel.

Since GP 0.8.0 the service verifies every signature in refine, and accumulate binds the
signer's key to the account (service/src/lib.rs authenticate()). Under 0.8.0 gas, one ed25519
verify no longer fits a work-report's accumulate budget. This test drives both operations
through the DEX API, which appends the account's registered key, and checks the accept and
reject paths end to end:
  - a withdraw signed by the account key debits the balance and advances the nonce;
  - the same withdraw replayed (stale nonce) changes nothing;
  - a withdraw signed by a different key changes nothing;
  - a signed cancel removes the account's resting order.

## Run it
    ./dex up
    JAMSWAP_URL=http://127.0.0.1:8081 python3 offchain/test_signed_ops_e2e.py

If the server isn't reachable it SKIPS (exit 0) so it never breaks CI, which has no node.
"""
import sys
import time
import urllib.error

from test_sealed_resting_e2e import (DOT, KEYS, MARKET, SCALE, URL, USDC, SigningKey, balance,
                                     canon, get, next_seq, p32, p64, post, register, wait_for)

SETTLE = 45   # a rejected op has no on-chain effect to wait for: give it ~7 slots to (not) land


def nonce(handle):
    return get(f"/api/nonce?handle={handle}")["nonce"]


def withdraw(handle, asset, amount, n, sk):
    atomic = int(round(amount * SCALE))
    msg = canon(b"withdraw", p32(handle), p32(asset), p64(atomic), p64(n))
    return post("/api/withdraw", {"account": handle, "asset": asset, "amount_atomic": atomic,
                                  "nonce": n, "sig": sk.sign(msg).signature.hex()})


def resting(handle, oid):
    return any(o.get("oid") == oid and o.get("status") == "resting"
               for o in get(f"/api/mine?account={handle}").get("orders", []))


def listed(handle, oid):
    return any(o.get("oid") == oid for o in get(f"/api/mine?account={handle}").get("orders", []))


def main():
    try:
        get("/api/state?market=1")
    except (urllib.error.URLError, ConnectionError) as e:
        print(f"SKIP — no jamswap server at {URL} ({e}). Bring up `./dex up` and set JAMSWAP_URL.")
        return 0

    h = register()
    sk = KEYS[h]
    post("/api/deposit", {"account": h, "asset": DOT, "amount": 5})
    post("/api/deposit", {"account": h, "asset": USDC, "amount": 5})
    wait_for(lambda: balance(h, DOT) >= 5 and balance(h, USDC) >= 5, "funding")
    print(f"registered + funded handle {h}")

    n0, b0 = nonce(h), balance(h, DOT)
    withdraw(h, DOT, 2, n0, sk)
    wait_for(lambda: nonce(h) == n0 + 1, "the signed withdraw to accumulate")
    assert abs(balance(h, DOT) - (b0 - 2)) < 1e-6, f"withdraw: expected {b0 - 2} DOT, got {balance(h, DOT)}"
    print("withdraw signed by the account key: 2 DOT debited, nonce advanced ✓")

    b1 = balance(h, DOT)
    withdraw(h, DOT, 2, n0, sk)                        # replay: the nonce is spent
    time.sleep(SETTLE)
    assert nonce(h) == n0 + 1 and balance(h, DOT) == b1, "a replayed withdraw must not land"
    print("same withdraw replayed with the spent nonce: rejected ✓")

    withdraw(h, DOT, 1, n0 + 1, SigningKey.generate())  # right nonce, someone else's key
    time.sleep(SETTLE)
    assert nonce(h) == n0 + 1 and balance(h, DOT) == b1, "a withdraw signed by another key must not land"
    print("withdraw signed by a different key: rejected ✓")

    # a public limit buy far below the market rests on-chain; then the owner cancels it
    price, qty, seq = 0.5, 1, next_seq()
    msg = canon(b"order", p32(h), p32(MARKET), bytes([0]), p32(qty * SCALE), b"\0", b"\0",
                p32(int(price * SCALE)), p64(seq))
    r = post("/api/order", {"market": MARKET, "base": DOT, "quote": USDC, "account": h,
                            "side": "buy", "qty": qty, "price": price, "type": "limit",
                            "sealed": False, "seq": seq, "sig": sk.sign(msg).signature.hex()})
    assert not r.get("error"), f"order rejected: {r}"
    oid = r["order_id"]
    wait_for(lambda: resting(h, oid), f"order {oid} to rest on-chain")
    n1 = nonce(h)
    cmsg = canon(b"cancel", p32(h), p32(MARKET), p32(oid), p64(n1))
    post("/api/cancel", {"account": h, "market": MARKET, "order_id": oid, "nonce": n1,
                         "sig": sk.sign(cmsg).signature.hex()})
    wait_for(lambda: nonce(h) == n1 + 1 and not listed(h, oid), "the signed cancel to accumulate")
    print(f"signed cancel of resting order {oid}: removed from the book ✓")

    print("\nALL ASSERTIONS PASSED — account-signed ops verified in refine, bound in accumulate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
