#!/usr/bin/env python3
"""dex_fuzz.py — an escalating correctness fuzzer for jamswap + lasair.

Same methodology as the JAM conformance fuzzer that hardened lasair: a seeded,
reproducible order stream, ESCALATING penetration, hard invariants checked after
every level, and HALT-ON-FIRST-DIVERGENCE with a forensic dump. Each bug we fix
lets the next run reach a deeper level. The target: thousands of orders settle
100% correctly, end to end (dex round-building -> lasair guarantee/assure/
accumulate -> on-chain balances read back via CE-129).

The oracle is deliberately model-light and invariant-heavy, so it cannot share a
bug with the code under test. Each level places a BALANCED, ALL-CROSSING batch
(every buy priced above every sell, total buy qty == total sell qty), which a
correct FBA must clear in full, leaving the book empty. That makes the expected
end state exactly predictable:

  * CONSERVATION  — per asset, sum over the six dev accounts is INVARIANT (no
                    mint/burn in trading). Exact. A step = a settlement bug or a
                    re-org rewriting history.
  * POSITION      — each account's base (DOT) balance moves by exactly
                    (bought - sold) qty. Exact (quantity is price-independent).
  * VOLUME        — cumulative on-chain volume rises by exactly the batch's total
                    crossing quantity. Exact at quiescence.
  * BOOK EMPTY    — a balanced all-cross leaves nothing resting.
  * NON-NEGATIVE  — no balance underflows.
  * VALUE         — each account's quote (USDC) move is consistent with its fills
                    at a price inside the crossing band (tolerance = the band).
  * LIVENESS      — the batch reaches quiescence within the settle budget. This
                    is the 99.99%-clearing SLO; a stall (e.g. lasair availability
                    not keeping up) fails HERE, loudly, with a reproducing seed.

Run it against a QUIESCENT net with the loadgen STOPPED (the fuzzer wants
exclusive control of the dev accounts' seqs):

    docker exec lasair6-dex-1 python3 dex_fuzz.py --max-pairs 2000
    # reproduce a halt exactly:
    docker exec lasair6-dex-1 python3 dex_fuzz.py --seed 1234 --only-level 5

Forensics for every halt land in FUZZ_DIR (default /shared/fuzz): the seed, the
full order stream, and the expected-vs-actual state diff — everything needed to
root-cause and to replay after a fix.
"""
import argparse
import json
import os
import struct
import time

import server as S           # read side: bal, mstate, book_of, storage, SCALE
import loadgen as L          # dev KEYS, canon, req, market/asset constants

USDC, DOT, JAMKB = 0, 1, 2
ASSETS = {USDC: "USDC", DOT: "DOT", JAMKB: "JAMKB"}
ACCTS = [1, 2, 3, 4, 5, 6]   # the six standard dev-account handles
MARKET, BASE, QUOTE = L.MARKET, L.BASE, L.QUOTE
SC = S.SCALE

# The service charges a flat fee per FILLED order in the market's BASE asset,
# routed to the treasury (service/src/lib.rs: FEE_FLAT, FEE_ACCOUNT). The oracle
# must model it: value is conserved only once the treasury is counted, and each
# filled order's owner pays the fee in base. (Found by this fuzzer at level 1,
# seed 1000 — the oracle was incomplete, the service was correct.)
FEE_FLAT = 300               # atomic base units per filled order
TREASURY = 4294967295        # u32::MAX = FEE_ACCOUNT, where fees accrue

FUZZ_DIR = os.environ.get("FUZZ_DIR", "/shared/fuzz")
STATE_FILE = os.path.join(FUZZ_DIR, "progress.json")

# escalating batch sizes, in crossing PAIRS (2 orders each). Deepen the tail as
# the pipeline is hardened; the fuzzer stops at --max-pairs.
LEVELS = [1, 3, 8, 20, 50, 120, 300, 750, 1500, 3000, 6000]

# crossing band: every buy strictly above every sell, so a balanced batch fully
# clears at one uniform price somewhere inside it.
BUY_LO, BUY_HI = 1.010, 1.020
SELL_LO, SELL_HI = 0.980, 0.990


class Halt(Exception):
    def __init__(self, invariant, detail, forensics):
        super().__init__(f"{invariant}: {detail}")
        self.invariant, self.detail, self.forensics = invariant, detail, forensics


# ── seq discipline: per-account, started above the current on-chain floor ─────
_seq = {}


def _init_seqs():
    for h in ACCTS:
        v = S.storage(b"sq" + struct.pack("<I", h))
        _seq[h] = (int.from_bytes(v, "little") if v else 0) + 1


def _place(key, handle, side, qty, price_d):
    """Place ONE signed public order with a controlled, strictly-increasing seq
    (so the fuzzer never manufactures the seq-scramble it is trying to detect)."""
    q = int(qty * SC)
    price = int(round(price_d * SC))
    seq = _seq[handle]
    _seq[handle] += 1
    msg = L.canon(b"order", struct.pack("<I", handle), struct.pack("<I", MARKET),
                  bytes([side]), struct.pack("<I", q), bytes([0]), bytes([0]),
                  struct.pack("<I", price), struct.pack("<Q", seq))
    return L.req("/api/order", {"market": MARKET, "base": BASE, "quote": QUOTE,
                                "account": handle, "side": "buy" if side == 0 else "sell",
                                "qty": qty, "price": price_d, "seq": seq,
                                "sig": key.sign(msg).signature.hex()})


# ── chain reads ──────────────────────────────────────────────────────────────
def balances():
    # trader accounts AND the fee treasury — conservation only closes with the treasury.
    accts = ACCTS + [TREASURY]
    return {a: {h: S.bal(a, h) for h in accts} for a in ASSETS}


def supply(bals, asset):
    return sum(bals[asset].values())     # includes TREASURY: DOT/USDC are a closed system


def trader_supply(bals, asset):
    return sum(bals[asset][h] for h in ACCTS)   # excludes TREASURY


def chain_state():
    return {"bal": balances(), "cv": S.mstate(b"cv", MARKET),
            "book": S.book_of(MARKET),
            "pending": len(S.book_of(MARKET))}   # book depth; mempool via api below


def mempool_inflight():
    try:
        st = L.req(f"/api/state?market={MARKET}")
        return len(st.get("mempool", [])), st.get("in_auction", 0)
    except Exception:
        return None, None


# ── order generation: a balanced, all-crossing batch (seeded) ────────────────
def gen_batch(rng, pairs):
    """`pairs` crossing pairs. Each pair: a random buyer buys q at a buy-band
    price, a random *different* seller sells the same q at a sell-band price.
    Balanced by construction (per pair buy qty == sell qty), so the whole batch
    clears in full. Returns (orders, expected_dot_delta, expected_volume)."""
    orders = []
    dot_delta = {h: 0 for h in ACCTS}
    gross_buy = {h: 0 for h in ACCTS}         # qty each account BUYS (for the USDC band)
    gross_sell = {h: 0 for h in ACCTS}        # qty each account SELLS
    norders = {h: 0 for h in ACCTS}           # filled-order COUNT per account (for the base fee)
    volume = 0
    for _ in range(pairs):
        buyer, seller = rng.sample(ACCTS, 2)
        q = rng.randint(1, 20)
        pb = round(rng.uniform(BUY_LO, BUY_HI), 4)
        ps = round(rng.uniform(SELL_LO, SELL_HI), 4)
        orders.append((buyer, 0, q, pb))     # side 0 = buy
        orders.append((seller, 1, q, ps))    # side 1 = sell
        dot_delta[buyer] += q
        dot_delta[seller] -= q
        gross_buy[buyer] += q
        gross_sell[seller] += q
        norders[buyer] += 1
        norders[seller] += 1
        volume += q
    rng.shuffle(orders)                       # interleave arrival order
    return orders, dot_delta, volume, gross_buy, gross_sell, norders


# ── quiescence: all offered orders have settled or the book has stabilised ────
def wait_quiescent(cv_target, settle_timeout, stable_secs=12):
    """Poll until cumulative volume reaches the target AND stops moving for
    stable_secs, or the budget runs out. Returns (ok, last_cv, waited)."""
    t0 = time.time()
    stable_since = None
    last_cv = S.mstate(b"cv", MARKET)
    while time.time() - t0 < settle_timeout:
        time.sleep(3)
        cv = S.mstate(b"cv", MARKET)
        mp, inflight = mempool_inflight()
        reached = cv >= cv_target
        drained = (mp == 0) if mp is not None else True
        if reached and drained:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= stable_secs:
                return True, cv, time.time() - t0
        else:
            stable_since = None
        if cv != last_cv:
            last_cv = cv
    return False, S.mstate(b"cv", MARKET), time.time() - t0


# ── verification: the invariants, checked hardest-first ──────────────────────
def verify(level, pairs, before, after, exp_dot, exp_vol, gbuy, gsell, norders,
           quiescent, waited, tol):
    def fail(inv, detail, extra=None):
        forensics = {"invariant": inv, "detail": detail, "level": level,
                     "pairs": pairs, "waited_s": round(waited, 1),
                     "before": _fmt(before), "after": _fmt(after),
                     "expected_dot_delta": exp_dot, "expected_volume": exp_vol,
                     "quiescent": quiescent}
        if extra:
            forensics.update(extra)
        raise Halt(inv, detail, forensics)

    # 1) LIVENESS — the SLO. A stall (dex round wedge / lasair availability) dies here.
    if not quiescent:
        got = (after["cv"] - before["cv"]) // SC
        fail("LIVENESS", f"batch did not clear within {round(waited)}s: "
             f"volume {got}/{exp_vol} settled, {exp_vol - got} orders still stuck")

    # 2) CONSERVATION — USDC and DOT are neither minted nor burned by trading.
    for a in (USDC, DOT):
        s0, s1 = supply(before["bal"], a), supply(after["bal"], a)
        if s0 != s1:
            fail("CONSERVATION", f"{ASSETS[a]} supply moved {s0} -> {s1} "
                 f"(delta {(s1 - s0) / SC:+g}) — value created/destroyed or re-org")

    # 3) NON-NEGATIVE — no underflow anywhere.
    for a in ASSETS:
        for h in ACCTS:
            if after["bal"][a][h] < 0:
                fail("NON_NEGATIVE", f"account {h} {ASSETS[a]} = {after['bal'][a][h]}")

    # 4) JAMKB — a DOT/USDC market never moves any TRADER's JAMKB (it is neither the
    #    base nor the quote here, and no rent is charged in this config). Checked over
    #    traders only: the service MINTS JAMKB into its treasury rent-reserve each round
    #    (a known devnet mechanism — "unbounded mint" in docs/TOKENS.md), so including the
    #    treasury here is wrong. A trader's JAMKB moving IS a bug.
    j0, j1 = trader_supply(before["bal"], JAMKB), trader_supply(after["bal"], JAMKB)
    if j1 != j0:
        fail("JAMKB_TRADER", f"trader JAMKB moved {j0} -> {j1} on a DOT/USDC market")

    # 5) VOLUME — cumulative on-chain volume rose by exactly the crossing quantity.
    got_vol = (after["cv"] - before["cv"]) // SC
    if abs(got_vol - exp_vol) > tol:
        fail("VOLUME", f"cv rose by {got_vol}, expected {exp_vol} (tol {tol})")

    # 6) POSITION — each account's DOT (the base asset) moved by exactly
    #    (bought - sold) MINUS the base fee it paid on each of its filled orders.
    for h in ACCTS:
        got = (after["bal"][DOT][h] - before["bal"][DOT][h])
        want = exp_dot[h] * SC - FEE_FLAT * norders[h]
        if abs(got - want) > tol * SC:
            fail("POSITION", f"account {h} DOT delta {got} atomic, expected {want} "
                 f"(traded {exp_dot[h]:+d} DOT, {norders[h]} orders x {FEE_FLAT} fee)",
                 {"per_account_dot_atomic": {h: (after['bal'][DOT][h] - before['bal'][DOT][h])
                                             for h in ACCTS}})
    # 6b) FEE ACCRUAL — the treasury received exactly FEE_FLAT per filled order.
    fee_got = after["bal"][DOT][TREASURY] - before["bal"][DOT][TREASURY]
    fee_want = FEE_FLAT * sum(norders.values())
    if fee_got != fee_want:
        fail("FEE_ACCRUAL", f"treasury DOT grew {fee_got} atomic, expected {fee_want} "
             f"({sum(norders.values())} filled orders x {FEE_FLAT})")

    # 7) BOOK EMPTY — a balanced all-cross rests nothing.
    if after["book"]:
        fail("BOOK_RESIDUE", f"{len(after['book'])} orders left resting after a "
             f"balanced all-crossing batch", {"book": after["book"]})

    # 8) VALUE — each account's USDC move is consistent with its GROSS fills at
    #    prices inside the crossing band. Buys cost gbuy*[SELL_LO..BUY_HI], sells
    #    earn gsell*[SELL_LO..BUY_HI]; every round's uniform price is inside the
    #    band, so the net USDC move must fall in the combined interval. (Using
    #    GROSS not net: an account that buys AND sells across rounds at different
    #    uniform prices can leave the tighter net band without any bug.)
    for h in ACCTS:
        du = (after["bal"][USDC][h] - before["bal"][USDC][h]) / SC
        lo = -gbuy[h] * BUY_HI + gsell[h] * SELL_LO - tol
        hi = -gbuy[h] * SELL_LO + gsell[h] * BUY_HI + tol
        if not (lo - 1e-6 <= du <= hi + 1e-6):
            fail("VALUE", f"account {h} USDC delta {du:+g} outside gross band "
                 f"[{lo:.3f}, {hi:.3f}] (bought {gbuy[h]}, sold {gsell[h]})")


def _fmt(state):
    return {"cv": state["cv"] / SC,
            "bal": {ASSETS[a]: {h: state["bal"][a][h] / SC for h in ACCTS} for a in ASSETS},
            "book_depth": len(state["book"])}


# ── forensics + progress ─────────────────────────────────────────────────────
def dump_forensics(seed, level, orders, halt):
    os.makedirs(FUZZ_DIR, exist_ok=True)
    stamp = f"seed{seed}_L{level}"
    path = os.path.join(FUZZ_DIR, f"halt_{stamp}.json")
    with open(path, "w") as fh:
        json.dump({"seed": seed, **halt.forensics,
                   "orders": [{"account": o[0], "side": "buy" if o[1] == 0 else "sell",
                               "qty": o[2], "price": o[3]} for o in orders]},
                  fh, indent=2)
    return path


def load_progress():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"deepest_pairs": 0, "levels_passed": 0}


def save_progress(p):
    os.makedirs(FUZZ_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(p, fh, indent=2)


# ── driver ───────────────────────────────────────────────────────────────────
def run():
    import random
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0, help="0 => derive from progress (advances each run)")
    ap.add_argument("--max-pairs", type=int, default=6000)
    ap.add_argument("--only-level", type=int, default=0, help="run just this level index (1-based) and stop")
    ap.add_argument("--settle-timeout", type=float, default=180.0, help="per-level clearing budget (s)")
    ap.add_argument("--tolerance", type=int, default=0, help="allowed volume/position slack, in whole base units")
    args = ap.parse_args()

    prog = load_progress()
    seed = args.seed or (1000 + prog["levels_passed"])
    print(f"dex_fuzz: seed={seed} deepest_so_far={prog['deepest_pairs']} pairs  "
          f"dex={L.DEX}  reader={S.READER_URL}")
    _init_seqs()

    levels = list(enumerate(LEVELS, 1))
    if args.only_level:
        levels = [(args.only_level, LEVELS[args.only_level - 1])]

    for lvl, pairs in levels:
        if pairs > args.max_pairs:
            print(f"stop: level {lvl} ({pairs} pairs) exceeds --max-pairs {args.max_pairs}")
            break
        rng = random.Random(seed * 100 + lvl)
        orders, exp_dot, exp_vol, gbuy, gsell, norders = gen_batch(rng, pairs)
        before = chain_state()
        cv_target = before["cv"] + exp_vol * SC
        print(f"\n── level {lvl}: {pairs} pairs / {len(orders)} orders, "
              f"expected volume {exp_vol} DOT ──")

        # Classify placement outcomes precisely. A 400 is the DEX *correctly*
        # refusing (open-order cap, insufficient funds) — legitimate backpressure,
        # not a fault. A 500 is a genuine server fault. Anything else is unexpected.
        placed = ref400 = ref500 = refother = 0
        first_err = None
        for (h, side, q, px) in orders:
            try:
                r = _place(L.KEYS[h - 1], h, side, q, px)
                placed += 1 if r.get("ok") else 0
                if not r.get("ok"):
                    refother += 1
                    first_err = first_err or f"ok=false: {r}"
            except Exception as e:
                code = getattr(e, "code", None)
                if code == 400:
                    ref400 += 1
                elif code == 500:
                    ref500 += 1
                    if ref500 <= 3:
                        print(f"  SERVER FAULT (500): {e}")
                else:
                    refother += 1
                first_err = first_err or f"HTTP {code}: {e}"
        refused = ref400 + ref500 + refother
        print(f"  placed {placed}/{len(orders)} "
              f"(refused {refused}: {ref400}×400 cap, {ref500}×500 fault, {refother}×other)"
              "; waiting to clear...")

        # A 500 is a real bug — halt immediately, before the batch even settles.
        if ref500:
            h = Halt("SERVER_FAULT",
                     f"{ref500} order(s) rejected with HTTP 500 (server fault): {first_err}",
                     {})
            path = dump_forensics(seed, lvl, orders, h)
            print(f"\n╳ HALT at level {lvl} ({pairs} pairs) — {h.invariant}")
            print(f"  {h.detail}")
            print(f"  forensics: {path}")
            print(f"  reproduce: dex_fuzz.py --seed {seed} --only-level {lvl}")
            return 1

        # A partially-placed batch is no longer balanced/all-crossing, so the exact
        # invariants below would fire misleadingly. A 400-refused batch means the DEX
        # hit its correct capacity ceiling at this depth: report it as CAPACITY, the
        # true penetration limit — not a correctness divergence.
        if refused:
            h = Halt("CAPACITY",
                     f"DEX refused {refused}/{len(orders)} orders at this depth "
                     f"({ref400} open-order-cap/funds, {refother} other) — the correct "
                     f"backpressure ceiling. Deeper penetration needs lasair availability "
                     f"throughput and/or a higher MAX_OPEN_ORDERS; first: {first_err}",
                     {})
            path = dump_forensics(seed, lvl, orders, h)
            print(f"\n╳ HALT at level {lvl} ({pairs} pairs) — {h.invariant}")
            print(f"  {h.detail}")
            print(f"  forensics: {path}")
            print(f"  deepest CLEAN level so far: {prog['deepest_pairs']} pairs")
            print(f"  reproduce: dex_fuzz.py --seed {seed} --only-level {lvl}")
            return 1

        quiescent, cv, waited = wait_quiescent(cv_target, args.settle_timeout)
        after = chain_state()
        try:
            verify(lvl, pairs, before, after, exp_dot, exp_vol, gbuy, gsell, norders,
                   quiescent, waited, args.tolerance)
        except Halt as h:
            path = dump_forensics(seed, lvl, orders, h)
            print(f"\n╳ HALT at level {lvl} ({pairs} pairs) — {h.invariant}")
            print(f"  {h.detail}")
            print(f"  forensics: {path}")
            print(f"  deepest CLEAN level so far: {prog['deepest_pairs']} pairs")
            print(f"  reproduce: dex_fuzz.py --seed {seed} --only-level {lvl}")
            return 1
        print(f"  ✓ level {lvl} clean in {round(waited)}s "
              f"(volume {exp_vol}, conservation exact, positions exact, book empty)")
        prog["deepest_pairs"] = max(prog["deepest_pairs"], pairs)
        prog["levels_passed"] = max(prog["levels_passed"], lvl)
        save_progress(prog)

    print(f"\n✓ ALL LEVELS PASSED up to {prog['deepest_pairs']} pairs. "
          f"Raise --max-pairs / extend LEVELS to penetrate deeper.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
