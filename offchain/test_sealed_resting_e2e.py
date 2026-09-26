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
  Round 3: a trader's PUBLIC buy (seq s) followed by the same trader's SEALED order
           (commit seq > s), with the commit ON-CHAIN before the buy's round — the order
           in which they land in practice (the commit is a standalone work-item submitted
           at placement; the buy waits for an auction). The buy must still fill. (Before
           the service kept a separate commit floor, the commit raised the shared seq
           floor past the buy, which was then rejected: the 2026-09-24 soak failure.)

Unlike a raw work-item harness (pre-baked committee payloads straight to the chain),
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
    # registration lands when its work-report accumulates: seconds on a healthy net, but a
    # minute or more while blocks are slow — and the server retries it for REG_GIVEUP_SECS
    # (240 s), so wait inside that window rather than a fixed 30 s
    for _ in range(90):
        h = get(f"/api/handle?pubkey={pk.hex()}").get("handle")
        if h:
            KEYS[h] = sk
            return h
        time.sleep(2)
    raise RuntimeError("registration didn't land on-chain within 180 s")


def next_seq():
    # ms wall clock: strictly rising per account across runs (the on-chain floor persists)
    return int(time.time() * 1000)


def place(account, side, sealed, price=PRICE, seq=None):
    # orders (and, for sealed, the on-chain commitment) are OWNER-SIGNED — the service
    # verifies both, so this test manages real keys like the UI does. `seq` lets a caller
    # sign with a seq it drew earlier (round 3 reproduces a signing/landing order that way).
    sk = KEYS[account]
    seq = seq or next_seq()
    msg = canon(b"order", p32(account), p32(MARKET), bytes([0 if side == "buy" else 1]),
                p32(QTY * SCALE), b"\0", bytes([1 if sealed else 0]),
                p32(price * SCALE), p64(seq))
    body = {"market": MARKET, "base": DOT, "quote": USDC, "account": account,
            "side": side, "qty": QTY, "price": price, "type": "limit",
            "sealed": sealed, "seq": seq, "sig": sk.sign(msg).signature.hex()}
    if sealed:
        prep = post("/api/seal_prepare", {"market": MARKET, "account": account, "side": side,
                                          "qty": QTY, "price": price, "type": "limit"})
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


def wait_for(pred, what, timeout=300):
    # Over JAMNP-S/QUIC a submission returns once a guarantor has it; the state change lands
    # when the work-report accumulates (and a settlement after its finality hold), several
    # slots later. So every on-chain effect is awaited, not read back immediately.
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return time.time() - t0
        time.sleep(2)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


REST_WINDOW = 45   # ~7 auctions (AUCTION_SECS = 6): long enough for a lone order to clear if it would


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
    TRADER = register()            # round 3: public buy, then a sealed order, same account
    COUNTER = register()           # round 3: the public buy's counterparty
    print(f"registered seller -> handle {SELLER}, buyer -> handle {BUYER}, "
          f"trader -> handle {TRADER}, counter -> handle {COUNTER}")

    # fund the sides (faucet; no signing needed). A PUBLIC order pays the flat per-order fee
    # (0.03 base) out of its base balance, so round 3's accounts get a unit of headroom.
    deposit(SELLER, DOT, QTY)      # seller needs base to sell
    deposit(BUYER, USDC, QTY * PRICE)   # buyer needs quote to buy
    deposit(TRADER, USDC, QTY * PRICE)
    deposit(TRADER, DOT, QTY + 1)       # the sealed sell + the fee headroom
    deposit(COUNTER, DOT, QTY + 1)
    wait_for(lambda: balance(SELLER, DOT) >= QTY and balance(BUYER, USDC) >= QTY * PRICE
             and balance(TRADER, USDC) >= QTY * PRICE and balance(TRADER, DOT) >= QTY + 1
             and balance(COUNTER, DOT) >= QTY + 1, "funding")
    seller_usdc_before = balance(SELLER, USDC)
    buyer_dot_before = balance(BUYER, DOT)

    # ── Round 1: a lone sealed SELL — nothing crosses, it must REST (not expire) ──
    place(SELLER, "sell", sealed=True)
    assert pending_count(SELLER) == 1, "sealed sell should be queued"
    run_auction()
    time.sleep(REST_WINDOW)          # the server auctions every 6 s on its own
    assert balance(SELLER, USDC) == seller_usdc_before, "R1: no settlement expected (nothing crossed)"
    assert pending_count(SELLER) == 1, \
        "R1 REGRESSION: the sealed sell must REST hidden, not be immediate-or-cancel"
    print("round 1: sealed SELL placed, auction ran -> no match, order RESTS hidden ✓")

    # ── Round 2: a sealed BUY that crosses -> both settle ──
    place(BUYER, "buy", sealed=True)
    run_auction()
    took = wait_for(lambda: balance(BUYER, DOT) > buyer_dot_before
                    and pending_count(SELLER) == 0 and pending_count(BUYER) == 0,
                    "the crossing sealed round to settle")
    seller_usdc_after = balance(SELLER, USDC)
    buyer_dot_after = balance(BUYER, DOT)
    # sealed reveals trade fee-free: the flat per-order fee rides the PUBLIC order bindings
    # only (service apply_settlement, a deliberate simplification — docs/TOKENS.md)
    assert abs((buyer_dot_after - buyer_dot_before) - QTY) < 1e-6, \
        f"R2: buyer must receive {QTY} DOT (sealed: no fee), got {buyer_dot_after - buyer_dot_before}"
    assert seller_usdc_after > seller_usdc_before, \
        f"R2: seller must receive USDC proceeds, got {seller_usdc_after - seller_usdc_before}"
    assert pending_count(SELLER) == 0 and pending_count(BUYER) == 0, "both orders should have cleared"
    print(f"round 2: sealed BUY crosses the resting SELL -> SETTLED in {took:.0f}s "
          f"(buyer +{QTY} DOT, seller +{seller_usdc_after - seller_usdc_before} USDC) ✓")

    # ── Round 3: public buy, THEN a sealed order from the same account -> the buy still fills ──
    trader_dot_before = balance(TRADER, DOT)
    counter_usdc_before = balance(COUNTER, USDC)
    buy_seq = next_seq()                                   # the buy is signed first (seq s) ...
    commits_before = get("/api/state?market=1")["sealed_onchain"]
    place(TRADER, "sell", sealed=True, price=PRICE * 100)  # ... then the sealed order: commit
    #                                                        seq > s. A sell far above the
    #                                                        market: it just rests hidden.
    # deterministic worst case: the commit (higher seq) is ON-CHAIN before the buy's round
    wait_for(lambda: get("/api/state?market=1")["sealed_onchain"] > commits_before,
             "the trader's sealed commit to land")
    buy = place(TRADER, "buy", sealed=False, seq=buy_seq)
    place(COUNTER, "sell", sealed=False)                   # the public buy's counterparty
    run_auction()
    took = wait_for(lambda: balance(TRADER, DOT) > trader_dot_before
                    and balance(COUNTER, USDC) > counter_usdc_before,
                    "the trader's public buy to fill despite its own later sealed commit")
    got = balance(TRADER, DOT) - trader_dot_before
    # +QTY bought, minus the flat 0.03 per-order fee the public buy pays in base
    assert abs(got - (QTY - 0.03)) < 1e-6, f"R3: trader must receive {QTY} DOT less the fee, got {got}"
    # the receipt follows the balance by a resolver sweep (it is written once the builder
    # sees the round's landed marker twice, a resolver period apart), so await it too
    def receipt():
        return [e for e in get(f"/api/executions?account={TRADER}")["executions"]
                if e["oid"] == buy["order_id"]]
    wait_for(receipt, "the public buy's receipt", timeout=60)
    fills = receipt()
    assert fills[0]["disposition"] == "filled", \
        f"R3 REGRESSION: the public buy must end filled, got {fills}"
    print(f"round 3: public BUY (seq s) after the same account's SEALED commit (seq > s) "
          f"landed -> the buy FILLED in {took:.0f}s (the commit no longer rejects it) ✓")
    print("\nALL ASSERTIONS PASSED — sealed orders rest hidden across auctions and cross "
          "a later counterparty, and a trader's own sealed commit never sinks their earlier "
          "public order (verified e2e on lasair).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
