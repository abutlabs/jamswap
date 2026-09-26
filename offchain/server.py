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
import metrics                     # /metrics + the submit->settle pending ledger
import order_telemetry            # per-order lifecycle SLO (placement -> durable clear)
from round import plan_round      # pure round planner (sealed carry-forward); tests/test_round_lifecycle.py
from clearing import clear        # builder-side clearing (mirrors refine); for per-order fill receipts
from treasury import (jamkb_rent, profit_split, max_withdrawable, solvency, reserve_target,
                      JAMKB_SUPPLY, PROFIT_BENEFICIARY, PROFIT_BENEFICIARY_CHAIN)

# service payload tags (must match service/src/lib.rs)
TAG_MATCH, TAG_DEPOSIT, TAG_COMMIT, TAG_REVEAL, TAG_CANCEL, TAG_WITHDRAW, TAG_LIST, TAG_REGISTER, TAG_TREASURY = range(9)
TAG_ENC_SETUP, TAG_ENC_COMMIT, TAG_ENC_ROUND = 9, 10, 11
# TAG_MATCH (0) is RETIRED in the service: public rounds are now TAG_SMATCH, whose orders
# carry the trader's pubkey + ed25519 signature and are verified IN REFINE (trustless — the
# builder can no longer inject an order nobody signed). See service/src/lib.rs.
TAG_SMATCH = 12
FLAG_MARKET = 1  # order-type flag carried beside a signed order (part of the signed message)
# Sealed commits are OWNER-SIGNED (verified in the service; commit/enc set entries bind
# hash‖account). Carry-forward re-seals are builder-posted but allowance-gated on-chain.
TAG_CARRY_COMMIT, TAG_CARRY_ENC_COMMIT = 13, 14
SET_ENTRY_LEN = 36  # commit/enc set entries: hash(32) ‖ account(4)
FEE_ACCOUNT = 0xFFFFFFFF               # treasury handle (matches FEE_ACCOUNT in the service)

# Sealing mode. The BASE STATE is commit–reveal (rung 3): fully permissionless — no committee,
# no extra operators, no asks of validators or client teams. Encrypt-until-batch (rung 2) is an
# OPT-IN upgrade (ENC_MODE=1 + a committee sidecar binary): sealed orders are ECIES-encrypted to
# an off-protocol committee and decrypted (with a Chaum-Pedersen proof refine verifies) at batch
# close — but its committee is a simulation until the open work in docs/COMMITTEE_DEPLOYMENT.md
# lands, so it stays opt-in rather than the default.
COMMITTEE_BIN = os.environ.get("COMMITTEE_BIN", "")
ENC_MODE = bool(COMMITTEE_BIN) and os.environ.get("ENC_MODE", "0") == "1"
# Order signatures are now enforced ON-CHAIN: each public order's ed25519 signature travels in
# the work package and is verified per-order in refine (the service also binds the key to the
# account registry + enforces a per-account replay floor in accumulate). The check below is a
# PREFLIGHT ONLY — it gives the trader an instant error instead of a silently-dropped round.
# Needs PyNaCl; REQUIRE_ORDER_SIG=0 skips the preflight (the service still enforces).
try:
    from nacl.signing import VerifyKey, SigningKey
    from nacl.exceptions import BadSignatureError
    HAVE_NACL = True
except Exception:
    HAVE_NACL = False
REQUIRE_ORDER_SIG = HAVE_NACL and os.environ.get("REQUIRE_ORDER_SIG", "1") == "1"

# --- self-funding treasury bootstrap + beneficiary access ---
# JAMKB is FINITE (see treasury.JAMKB_SUPPLY). A service holds only enough to back its
# footprint plus a small operational buffer — never a hoard — because every JAMKB it holds
# is RAM some other service can't use. This buffer (KB of headroom above the live obligation)
# is the ONLY slack the reserve targets; the endowment and top-ups are capped at
# obligation+buffer. (Was `INITIAL_JAMKB_RESERVE`, a flat mint that could balloon meaninglessly.)
RESERVE_BUFFER_KB = int(os.environ.get("JAMKB_RESERVE_BUFFER", "8") or 0)
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
# Standard-service path. When BUILDER_URL is set, work-items are submitted through the
# JAMNP-S builder daemon (which wraps each payload in a GP work-package and submits it
# to the node's guarantor over CE-133/QUIC — refine -> accumulate), instead of the node's
# operator-RPC /item route. This is what makes jamswap a STANDARD JAM service: it reaches
# the chain through the published network protocol, not a lasair-specific API. Service
# DEPLOY and all storage READS still use the node RPC (deploy is an operator action; the
# guarantor shares the node's in-process service registry, so state settles in one place).
BUILDER_URL = os.environ.get("BUILDER_URL", "").rstrip("/")
# Storage READS: when READER_URL is set, on-chain service storage is read through the
# lasair-reader daemon (which turns each GET into a JAMNP-S CE-129 state request to the
# node over QUIC), instead of the node's operator-RPC storage route. Together with
# BUILDER_URL (submit -> CE-133), this makes jamswap reach the chain ENTIRELY through
# the published QUIC/JAMNP protocol — no lasair-specific HTTP API. In this mode the
# service is DEPLOYED by being seeded into genesis (Chain.seed_service), so its id is
# fixed and SERVICE_ID is required (there is no runtime deploy over QUIC).
READER_URL = os.environ.get("READER_URL", "").rstrip("/")
QUIC_MODE = bool(READER_URL or BUILDER_URL)
# Service id: an explicit SERVICE_ID wins; otherwise (node-RPC mode only) we DEPLOY the
# blob ($JAM) at startup and use whatever id the node assigns. Deploying here (rather
# than trusting a hardcoded id) is what keeps the UI pointed at THIS service — node
# service ids are assigned sequentially, so a node reused across runs drifts 1729 -> ...
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
def reader_get(path):
    # local HTTP bridge to the lasair-reader daemon -> CE-129/QUIC to the node
    req = urllib.request.Request(READER_URL + path, method="GET")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())

# ---- finality (β) visibility ---------------------------------------------
# A settled fill is on-chain and its balance delta is already applied, but the
# block it landed in is not yet GRANDPA-finalized (β) — a short "Finalizing"
# window (~finality lag, measured ~2 blocks / ~12s on the all-lasair net) before
# it is irreversible. We surface it by scraping the node's Prometheus gauges
# (lasair_finalized_height / _block_height) and stamping each fill with the head
# height at settle time; the UI compares settle_height vs finalized_height.
NODE_METRICS_URL = os.environ.get("NODE_METRICS_URL", "").strip()
_fin_cache = {"t": 0.0, "v": None}
def _read_finality():
    # {available, finalized_height, finalized_slot, block_height, slot, lag}; cached ~2s.
    if not NODE_METRICS_URL:
        return {"available": False}
    tnow = time.time()
    if _fin_cache["v"] is not None and tnow - _fin_cache["t"] < 2.0:
        return _fin_cache["v"]
    try:
        txt = urllib.request.urlopen(NODE_METRICS_URL, timeout=2).read().decode()
        g = {}
        for ln in txt.splitlines():
            if ln.startswith("lasair_") and " " in ln:
                k, _, val = ln.partition(" ")
                try: g[k] = float(val)
                except ValueError: pass
        fh, bh = int(g.get("lasair_finalized_height", 0)), int(g.get("lasair_block_height", 0))
        v = {"available": fh > 0, "finalized_height": fh, "block_height": bh,
             "finalized_slot": int(g.get("lasair_finalized_slot", 0)),
             "slot": int(g.get("lasair_slot", 0)), "lag": max(0, bh - fh)}
    except Exception:
        v = {"available": False}
    _fin_cache["t"], _fin_cache["v"] = tnow, v
    return v
def api_finality(q):
    return _read_finality()
def order_bytes(a, oid, side, p, q): return struct.pack("<IIBII", a, oid, side, p, q)
def _post_json(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
        headers={"content-type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())
# payload tag byte -> human op name, for the metrics ledger (matches the TAG_* consts)
TAG_NAMES = {0: "match", 1: "deposit", 2: "commit", 3: "reveal", 4: "cancel", 5: "withdraw",
             6: "list", 7: "register", 8: "treasury", 9: "enc_setup", 10: "enc_commit",
             11: "enc_round", 12: "round", 13: "carry_commit", 14: "carry_enc_commit"}
class ChainBusy(Exception):
    """Every guarantor refused the CE-133 submission (mempool at --wp-queue-cap):
    the payload never reached the chain. Callers either retry-later (api_round
    re-queues the round's orders) or surface it as HTTP 503 (user ops)."""

def submit(payload, check=None, detail=""):
    # STANDARD path when BUILDER_URL is set: the builder daemon wraps this payload in a
    # GP work-package and submits it to the guarantor over CE-133/QUIC. Otherwise the
    # operator-RPC /item route (single-node harness / backward-compatible).
    # Every relay is recorded in the pending ledger (by its tag byte); ops whose caller
    # supplies a settle predicate are tracked submit->state-visible (/api/pending +
    # the jamswap_settle_latency_seconds histogram).
    op = TAG_NAMES.get(payload[0], f"tag{payload[0]}")
    tid = metrics.track(op, detail, check)
    if payload[0] in (TAG_SMATCH, TAG_REVEAL):
        # forensic: keep the LAST FEW round payloads byte-exact so a failing round
        # can be replayed through the service offline (refine_run + auth replica)
        try:
            import glob
            for old_f in sorted(glob.glob("/tmp/round_*.hex"))[:-7]:
                os.remove(old_f)
            with open(f"/tmp/round_{int(time.time())}_{payload[0]}.hex", "w") as f:
                f.write(payload.hex())
        except Exception:
            pass
    if BUILDER_URL:
        r = _post_json(BUILDER_URL + "/submit", {"service_id": SID, "payload_hex": payload.hex()})
        if r.get("accepted") is False:
            # backpressure: no guarantor took it — resolve the ledger entry (it can
            # never settle) and raise for the caller to back off
            metrics.refused(tid)
            raise ChainBusy(f"{op}: all guarantors refused (CE-133 queues full)")
        return r
    return node(f"/v1/service/{SID}/item", {"payload_hex": payload.hex()})
def storage(key):
    # STANDARD path when READER_URL is set: read the key from the node's on-chain
    # State_db over CE-129/QUIC via the reader bridge. Otherwise the operator-RPC route.
    if READER_URL:
        r = reader_get(f"/read?service={SID}&key={key.hex()}")
        return bytes.fromhex(r["value_hex"]) if r.get("value_hex") else b""
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

def _marketable(m, side, price):
    """True if an incoming order at (side, atomic price) crosses standing liquidity —
    the on-chain resting book OR an opposing order already in the mempool. A
    marketable order SHOULD clear on a healthy chain; the order SLO counts the ones
    that then fail to. Best-effort and read-only; any error is treated as non-marketable
    (conservative: it never inflates the SLO denominator)."""
    try:
        raw = storage(b"book" + struct.pack("<I", m))
        for i in range(len(raw) // 17):
            _a, _oid, bside, p, q = struct.unpack_from("<IIBII", raw, i * 17)
            if q <= 0:
                continue
            if side == BUY and bside == SELL and price >= p:
                return True
            if side == SELL and bside == BUY and price <= p:
                return True
        with _lock:
            rest = list(pending.get(m, []))
        for o in rest:
            if o.get("sealed") or o.get("price") is None:
                continue                      # sealed terms are hidden — can't judge crossing
            if side == BUY and o["side"] == SELL and price >= o["price"]:
                return True
            if side == SELL and o["side"] == BUY and price <= o["price"]:
                return True
    except Exception:
        return False
    return False

# ---- API handlers ---------------------------------------------------------
def api_deposit(b):
    acct, asset, amount = int(b["account"]), int(b["asset"]), to_atomic(b["amount"])
    before = bal(asset, acct)
    submit(bytes([1]) + struct.pack("<IIQ", acct, asset, amount),
           check=lambda: bal(asset, acct) >= before + amount,
           detail=f"account {acct} +{disp(amount)} asset {asset}")
    return {"ok": True}
def api_withdraw(b):
    # signed + replay-proof: the client signs canon(withdraw, handle, asset, amount, nonce)
    # with its account key; the SERVICE verifies (trustless). We relay the bytes plus the
    # account's registered key (signer_key).
    handle, asset, nonce = int(b["account"]), int(b["asset"]), int(b["nonce"])
    amount = int(b["amount_atomic"])   # client scales + signs the atomic amount
    sig = bytes.fromhex(b["sig"])
    submit(bytes([TAG_WITHDRAW]) + struct.pack("<IIQQ", handle, asset, amount, nonce) + sig
           + signer_key(handle),
           check=lambda: nonce_of(handle) > nonce,     # the service bumps the nonce on settle
           detail=f"account {handle} -{disp(amount)} asset {asset}")
    return {"ok": True, "balance": disp(bal(asset, handle))}
def api_cancel(b):
    # signed cancel of a RESTING (on-chain) order: canon(cancel, handle, market, oid, nonce)
    handle, market, oid, nonce = int(b["account"]), int(b["market"]), int(b["order_id"]), int(b["nonce"])
    sig = bytes.fromhex(b["sig"])
    try:
        resting = _resting_entry(market, handle, oid)      # its terms, for the receipt
    except Exception:
        resting = None                                     # a read hiccup must not block the cancel
    submit(bytes([TAG_CANCEL]) + struct.pack("<IIIQ", handle, market, oid, nonce) + sig
           + signer_key(handle),
           check=lambda: nonce_of(handle) > nonce,
           detail=f"account {handle} cancel order {oid} market {market}")
    if resting:
        with _lock:                                        # the resolver swaps the list out
            _cancel_watch.append({"market": market, "account": handle, "oid": oid,
                                  "nonce": nonce, "t": time.time(), "order": resting})
    return {"ok": True}

# Signed cancels awaiting their effect. A resting order's lifecycle is otherwise never
# closed when its owner cancels it on-chain: it stays "open" in the order telemetry
# forever. The resolver watches each cancel until it lands, then ends the order with a
# "cancelled" receipt — unless a round that trades the order is still unresolved (it may
# have filled it first), or the cancel removed nothing (the order is still on the book).
_cancel_watch = []
CANCEL_WATCH_SECS = 600            # a cancel not landed by then is dropped from the watch
def _resting_entry(m, acct, oid):
    # the order's (side, price, qty) while it rests on the market's on-chain book, else None
    raw = storage(b"book" + struct.pack("<I", m))
    for i in range(len(raw) // 17):
        a, o, side, p, q = struct.unpack_from("<IIBII", raw, i * 17)
        if a == acct and o == oid:
            return {"side": side, "price": p, "qty": q}
    return None
def _may_fill(m, oid):
    # a round of market m that fills this order has LANDED but isn't finalized yet: its
    # receipt, not the cancel, ends the order (finalizing it makes the order non-open). A
    # round that fills it but has NOT landed never can, once the cancel has: either the cancel
    # took the order off the book (the round's input book no longer matches) or the round
    # landed first (then it is marked). So an unlanded released round must not hold the
    # cancel back — it used to, until the watch lapsed and the order stayed "open" forever.
    rounds = ([_inflight[m]] if m in _inflight else []) + list(_zombies.get(m, []))
    return any(fr["clearing"]["fills"].get(oid) and _landed_slot(fr["rid"]) is not None
               for fr in rounds)
def _resolve_cancels(now):
    with _lock:                    # take the list: api_cancel appends from HTTP threads, and an
        batch = list(_cancel_watch)    # append between a scan and a rewrite used to be lost
        _cancel_watch.clear()
    keep = []
    for c in batch:
        m, a, oid = c["market"], c["account"], c["oid"]
        if now - c["t"] > CANCEL_WATCH_SECS:
            continue                                   # never took effect: nothing ended
        try:
            if nonce_of(a) <= c["nonce"] or _may_fill(m, oid):
                keep.append(c)                         # not landed yet / a fill may beat it
                continue
            still = _resting_entry(m, a, oid)
        except Exception:
            keep.append(c)                             # reader hiccup: retry next sweep
            continue
        if still is None and order_telemetry.is_open(m, a, oid):
            order_expiry.pop((m, a, oid), None)
            _record_exec(dict(c["order"], market=m, account=a, oid=oid, _outcome="cancelled",
                              _reason="cancelled by owner (signed on-chain cancel)"),
                         0, c["order"]["price"], False, now)
            _save_execs()
    with _lock:
        _cancel_watch[:0] = keep       # ahead of any cancel appended meanwhile
def _try_submit_register(pk_hex, now=None):
    # one submit attempt for a pending registration; swallow backpressure (retry next sweep)
    e = _reg_pending.get(pk_hex)
    if not e:
        return
    now = now or time.time()
    try:
        submit(e["payload"], check=lambda: handle_of(bytes.fromhex(pk_hex)) is not None,
               detail=f"pubkey {pk_hex[:12]}..")
        e["attempts"] += 1
    except ChainBusy:
        pass                                   # queues full — back off, resolver retries later
    e["last"] = now

def _resolve_registrations_once(now):
    # confirm-or-retry every pending registration; runs on the resolver thread.
    for pk_hex in list(_reg_pending):
        e = _reg_pending[pk_hex]
        if handle_of(bytes.fromhex(pk_hex)) is not None:
            _reg_pending.pop(pk_hex, None)                     # landed — done
            continue
        if now - e["t"] > REG_GIVEUP_SECS or e["attempts"] >= REG_MAX_ATTEMPTS:
            _reg_pending.pop(pk_hex, None)                     # give up (client can re-request)
            print(f"register {pk_hex[:12]}.. gave up after {e['attempts']} attempts")
            continue
        if now - e["last"] >= REG_RETRY_SECS:
            _try_submit_register(pk_hex, now)

def api_register(b):
    # bind an ed25519 pubkey to an account handle: canon(register, pubkey) signed by that key
    pubkey, sig = bytes.fromhex(b["pubkey"]), bytes.fromhex(b["sig"])
    h = handle_of(pubkey)
    if h is not None:
        # already on-chain: re-submitting builds a work-package byte-identical to the one
        # that registered this key, which GP rejects as duplicate_package — don't relay it
        _reg_pending.pop(pubkey.hex(), None)
        return {"ok": True, "handle": h}
    # enqueue for confirm-and-retry (deduped by pubkey), then fire the first attempt now.
    # The resolver re-submits with backoff until the handle lands, so a fresh account
    # reliably registers even under queue pressure without flooding the fleet.
    pk_hex = pubkey.hex()
    if pk_hex not in _reg_pending:
        _reg_pending[pk_hex] = {"payload": bytes([TAG_REGISTER]) + pubkey + sig,
                                "t": time.time(), "attempts": 0, "last": 0.0}
        _try_submit_register(pk_hex)
    return {"ok": True, "handle": None, "registering": True}   # None until accumulate lands
def api_handle(q):
    return {"handle": handle_of(bytes.fromhex(q["pubkey"]))}
def api_nonce(q):
    return {"nonce": nonce_of(int(q["handle"]))}
def footprint_octets():
    # the service's live state footprint in octets (validator RAM), from the node. 0 if
    # the node predates the footprint endpoint (rent then reads as 0 — fail-open, honest).
    # QUIC mode has no CE for the account footprint yet -> 0 (same fail-open).
    if QUIC_MODE:
        return 0
    try:
        return int(node(f"/v1/service/{SID}/footprint").get("octets", 0))
    except Exception:
        return 0
def rent_reserve_atomic():
    # JAMKB the treasury MUST hold to back its footprint = the obligation (1 JAMKB = 1 KB).
    return jamkb_rent(footprint_octets()) * SCALE
def reserve_target_atomic():
    # JAMKB the treasury should AIM to hold = obligation + a small buffer, capped at the finite
    # supply. This is the anti-hoarding ceiling the endowment and top-ups obey — a service never
    # acquires RAM rights beyond what it needs (they'd be idle, denying other services).
    return reserve_target(jamkb_rent(footprint_octets()), RESERVE_BUFFER_KB) * SCALE
def jamkb_solvency():
    # JAMKB-standard invariant: held JAMKB reserve ≥ state footprint obligation.
    # Returns (solvent, shortfall_atomic). See docs/JAMKB_STANDARD.md.
    return solvency(bal(JAMKB, FEE_ACCOUNT), rent_reserve_atomic())
def treasury_status():
    # self-funding treasury view: the obligation (JAMKB) is covered FIRST out of fees; profit is
    # the leftover FEE revenue (USDC/DOT) — JAMKB itself is a working reserve, never a hoard.
    # See treasury.py / docs/REVENUE.md + JAMKB_STANDARD.md.
    reserve = rent_reserve_atomic()
    bals = {a: bal(a, FEE_ACCOUNT) for a in (USDC, DOT, JAMKB)}
    s = profit_split(bals, reserve)
    held = bals[JAMKB]
    return {"treasury": {a: disp(v) for a, v in bals.items()},
            "rent_jamkb": disp(reserve), "reserve_held_jamkb": disp(s["reserve_held"]),
            "shortfall_jamkb": disp(s["shortfall"]), "over_reserved_jamkb": disp(s["over_reserved"]),
            "solvent": s["solvent"],
            "withdrawable": {a: disp(v) for a, v in s["withdrawable"].items()},
            "beneficiary": PROFIT_BENEFICIARY, "beneficiary_chain": PROFIT_BENEFICIARY_CHAIN,
            "sweep_enabled": BENEFICIARY_SWEEP,   # can the owner sweep profit over the API (prototype)
            "backpressure": JAMKB_BACKPRESSURE and not s["solvent"],   # is new state growth blocked?
            "reserve_target_jamkb": disp(reserve_target_atomic()),    # obligation + buffer (the cap)
            "held_jamkb": disp(held),                                 # what the treasury actually holds
            "supply_jamkb": JAMKB_SUPPLY}                             # finite testnet-wide pool
def api_reserve_topup(b):
    # Beneficiary top-up = ACQUIRE scarce JAMKB (from the finite pool) into the reserve, up to
    # the target (obligation + buffer). You cannot acquire beyond what you need — the excess would
    # be idle RAM rights denied to other services. Refuses over-target and over-supply requests.
    # (Prototype: a mock draw from the pool — like the mock USDC/DOT custody; production = a signed
    # transfer of real, already-minted JAMKB the beneficiary holds. See docs/JAMKB_STANDARD.md.)
    amount = to_atomic(b["amount"])
    if amount <= 0:
        raise ValueError("top-up amount must be positive")
    held = bal(JAMKB, FEE_ACCOUNT)
    target = reserve_target_atomic()
    room = target - held
    if room <= 0:
        raise ValueError(f"reserve already at target ({disp(target)} JAMKB = obligation + buffer) — "
                         f"holding more would be idle RAM rights; nothing to acquire")
    if amount > room:
        raise ValueError(f"top-up capped at {disp(room)} JAMKB (target {disp(target)}); "
                         f"a service holds only what it occupies, not a hoard")
    submit(bytes([TAG_DEPOSIT]) + struct.pack("<IIQ", FEE_ACCOUNT, JAMKB, amount))
    return {"ok": True, "reserve_jamkb": disp(bal(JAMKB, FEE_ACCOUNT)), "target_jamkb": disp(target)}
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
def signer_key(handle):
    # An account-signed op (withdraw, cancel, sealed commit) carries its signer's key: the
    # service verifies the signature in refine under this key, and accumulate checks that it
    # is the account's registered key. (Under GP 0.8.0 one in-PVM ed25519 verify is ~5.3M gas,
    # more than a work-report's accumulate budget can spare per item.)
    pk = pubkey_of_handle(handle)
    if pk is None:
        raise ValueError(f"account {handle} is not registered")
    return pk[:32]
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
# within MARKET_BAND_PCT of the last clearing price. With no last price yet (cold market) a
# market order is refused — there's no reference to bound it, so use a limit order.
MARKET_BAND_PCT = 10                   # ±10% of the last price (MARKET_BAND_PCT in service/src/lib.rs)
def market_price(side, lp):
    # A market order's executed price: the edge of the band the service accepts around the
    # last price lp, in the service's own integer arithmetic (it accepts p iff
    # lp*90 <= p*100 <= lp*110) — floor a buy, ceil a sell. round(lp * 1.1) put ~45% of
    # prices one atomic unit OUTSIDE the band, and the service then rejected the whole round.
    if side == BUY:
        return lp * (100 + MARKET_BAND_PCT) // 100
    return max(1, -(-lp * (100 - MARKET_BAND_PCT) // 100))

# ---- anti-bloat: no order rests forever (rent-funded expiry) ---------------
# A resting order occupies validator RAM continuously → it consumes JAMKB state rent whether
# or not it ever trades. "Good-till-cancelled forever" is therefore a griefing vector: spam the
# book with far-from-market orders that never fill, never expire, and bloat the footprint (and
# the matching engine's per-round work) indefinitely. Defense: EVERY order — including GTC — gets
# an automatic expiry funded by the min profit of its fee. An order rests only as long as that
# fee can subsidize the rent it accrues; a bigger on-chain footprint burns the budget faster, so
# **sealed orders (32 B commitment) expire sooner than public ones (17 B)** — a direct tie to
# "sealed costs more JAMKB". A hard cap and a per-account order limit backstop it.
FOOTPRINT_PUBLIC = 17                   # bytes a resting public order occupies on-chain
FOOTPRINT_SEALED = 32                   # bytes a sealed commitment occupies on-chain
# KB·seconds of state rent the per-order min-profit is willing to fund. Policy parameter (like
# JAMKB_SUPPLY) — default sizes a public order's GTC life to ~1 h, a sealed one's to ~32 min.
ORDER_RENT_BUDGET_KBS = float(os.environ.get("ORDER_RENT_BUDGET_KBS", "60") or 0)
MAX_RESTING_SECS = float(os.environ.get("MAX_RESTING_SECS", "86400") or 0)      # hard cap: 24 h, no order rests longer
MAX_OPEN_ORDERS = int(os.environ.get("MAX_OPEN_ORDERS", "50") or 0)             # per account per market (0 = unlimited)
def order_lifetime_secs(sealed):
    # rent-funded max resting time = budget / footprint. Bigger footprint (sealed) → shorter life.
    # Capped at MAX_RESTING_SECS so nothing ever rests indefinitely.
    fp_kb = (FOOTPRINT_SEALED if sealed else FOOTPRINT_PUBLIC) / 1024.0
    life = ORDER_RENT_BUDGET_KBS / fp_kb if ORDER_RENT_BUDGET_KBS > 0 else MAX_RESTING_SECS
    return min(life, MAX_RESTING_SECS)
def open_order_count(m, acct):
    # how many live orders this account already has on this market: queued (mempool) + resting
    # (on-chain book). Bounds one actor's ability to bloat the book.
    n = sum(1 for o in pending.get(m, []) if o["account"] == acct)
    raw = storage(b"book" + struct.pack("<I", m))
    for i in range(len(raw) // 17):
        a, _oid, _side, _p, _q = struct.unpack_from("<IIBII", raw, i * 17)
        if a == acct:
            n += 1
    return n
def _seal_material(m, o):
    # Build the hiding material for a sealed order dict {account,oid,side,price,qty}: the
    # ciphertext (encrypt-until-batch) or reveal preimage (commit–reveal). Returns the 32-byte
    # commit id the chain will store — H(C1‖body) or H(order‖nonce) — which is also what the
    # OWNER SIGNS (canon(commit, market, account, commit_id, seq)) to authorize the placement.
    ob = order_bytes(o["account"], o["oid"], o["side"], o["price"], o["qty"])
    if ENC_MODE:
        seed = secrets.token_bytes(32).hex()
        d = committee_run("encrypt", m, ob.hex(), seed)
        o["ciphertext"] = d["ciphertext"]
        return commitment(bytes.fromhex(d["ciphertext"]))
    nonce = secrets.token_bytes(32)
    o["reveal"] = ob + nonce
    return commitment(o["reveal"])
# Sealed drafts awaiting the owner's commit signature: (market, oid) -> order dict.
# Created by /api/seal_prepare, consumed by /api/order (two-phase placement: prepare ->
# the browser signs the commit id -> order). Never posted on-chain unsigned.
seal_drafts = {}
def api_seal_prepare(b):
    m, acct = int(b["market"]), int(b["account"])
    side = BUY if b["side"] == "buy" else SELL
    otype = b.get("type", "limit")
    qty = to_atomic(b["qty"])
    if otype == "market":
        lp = mstate(b"lp", m)
        if lp <= 0:
            raise ValueError("no reference price yet on this market — place a limit order")
        price = market_price(side, lp)
    else:
        price = to_atomic(b["price"])
    oid = next_oid[0]; next_oid[0] += 1
    o = {"account": acct, "oid": oid, "side": side, "price": price, "qty": qty,
         "type": otype, "sealed": True}
    o["commit"] = _seal_material(m, o)
    seal_drafts[(m, oid)] = o
    return {"ok": True, "oid": oid, "commit": o["commit"].hex()}
def _submit_signed_commit(m, draft, commit_seq, commit_sig):
    # the owner-signed on-chain commitment for a prepared sealed order
    acct = draft["account"]
    tail = struct.pack("<Q", commit_seq) + commit_sig + signer_key(acct)
    if ENC_MODE:
        ct = bytes.fromhex(draft["ciphertext"])
        submit(bytes([TAG_ENC_COMMIT]) + struct.pack("<I", m) + ct + struct.pack("<I", acct) + tail)
    else:
        submit(bytes([TAG_COMMIT]) + struct.pack("<II", m, acct) + draft["commit"] + tail)
def _post_carry_seal(m, o):
    # Re-seal the unfilled remainder of a partially-filled sealed order so it carries forward
    # (still hidden). The owner is offline, so this is builder-posted — the service accepts it
    # only against the carry allowance the settling round just minted for this account.
    # The seal is made ONCE per remainder: a retry (chain busy, or a submit whose outcome is
    # unknown) re-posts the same commitment, so if an earlier attempt did land, the retry is
    # refused harmlessly — a fresh nonce would leave the remainder holding a commitment that
    # never went on-chain (deferred until it expired).
    if "commit" not in o:
        o["commit"] = _seal_material(m, o)
    cid = o["commit"]
    if ENC_MODE:
        submit(bytes([TAG_CARRY_ENC_COMMIT]) + struct.pack("<I", m)
               + bytes.fromhex(o["ciphertext"]) + struct.pack("<I", o["account"]))
    else:
        submit(bytes([TAG_CARRY_COMMIT]) + struct.pack("<II", m, o["account"]) + cid)
    return o
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
        price = market_price(side, lp)
    else:
        price = to_atomic(b["price"])
    # Order authentication — TRUSTLESS end-to-end for public orders: the client signs
    # canon(order, …, seq) with the account key; the signature travels INTO the work package,
    # refine verifies it per-order, and accumulate binds the key to the on-chain registry and
    # enforces the per-account monotonic seq (replay-proof). The builder's checks here exist
    # only to give the trader an immediate error — the SERVICE is the enforcer, so a malicious
    # builder gains nothing by skipping them. Market price is server-derived within the band,
    # signed as 0; the service band-checks the executed price against the on-chain last price.
    pub = pubkey_of_handle(acct)
    seq = int(b.get("seq", 0) or 0)
    sig = bytes.fromhex(b.get("sig", "") or "")
    sealed_flag = bool(b.get("sealed"))
    draft = None
    if sealed_flag:
        # two-phase sealed placement: the draft (from /api/seal_prepare) holds the committed
        # terms + hiding material; the owner's commit signature authorizes it on-chain.
        draft = seal_drafts.pop((m, int(b.get("oid", -1))), None)
        if draft is None:
            raise ValueError("sealed order needs /api/seal_prepare first (draft missing or already used)")
        if draft["account"] != acct:
            raise ValueError("sealed draft belongs to a different account")
        price, qty = draft["price"], draft["qty"]   # the committed terms are canonical
        if not pub:
            raise ValueError("account not registered — connect a wallet and register first")
        if len(bytes.fromhex(b.get("commit_sig", "") or "")) != 64 or not int(b.get("commit_seq", 0) or 0):
            raise ValueError("sealed orders must carry an owner commit signature (commit_sig, commit_seq)")
    signed_price = 0 if otype == "market" else price
    if not sealed_flag:
        # public orders CANNOT settle unsigned any more — fail fast at the door
        if not pub:
            raise ValueError("account not registered — connect a wallet and register first")
        if not seq or len(sig) != 64:
            raise ValueError("public orders must carry a signature and seq (verified in refine)")
    msg = canon(b"order", struct.pack("<I", acct), struct.pack("<I", m), bytes([side]),
                struct.pack("<I", qty), bytes([1 if otype == "market" else 0]),
                bytes([1 if sealed_flag else 0]), struct.pack("<I", signed_price),
                struct.pack("<Q", seq))
    if REQUIRE_ORDER_SIG and pub and sig and not verify_order_sig(pub, msg, sig):
        raise ValueError("bad order signature")
    if not sealed_flag:
        # the service refuses an order whose seq doesn't beat its account's floor, or repeats
        # another order's seq in the same round — and it refuses the WHOLE round for it. Turn
        # such an order away here instead (a re-POSTed signed order is the usual cause); the
        # round builder still drops any that slip past (_seq_sanitize).
        fl = _seq_floor(acct)
        if seq <= fl:
            raise ValueError(f"order seq {seq} is not above the account's settled seq {fl} — "
                             f"sign the order with a fresh seq")
        if _seq_in_use(acct, seq):
            raise ValueError(f"order seq {seq} is already used by a live order of this account — "
                             f"sign each order with a fresh seq")
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
    # anti-spam: cap how many live orders one account can rest on a market at once.
    if MAX_OPEN_ORDERS > 0 and open_order_count(m, acct) >= MAX_OPEN_ORDERS:
        raise ValueError(f"open-order limit reached ({MAX_OPEN_ORDERS} per market) — "
                         f"cancel or let some clear before placing more")
    oid = draft["oid"] if draft else next_oid[0]
    if not draft:
        next_oid[0] += 1
    sealed = sealed_flag
    # EVERY order gets an automatic expiry — there is no rest-forever GTC. A user-supplied TTL may
    # only make it SHORTER, never longer than the rent-funded lifetime (so spam self-expires and the
    # JAMKB it consumes is always reclaimed). "GTC" (ttl=0) means "rest until the rent budget runs out".
    life = order_lifetime_secs(sealed)
    user_ttl = float(b.get("ttl", 0) or 0)
    eff_ttl = min(user_ttl, life) if user_ttl > 0 else life
    order_expiry[(m, acct, oid)] = time.time() + eff_ttl
    o = {"account": acct, "oid": oid, "side": side, "price": price, "qty": qty, "type": otype,
         "sealed": sealed, "address": b.get("address", ""),
         # the trustless-order material carried into the work package (public orders)
         "sig": sig, "pubkey": pub or b"\x00" * 32, "seq": seq, "signed_price": signed_price}
    if draft:
        # post the OWNER-SIGNED commitment/ciphertext (terms hidden; the service verifies
        # the signature against the account's registered key + the monotonic seq floor).
        _submit_signed_commit(m, draft, int(b["commit_seq"]), bytes.fromhex(b["commit_sig"]))
        o.update({k: draft[k] for k in ("reveal", "ciphertext", "commit") if k in draft})
    marketable = _marketable(m, side, price) if not sealed else False
    with _lock:
        pending.setdefault(m, []).append(o)
        n = len(pending[m])
    order_telemetry.placed(m, acct, oid, side, price, qty, sealed, marketable)
    return {"ok": True, "order_id": oid, "sealed": o["sealed"], "type": otype, "pending": n}
def expired_pairs(m, rest_bytes):
    # (account, oid) pairs of resting orders whose good-till-time has passed. The book bytes
    # themselves are NO LONGER edited here: the service hash-binds refine's input book to the
    # on-chain book (so a builder can't fabricate resting orders), and expiry is passed as an
    # EXPLICIT prune list inside the round — auditable in the work package, never silent.
    # Nothing ends HERE: a prune takes effect only if the round carrying it settles, so the
    # order's "expired" terminal (and dropping its expiry) waits for _finalize_round. A round
    # that is released instead leaves the expiry in place, and the next round prunes again —
    # ending it at build time left a released round's expired orders on the book for good.
    now, out = time.time(), []
    for i in range(len(rest_bytes) // 17):
        a, oid, side, p, q = struct.unpack_from("<IIBII", rest_bytes, i * 17)
        exp = order_expiry.get((m, a, oid))
        if exp and exp <= now:
            out.append((a, oid))
    return out
def signed_order_bytes(o):
    # order(17) ‖ flags(1) ‖ signed_price(4) ‖ seq(8) ‖ pubkey(32) ‖ sig(64) — must match
    # wire::SignedOrder in crates/match-engine (refine re-verifies the signature from these).
    flags = FLAG_MARKET if o.get("type") == "market" else 0
    return (order_bytes(o["account"], o["oid"], o["side"], o["price"], o["qty"])
            + bytes([flags]) + struct.pack("<IQ", o["signed_price"], o["seq"])
            + o["pubkey"] + o["sig"])
def _seq_in_use(acct, seq):
    # does a live public order of this account (queued, or in a round in flight, on ANY market:
    # the order floor is per account) already carry this seq?
    with _lock:
        live = [o for orders in pending.values() for o in orders]
    live += [o for fr in list(_inflight.values()) for o in fr["public"]]
    return any(not o.get("sealed") and o["account"] == acct and o.get("seq") == seq for o in live)
def _seq_floor(acct):
    # the account's on-chain PUBLIC-ORDER floor (b"sq"‖handle). Sealed commits have their own
    # floor (b"sc"), so a commit can no longer push this past the trader's older orders.
    v = storage(b"sq" + struct.pack("<I", acct))
    return int.from_bytes(v, "little") if v else 0
def _seq_sanitize(m, orders):
    """Enforce the service's per-account seq discipline BEFORE a round is submitted, so the
    round can never be rejected wholesale for a stale, repeated or out-of-order seq.
    Returns (kept, dead, floors): kept is sorted (account, seq) ascending, each seq strictly
    above the account's RUNNING floor — the on-chain floor, then each kept seq in turn,
    exactly as the service's check_bindings walks a round; dead is [(order, reason)] for the
    orders that can never settle: at/below the on-chain floor (a newer order of the account
    already settled past it) or repeating a seq another order of the round carries (a
    re-posted signed order — kept, it would sink every round it rode in); floors holds the
    on-chain floor read for each account seen."""
    floors, run = {}, {}
    kept, dead = [], []
    # ascending by (account, seq): the service raises each account's running floor to
    # the order's seq in this order, so ascending guarantees every step strictly rises.
    for o in sorted(orders, key=lambda o: (o["account"], o.get("seq", 0))):
        a, s = o["account"], o.get("seq", 0)
        if a not in floors:
            floors[a] = run[a] = _seq_floor(a)
        if s <= floors[a]:
            dead.append((o, f"superseded: account seq floor (floor {floors[a]} >= order seq {s})"))
        elif s <= run[a]:
            dead.append((o, f"duplicate: another order of this account carries seq {s}"))
        else:
            kept.append(o)
            run[a] = s
    return kept, dead, floors

def _price_market_orders(m, public):
    """Market orders sign no price: the builder picks the executed price, and the service
    accepts it only within MARKET_BAND_PCT of the market's last price AT ACCUMULATE — which
    may have moved since the order was placed. Re-price each at the band edge of the CURRENT
    last price (one round in flight per market, so it holds until this one lands). With no
    last price at all the order can't be bounded, and a round carrying it would be rejected
    whole: it is dead. Returns (public, dead) with dead as [(order, reason)]."""
    market = [o for o in public if o.get("type") == "market"]
    if not market:
        return public, []
    lp = mstate(b"lp", m)
    if lp <= 0:
        return ([o for o in public if o.get("type") != "market"],
                [(o, "market order: the market has no last price to bound it") for o in market])
    for o in market:
        o["price"] = market_price(o["side"], lp)
    return public, []

def _cap_batch(orders, cap):
    """Split the mempool into this round's batch (the first `cap`) and the overflow, without
    ever letting an account's NEWER public order into the batch while an OLDER one of the
    same account waits in the overflow: the newer one settling would raise the account's
    floor past the older one, stranding it for good. Each account's public orders are
    re-dealt into the queue positions they already hold, lowest seq first, so the cap takes
    every account's oldest orders. Sealed orders don't move (they carry no order seq)."""
    slots = {}
    for i, o in enumerate(orders):
        if not o.get("sealed"):
            slots.setdefault(o["account"], []).append(i)
    out = list(orders)
    for idx in slots.values():
        for i, o in zip(idx, sorted((orders[i] for i in idx), key=lambda o: o.get("seq", 0))):
            out[i] = o
    return out[:cap], out[cap:]

# ---- round identity: exactly which rounds settled --------------------------
# A round's id is blake2s(DOMAIN ‖ the exact work-item payload): refine derives it from the
# bytes it is given, so it binds everything the round is — market, signed orders, prune list,
# reveals / ciphertexts, the input book. Accumulate marks every accepted round landed:
# b"rl"‖id → the slot it landed in, removed only by age (crates/match-engine/src/round_id.rs,
# the one definition; pinned byte-for-byte by tests/test_round_poison.py). The builder hashes
# the payload it submits, so `_landed_slot(rid) is not None` says exactly whether THIS round
# settled — for the live round and for released ones (a late landing is finalized, not
# misread as a rejection). No stream of other rounds can push a live marker out; a ring of
# the newest 32 ids could be flushed by anyone who landed 32 cheap rounds.
ROUND_ID_DOMAIN = b"jamswap:v1:round"
def round_id(payload):
    return hashlib.blake2s(ROUND_ID_DOMAIN + payload, digest_size=32).digest()
def _landed_slot(rid):
    v = storage(b"rl" + rid)
    return int.from_bytes(v[:4], "little") if len(v) >= 4 else None
def _consumed_entry(o):
    # the (hash ‖ account) set entry a revealed sealed order consumes: H(order‖nonce) in
    # commit–reveal, H(C1‖body) (the ciphertext id) in encrypt-until-batch
    h = commitment(bytes.fromhex(o["ciphertext"])) if ENC_MODE else commitment(o["reveal"])
    return h + struct.pack("<I", o["account"])
def _set_entries(raw):
    return {raw[i:i + SET_ENTRY_LEN] for i in range(0, len(raw) - SET_ENTRY_LEN + 1, SET_ENTRY_LEN)}
def _okey(o):
    # one incarnation of an order: a carried sealed remainder keeps its oid but gets a fresh
    # commitment, so it is a different key from the copy that traded
    return (o["account"], o["oid"], o.get("commit"))
def _keys(fr):
    return {_okey(o) for o in fr["sealed"] + fr["public"]}

def public_section_bytes(public, pruned, raw_book):
    # the signed public-order section every round type now ends with:
    # [ns:u16][signed orders][np:u16][pruned (account,oid) pairs][on-chain book, byte-exact]
    sec = struct.pack("<H", len(public)) + b"".join(signed_order_bytes(o) for o in public)
    sec += struct.pack("<H", len(pruned)) + b"".join(struct.pack("<II", a, oid) for a, oid in pruned)
    return sec + raw_book
def _parse_book(raw):
    # resting book bytes -> planner order dicts (integer side, atomic price)
    out = []
    for i in range(len(raw) // 17):
        a, oid, side, p, q = struct.unpack_from("<IIBII", raw, i * 17)
        out.append({"account": a, "oid": oid, "side": side, "price": p, "qty": q, "sealed": False})
    return out
# One round IN FLIGHT per market: the service hash-checks each round's included
# book byte-exact against its current on-chain book, so a second round whose
# snapshot was taken before the first settled is REJECTED wholesale on-chain.
# On the contested chain a filling round takes 30-90s to settle while the auction
# loop ticks every 6s — ungated, rounds N+1..N+k all raced round N and died
# (surfaced immediately by the phase-3 load test: offered volume >> on-chain cv).
# Gate: hold a market's next round until the previous one SETTLES — its round id is
# marked landed (see round_id above) — with hard caps so a rejected round can never
# wedge the market. Orders keep queueing meanwhile and BATCH into the next round —
# throughput is settlement-bound, exactly what the funnel dashboard shows. Every
# submitted round is tracked, zero-fill ones too: a zero-fill round that rests orders
# rewrites the book just the same, and its marker shows it.
#
# RECEIPTS ARE SETTLEMENT-CONTINGENT: a round's per-order receipts and sealed-remainder
# carries are recorded only once its id is marked landed and is still marked at a second
# sighting (SETTLE_HOLD_SECS, and at least one resolver period, later) — on the real mixed
# chain most overloaded rounds time out or are service-rejected, and receipting at submit
# time filled the execution report with "filled" orders whose balances never moved (phantom
# fills, found live 2026-07-09: report full of fills, every balance still genesis
# 1,000,000). A round that won't settle re-queues its orders (nothing is lost) and leaves
# NO receipts.
#
# A round that CAN'T settle is released in seconds, not after the gate: accumulate
# re-checks the input book hash, every order seq against its account's floor, and every
# consumed commit, so once any of those is gone (and stays gone for DEAD_CONFIRM_SECS,
# a short re-org guard) the round is dead. Its orders go back to the FRONT of the
# mempool. Every released round (dead or timed out) is still WATCHED as a zombie: if
# its id is later marked landed it settled after all — its orders are CLAIMED (taken out
# of the mempool and noted on every other round carrying copies, so none re-queues or
# receipts them) and it is finalized (receipts, carries), instead of being re-submitted
# and later misrecorded as rejected. (The 2026-09-24 soak lost 6 rounds to a 60 s gate +
# tail requeue while each was already dead; see the jamswap late-settlement analysis.)
#
# One thread at a time per market: api_round (building from the mempool), the resolver
# (judging the market's rounds) and a mempool cancel all hold the market's lock, so a
# released round seen landing is seen either before a build takes its orders or after the
# build registered its round (and is claimed from it) — never while its orders sit only
# in a build's locals, where they used to be receipted "rejected" and then "filled".
_round_gate = {}                   # market -> {"check": None, "t": ...} cooldowns (busy/timeout)
_inflight = {}                     # market -> the round in flight, awaiting its landed marker:
                                   #   {"t","sealed","public","resting","clearing","pruned",
                                   #    identity: "rid","book_hash","consumed","set_key","minseq",
                                   #    "claimed": {order key: rid of the landed round holding it}}
_zombies = {}                      # market -> [released rounds still watched for a late landing]
_market_locks = {}                 # market -> threading.Lock (see above)
_market_locks_guard = threading.Lock()
def _market_lock(m):
    with _market_locks_guard:
        return _market_locks.setdefault(m, threading.Lock())
DEAD_CONFIRM_SECS = float(os.environ.get("DEAD_CONFIRM_SECS", "4"))     # dead this long -> released
MIN_CONFIRM_SECS = 2.0             # a landing is confirmed by a second sighting at least one
                                   # resolver period after the first, even with no settle hold:
                                   # two reads a moment apart (a build checks twice) see one head
def _confirmed(ok_since, now):
    return now - ok_since >= max(SETTLE_HOLD_SECS, MIN_CONFIRM_SECS)
ZOMBIE_WATCH_SECS = float(os.environ.get("ZOMBIE_WATCH_SECS", "900"))   # watch a released round this long
ZOMBIE_CAP = 32                    # per market: beyond it the oldest UNSIGHTED zombies are forgotten

# --- registration confirm-and-retry ------------------------------------------
# A fresh account's registration is a STANDALONE work-item (not part of an auction
# round), and submit() is fire-and-forget: on the multi-validator net a lone work-item
# can fail to accumulate under ce133-queue pressure, and with no retry the account never
# gets a handle — so the trader can't fund or trade (the "every manual test fails" bug;
# invisible to tests because the six dev accounts are pre-registered in genesis).
# This resolver re-submits an un-landed registration with backoff until the handle
# appears on-chain, then stops. Keyed by pubkey so a flood of identical /api/register
# calls collapses to ONE in-flight attempt (≤1 submit / REG_RETRY_SECS) — which both
# lands reliably AND stops registration from wedging the cap-16 queue. Safe to retry:
# register_key() is idempotent (an already-registered key keeps its handle) and a
# duplicate register work-package is dropped by the node as a duplicate.
_reg_pending = {}                  # pubkey_hex -> {"payload","t","attempts","last"}
REG_RETRY_SECS   = float(os.environ.get("REG_RETRY_SECS", "8"))     # min spacing between resubmits
REG_GIVEUP_SECS  = float(os.environ.get("REG_GIVEUP_SECS", "240"))  # abandon after this long unlanded
REG_MAX_ATTEMPTS = int(os.environ.get("REG_MAX_ATTEMPTS", "20"))
ROUND_GATE_SECS = float(os.environ.get("ROUND_GATE_SECS", "300"))   # settle patience for a round that is
                                   # NOT dead (still landable) before it is abandoned + re-queued: it only
                                   # catches rounds the chain never included (dead ones go in seconds)
SETTLE_HOLD_SECS = float(os.environ.get("SETTLE_HOLD_SECS", "150"))
                          # durability: the cv predicate must HOLD this long before receipts. The
                          # right value is the DEEPEST re-org the chain can produce, which depends
                          # on the consensus:
                          #   * MIXED lasair+PolkaJam (no shared finality): a settling branch can
                          #     lose fork choice minutes later — observed live 2026-07-09: volume
                          #     54 -> 0, balances snapped back to genesis; a 60 s hold was breached
                          #     once by a re-org spanning a full Safrole epoch. 150 s (> 2 tiny
                          #     epochs of 12x6 s) rides those out. This is the honest stopgap until
                          #     a cross-client finality gadget lands (docs/TOKENS.md roadmap).
                          #   * ALL-LASAIR (one coherent fork choice): re-orgs are 1-2 blocks, so
                          #     SETTLE_HOLD_SECS=18 (3 slots) confirms fast — set it in the compose.
                          # jamswap_settle_reverted_total measures whether the chosen hold is safe.
MAX_ROUND_ORDERS = int(os.environ.get("MAX_ROUND_ORDERS", "150"))
                                   # per-round batch cap. Refine gas ~5.29M/signed order (GP 0.8.0)
                                   # against a package budget of G_R (1e9 on tiny) caps a round at
                                   # ~185 signed orders; 150 leaves margin. Wire ~130 B/order also
                                   # bounds it from above, but there is also a THROUGHPUT
                                   # argument for keeping it SMALL: one round settles per market at a
                                   # time (the in-flight gate), so a giant round that fails to settle
                                   # wedges the whole market for the gate window while it cycles. Many
                                   # small rounds that each settle fast drain a backlog better than one
                                   # 253-order round that keeps timing out (found live 2026-07-09 on the
                                   # all-lasair net). Set it per-net in the compose (e.g. 48).
ROUND_ZEROFILL_SECS = 30.0         # cooldown after a busy refusal or a timed-out round, so retries
                                   # don't re-flood the fleet (the name predates tracking zero-fill rounds)

# Remainders whose carry-commit couldn't be posted yet (all guarantor queues full).
# The carry allowance the settled round minted PERSISTS on-chain, so we retry the
# re-seal each resolver sweep until it lands or the order's good-till-time expires —
# a revealed sealed order is NEVER silently dropped under load.  market -> [remainder]
_carry_retry = {}

def _carry_sealed_remainders(m, sealed_orders, fills, now):
    """Re-seal the unfilled remainder of each revealed sealed order (fresh commitment)
    and re-queue it, so a revealed sealed order keeps working under the same oid until it
    fully fills or its good-till-time expires — it is never IOC-dropped. Stamps each
    order's o["_outcome"]/o["_reason"] for the receipt feed; returns the list carried now.

    Outcomes stamped:
      * filled           — nothing to carry (fully filled)
      * carried / partial-carried — remainder re-sealed (or queued for re-seal); NON-terminal
      * cancelled / partial-cancelled (reason=expired) — GTT elapsed, remainder truly dropped
    """
    carried = []
    for o in sealed_orders:
        filled = fills.get(o["oid"], 0)
        rem = o["qty"] - filled
        if rem <= 0:
            o["_outcome"] = "filled"
            continue
        exp = order_expiry.get((m, o["account"], o["oid"]))
        if exp and exp <= now:
            # good-till-time elapsed: the remainder can't be carried past its own expiry.
            o["_outcome"] = "partial-cancelled" if filled > 0 else "cancelled"
            o["_reason"] = "expired"
            continue
        r = {"account": o["account"], "oid": o["oid"], "side": o["side"], "price": o["price"],
             "qty": rem, "type": o.get("type", "limit"), "sealed": True, "address": o.get("address", "")}
        try:
            _post_carry_seal(m, r)         # allowance-gated: the settled round minted this credit
            carried.append(r)
            if filled > 0:
                o["_outcome"], o["_reason"] = "partial-carried", f"filled {disp(filled)}, rest re-sealed & still working"
            else:
                o["_outcome"], o["_reason"] = "carried", "didn't cross this round — still working (hidden)"
        except Exception as e:
            # R4: don't drop — the credit persists on-chain, so queue the re-seal and
            # retry it each sweep. The order stays live; its receipt is a carry note.
            # Any failure, not just ChainBusy: a builder timeout used to escape here and
            # abort the round's finalize before a single receipt was written. (A retry
            # re-posts the same commitment, so one that did land is refused harmlessly.)
            _carry_retry.setdefault(m, []).append(r)
            o["_outcome"] = "partial-carried" if filled > 0 else "carried"
            o["_reason"] = ("re-seal queued (chain busy)" if isinstance(e, ChainBusy)
                            else f"re-seal queued ({type(e).__name__})")
    if carried:
        with _lock:
            pending.setdefault(m, []).extend(carried)
    return carried

def _drain_carry_retry(now):
    """Retry re-seals that were queued when the chain was busy. On success the remainder
    re-enters the mempool; if its good-till-time elapses first it is genuinely lost and
    gets a terminal cancelled(expired) receipt — the one place a carried remainder ends
    without filling, and it is surfaced, never silent."""
    for m, items in list(_carry_retry.items()):
        keep = []
        for r in items:
            exp = order_expiry.get((m, r["account"], r["oid"]))
            if exp and exp <= now:
                r["market"], r["_outcome"], r["_reason"] = m, "cancelled", "expired-before-reseal"
                _record_exec(r, 0, mstate(b"lp", m), True, now)
                print(f"round m{m}: carry re-seal expired before the chain drained — remainder cancelled")
                continue
            try:
                _post_carry_seal(m, r)
            except Exception:
                keep.append(r)             # still busy / unreachable — retry next sweep
                continue
            with _lock:
                pending.setdefault(m, []).append(r)
        if keep:
            _carry_retry[m] = keep
        else:
            _carry_retry.pop(m, None)

def _finalize_round(m, fr):
    """The round's id is marked landed and has held: its clearing is REAL on-chain. Only
    now carry sealed remainders forward, hand out fill receipts, and end the resting orders
    its prune list expired. Orders another landed round claimed (see _claim) are that
    round's to receipt: two rounds sharing an order can't both land, so this only happens
    after reads that straddled a re-org, and never receipts an order twice."""
    now = time.time()
    lost = fr.get("claimed") or {}
    sealed = [o for o in fr["sealed"] if _okey(o) not in lost]
    public = [o for o in fr["public"] if _okey(o) not in lost]
    if lost:
        print(f"round m{m}: WARNING settled while {len(lost)} of its order(s) are held by "
              f"another landed round — receipting only the rest")
    carried = _carry_sealed_remainders(m, sealed, fr["clearing"]["fills"], now)
    try: record_executions(m, fr["resting"], sealed, public, fr["clearing"])
    except Exception as e: print("exec record failed", m, e)
    for a, oid in fr.get("pruned", ()):
        # the prune landed with the round: the expired resting order is off the book now
        order_expiry.pop((m, a, oid), None)
        order_telemetry.terminal(m, a, oid, "expired")
    print(f"round m{m}: settled on-chain — receipted {len(sealed) + len(public)} order(s), carried {len(carried)}")

def _round_dead(m, fr):
    """Why the round can no longer settle, or None. These are the preconditions its
    accumulate re-checks (service check_round_auth / consume_set), read from the chain.
    Callers read the landed marker AFTER this, so a round that lands between the two reads
    is still seen as landed, not dead. It never calls a settle predicate, so a zero-fill
    round (which has none) is judged the same way as a filling one."""
    if commitment(storage(b"book" + struct.pack("<I", m))) != fr["book_hash"]:
        return "book-moved"           # another round or a cancel rewrote the book
    if fr["consumed"]:
        have = _set_entries(storage(fr["set_key"] + struct.pack("<I", m)))
        if any(e not in have for e in fr["consumed"]):
            return "commit-gone"      # a sealed order's commit was consumed or expired
    for a, s in fr["minseq"].items():
        if _seq_floor(a) >= s:
            return "seq-floor"        # the account's floor passed this round's oldest order
    return None

def _abandon(m, fr, why, now):
    """Release a round that won't settle (dead) or hasn't (timeout). Its orders go back to
    the FRONT of the mempool: they are the oldest orders of their accounts, and behind
    newer ones they would be overtaken and stranded by the rising seq floor. Orders a
    landed released round claimed stay out (they settled there). The round is kept as a
    zombie so a late landing is still finalized (see _resolve_zombies)."""
    lost = fr.get("claimed") or {}
    orders = [o for o in fr["sealed"] + fr["public"] if _okey(o) not in lost]
    with _lock:
        pending[m] = orders + pending.get(m, [])
    for o in orders:
        order_telemetry.requeued(m, o["account"], o["oid"])
    zs = _zombies.setdefault(m, [])
    zs.append(dict(fr, abandoned_at=now, why=why, ok_since=None, claimed=dict(lost)))
    while len(zs) > ZOMBIE_CAP:
        # forget the OLDEST unsighted zombie: a sighted one holds orders it will receipt
        i = next((i for i, z in enumerate(zs) if z.get("ok_since") is None), None)
        if i is None:
            break
        del zs[i]
    if why == "timeout":
        # the chain never included it: cool down before re-submitting. A DEAD round is
        # rebuilt at the next auction — the chain did its part, the round was just stale.
        _round_gate[m] = {"check": None, "t": now}
    metrics.inc("jamswap_round_abandoned_total", {"market": str(m), "reason": why})
    print(f"round m{m}: released ({why}) — re-queued {len(orders)} order(s) to the front, no receipts")

def _records(m):
    # every round of market m still tracked: the one in flight, then the released ones
    return ([_inflight[m]] if m in _inflight else []) + list(_zombies.get(m, []))

def _claim(m, z):
    """Released round z landed after all: its orders are ITS to receipt. Take them out of the
    mempool, and note on every other tracked round carrying copies (the round in flight,
    other released ones) that z holds them — so none of those re-queues or receipts them,
    and nothing is ever stripped from a round's own record (a re-org can hand them back,
    see _unclaim). Returns z's order keys."""
    keys = _keys(z)
    with _lock:
        pending[m] = [o for o in pending.get(m, []) if _okey(o) not in keys]
    for fr in _records(m):
        if fr is not z:
            for k in _keys(fr) & keys:
                fr.setdefault("claimed", {})[k] = z["rid"]
    return keys

def _unclaim(m, z):
    """A re-org erased z's late landing: its orders are free again. Rounds carrying copies
    own them again; the rest go back to the FRONT of the mempool — not those the round in
    flight carries (it may still settle them), nor those another landed round holds, nor
    any already queued. Returns the orders re-queued."""
    busy = set()
    for fr in _records(m):
        if fr is z:
            continue
        cl = fr.get("claimed") or {}
        for k in [k for k, r in cl.items() if r == z["rid"]]:
            del cl[k]
        if fr is _inflight.get(m) or fr.get("ok_since") is not None:
            busy |= _keys(fr)
    with _lock:
        busy |= {_okey(o) for o in pending.get(m, [])}
        back = [o for o in z["sealed"] + z["public"] if _okey(o) not in busy]
        pending[m] = back + pending.get(m, [])
    return back

def _resolve_zombies(now, m):
    """Watch market m's released rounds for a LATE landing: lasair can still include a
    round's work-item after the builder gave up on it. A zombie whose id is marked landed
    settled: its orders are claimed at once and, on a LATER sweep once the settle hold has
    passed, it is finalized like any round — two sightings, exactly as for the round in
    flight, so a landing seen once on a fork that then loses is undone, not receipted (with
    SETTLE_HOLD_SECS=0 it used to finalize, irreversibly, on the very sweep that first saw
    it). A late landing a re-org erases gives its orders back. Unsighted zombies are
    forgotten after ZOMBIE_WATCH_SECS. Returns the order keys this call claimed.
    Callers hold the market lock."""
    taken = set()
    keep = []
    for z in list(_zombies.get(m, [])):
        if z.get("ok_since") is None and now - z["abandoned_at"] > ZOMBIE_WATCH_SECS:
            continue
        try:
            slot = _landed_slot(z["rid"])
        except Exception:
            keep.append(z)                               # reader hiccup: retry next sweep
            continue
        if slot is not None:
            if z.get("ok_since") is None:
                z["ok_since"] = now                      # first sighting: it settled after all
                taken |= _claim(m, z)
                metrics.inc("jamswap_round_late_landed_total", {"market": str(m)})
                print(f"round m{m}: released round ({z['why']}) SETTLED LATE at slot {slot} — "
                      f"claimed its {len(z['sealed']) + len(z['public'])} order(s)")
            elif _confirmed(z["ok_since"], now) and not _rival_landed(m, z):
                try: _finalize_round(m, z)
                except Exception as e: print("late round finalize failed", m, e)
                continue                                 # done: stop watching it
        elif z.get("ok_since") is not None:
            z["ok_since"] = None                         # a re-org erased the late landing
            _unclaim(m, z)
            metrics.inc("jamswap_settle_reverted_total", {"market": str(m)})
            for o in z["sealed"] + z["public"]:
                order_telemetry.reverted(m, o["account"], o["oid"])
            print(f"round m{m}: late landing of a released round REVERTED by re-org — orders freed")
        keep.append(z)
    if keep:
        _zombies[m] = keep
    else:
        _zombies.pop(m, None)
    return taken

def _rival_landed(m, fr):
    # another tracked round sharing an order with fr ALSO reads as landed. Both can't have (the
    # shared order's seq floor / commit admits only one), so the reads straddled a re-org:
    # finalize neither until one view remains, rather than receipt an order twice
    mine = _keys(fr)
    return any(other is not fr and _keys(other) & mine and _landed_slot(other["rid"]) is not None
               for other in _records(m))

def _resolve_inflight(m, fr, now):
    """The round in flight: landed and held -> finalize (receipts + carries); landed then
    erased -> keep waiting; can't settle (dead, confirmed for DEAD_CONFIRM_SECS) or overdue
    -> release it, re-queueing its orders with NO receipts."""
    try:
        why = _round_dead(m, fr)                  # the preconditions FIRST ...
        slot = _landed_slot(fr["rid"])            # ... then the marker (see _round_dead)
    except Exception:
        return                                    # reader hiccup: retry next sweep
    if slot is not None:
        fr.pop("dead_since", None)
        if fr.get("ok_since") is None:
            fr["ok_since"] = now                  # first sighting on-chain: start the hold
        elif _confirmed(fr["ok_since"], now) and not _rival_landed(m, fr):
            _inflight.pop(m, None)                # survived the hold window: durable
            try: _finalize_round(m, fr)
            except Exception as e: print("round finalize failed", m, e)
    elif fr.get("ok_since") is not None:
        # the round left the chain: the settling branch lost fork choice — a re-org ate
        # the round. Keep waiting (the guarantor re-queues and re-guarantees its
        # work-item); count it so the dashboard shows it.
        fr["ok_since"] = None
        metrics.inc("jamswap_settle_reverted_total", {"market": str(m)})
        for o in fr["sealed"] + fr["public"]:
            order_telemetry.reverted(m, o["account"], o["oid"])
        print(f"round m{m}: settlement REVERTED by re-org — holding for re-settle")
    elif why:
        # dead — but a re-org could still restore what it needs, so it must stay dead
        # for DEAD_CONFIRM_SECS before its orders are re-batched
        if now - fr.setdefault("dead_since", now) >= DEAD_CONFIRM_SECS:
            _inflight.pop(m, None)
            _abandon(m, fr, why, now)
    else:
        fr.pop("dead_since", None)
        if now - fr["t"] > ROUND_GATE_SECS:
            _inflight.pop(m, None)
            _abandon(m, fr, "timeout", now)

def _resolve_rounds_once(now=None):
    """One sweep: fresh-account registrations and queued re-seals; then, per market, its
    released rounds (late landings first, so their claims are known before the live round
    is judged) and its round in flight; then signed cancels. Runs on the resolver thread
    every 2 s; tests call it directly. A market whose next round is being built right now
    is skipped until the next sweep (see _market_lock)."""
    now = now or time.time()
    _resolve_registrations_once(now)   # confirm-or-retry fresh-account registrations
    _drain_carry_retry(now)            # retry any re-seals the chain was too busy to accept
    for m in sorted(set(_inflight) | set(_zombies)):
        lk = _market_lock(m)
        if not lk.acquire(blocking=False):
            continue
        try:
            _resolve_zombies(now, m)
            fr = _inflight.get(m)
            if fr:
                _resolve_inflight(m, fr, now)
        finally:
            lk.release()
    _resolve_cancels(now)

def _round_resolver():
    while True:
        time.sleep(2.0)
        try: _resolve_rounds_once()
        except Exception as e: print("round resolver error:", e)

# ---- account & market observability (Grafana: "JAMswap accounts & trading") ----
# Polls ON-CHAIN state via the reader every 15 s into labeled gauges. The
# conservation panels rest on one invariant: per asset, sum(dev balances) only
# moves by faucet mints — anything else stepping that line is a settlement bug
# or a re-org rewriting history (both worth an alarm, not a shrug).
ACCOUNT_NAMES = {1: "Alice", 2: "Bob", 3: "Carol", 4: "David", 5: "Eve", 6: "Fergie"}
ASSET_NAMES = {USDC: "USDC", DOT: "DOT", JAMKB: "JAMKB"}
metrics.describe("jamswap_balance", "on-chain balance per dev account and asset (display units)")
metrics.describe("jamswap_dev_supply", "sum of the six dev accounts' balances per asset — flat unless the faucet mints")
metrics.describe("jamswap_last_price", "on-chain last clearing price per market")
metrics.describe("jamswap_cum_volume", "on-chain cumulative traded volume per market (a DROP = re-org erased settlements)")
metrics.describe("jamswap_book_depth", "resting on-chain book quantity per market and side")
metrics.describe("jamswap_mempool_orders", "orders waiting in the off-chain mempool per market")
metrics.describe("jamswap_inflight_orders", "orders inside a round awaiting durable settlement per market")
metrics.describe("jamswap_settle_reverted_total", "settlements observed on-chain then ERASED by a re-org before the hold window passed")
metrics.describe("jamswap_round_abandoned_total", "rounds released without settling, by reason (book-moved / commit-gone / seq-floor = dead; timeout = never included)")
metrics.describe("jamswap_round_late_landed_total", "released rounds that settled after all (seen by their landed-round marker, then finalized)")

def _stats_poller():
    while True:
        time.sleep(15.0)
        try:
            for a, an in ASSET_NAMES.items():
                total = 0.0
                for h, hn in ACCOUNT_NAMES.items():
                    v = bal(a, h) / SCALE
                    metrics.set_gauge("jamswap_balance", {"account": hn, "asset": an}, v)
                    total += v
                metrics.set_gauge("jamswap_dev_supply", {"asset": an}, total)
            for m, base, quote in DEFAULT_MARKETS:
                lbl = {"market": str(m)}
                metrics.set_gauge("jamswap_last_price", lbl, mstate(b"lp", m) / SCALE)
                metrics.set_gauge("jamswap_cum_volume", lbl, mstate(b"cv", m) / SCALE)
                depth = {"buy": 0.0, "sell": 0.0}
                for o in book_of(m):
                    depth[o["side"]] += o["qty"]
                for side, q in depth.items():
                    metrics.set_gauge("jamswap_book_depth", {"market": str(m), "side": side}, q)
                metrics.set_gauge("jamswap_mempool_orders", lbl, float(len(pending.get(m, []))))
                fr = _inflight.get(m)
                metrics.set_gauge("jamswap_inflight_orders", lbl,
                                  float(len(fr["sealed"]) + len(fr["public"])) if fr else 0.0)
            order_telemetry.snapshot()   # refresh jamswap_order_open + SLO gauge
        except Exception as e:
            print("stats poller error:", e)

# Per-market record of the head height at which each on-chain commit entry was first
# observed — the basis for the Phase-2 finality gate below.  market -> {entry: height}
_commit_seen = {}

def _sealed_ready_predicate(m, commit_entries, fin):
    """Return a predicate `o -> bool`: may this sealed order be REVEALED this round?

    Phase 1 — its owner-signed commit must be on the best chain (`commit_entries`: the
    commit set, or the encset in encrypt-until-batch mode), so the round's consume_set
    won't miss it.

    Phase 2 — on a FINALIZING chain, the commit must also be β-FINALIZED. A finalized
    commit can never re-org out, so the reveal round can't be rolled back for a vanished
    commit (the race the old deferral loop fought). We approximate 'finalized'
    conservatively without a finalized-state read: pin the head height at which each
    commit is first seen on-chain, and treat it as final once `finalized_height` reaches
    that height. This only ever DELAYS a reveal relative to Phase 1 — it never reveals a
    commit that isn't on-chain — so it is a strict safety improvement. On a NON-finalizing
    chain (no finalized height, e.g. the mixed net) it falls back to best-chain membership,
    so sealed trading still works there.
    """
    seen = _commit_seen.setdefault(m, {})
    bh = fin.get("block_height")
    for e in commit_entries:                 # first sighting of a commit: pin the head height
        if e not in seen and bh is not None:
            seen[e] = bh
    for e in list(seen):                     # forget commits that left the set (consumed/expired)
        if e not in commit_entries:
            del seen[e]
    fh = fin.get("finalized_height") if fin.get("available") else None
    def ready(o):
        e = _consumed_entry(o)
        if e not in commit_entries:
            return False                     # not on-chain yet — defer
        if fh is None:
            return True                      # non-finalizing chain: best-chain membership is all we have
        h = seen.get(e)
        return h is not None and fh >= h     # β-finalized: durable, safe to reveal
    return ready

def api_round(b):
    m, base, quote = int(b["market"]), int(b["base"]), int(b["quote"])
    with _market_lock(m):              # one builder per market, never mid-resolve (see above)
        return _build_round(m, base, quote)

def _build_round(m, base, quote):
    if m in _inflight:
        return {"ok": True, "gated": True, "reason": "previous round still settling"}
    g = _round_gate.get(m)
    if g:
        if time.time() - g["t"] < ROUND_ZEROFILL_SECS:
            return {"ok": True, "gated": True, "reason": "previous round cooling down"}
        _round_gate.pop(m, None)
    if _zombies.get(m):
        # a released round of this market that settled after all claims its orders OUT of
        # the mempool (they are receipted when it is finalized) before we batch them again
        _resolve_zombies(time.time(), m)
    now = time.time()
    raw = storage(b"book" + struct.pack("<I", m))        # the market's on-chain resting book
    pruned = expired_pairs(m, raw)                        # good-till-time entries past expiry
    shrank = bool(pruned)                                 # some resting order expired this round
    # COMMIT-READINESS GATE (built before the lock — it does a network read of the
    # on-chain commit set). A reveal only settles if its owner-signed commit has ALREADY
    # accumulated: the service's consume_set (lib.rs) matches every consumed hash‖account
    # against the ON-CHAIN commit set and rejects the WHOLE round on one miss. On a
    # contested chain a TAG_COMMIT takes slots (10-60 s) to accumulate while this auction
    # fires 6 s after placement, so revealing immediately raced the commit and the round
    # was silently rolled back. We therefore feed this readiness predicate INTO the
    # planner (not after it): a not-yet-committed sealed order is held OUT of the batch
    # entirely — it can neither reveal nor make another order appear to cross against
    # liquidity that won't be submitted. That keeps the crossing decision and the batch
    # membership consistent, so a revealed order always has its counterparty in the same
    # round (fixes the "revealed alone → leaked + dropped" bug). Encrypt-until-batch gates
    # the same way on its encset: ungated, a round revealing a ciphertext whose ENC_COMMIT
    # hadn't landed was judged dead ("commit-gone"), released and rebuilt — re-running the
    # committee and re-submitting — every auction until the commit landed.
    set_key = b"encset" if ENC_MODE else b"commits"
    commit_entries = _set_entries(storage(set_key + struct.pack("<I", m)))
    sealed_ready = _sealed_ready_predicate(m, commit_entries, _read_finality())
    with _lock:                        # snapshot + re-queue atomically so a concurrent
        pend_all = pending.get(m, [])  # api_order during submit isn't dropped
        # Cap the batch: a round refines one in-PVM ed25519 verify per signed order
        # (~5.29M gas each), so an unbounded batch eventually exceeds any refine
        # budget and the round becomes a poison pill that can never settle — while
        # re-queued failures keep GROWING it (observed live: 116-order batches,
        # volume pinned at 0). Oldest orders go first; the overflow waits its turn —
        # and never holds an account's older order back behind its newer one (_cap_batch).
        pend, overflow = _cap_batch(pend_all, MAX_ROUND_ORDERS)
        for o in pend:                 # attach current GTT expiry for the planner
            o["expiry"] = order_expiry.get((m, o["account"], o["oid"]))
        # Decide which orders clear now. Sealed orders that DON'T cross current liquidity
        # rest HIDDEN (carried forward) rather than being immediate-or-cancel — so a sealed
        # sell placed now can meet a sealed buy placed in a later auction. A sealed order is
        # revealed only in the round it actually crosses AND its commit is on-chain
        # (sealed_ready), so the planner and the batch agree (see round.py + tests).
        resting_orders = [o for o in _parse_book(raw) if (o["account"], o["oid"]) not in set(pruned)]
        plan = plan_round(pend, resting_orders, now, sealed_ready=sealed_ready)
        # hidden non-crossing sealed + not-yet-committed (deferred) + over-cap tail all wait
        pending[m] = plan.carry + plan.deferred + overflow
    # From here this round's orders (the batch, plus the GTT-expired sealed ones) are OUT of the
    # mempool. Each is ended with a terminal, or registered in flight with the round — or, on
    # ANY exception before the round is registered (a reader timeout in the price / seq-floor
    # reads below, a committee failure), put back at the front in mempool order
    # (_requeue_unsent). Such an exception used to lose the whole batch: in neither the mempool
    # nor _inflight, never submitted, and "open" in the order telemetry forever.
    out = {_okey(o) for o in plan.reveal + plan.public + plan.expired}
    taken = [o for o in pend if _okey(o) in out]
    ended = set()                      # keys of taken orders already ended with a terminal
    try:
        for o in plan.expired:         # GTT-expired sealed orders that never found a counterparty
            order_telemetry.terminal(m, o["account"], o["oid"], "expired")
            order_expiry.pop((m, o["account"], o["oid"]), None)
            ended.add(_okey(o))
        rec, payload, detail, reply = _assemble_round(
            m, base, quote, now, raw, pruned, shrank, plan, resting_orders, commit_entries,
            set_key, ended)
    except BaseException:
        back = _requeue_unsent(m, taken, ended)
        print(f"round m{m}: build failed before submit — re-queued {len(back)} order(s) to the front")
        raise
    if reply is not None:
        return reply                   # nothing to submit
    sealed, public, rid = rec["sealed"], rec["public"], rec["rid"]
    # IN FLIGHT from before the submit: nothing is receipted or carried until the resolver
    # sees its id marked landed — a round that can't or doesn't settle re-queues these exact
    # orders instead (no phantom fills, nothing silently lost), and a released round seen
    # landing from here on claims its orders from this record. A revealed sealed order that
    # crossed nothing is carried at finalize, not dropped.
    _inflight[m] = rec
    try:
        submit(payload, check=lambda: _landed_slot(rid) is not None, detail=detail)
    except ChainBusy:
        # BACKPRESSURE: every lm node's CE-133 queue is at cap (lasair --wp-queue-cap),
        # so the round never left the builder. Nothing cleared — put its orders back in
        # the mempool (they BATCH into the retry, same as gate-held orders) and cool the
        # market down so retries don't re-flood the fleet. No receipts, no carry, nothing
        # in flight: the round simply never happened.
        _inflight.pop(m, None)
        with _lock:                    # FRONT: they are still the oldest orders of their accounts
            pending[m] = sealed + public + pending.get(m, [])
        _round_gate[m] = {"check": None, "t": time.time()}
        print(f"round m{m}: chain busy — re-queued {len(sealed) + len(public)} order(s), cooling down")
        return {"ok": False, "backpressure": True, "requeued": len(sealed) + len(public)}
    except Exception as e:
        # outcome UNKNOWN (builder timeout, connection reset): the payload may still have been
        # relayed, so the round stays in flight — its marker says if it landed, and dead
        # detection or the gate releases it otherwise. Re-queueing now could put the same
        # orders in two rounds; the old path dropped them instead (in neither the mempool nor
        # a round, never receipted).
        print(f"round m{m}: submit outcome unknown ({type(e).__name__}: {e}) — tracking it in flight")
    # one record per round id: an unchanged rebuild of a released round IS that round, and
    # two records would each finalize it (duplicate receipts, a second carry of each
    # sealed remainder). Dropped only now that this record is on its way to the chain.
    zs = [z for z in _zombies.get(m, []) if z["rid"] != rid or z.get("ok_since") is not None]
    if zs:
        _zombies[m] = zs
    else:
        _zombies.pop(m, None)
    for o in sealed + public:
        order_telemetry.rounded(m, o["account"], o["oid"])
    return {"ok": True, "queued": True,
            "cleared": {"sealed": len(sealed), "public": len(public),
                        "resting_hidden": len(plan.carry), "expired": len(plan.expired)}}

def _requeue_unsent(m, taken, ended):
    """A build failed after taking its orders out of the mempool, before registering the
    round: put back every one it still owns, at the FRONT in the order they were taken —
    not those it ended (a terminal was recorded), not those a released round that landed
    holds (they are that round's to receipt, see _claim), not any already queued (nothing
    is queued twice). Runs under the market lock, so the resolver can't interleave.
    Returns the orders re-queued."""
    skip = set(ended)
    for z in _zombies.get(m, []):
        if z.get("ok_since") is not None:
            skip |= _keys(z)
    with _lock:
        skip |= {_okey(o) for o in pending.get(m, [])}
        back = [o for o in taken if _okey(o) not in skip]
        pending[m] = back + pending.get(m, [])
    return back

def _assemble_round(m, base, quote, now, raw, pruned, shrank, plan, resting_orders,
                    commit_entries, set_key, ended):
    """The rest of a build, once its orders are out of the mempool: price market orders,
    enforce the seq discipline (both read the chain), end the orders that can never settle,
    clear, and build the payload + the in-flight record. Adds the key of every order it ends
    to `ended`. Returns (record, payload, detail, None), or (None, None, None, reply) when
    there is nothing to submit. Any exception propagates to _build_round, which re-queues
    whatever the build still owns."""
    if plan.deferred:                  # observable, non-terminal: waiting for the commit
        for o in plan.deferred:        # distinguish "not on-chain yet" from "on-chain, awaiting β"
            onch = _consumed_entry(o) in commit_entries
            order_telemetry.deferred(m, o["account"], o["oid"],
                                     "awaiting-finality" if onch else "commit-not-onchain")
        print(f"round m{m}: deferred {len(plan.deferred)} sealed reveal(s) — commit not final yet")
    sealed, public = plan.reveal, plan.public
    # SEQ DISCIPLINE (service floors::check_bindings): every signed order's seq must
    # STRICTLY beat its account's on-chain floor, and the service raises the floor to
    # each order's seq IN ROUND ORDER — so one order at/below the floor rejects the
    # WHOLE round untouched (fail-closed). Three failure modes this guards:
    #   (1) a re-queued order whose account settled a higher seq meanwhile is now
    #       permanently stale (seq <= floor) — it can NEVER settle; drop it (don't
    #       re-queue it to poison every future round);
    #   (2) two live orders from one account in one round out of seq order — the
    #       higher raises the floor and rejects the lower. Sort ascending per account;
    #   (3) two orders carrying the SAME seq (a re-posted signed order) — the second
    #       can never settle; drop it.
    # A market order also can't ride at a price outside the band the service checks, so
    # it is re-priced at the current last price first (or dropped when there is none).
    # Without this the first round timeout cascades into a permanent cv stall (found
    # live on the all-lasair net 2026-07-09: chain coherent + accumulating, cv frozen).
    public, dead = _price_market_orders(m, public)
    public, stale, floors = _seq_sanitize(m, public)
    dead += stale
    if _zombies.get(m):
        # an order may be stale because its OWN released round landed a moment ago, after
        # the zombie check above but before the floor reads: look again (now after the
        # floors) so such orders are finalized as the fills they are, not rejected. Nothing
        # else can claim them while we hold the market lock.
        claimed = _resolve_zombies(now, m)
        if claimed:
            sealed = [o for o in sealed if _okey(o) not in claimed]
            public = [o for o in public if _okey(o) not in claimed]
            dead = [(o, why) for o, why in dead if _okey(o) not in claimed]
    for o, why in dead:
        # truly unsettleable (a newer order of the account settled first — e.g. on another
        # market: floors are per account — or a repeated seq): end it with a receipt that
        # says why, instead of letting it vanish from the trader's view
        _record_exec(dict(o, market=m, _outcome="rejected", _reason=why), 0, o["price"], False, now)
        order_expiry.pop((m, o["account"], o["oid"]), None)
        ended.add(_okey(o))
    if dead:
        _save_execs()
    if not (sealed or public or shrank):
        # nothing to submit (every sealed order is carried hidden or deferred): no round
        return None, None, None, {
            "ok": True, "price": disp(mstate(b"lp", m)), "volume": disp(mstate(b"cv", m)),
            "book": book_of(m), "cleared": {"sealed": len(sealed), "public": len(public),
            "resting_hidden": len(plan.carry), "carried_remainder": 0,
            "expired": len(plan.expired)}}
    hdr = struct.pack("<III", m, base, quote)
    # every round type carries the same signed public section: new orders WITH their
    # signatures (verified in refine), the explicit prune list, and the on-chain book
    # byte-exact (the service hash-checks it — a fabricated book rejects the round).
    section = public_section_bytes(public, pruned, raw)
    # Pre-compute this round's clearing (pure; mirrors refine): the per-order fill
    # receipts and sealed-remainder carries handed out once the round settles.
    combined = resting_orders + sealed + public
    clearing = clear(combined) if combined else {"price": 0, "volume": 0, "fills": {}}
    try:
        if sealed and ENC_MODE:
            # encrypt-until-batch round: the committee decrypts each sealed ciphertext (proving it
            # via Chaum-Pedersen); refine verifies every proof, recovers the orders, and clears them
            # with the resting book + public orders at ONE uniform price. Only the sealed orders that
            # CROSS this round are here (the planner keeps non-crossing ones hidden for later), so a
            # sealed order is decrypted on-chain only in the round it actually trades. Any unfilled
            # remainder of a revealed order is immediate-or-cancel (never rests publicly exposed).
            # No reveal round — traders needn't be online at match time.
            cts = ",".join(o["ciphertext"] for o in sealed)
            payload = bytes.fromhex(committee_run("round", m, base, quote, section.hex(), cts)["round"])
        elif sealed:
            # UNIFIED sealed round (commit–reveal): the resting book + this round's public orders +
            # the revealed sealed orders all clear together at ONE uniform price (so a sealed order
            # can cross public/resting liquidity). Only sealed orders that CROSS this round are
            # revealed (non-crossing ones stay hidden, carried forward by the planner); the node
            # re-checks each reveal's hash ∈ commits. Any unfilled remainder of a revealed order is
            # immediate-or-cancel (never rests publicly exposed).
            commits = b"".join(commitment(o["reveal"]) for o in sealed)
            reveals = b"".join(o["reveal"] for o in sealed)
            payload = (bytes([TAG_REVEAL]) + hdr + struct.pack("<I", len(commits)) + commits
                       + struct.pack("<I", len(reveals)) + reveals + section)
        else:
            # signed public round: the section carries the new signed orders + prune list + the
            # on-chain book. Also runs on `shrank` (an order expired) with no new orders, to
            # rewrite the book without the expired one — an empty cross conserves value and
            # leaves the last price untouched (apply_settlement only updates lp on real fills).
            payload = bytes([TAG_SMATCH]) + hdr + section
    except Exception:
        # the round was never submitted (the committee sidecar failed): nothing is lost —
        # _build_round puts its orders back at the front; the market cools down
        _round_gate[m] = {"check": None, "t": time.time()}
        raise
    # This round's identity — the id refine derives from these exact bytes — and the
    # preconditions accumulate will re-check (for early dead-round detection).
    rid = round_id(payload)
    minseq = {}
    for o in public:
        minseq[o["account"]] = min(minseq.get(o["account"], o["seq"]), o["seq"])
    rec = {"t": time.time(), "sealed": sealed, "public": public,
           "resting": resting_orders, "clearing": clearing, "pruned": pruned,
           "rid": rid, "book_hash": commitment(raw),
           "consumed": [_consumed_entry(o) for o in sealed],
           "set_key": set_key, "minseq": minseq}
    detail = f"market {m}: {len(sealed)} sealed + {len(public)} public, vol {disp(clearing['volume'])}"
    return rec, payload, detail, None
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
    onchain_sealed = len(storage(seal_key + struct.pack("<I", m))) // SET_ENTRY_LEN
    fr = _inflight.get(m)
    return {"price": disp(mstate(b"lp", m)), "volume": disp(mstate(b"cv", m)), "book": book_of(m),
            "pending": len(pending.get(m, [])), "mempool": mempool, "sealed_onchain": onchain_sealed,
            # orders inside the round currently AWAITING SETTLEMENT: neither in the
            # mempool nor receipted until the chain confirms (or they re-queue)
            "in_auction": (len(fr["sealed"]) + len(fr["public"])) if fr else 0,
            "seal_mode": "encrypt-until-batch" if ENC_MODE else "commit-reveal",
            "next_auction_in": round(max(0.0, _next_auction[0] - time.time()), 1), "auction_secs": AUCTION_SECS,
            # anti-bloat policy so the UI can tell traders orders auto-expire (no rest-forever GTC)
            "order_life": {"public_secs": round(order_lifetime_secs(False)),
                           "sealed_secs": round(order_lifetime_secs(True)),
                           "max_secs": round(MAX_RESTING_SECS), "max_open": MAX_OPEN_ORDERS}}
def api_mine(q):
    # a trader's own LIVE orders across all markets, in EVERY lifecycle state — sealed terms
    # DECRYPTED for the owner. An order must never disappear from the trader's view between
    # states, or a manual trader reads the gap as "it failed". Three states, each tagged so
    # the UI can show a distinct chip:
    #   mempool  — queued off-chain, waiting for the next auction (◎)
    #   settling — inside an in-flight round, awaiting β-durable settlement (⧗)
    #   resting  — rested on-chain, live on the order book (◉)
    acct = int(q["account"])
    now = time.time()
    out = []
    def tag(e, mid, status):
        e["market"] = mid
        e["status"] = status
        e["source"] = status
        exp = order_expiry.get((mid, acct, e["oid"]))   # every order has a bounded expiry
        e["expires_in"] = round(max(0.0, exp - now)) if exp else None
        return e
    seen = set()                                        # (market, oid) already surfaced
    for mid, orders in pending.items():                 # 1) waiting in the mempool
        for o in orders:
            if o["account"] == acct:
                out.append(tag(mempool_entry(o, owner=True), mid, "settling"
                               if o.get("_outcome", "").endswith("carried") else "mempool"))
                seen.add((mid, o["oid"]))
    # 2) in a round, settling on-chain — the round in flight, or a released round that
    #    landed late and is in its settle hold (its orders already left the mempool)
    settling = [(mid, fr) for mid, fr in list(_inflight.items())]
    settling += [(mid, z) for mid, zs in list(_zombies.items()) for z in zs if z.get("ok_since")]
    for mid, fr in settling:
        for o in fr["sealed"] + fr["public"]:
            if o["account"] == acct and (mid, o["oid"]) not in seen:
                out.append(tag(mempool_entry(o, owner=True), mid, "settling"))
                seen.add((mid, o["oid"]))
    for m, base, quote in DEFAULT_MARKETS:              # 3) resting live on the on-chain book
        for r in book_of(m):
            if r["account"] == acct and (m, r["id"]) not in seen:
                out.append(tag({"oid": r["id"], "account": acct, "side": r["side"],
                                "sealed": False, "type": "limit",
                                "who": f"acct {acct}", "price": r["price"], "qty": r["qty"]},
                               m, "resting"))
                seen.add((m, r["id"]))
    return {"orders": out}
def api_cancel_pending(b):
    # remove an un-processed (not yet cleared) order from the mempool, owner-checked
    acct, oid = int(b["account"]), int(b["order_id"])
    removed = []
    for mid in list(pending):
        # under the market lock: a round being built from this mempool can't resurrect the
        # order afterwards, and the released rounds checked below can't change meanwhile
        with _market_lock(mid):
            with _lock:
                hits = [o for o in pending.get(mid, []) if o["account"] == acct and o["oid"] == oid]
            if not hits:
                continue
            keys = {_okey(o) for o in hits}
            for z in _zombies.get(mid, []):
                # the order is back in the mempool because its round was released — but if that
                # round can still land, the order may trade there (then it gets THAT receipt and
                # a "cancelled" one would be false). Refuse until it can't: that takes a few
                # seconds after the round's book / floors / commits move on.
                if keys & _keys(z) and (z.get("ok_since") is not None or _round_dead(mid, z) is None):
                    raise ValueError(f"order {oid} is in a round that may still settle on-chain — "
                                     f"try the cancel again shortly")
            with _lock:
                pending[mid] = [o for o in pending.get(mid, [])
                                if not (o["account"] == acct and o["oid"] == oid)]
            removed += [(mid, o) for o in hits]
    now = time.time()
    for mid, o in removed:
        # end its lifecycle with a receipt, or it stays "open" in the order telemetry forever
        order_expiry.pop((mid, acct, oid), None)
        _record_exec(dict(o, market=mid, _outcome="cancelled",
                          _reason="cancelled by owner (removed from the mempool)"),
                     0, o["price"], bool(o.get("sealed")), now)
    if removed:
        _save_execs()
    return {"ok": True, "removed": len(removed)}
def api_balance(q):
    return {"balance": disp(bal(int(q["asset"]), int(q["account"])))}
def api_footprint(q):
    # the service's live state footprint (validator RAM) + the JAMKB it implies.
    # JAMKB is a READ-ONLY tracker for now: 1 JAMKB = 1 KB of footprint. This is a
    # measurement only — nothing is held, funded, or consumed. Whether to enforce a
    # reserve/consumption model in the node is a deferred protocol decision (docs/JAMKB.md).
    if QUIC_MODE:
        # no CE-129 account-footprint read yet — degrade gracefully
        return {"available": False}
    try:
        fp = node(f"/v1/service/{SID}/footprint")
    except Exception:
        # older lasair-node (< the footprint endpoint) — degrade gracefully
        return {"available": False}
    fp["available"] = True
    return fp

ROUTES_POST = {"/api/deposit": api_deposit, "/api/withdraw": api_withdraw,
               "/api/list": api_list, "/api/order": api_order, "/api/round": api_round,
               "/api/seal_prepare": api_seal_prepare,
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
    # deploy with a JAMKB reserve sized to the genesis footprint (obligation + a small buffer),
    # so the service is solvent before any fees accrue — NOT a flat mint. Only tops UP to the
    # target; never seeds a hoard. Idempotent across restarts.
    try:
        target = reserve_target_atomic()          # obligation + buffer, capped at the finite supply
        held = bal(JAMKB, FEE_ACCOUNT)
        if held >= target:
            print(f"treasury JAMKB reserve funded: {disp(held)} JAMKB (target {disp(target)})"); return
        submit(bytes([TAG_DEPOSIT]) + struct.pack("<IIQ", FEE_ACCOUNT, JAMKB, target - held))
        print(f"seeded treasury JAMKB reserve -> {disp(bal(JAMKB, FEE_ACCOUNT))} JAMKB (target {disp(target)})")
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
# ---- execution reports (per-order fill receipts) --------------------------
# The trade tape above is market-level (a clearing print per round). Traders also want a
# per-ORDER receipt: "your BUY 500 filled 200 @ 1.20 (uniform) · 300 cancelled". The chain
# only exposes market-level lp/cv, so the builder recomputes the SAME clearing it hands to
# refine (offchain/clearing.clear, pinned to the Rust engine by tests/test_clearing.py) and
# attributes per-order fills. Kept per account, same 24h TTL + disk persistence as the tape.
EXEC_HISTORY = 200
EXECS_FILE = os.environ.get("EXECS_FILE", "/tmp/jamswap_execs.json")
executions = {}                        # account -> deque[{ts, market, side, price, qty, filled, remainder, disposition}]
def _save_execs():
    try:
        with open(EXECS_FILE, "w") as fh:
            json.dump({str(a): list(dq) for a, dq in executions.items()}, fh)
    except Exception:
        pass
def load_execs():
    try:
        with open(EXECS_FILE) as fh:
            data = json.load(fh)
        now = time.time()
        for k, lst in data.items():
            dq = deque([e for e in lst if e.get("ts", 0) >= now - TRADE_TTL], maxlen=EXEC_HISTORY)
            if dq:
                executions[int(k)] = dq
    except Exception:
        pass
# Receipt dispositions that END an order's lifecycle. A carried remainder is NON-terminal:
# the order keeps working under the same oid until it fully fills or its good-till-time
# expires, so "carried"/"partial-carried" are progress notes, not an end state — the single
# terminal comes when the carried remainder finally clears or expires. This is what lets a
# sealed order cross many auctions and still resolve to EXACTLY ONE terminal outcome (the
# zero-loss invariant). "resting"/"partial-resting" (public remainder on the book) are
# likewise non-terminal.
_TERMINAL_DISP = {"filled", "partial-cancelled", "cancelled", "rejected"}

def _record_exec(o, filled, price, sealed, now):
    # append one order's outcome to its owner's receipt feed. A sealed order's disposition is
    # decided upstream (_carry_sealed_remainders) and stamped on o["_outcome"] with a human
    # o["_reason"]; a public order derives its disposition from the fill here.
    qty, rem = o["qty"], o["qty"] - filled
    disp_ = o.get("_outcome")
    reason = o.get("_reason")
    if disp_ is None:
        if filled >= qty:
            disp_ = "filled"
        elif sealed:
            # a revealed sealed order with no upstream decision: IOC. Should not occur once
            # _carry_sealed_remainders runs (gate-then-plan makes revealed⟹counterparty),
            # but fail SAFE and always give a reason rather than a bare "cancelled".
            disp_ = "partial-cancelled" if filled > 0 else "cancelled"
            reason = reason or "unfilled"
        else:
            disp_ = "partial-resting" if filled > 0 else "resting"       # remainder rests in book
    dq = executions.setdefault(o["account"], deque(maxlen=EXEC_HISTORY))
    _fin = _read_finality()
    _sh = _fin.get("block_height") if _fin.get("available") else None
    dq.append({"ts": now, "market": o["market"], "side": o["side"],
               "price": disp(price), "qty": disp(qty), "filled": disp(filled),
               "remainder": disp(rem), "disposition": disp_, "reason": reason, "oid": o["oid"],
               # head height at settle time; the fill is β-final once finalized_height reaches it
               "settle_height": _sh})
    if disp_ in _TERMINAL_DISP:
        order_telemetry.terminal(o["market"], o["account"], o["oid"], disp_, filled=filled,
                                 reason=reason)
def record_executions(m, resting, reveal, public, clearing=None):
    # write a per-order receipt for the trader-submitted orders (reveal=sealed, public=rests) and
    # any resting maker that filled, from this round's clearing. `clearing` may be passed in (api_round
    # computes it once, for both the receipt and the sealed-remainder carry); recomputed if omitted.
    # No receipts when nothing crossed. Every public order the round leaves on the book (unfilled,
    # part-filled, or a resting maker the price reached but rationing passed over) is also marked
    # `rested` in the order telemetry, with the clearing price it met (soak_verdict judges by it).
    combined = list(resting) + list(reveal) + list(public)
    if not combined:
        return
    c = clearing if clearing is not None else clear(combined)
    price, fills = c["price"], c["fills"]
    reveal_ids = {o["oid"] for o in reveal}
    now = time.time()
    touched = False

    def crossed(o):
        # did the order's limit reach this auction's uniform price (eligible to trade at it)?
        if not c["volume"]:
            return False
        return o["price"] >= price if o["side"] == BUY else o["price"] <= price

    def rests(o, filled):
        # the order (or its remainder) stays on the book: say so in the order telemetry, with
        # the price it met, so an order outbid in its auction is not later judged a miss
        order_telemetry.rested(m, o["account"], o["oid"], price, crossed(o), filled=filled,
                               expires_at=order_expiry.get((m, o["account"], o["oid"])))

    for o in reveal + public:                       # this round's own submissions
        o = dict(o, market=m)
        filled = fills.get(o["oid"], 0)
        if o["oid"] not in reveal_ids and filled < o["qty"]:
            rests(o, filled)                        # a public order's remainder rests on the book
            if filled == 0:
                continue                            # a fully-unfilled public order: no receipt
        _record_exec(o, filled, price, o["oid"] in reveal_ids, now)
        touched = True
    for o in resting:                               # resting makers that got filled this round
        filled = fills.get(o["oid"], 0)
        if filled < o["qty"] and (filled > 0 or crossed(o)):
            # still resting, and this auction changed its story: part-filled, or the price
            # reached it and rationing passed it over (an untouched maker needs no event)
            rests(o, filled)
        if filled > 0:
            _record_exec(dict(o, market=m), filled, price, sealed=False, now=now)
            touched = True
    if touched:
        _save_execs()
def api_executions(q):
    # a trader's recent per-order fill receipts, most-recent first.
    acct = int(q["account"])
    dq = executions.get(acct, ())
    return {"executions": list(reversed(list(dq)))[:100]}

def api_pending(q):
    # the submit->settle ledger (docs/OBSERVABILITY_PLAN.md phase 0): where every
    # relayed op sits right now — pending / settled (+latency) / timed_out.
    return {"pending": metrics.pending_snapshot()}

def api_orders_slo(q):
    # the per-order lifecycle SLO: cleared/(cleared+missed) among marketable orders,
    # plus how many orders sit in each phase right now. The soak's headline number.
    return order_telemetry.snapshot()

ROUTES_GET = {"/api/state": api_state, "/api/balance": api_balance, "/api/mine": api_mine,
              "/api/footprint": api_footprint, "/api/handle": api_handle, "/api/nonce": api_nonce,
              "/api/govnonce": api_govnonce, "/api/treasury_status": api_treasury_status,
              "/api/trades": api_trades, "/api/executions": api_executions,
              "/api/pending": api_pending, "/api/orders_slo": api_orders_slo,
              "/api/finality": api_finality}

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
        if path == "/metrics":               # Prometheus text exposition (plan phase 1)
            self._send(200, metrics.render().encode(), "text/plain; version=0.0.4")
            return
        if path in ROUTES_GET:
            try:
                self._send(200, json.dumps(ROUTES_GET[path](q)).encode())
                metrics.inc("jamswap_api_requests_total", {"route": path, "code": 200})
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode())
                metrics.inc("jamswap_api_requests_total", {"route": path, "code": 500})
                metrics.inc("jamswap_api_errors_total", {"route": path})
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
            try:
                self._send(200, json.dumps(ROUTES_POST[path](body)).encode())
                metrics.inc("jamswap_api_requests_total", {"route": path, "code": 200})
            except ChainBusy as e:
                # backpressure, not a bug: the chain refused the payload — tell the
                # caller to retry later (the UI/loadgen treat 503 as retryable)
                self._send(503, json.dumps({"error": str(e), "retry": True}).encode())
                metrics.inc("jamswap_api_requests_total", {"route": path, "code": 503})
            except ValueError as e:
                # CLIENT error, not a server fault: validation refused the request
                # (open-order cap, insufficient funds, bad signature, unregistered
                # account, malformed body). 400 so callers — and the fuzzer — can tell
                # a legitimate refusal from a server that actually broke (500).
                self._send(400, json.dumps({"error": str(e)}).encode())
                metrics.inc("jamswap_api_requests_total", {"route": path, "code": 400})
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode())
                metrics.inc("jamswap_api_requests_total", {"route": path, "code": 500})
                metrics.inc("jamswap_api_errors_total", {"route": path})
        else:
            self._send(404, json.dumps({"error": "no route"}).encode())

def wait_for_node():
    # QUIC mode: wait for the reader bridge to have learned a chain head (so CE-129
    # reads will succeed). Node-RPC mode: wait for the operator /v1/healthz.
    for _ in range(60):
        try:
            if READER_URL:
                if reader_get("/healthz").get("head_hex"): return
            elif "ok" in str(node("/v1/healthz")): return
        except Exception: pass
        time.sleep(1)

def deploy_jam():
    jam = open(os.environ["JAM"], "rb").read()
    r = node("/v1/service", {"jam_hex": jam.hex()})
    return int(r["service_id"])

if __name__ == "__main__":
    if SID is None and QUIC_MODE:
        raise SystemExit("QUIC mode (BUILDER_URL/READER_URL set) requires SERVICE_ID: "
                         "the service is seeded into genesis (Chain.seed_service), not "
                         "deployed at runtime. Set SERVICE_ID to the seeded id.")
    if SID is None and os.environ.get("JAM"):
        wait_for_node()
        SID = deploy_jam()                      # use the id THIS deploy was assigned
        print(f"deployed jamswap-service -> service id {SID}")
    elif SID is None:
        SID = 1729                              # last-resort default (first deploy on a fresh node)
    elif QUIC_MODE:
        # explicit, genesis-seeded id — just wait for the chain to be reachable
        print(f"jamswap-service pre-seeded in genesis at id {SID} (QUIC mode); waiting for head ...")
        wait_for_node()
    print(f"jamswap off-chain API + UI on :{PORT} (node {RPC}, service {SID})")
    load_trades(); load_execs()
    try: ensure_markets(); ensure_reserve(); print("listed default markets:", DEFAULT_MARKETS)
    except Exception as e: print("market listing skipped:", e)
    ensure_committee()
    # service-state gauges, read lazily per scrape (a failed CE-129 read skips the sample)
    metrics.gauge_fn("jamswap_accounts_registered",
                     "account handles assigned on-chain (nexthandle - 1)",
                     lambda: max(0, int.from_bytes(storage(b"nexthandle") or b"\x01", "little") - 1))
    metrics.gauge_fn("jamswap_treasury_jamkb_atomic",
                     "treasury JAMKB balance in atomic units",
                     lambda: bal(JAMKB, FEE_ACCOUNT))
    metrics.gauge_fn("jamswap_treasury_reserve_target_atomic",
                     "JAMKB the treasury must hold to back its footprint (atomic)",
                     lambda: reserve_target_atomic())
    metrics.gauge_fn("jamswap_resting_orders",
                     "resting orders across all on-chain books",
                     lambda: sum(len(book_of(m)) for m, _b, _q in DEFAULT_MARKETS))
    metrics.start_watcher()
    threading.Thread(target=auction_loop, daemon=True).start()
    threading.Thread(target=_round_resolver, daemon=True).start()
    threading.Thread(target=_stats_poller, daemon=True).start()
    print(f"auction loop running every {AUCTION_SECS}s (like JAM block production); "
          f"round resolver confirming settlements every 2s")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
