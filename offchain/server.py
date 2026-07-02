#!/usr/bin/env python3
"""Jamswap off-chain layer — the round builder + a trading API + the UI.

This is the operating layer the plan calls Phase 6: it collects orders into a
pending batch per market, and on `/api/round` reads the market's resting book from
chain, assembles the work-package (book + pending), submits it to the JAM node
(TAG_MATCH), and clears the pending queue. It also serves the trading UI and proxies
balance/state reads. Stdlib only (http.server, urllib, struct).

  LASAIR_RPC=http://localhost:19900 PORT=8080 python3 offchain/server.py
"""
import hashlib, json, os, secrets, struct, subprocess, threading, time, urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from round import plan_round      # pure round planner (sealed carry-forward); tests/test_round_lifecycle.py
from treasury import jamkb_rent, profit_split, max_withdrawable, solvency, PROFIT_BENEFICIARY, PROFIT_BENEFICIARY_CHAIN

# service payload tags (must match service/src/lib.rs)
TAG_MATCH, TAG_DEPOSIT, TAG_COMMIT, TAG_REVEAL, TAG_CANCEL, TAG_WITHDRAW, TAG_LIST, TAG_REGISTER, TAG_TREASURY = range(9)
TAG_ENC_SETUP, TAG_ENC_COMMIT, TAG_ENC_ROUND = 9, 10, 11
FEE_ACCOUNT = 0xFFFFFFFF               # treasury handle (matches FEE_ACCOUNT in the service)

# Encrypt-until-batch (option 2): if a committee sidecar binary is available, sealed orders are
# ECIES-encrypted to an off-protocol committee and decrypted (with a Chaum-Pedersen proof refine
# verifies) at batch close — NO reveal round, no non-reveal griefing. Without the binary we fall
# back to commit–reveal (option 3). Set ENC_MODE=0 to force commit–reveal even if present.
COMMITTEE_BIN = os.environ.get("COMMITTEE_BIN", "")
ENC_MODE = bool(COMMITTEE_BIN) and os.environ.get("ENC_MODE", "1") == "1"
# Optional off-chain order-signature verification (the trustless in-refine version is the
# documented upgrade; the builder is a trusted role in the current model). Needs PyNaCl —
# if it's absent we degrade to accepting orders unsigned (withdraw/cancel stay trustless in
# the service regardless). Set REQUIRE_ORDER_SIG=0 to disable even when PyNaCl is present.
try:
    from nacl.signing import VerifyKey, SigningKey
    from nacl.exceptions import BadSignatureError
    HAVE_NACL = True
except Exception:
    HAVE_NACL = False
REQUIRE_ORDER_SIG = HAVE_NACL and os.environ.get("REQUIRE_ORDER_SIG", "1") == "1"

# --- self-funding treasury bootstrap + beneficiary access ---
# Deploy the service with a starting JAMKB state-rent reserve so it's solvent before fees
# accrue (0 = don't seed). The operator can raise it via env.
INITIAL_JAMKB_RESERVE = float(os.environ.get("INITIAL_JAMKB_RESERVE", "100") or 0)
# The demo governance seed (derives the service's GOV_PUBKEY — verified). When
# BENEFICIARY_SWEEP=1 the server holds this key so the OWNER can sweep profit to a
# beneficiary account over the API. PROTOTYPE-ONLY: it means anyone who can reach this
# server can move treasury profit — run it only where operator == owner. Default OFF; when
# off, sweeps must be gov-signed out-of-band (crates/committee). See docs/REVENUE.md.
GOV_SEED = (os.environ.get("GOV_SEED", "jamswap:demo:governance:key:v1!!")).encode()
BENEFICIARY_SWEEP = HAVE_NACL and os.environ.get("BENEFICIARY_SWEEP", "0") == "1"
def gov_sign(msg):                     # ed25519 signature by the governance key (matches GOV_PUBKEY)
    return bytes(SigningKey(GOV_SEED).sign(msg).signature)
# JAMKB standard: when the service holds less JAMKB than its state footprint requires, refuse
# to GROW state (new orders) until it's topped up or auctions free state. Degrades to a no-op
# on a node without the /footprint endpoint (obligation reads 0 → always solvent). Default ON.
# See docs/JAMKB_STANDARD.md.
JAMKB_BACKPRESSURE = os.environ.get("JAMKB_BACKPRESSURE", "1") == "1"

# assets + the six markets are config; the service itself is asset-agnostic.
USDC, DOT, JAMKB = 0, 1, 2
AUCTION_SECS = 6                       # auctions clear every 6s, like JAM block production
# Fixed-point price scale (must match SCALE in service/src/lib.rs). On-chain, prices,
# quantities, and balances are integer *atomic* units = display × SCALE, so a fractional
# price like 1.1050 is carried as 11050. We scale on the way IN (orders, deposits) and
# de-scale on the way OUT (book, mempool, balances, prices), so the UI speaks plain
# decimals while the chain/engine stay integer-only.
SCALE = 10_000                         # 4 decimal places
def to_atomic(x): return int(round(float(x) * SCALE))
def disp(v):                           # atomic int -> display number (int if whole)
    d = round(v / SCALE, 4)
    return int(d) if d == int(d) else d
_lock = threading.Lock()              # guards the pending books across request + auction threads
_next_auction = [0.0]                 # wall-clock of the next auction tick (for the UI countdown)

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
# good-till-time expiry (builder-enforced; the trustless on-chain version would carry an
# expiry field + a round counter in the service). (market, account, oid) -> unix expiry.
# When a round rewrites a market's book, resting orders past their expiry are dropped.
order_expiry = {}

def commitment(reveal_bytes):     # must match service commitment(): Blake2s256, 32B
    return hashlib.blake2s(reveal_bytes, digest_size=32).digest()

def committee_run(*args):
    # shell out to the committee sidecar; parse its "key hex" lines into a dict
    out = subprocess.run([COMMITTEE_BIN, *[str(a) for a in args]],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"committee {args[0]} failed: {out.stderr.strip()}")
    d = {}
    for line in out.stdout.strip().splitlines():
        p = line.split()
        if len(p) >= 2:
            d[p[0]] = p[1]
    return d

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
def handle_of(pubkey):                 # b"h"+pubkey(32) -> account handle (or None)
    v = storage(b"h" + pubkey); return int.from_bytes(v, "little") if v else None
def nonce_of(handle):                  # b"nc"+handle(4) -> per-account nonce
    v = storage(b"nc" + struct.pack("<I", handle)); return int.from_bytes(v, "little") if v else 0
def canon(action, *parts):             # must match canon() in service/src/lib.rs
    return b"jamswap:v1:" + action + b"".join(parts)
def mstate(prefix, m):
    v = storage(prefix + struct.pack("<I", m)); return int.from_bytes(v, "little") if v else 0
def book_of(m):
    bk = storage(b"book" + struct.pack("<I", m)); out = []
    for i in range(len(bk) // 17):
        a, oid, side, p, q = struct.unpack_from("<IIBII", bk, i * 17)
        out.append({"account": a, "id": oid, "side": "buy" if side == BUY else "sell",
                    "price": disp(p), "qty": disp(q)})
    return out

# ---- API handlers ---------------------------------------------------------
def api_deposit(b):
    submit(bytes([1]) + struct.pack("<IIQ", int(b["account"]), int(b["asset"]), to_atomic(b["amount"])))
    return {"ok": True}
def api_withdraw(b):
    # signed + replay-proof: the client signs canon(withdraw, handle, asset, amount, nonce)
    # with its account key; the SERVICE verifies (trustless). We just relay the bytes.
    handle, asset, nonce = int(b["account"]), int(b["asset"]), int(b["nonce"])
    amount = int(b["amount_atomic"])   # client scales + signs the atomic amount
    sig = bytes.fromhex(b["sig"])
    submit(bytes([TAG_WITHDRAW]) + struct.pack("<IIQQ", handle, asset, amount, nonce) + sig)
    return {"ok": True, "balance": disp(bal(asset, handle))}
def api_cancel(b):
    # signed cancel of a RESTING (on-chain) order: canon(cancel, handle, market, oid, nonce)
    handle, market, oid, nonce = int(b["account"]), int(b["market"]), int(b["order_id"]), int(b["nonce"])
    sig = bytes.fromhex(b["sig"])
    submit(bytes([TAG_CANCEL]) + struct.pack("<IIIQ", handle, market, oid, nonce) + sig)
    return {"ok": True}
def api_register(b):
    # bind an ed25519 pubkey to an account handle: canon(register, pubkey) signed by that key
    pubkey, sig = bytes.fromhex(b["pubkey"]), bytes.fromhex(b["sig"])
    submit(bytes([TAG_REGISTER]) + pubkey + sig)
    return {"ok": True, "handle": handle_of(pubkey)}   # None until accumulate lands
def api_handle(q):
    return {"handle": handle_of(bytes.fromhex(q["pubkey"]))}
def api_nonce(q):
    return {"nonce": nonce_of(int(q["handle"]))}
def footprint_octets():
    # the service's live state footprint in octets (validator RAM), from the node. 0 if
    # the node predates the footprint endpoint (rent then reads as 0 — fail-open, honest).
    try:
        return int(node(f"/v1/service/{SID}/footprint").get("octets", 0))
    except Exception:
        return 0
def rent_reserve_atomic():
    # JAMKB the treasury must hold to cover the state rent (1 JAMKB = 1 KB), in atomic units.
    return jamkb_rent(footprint_octets()) * SCALE
def jamkb_solvency():
    # JAMKB-standard invariant: held JAMKB reserve ≥ state footprint obligation.
    # Returns (solvent, shortfall_atomic). See docs/JAMKB_STANDARD.md.
    return solvency(bal(JAMKB, FEE_ACCOUNT), rent_reserve_atomic())
def treasury_status():
    # self-funding treasury view: rent (JAMKB) covered FIRST out of fees, only the surplus
    # is withdrawable profit (to the owner). See treasury.py / docs/REVENUE.md + JAMKB_STANDARD.md.
    reserve = rent_reserve_atomic()
    bals = {a: bal(a, FEE_ACCOUNT) for a in (USDC, DOT, JAMKB)}
    s = profit_split(bals, reserve)
    return {"treasury": {a: disp(v) for a, v in bals.items()},
            "rent_jamkb": disp(reserve), "reserve_held_jamkb": disp(s["reserve_held"]),
            "shortfall_jamkb": disp(s["shortfall"]), "solvent": s["solvent"],
            "withdrawable": {a: disp(v) for a, v in s["withdrawable"].items()},
            "beneficiary": PROFIT_BENEFICIARY, "beneficiary_chain": PROFIT_BENEFICIARY_CHAIN,
            "sweep_enabled": BENEFICIARY_SWEEP,   # can the owner sweep profit over the API (prototype)
            "backpressure": JAMKB_BACKPRESSURE and not s["solvent"],   # is new state growth blocked?
            "endowment_jamkb": INITIAL_JAMKB_RESERVE}   # the deploy-time reserve seeded
def api_reserve_topup(b):
    # Beneficiary top-up: add JAMKB to the treasury's state-rent reserve (deposit to
    # FEE_ACCOUNT). This is the "runway/backstop" inflow of the JAMKB standard — used when
    # usage doesn't yet cover the footprint, or to hold more state than fees fund.
    # (Prototype: a permissionless faucet-style credit; production = a signed transfer from
    # the beneficiary's own JAMKB holdings. See docs/JAMKB_STANDARD.md.)
    amount = to_atomic(b["amount"])
    if amount <= 0:
        raise ValueError("top-up amount must be positive")
    submit(bytes([TAG_DEPOSIT]) + struct.pack("<IIQ", FEE_ACCOUNT, JAMKB, amount))
    return {"ok": True, "reserve_jamkb": disp(bal(JAMKB, FEE_ACCOUNT))}
def api_treasury_status(q):
    return treasury_status()
def api_beneficiary_sweep(b):
    # Beneficiary access: sweep withdrawable PROFIT (any asset) from the treasury to a
    # destination account (the owner's trading account), from which they can swap
    # JAMKB/DOT/USDC on the DEX normally. Server-side gov-signed (prototype) — gated on
    # BENEFICIARY_SWEEP. Only profit is sweepable; the JAMKB rent reserve is never touched.
    if not BENEFICIARY_SWEEP:
        raise ValueError("beneficiary sweep is disabled — run with BENEFICIARY_SWEEP=1 "
                         "(prototype: server holds the demo gov key), or sweep out-of-band "
                         "with the committee CLI. See docs/REVENUE.md")
    asset, dest = int(b["asset"]), int(b["dest"])
    amount = to_atomic(b["amount"])
    allowed = max_withdrawable({a: bal(a, FEE_ACCOUNT) for a in (USDC, DOT, JAMKB)}, rent_reserve_atomic(), asset)
    if amount > allowed:
        raise ValueError(f"exceeds withdrawable profit: {disp(amount)} requested, "
                         f"{disp(allowed)} {ASSET_NAME.get(asset, asset)} available "
                         f"(the rest covers the JAMKB state rent)")
    nonce = int.from_bytes(storage(b"govnonce") or b"\0", "little")
    msg = canon(b"treasury", struct.pack("<I", asset), struct.pack("<Q", amount),
                struct.pack("<I", dest), struct.pack("<Q", nonce))
    submit(bytes([TAG_TREASURY]) + struct.pack("<IQIQ", asset, amount, dest, nonce) + gov_sign(msg))
    return {"ok": True, "swept": disp(amount), "asset": ASSET_NAME.get(asset, asset), "dest": dest}
def api_treasury(b):
    # governance fee sweep: relays a GOV-key-signed canon(treasury, asset, amount, dest, nonce).
    # OPERATOR POLICY (docs/REVENUE.md): only PROFIT is withdrawable — a sweep that would dip
    # into the JAMKB rent reserve is refused here (the service must stay solvent for its state).
    asset, amount, dest, nonce = int(b["asset"]), int(b["amount_atomic"]), int(b["dest"]), int(b["nonce"])
    allowed = max_withdrawable({a: bal(a, FEE_ACCOUNT) for a in (USDC, DOT, JAMKB)}, rent_reserve_atomic(), asset)
    if amount > allowed:
        raise ValueError(f"withdrawal exceeds profit: {ASSET_NAME.get(asset, asset)} "
                         f"{disp(amount)} requested, {disp(allowed)} withdrawable "
                         f"(the rest covers the JAMKB state rent — see docs/REVENUE.md)")
    submit(bytes([TAG_TREASURY]) + struct.pack("<IQIQ", asset, amount, dest, nonce) + bytes.fromhex(b["sig"]))
    return {"ok": True}
def api_govnonce(q):
    v = storage(b"govnonce"); return {"nonce": int.from_bytes(v, "little") if v else 0,
                                      "treasury": {a: disp(bal(a, FEE_ACCOUNT)) for a in (USDC, DOT, JAMKB)}}
def api_list(b):
    submit(bytes([6]) + struct.pack("<III", int(b["market"]), int(b["base"]), int(b["quote"])))
    return {"ok": True}
def pubkey_of_handle(handle):          # b"pk"+handle(4) -> the registered 32-byte key
    v = storage(b"pk" + struct.pack("<I", handle)); return v if len(v) >= 32 else None
def verify_order_sig(pubkey, msg, sig):
    if not HAVE_NACL:
        return True
    for m in (msg, b"<Bytes>" + msg + b"</Bytes>"):   # accept wallet <Bytes> framing too
        try:
            VerifyKey(pubkey).verify(m, sig); return True
        except BadSignatureError:
            continue
    return False
ASSET_NAME = {0: "USDC", 1: "DOT", 2: "JAMKB"}
# A market order is a *marketable limit* with a slippage guard: instead of an unbounded
# sentinel (which, in a thin book, would clear at an absurd uniform price), it crosses only
# within MARKET_BAND of the last clearing price. With no last price yet (cold market) a
# market order is refused — there's no reference to bound it, so use a limit order.
MARKET_BAND = 0.10                     # ±10% of the last price
def api_order(b):
    m = int(b["market"])
    side = BUY if b["side"] == "buy" else SELL
    otype = b.get("type", "limit")
    # JAMKB-standard backpressure: a new order grows service state (a resting order and/or a
    # sealed commitment). If the treasury is under-reserved — holding more RAM than its JAMKB
    # covers — refuse to grow further until it's topped up or auctions free state. Cancels and
    # the auctions that clear the book are never blocked, so a service can always recover.
    # No-op on a node that doesn't expose /footprint (obligation reads 0 → always solvent).
    if JAMKB_BACKPRESSURE:
        solvent, shortfall = jamkb_solvency()
        if not solvent:
            raise ValueError(f"service under-reserved on JAMKB (short {disp(shortfall)} KB) — "
                             f"top up the reserve before placing new orders")
    # atomic units on-chain: qty and limit price scale by SCALE.
    acct, qty = int(b["account"]), to_atomic(b["qty"])
    base, quote = int(b.get("base", -1)), int(b.get("quote", -1))
    if otype == "market":
        lp = mstate(b"lp", m)          # atomic last clearing price
        if lp <= 0:
            raise ValueError("no reference price yet on this market — place a limit order")
        price = int(round(lp * (1 + MARKET_BAND))) if side == BUY else max(1, int(round(lp * (1 - MARKET_BAND))))
    else:
        price = to_atomic(b["price"])
    # Order authentication (builder-side; the trustless in-refine check is the documented
    # upgrade): the client signs its intent with the account key. Market price is server-derived,
    # so it's signed as 0 (the band is applied here). Refuse if unregistered or the sig is bad.
    if REQUIRE_ORDER_SIG:
        pub = pubkey_of_handle(acct)
        if not pub:
            raise ValueError("account not registered — connect a wallet and register first")
        signed_price = 0 if otype == "market" else price
        msg = canon(b"order", struct.pack("<I", acct), struct.pack("<I", m), bytes([side]),
                    struct.pack("<I", qty), bytes([1 if otype == "market" else 0]),
                    bytes([1 if b.get("sealed") else 0]), struct.pack("<I", signed_price))
        if not verify_order_sig(pub, msg, bytes.fromhex(b.get("sig", ""))):
            raise ValueError("bad order signature")
    # Collateral guard (best-effort; on-chain escrow is the trustless version): refuse an
    # order the account can't currently fund. A buy needs qty·price/SCALE of the quote asset;
    # a sell needs qty of the base asset. Note: this checks the current on-chain balance only,
    # not funds already committed by other pending orders.
    if base >= 0 and quote >= 0:
        if side == BUY:
            need = (qty * price + SCALE - 1) // SCALE
            if bal(quote, acct) < need:
                raise ValueError(f"insufficient {ASSET_NAME.get(quote, quote)} to fund this buy (need {disp(need)})")
        elif bal(base, acct) < qty:
            raise ValueError(f"insufficient {ASSET_NAME.get(base, base)} to fund this sell (need {disp(qty)})")
    oid = next_oid[0]; next_oid[0] += 1
    ttl = float(b.get("ttl", 0) or 0)      # seconds; 0 = good-till-cancelled
    if ttl > 0:
        order_expiry[(m, acct, oid)] = time.time() + ttl
    o = {"account": acct, "oid": oid, "side": side, "price": price, "qty": qty, "type": otype,
         "sealed": bool(b.get("sealed")), "address": b.get("address", "")}
    if o["sealed"]:
        if ENC_MODE:
            # encrypt-until-batch: ECIES-encrypt the order to the committee key and post the
            # ciphertext (ENC_COMMIT). No nonce/reveal is ever needed — the committee decrypts
            # at batch close and refine verifies the decryption. The order terms never touch
            # the chain in the clear until they clear.
            seed = secrets.token_bytes(32).hex()
            d = committee_run("encrypt", m, order_bytes(acct, oid, side, price, qty).hex(), seed)
            o["ciphertext"] = d["ciphertext"]
            submit(bytes.fromhex(d["commit"]))
        else:
            # commit-reveal: publish ONLY the hash now (orders hidden on-chain), reveal at round
            nonce = secrets.token_bytes(32)
            o["reveal"] = order_bytes(acct, oid, side, price, qty) + nonce
            submit(bytes([TAG_COMMIT]) + struct.pack("<II", m, acct) + commitment(o["reveal"]))
    with _lock:
        pending.setdefault(m, []).append(o)
        n = len(pending[m])
    return {"ok": True, "order_id": oid, "sealed": o["sealed"], "type": otype, "pending": n}
def prune_expired(m, rest_bytes):
    # remove resting orders whose good-till-time has passed (they don't get re-included in the
    # rewritten book, so the round effectively cancels them). GTC orders (no expiry) are kept.
    now, out = time.time(), b""
    for i in range(len(rest_bytes) // 17):
        a, oid, side, p, q = struct.unpack_from("<IIBII", rest_bytes, i * 17)
        exp = order_expiry.get((m, a, oid))
        if exp and exp <= now:
            order_expiry.pop((m, a, oid), None); continue
        out += rest_bytes[i * 17:(i + 1) * 17]
    return out
def _parse_book(raw):
    # resting book bytes -> planner order dicts (integer side, atomic price)
    out = []
    for i in range(len(raw) // 17):
        a, oid, side, p, q = struct.unpack_from("<IIBII", raw, i * 17)
        out.append({"account": a, "oid": oid, "side": side, "price": p, "qty": q, "sealed": False})
    return out
def api_round(b):
    m, base, quote = int(b["market"]), int(b["base"]), int(b["quote"])
    now = time.time()
    hdr = struct.pack("<III", m, base, quote)
    raw = storage(b"book" + struct.pack("<I", m))        # the market's on-chain resting book
    rest = prune_expired(m, raw)                          # drop good-till-time orders past expiry
    shrank = len(rest) < len(raw)                         # some resting order expired this round
    with _lock:                        # snapshot + re-queue atomically so a concurrent
        pend = pending.get(m, [])      # api_order during submit isn't dropped
        for o in pend:                 # attach current GTT expiry for the planner
            o["expiry"] = order_expiry.get((m, o["account"], o["oid"]))
        # Decide which orders clear now. Sealed orders that DON'T cross current liquidity
        # rest HIDDEN (carried forward) rather than being immediate-or-cancel — so a sealed
        # sell placed now can meet a sealed buy placed in a later auction. A sealed order is
        # revealed only in the round it actually crosses (see round.py + tests).
        plan = plan_round(pend, _parse_book(rest), now)
        pending[m] = plan.carry        # non-crossing sealed orders stay hidden for next round
        for o in plan.expired:         # GTT-expired sealed orders that never found a counterparty
            order_expiry.pop((m, o["account"], o["oid"]), None)
    sealed, public = plan.reveal, plan.public
    public_bytes = b"".join(order_bytes(o["account"], o["oid"], o["side"], o["price"], o["qty"]) for o in public)
    if sealed and ENC_MODE:
        # encrypt-until-batch round: the committee decrypts each sealed ciphertext (proving it
        # via Chaum-Pedersen); refine verifies every proof, recovers the orders, and clears them
        # with the resting book + public orders at ONE uniform price. Only the sealed orders that
        # CROSS this round are here (the planner keeps non-crossing ones hidden for later), so a
        # sealed order is decrypted on-chain only in the round it actually trades. Any unfilled
        # remainder of a revealed order is immediate-or-cancel (never rests publicly exposed).
        # No reveal round — traders needn't be online at match time.
        cts = ",".join(o["ciphertext"] for o in sealed)
        d = committee_run("round", m, base, quote, (rest + public_bytes).hex(), cts)
        submit(bytes.fromhex(d["round"]))
    elif sealed:
        # UNIFIED sealed round (commit–reveal): the resting book + this round's public orders +
        # the revealed sealed orders all clear together at ONE uniform price (so a sealed order
        # can cross public/resting liquidity). Only sealed orders that CROSS this round are
        # revealed (non-crossing ones stay hidden, carried forward by the planner); the node
        # re-checks each reveal's hash ∈ commits. Any unfilled remainder of a revealed order is
        # immediate-or-cancel (never rests publicly exposed).
        commits = b"".join(commitment(o["reveal"]) for o in sealed)
        reveals = b"".join(o["reveal"] for o in sealed)
        submit(bytes([TAG_REVEAL]) + hdr
               + struct.pack("<I", len(commits)) + commits
               + struct.pack("<I", len(reveals)) + reveals
               + rest + public_bytes)
    elif public or shrank:
        # plaintext round: resting book + this round's public orders -> MATCH. Also runs on
        # `shrank` (an order expired) with no new orders, to rewrite the book without the
        # expired one — an empty cross conserves value and leaves the last price untouched
        # (apply_settlement only updates lp when something actually fills).
        submit(bytes([TAG_MATCH]) + hdr + rest + public_bytes)
    return {"ok": True, "price": disp(mstate(b"lp", m)), "volume": disp(mstate(b"cv", m)),
            "book": book_of(m), "cleared": {"sealed": len(sealed), "public": len(public),
            "resting_hidden": len(plan.carry), "expired": len(plan.expired)}}
def short(a):
    return (a[:6] + "…" + a[-4:]) if a and len(a) > 12 else a
def mempool_entry(o, owner=False):
    # owner=True ⇒ the requester owns this order, so a SEALED order's terms are revealed
    # to them (they hold the nonce); to everyone else, sealed terms stay hidden.
    e = {"oid": o["oid"], "account": o["account"], "side": "buy" if o["side"] == BUY else "sell",
         "sealed": o["sealed"], "type": o.get("type", "limit"),
         "who": short(o.get("address", "")) or f"acct {o['account']}"}
    e["price"], e["qty"] = (None, None) if (o["sealed"] and not owner) else (disp(o["price"]), disp(o["qty"]))
    return e
def api_state(q):
    m = int(q.get("market", "1"))
    mempool = [mempool_entry(o) for o in pending.get(m, [])]
    # sealed orders live on-chain as commit hashes (option 3) or ciphertexts (option 2); both
    # are 32-byte / fixed-size entries in per-market sets — count whichever this mode uses.
    seal_key = b"encset" if ENC_MODE else b"commits"
    onchain_sealed = len(storage(seal_key + struct.pack("<I", m))) // 32
    return {"price": disp(mstate(b"lp", m)), "volume": disp(mstate(b"cv", m)), "book": book_of(m),
            "pending": len(pending.get(m, [])), "mempool": mempool, "sealed_onchain": onchain_sealed,
            "seal_mode": "encrypt-until-batch" if ENC_MODE else "commit-reveal",
            "next_auction_in": round(max(0.0, _next_auction[0] - time.time()), 1), "auction_secs": AUCTION_SECS}
def api_mine(q):
    # a trader's own queued orders, across all markets — sealed terms DECRYPTED for them
    acct = int(q["account"])
    out = []
    for mid, orders in pending.items():
        for o in orders:
            if o["account"] == acct:
                e = mempool_entry(o, owner=True); e["market"] = mid; out.append(e)
    return {"orders": out}
def api_cancel_pending(b):
    # remove an un-processed (not yet cleared) order from the mempool, owner-checked
    acct, oid = int(b["account"]), int(b["order_id"])
    removed = 0
    with _lock:
        for mid, orders in pending.items():
            keep = [o for o in orders if not (o["account"] == acct and o["oid"] == oid)]
            removed += len(orders) - len(keep); pending[mid] = keep
    return {"ok": True, "removed": removed}
def api_balance(q):
    return {"balance": disp(bal(int(q["asset"]), int(q["account"])))}
def api_footprint(q):
    # the service's live state footprint (validator RAM) + the JAMKB it implies.
    # JAMKB is a READ-ONLY tracker for now: 1 JAMKB = 1 KB of footprint. This is a
    # measurement only — nothing is held, funded, or consumed. Whether to enforce a
    # reserve/consumption model in the node is a deferred protocol decision (docs/JAMKB.md).
    try:
        fp = node(f"/v1/service/{SID}/footprint")
    except Exception:
        # older lasair-node (< the footprint endpoint) — degrade gracefully
        return {"available": False}
    fp["available"] = True
    return fp

ROUTES_POST = {"/api/deposit": api_deposit, "/api/withdraw": api_withdraw,
               "/api/list": api_list, "/api/order": api_order, "/api/round": api_round,
               "/api/cancel_pending": api_cancel_pending, "/api/register": api_register,
               "/api/cancel": api_cancel, "/api/treasury": api_treasury,
               "/api/beneficiary_sweep": api_beneficiary_sweep,
               "/api/reserve_topup": api_reserve_topup}

# the markets the UI shows; listed once at startup so they're tradable.
# every combination of the three assets: (market_id, base, quote)
DEFAULT_MARKETS = [(1, DOT, USDC), (2, JAMKB, USDC), (3, JAMKB, DOT)]
def ensure_markets():
    for m, base, quote in DEFAULT_MARKETS:
        try: api_list({"market": m, "base": base, "quote": quote})
        except Exception as e: print("list failed", m, e)

def ensure_reserve():
    # deploy with a starting JAMKB state-rent reserve so the treasury is solvent before any
    # fees accrue. Tops up to INITIAL_JAMKB_RESERVE if under it; idempotent across restarts.
    if INITIAL_JAMKB_RESERVE <= 0:
        return
    try:
        target = to_atomic(INITIAL_JAMKB_RESERVE)
        held = bal(JAMKB, FEE_ACCOUNT)
        if held >= target:
            print(f"treasury JAMKB reserve already funded: {disp(held)} JAMKB"); return
        submit(bytes([TAG_DEPOSIT]) + struct.pack("<IIQ", FEE_ACCOUNT, JAMKB, target - held))
        print(f"seeded treasury JAMKB reserve -> {disp(bal(JAMKB, FEE_ACCOUNT))} JAMKB")
    except Exception as e:
        print("reserve seeding skipped:", e)

def ensure_committee():
    # encrypt-until-batch: commit the off-protocol committee keys on-chain (gov-signed), once.
    # Idempotent — if a committee is already committed (node reused across runs), do nothing.
    if not ENC_MODE:
        return
    try:
        if storage(b"committee"):
            print("encrypt-until-batch: committee already committed on-chain"); return
        d = committee_run("setup")
        submit(bytes.fromhex(d["setup"]))
        ok = bool(storage(b"committee"))
        print(f"encrypt-until-batch ENABLED — committee committed on-chain: {ok}")
    except Exception as e:
        print("committee setup failed (falling back to commit-reveal):", e)
# ---- trade tape (per-market recent-fills history) -------------------------
# A clearing print is recorded whenever a market's on-chain CUMULATIVE volume grows.
# This is robust for both the immediate single-node path and the slot-delayed testnet
# path (cv is cumulative, so a settlement is caught on a later tick even if it lands a
# block or two after the round). Prints are kept for TRADE_TTL (24h) OR up to
# TRADE_HISTORY entries, whichever is hit first — then the oldest roll off. The tape is
# persisted to disk so it survives a server restart (set TRADES_FILE to a mounted path
# for cross-container persistence; the default /tmp path survives a process restart).
TRADE_HISTORY = 500                    # hard count cap per market (deque maxlen)
TRADE_TTL = 24 * 3600                  # keep clearing prints for 24h, then roll off
TRADES_FILE = os.environ.get("TRADES_FILE", "/tmp/jamswap_trades.json")
trades = {}                            # market_id -> deque[{ts, price, volume, dir}]
_last_cv = {}                          # market_id -> last-seen cumulative volume (atomic)
def _prune_trades(m, now):
    dq = trades.get(m)
    if not dq:
        return
    cutoff = now - TRADE_TTL           # drop prints older than 24h
    while dq and dq[0]["ts"] < cutoff:
        dq.popleft()
def _save_trades():
    try:
        with open(TRADES_FILE, "w") as fh:
            json.dump({str(m): list(dq) for m, dq in trades.items()}, fh)
    except Exception:
        pass                           # read-only FS or similar — tape just won't persist
def load_trades():
    try:
        with open(TRADES_FILE) as fh:
            data = json.load(fh)
        now = time.time()
        for k, lst in data.items():
            dq = deque([t for t in lst if t.get("ts", 0) >= now - TRADE_TTL], maxlen=TRADE_HISTORY)
            if dq:
                trades[int(k)] = dq
        if trades:
            print(f"loaded trade tape: {sum(len(d) for d in trades.values())} recent prints")
    except Exception:
        pass                           # no prior tape — start fresh
def record_trade(m):
    cv = mstate(b"cv", m)              # atomic cumulative base volume settled on this market
    prev = _last_cv.get(m)
    if prev is None:                   # first sight — seed without emitting (don't dump prior cv)
        _last_cv[m] = cv; return
    if cv > prev:
        now = time.time()
        price = disp(mstate(b"lp", m))
        dq = trades.setdefault(m, deque(maxlen=TRADE_HISTORY))
        prev_price = dq[-1]["price"] if dq else None
        direction = "flat" if prev_price is None or price == prev_price else ("up" if price > prev_price else "down")
        dq.append({"ts": now, "price": price, "volume": disp(cv - prev), "dir": direction})
        _last_cv[m] = cv
        _prune_trades(m, now)
        _save_trades()
def api_trades(q):
    # recent cleared trades + volume metrics for one market (the active pair).
    m = int(q.get("market", "1"))
    _prune_trades(m, time.time())      # roll off anything older than 24h even without a new trade
    dq = list(trades.get(m, ()))
    prices = [t["price"] for t in dq]
    return {"trades": list(reversed(dq))[:100],   # most-recent first, last ~100 prints
            "metrics": {"last": disp(mstate(b"lp", m)),
                        "volume": round(sum(t["volume"] for t in dq), 4),   # base traded, 24h window
                        "trades": len(dq),
                        "high": max(prices) if prices else None,
                        "low": min(prices) if prices else None,
                        "window_hours": 24}}
ROUTES_GET = {"/api/state": api_state, "/api/balance": api_balance, "/api/mine": api_mine,
              "/api/footprint": api_footprint, "/api/handle": api_handle, "/api/nonce": api_nonce,
              "/api/govnonce": api_govnonce, "/api/treasury_status": api_treasury_status,
              "/api/trades": api_trades}

def has_expired(m):
    now = time.time()
    return any(mid == m and exp <= now for (mid, _a, _o), exp in list(order_expiry.items()))
def auction_loop():
    # clear every market every AUCTION_SECS, mirroring JAM's 6s block cadence. Runs a round
    # when orders are queued OR a resting order has expired (to prune it); otherwise idle.
    _next_auction[0] = time.time() + AUCTION_SECS
    while True:
        time.sleep(max(0.0, _next_auction[0] - time.time()))
        _next_auction[0] = time.time() + AUCTION_SECS
        for m, base, quote in DEFAULT_MARKETS:
            if pending.get(m) or has_expired(m):
                try: api_round({"market": m, "base": base, "quote": quote})
                except Exception as e: print("auction round failed", m, e)
            # record a clearing print if this market's cumulative volume grew (works even
            # when settlement lands a slot later than the round, e.g. on the testnet).
            try: record_trade(m)
            except Exception as e: print("trade record failed", m, e)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json", no_cache=False):
        self.send_response(code); self.send_header("Content-Type", ctype)
        # the UI (index.html/JS) is served from a live volume mount and changes often —
        # tell the browser never to cache it, so a redeploy is always picked up on refresh
        # (a stale cached UI calling new/old endpoints was a real footgun).
        if no_cache:
            self.send_header("Cache-Control", "no-store, must-revalidate")
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
                self._send(200, data, ctype, no_cache=True)
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
    load_trades()
    try: ensure_markets(); ensure_reserve(); print("listed default markets:", DEFAULT_MARKETS)
    except Exception as e: print("market listing skipped:", e)
    ensure_committee()
    threading.Thread(target=auction_loop, daemon=True).start()
    print(f"auction loop running every {AUCTION_SECS}s (like JAM block production)")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
