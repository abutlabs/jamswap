# Sealed-order robustness review

**Scope:** how the matching engine + sealed-order commit–reveal are designed, every
way a legitimate sealed order can fail to match or vanish, and a redesign that makes
sealed matching execute at 100% reliability. Written 2026-07-14, grounded in a full
read of `service/src/lib.rs`, `crates/match-engine/src/lib.rs`, `offchain/round.py`,
and `offchain/server.py` (line anchors throughout).

Sealed orders are the JAMswap differentiator — a hidden, MEV-resistant limit order
that rests on-chain as a 32-byte commitment and only ever reveals in the batch it
actually trades. That promise is only worth making if it *never* silently loses an
order. Today it can, in six distinct ways. This document names them and fixes them.

> **Status (2026-07-14): all four phases IMPLEMENTED.** §4/§5 describe the design;
> the "Implemented as" notes below each R-item point at the code. Verified by
> `crates/setops` (8 Rust tests), `offchain/tests` (77 Python tests, incl. the
> gate-then-plan + carry-retry + finality-gate cases), and a live sealed-order
> zero-loss soak on the all-lasair finalizing net (`PROFILE=sealed` loadgen →
> `soak_verdict.py`, which fails on any silently-dropped sealed order).

---

## 1. How it works today (as-built)

A sealed order's life crosses three subsystems that each independently decide whether
it proceeds — and they don't share one view of the world:

```
 place (sealed)
   │  owner signs a COMMIT = Blake2s256(order‖nonce)      [17B order ‖ 32B nonce]
   ▼
 TAG_COMMIT  ──accumulate──►  append  commitment‖account (36B)  to  b"commits"‖market
   │            (owner-sig verified in ACCUMULATE, seq-floor replay guard)  lib.rs:807-826
   │
   │   … 10–60 s later on a contested chain the commit ACCUMULATES …
   ▼
 every 6 s AUCTION  (api_round, server.py:864)
   │
   ├─(A) plan_round(pending, resting)                                     round.py:89
   │      reveal   ⟵ sealed order CROSSES current liquidity (builder plaintext view)
   │      carry    ⟵ doesn't cross → stays hidden, retry next round
   │      expired  ⟵ GTT passed → DROPPED
   │
   ├─(B) COMMIT GATE  (server.py:909-921)
   │      reveal only if its commit is ALREADY on b"commits"; else DEFER (re-queue)
   │
   ├─(C) SEQ SANITIZE (public only)                                       server.py:933
   │
   ▼
 submit TAG_REVEAL  [commits ‖ reveals ‖ signed public section]          server.py:972
   │   refine re-hashes each reveal, admits only those matching a builder-supplied commit
   │   accumulate: check_round_auth + consume_set(b"commits") → apply_settlement
   ▼
 _inflight[m]  (awaiting cv predicate: on-chain cum_volume ≥ cv_before+volume)  server.py:997
   │
   ├─ predicate holds SETTLE_HOLD_SECS (=0 on lasair6; finality replaces it) → FINALIZE
   │        _finalize_round: carry sealed remainder + emit receipts        server.py:750
   │
   ├─ predicate flips back (re-org ate it) → jamswap_settle_reverted_total, keep waiting
   └─ ROUND_GATE_SECS elapse, never settled → re-queue orders, NO receipts   server.py:804
```

**Matching itself is sound.** `clear()` (`crates/match-engine/src/lib.rs:82-131`) is a
deterministic, integer-only, uniform-price batch auction: maximize matched volume,
tie-break minimal imbalance then lowest price; fill by price-time priority. The engine
sees no sealed/public distinction — the builder hands it `resting + sealed + public`
and it clears one set at one price. The chain re-clears and re-verifies in refine
(never trusts the builder's fills), and `check_round_auth` (`lib.rs:553-586`) rejects a
fabricated book, a forged binding, a replayed seq, or an out-of-band market price. The
clearing math and the trust model are not the problem.

**The problem is the admission pipeline around the clear** — stages (A), (B), (C) and
the settlement resolver each defer or drop, on inconsistent views. That is where a
legitimate sealed order dies.

---

## 2. Failure modes — where a legitimate sealed order is lost

### F1 — Planner and commit-gate disagree → revealed alone → dropped  ★ (the "Fergie disappeared" bug)

Stage (A) `plan_round` decides *reveal* using the builder's **plaintext** view of **all**
liquidity, including other sealed orders whose commits are **not yet on-chain**
(`round.py:105` — `everything = resting + public + sealed`). Stage (B) the commit-gate
then **removes** any sealed order whose commit hasn't accumulated (`server.py:914-921`).

So: Fergie BUY 50 @ 1.00 (sealed, commit ready) crosses Alice SELL 25 @ 0.95 (sealed,
commit still accumulating). The planner reveals Fergie *because Alice's price made it
cross*. The gate then drops Alice. The round clears with Fergie alone → fills 0 →
`_record_exec` marks it `cancelled` (`server.py:1231`, sealed + not `_carried` + filled 0).
**Fergie's order both leaked its terms (it revealed) and vanished (IOC), and never traded
against the counterparty that existed.** This is exactly what Aodh observed.

Root cause: two stages gate on two different views (plaintext-all vs commit-on-chain),
and the reveal decision is made *before* the gate that invalidates it.

### F2 — Revealed-but-unfilled is IOC → silent drop + terms leaked

Independent of F1: any revealed sealed order that doesn't fully fill — the marginal
order past the volume boundary (`lib.rs:128`), or a counterparty pulled by the gate — is
**immediate-or-cancel**. On-chain, an unfilled sealed remainder is excluded from the
emitted book so it never rests (`reveal_output` `lib.rs:343-357`); off-chain it's dropped
as `cancelled`. The order revealed (terms now public) *and* died. A hidden order that
leaks and vanishes is worse than a public order that rests.

### F3 — Commit/reveal latency race → unbounded deferral, then expiry

The auction fires 6 s after placement; TAG_COMMIT takes 10–60 s to accumulate on a
contested chain. The gate (F1-B) correctly defers the reveal until the commit lands —
but deferral repeats every round, and the order's GTT lifetime keeps burning while it
waits. A sealed order can be deferred round after round and then **expire before its
commit ever surfaces** (`plan_round` → `expired`, `round.py:114`), dropped without ever
having had a chance to trade.

### F4 — Partial remainder IOC-dropped under backpressure

`_finalize_round` (`server.py:764-769`): when a genuine partial fill tries to re-seal its
remainder and the carry-commit queues are full (`ChainBusy`), the remainder is **silently
cancelled** rather than retried. Deliberate anti-wedge choice, but a legitimate,
already-partially-filled order is lost precisely when the system is busiest.

### F5 — Whole-round fail-closed forfeits everyone's fills

`consume_set` (`lib.rs:486-513`) and `check_round_auth` reject the **entire** round on any
single bad entry. `_seq_sanitize` (`server.py:933`) defends the public seq case, but sealed
reveals carry no seq — they ride commit-set membership, and a single stale/duplicate
consumed entry drops the whole batch. One bad order forfeits every other trader's fill
in that round.

### F6 — Round never settles → re-queue with no receipt; user sees a vanish

`_resolve_rounds_once` (`server.py:804`): after `ROUND_GATE_SECS` an unsettled round is
abandoned and its orders re-queued **with no receipts**. On a degraded/OOM'd net this
cascades. Orders aren't strictly lost (they re-queue), but they disappear from the
mempool during the in-flight window and reappear later — and if they expired meanwhile,
they're dropped. To the user this is indistinguishable from a loss.

### Cross-cutting: F7 — silent outcomes

Across F1–F6 the terminal state a user sees is at best `cancelled` with **no reason**, and
at worst a mempool vanish with no receipt at all. Aodh's literal question — *"Fergie's
order disappeared but no balance update — why?"* — is the design failing to explain
itself. A reliability guarantee the user can't observe isn't a guarantee.

### F8 — commits never GC'd (state bloat, not a match-loss)

A committed order never revealed (lost nonce, offline trader) sits in `b"commits"`
**forever** — no TTL, no rent, no height expiry (service map §6). Unbounded state growth
and no definite "your sealed order expired" outcome for the trader.

---

## 3. Root cause, in one sentence

**Three admission stages (plaintext-planner, on-chain commit-gate, IOC-clear) gate a
sealed order on three inconsistent views of liquidity, and a revealed order that the
later stages invalidate is dropped instead of carried — silently.**

Everything below follows from making the views consistent and making "revealed" imply
"will trade," then never dropping without a reasoned receipt.

---

## 4. Redesign for 100% reliability

The finality gadget we just shipped (P5, β-finalization on lasair6) is the missing tool:
a finalized commit **cannot** re-org out, so reveal can be made deterministic.

### R1 — Gate first, then plan (fixes F1). *Offchain, no consensus change.*
Reorder the pipeline: run the commit-gate **before** `plan_round`, so the planner only
ever sees sealed orders whose commits are actually on-chain. Then a reveal decision is
made against exactly the liquidity that will be in the batch — a sealed order reveals
only if it crosses a counterparty that is *also* revealing. "Revealed alone → cleared 0"
becomes structurally impossible.
> **Implemented as:** `plan_round(pending, resting, now, sealed_ready=…)` in
> `offchain/round.py` — a `sealed_ready` predicate partitions sealed orders into
> `ready`/`deferred` *before* `_best_opposing`, so only commit-ready sealed liquidity is
> in the crossing view; `api_round` (`offchain/server.py`) builds the predicate from the
> on-chain commit set and passes it in. Tests: `CommitReadinessGate` in
> `tests/test_round_lifecycle.py`.

### R2 — Reveal on commit *finalization*, not accumulation (hardens F1/F3). *Offchain; reads `/api/finality` we already built.*
Gate reveals on β-finalized commits, not merely accumulated ones. A finalized commit
can never disappear, so the gate never flips back and a revealed round can't be
rolled back under it. Cost: sealed first-match latency gains the finality lag
(~2–3 blocks / 12–18 s). That is the honest price of the guarantee, and it's bounded.
> **Implemented as:** `_sealed_ready_predicate(m, commit_entries, fin)` in
> `offchain/server.py`. It pins the head height at which each commit is first seen and
> treats it as final once `finalized_height` reaches that height — a strict safety
> improvement over R1 (it only ever *delays* a reveal, never reveals a not-on-chain
> commit), falling back to best-chain membership on a non-finalizing net. Tests:
> `SealedReadyFinality` in `tests/test_sealed_carry.py`.

### R3 — Never drop a revealed order; carry the remainder (fixes F2). *Offchain.*
Extend the existing `_post_carry_seal` remainder mechanism from partial-fills to the
zero-fill case: a revealed order that doesn't fully fill re-seals its remainder (fresh
commitment, fresh nonce) and retries, instead of IOC-cancel.
> **Implemented as:** `_carry_sealed_remainders(m, sealed, fills, now)` in
> `offchain/server.py`, used by both `_finalize_round` and the zero-fill path in
> `api_round`; carries any `rem > 0` that isn't past its GTT (partial *or* full unfill).
> `carried`/`partial-carried` are non-terminal in `order_telemetry`, so the order keeps
> working under one oid until it fills or expires.

### R4 — Retry carry under backpressure instead of dropping (fixes F4). *Offchain.*
When carry-commit queues are full, keep the remainder in a retry queue and re-post it on
the next sweep, rather than IOC-cancel. The order is already off the book and safe;
losing it under load is the one thing we must not do.
> **Implemented as:** `_carry_retry` (market → remainders) drained by
> `_drain_carry_retry(now)` inside the resolver sweep; on `ChainBusy` the remainder is
> queued (receipt `partial-carried`, reason *"re-seal queued (chain busy)"*) and retried
> until it re-seals or its GTT elapses (then a surfaced `cancelled`, reason
> *"expired-before-reseal"*). Tests: `test_carry_retry_*` in `tests/test_sealed_carry.py`.

### R5 — Every terminal outcome is a receipt with a reason (fixes F6/F7). *Offchain + UI.*
No order ever leaves the pipeline without a receipt: `filled`, `carried`/`partial-carried`
(re-sealed, still working), `cancelled(reason)`, `rejected(reason)`, `deferred(reason)`.
> **Implemented as:** `_record_exec` reads an upstream `_outcome`/`_reason` and writes a
> `reason` into every `/api/executions` receipt; `order_telemetry.deferred(...)` records
> the non-terminal waiting state; the Explorer tab (`web/index.html`) renders four
> distinct states — filled / carrying / resting / cancelled·reason — so the trader always
> sees what became of their order (answers the "why did it disappear?" report directly).

### R6 — Commit TTL / GC (fixes F8). *Small on-chain change.*
Give each commit a slot expiry (mirror the GTT the public book already has). A commit not
revealed within its window is reclaimed, and state can't grow without bound.
> **Implemented as:** a parallel age index `b"cage"‖market` (`cid‖account‖expiry_slot`,
> `COMMIT_TTL_SLOTS = 3600` ≈ 6 h) in `service/src/lib.rs`, kept in step with `b"commits"`:
> `cage_add` on every commit, `cage_purge` on reveal-consume, and `gc_commits(market,
> slot)` reaps expired entries from BOTH sets at each new commit — so a market bounds its
> own commit-set growth. The pure reap/remove byte-logic is the `crates/setops` crate
> (8 host tests); the service wires it to storage. `accumulate` now uses its `slot`.

### R7 — Zero-loss soak proof. *Harness.*
Inject sealed **crossing pairs** and assert the invariant: every accepted sealed order
reaches exactly one terminal state (`filled` / `carried→…→filled` / `expired-with-reason`)
— **zero silent drops** — on lasair6 with finality on.
> **Implemented as:** the `sealed` loadgen profile (`offchain/loadgen.py`) — both-sides-
> sealed crossing, mixing same-tick pairs with staggered lone orders — plus
> `offchain/soak_verdict.py`, which reconstructs each order's lifecycle from the telemetry
> log and **FAILS if any sealed order is stuck open past grace** (a silent drop). Verified
> live on the all-lasair finalizing net.

---

## 5. Sequencing

| Phase | Changes | Surface | Fixes | Consensus change? | Status |
|---|---|---|---|---|---|
| **1 — Consistency** | R1 gate-then-plan · R3 carry-on-unfill · R4 retry-not-drop · R5 reasoned receipts | `server.py`, `round.py`, UI | F1, F2, F4, F6, F7 | **No** | ✅ done |
| **2 — Determinism** | R2 reveal on finalized commits | `server.py` (+ `/api/finality`) | F1, F3 | No | ✅ done |
| **3 — Hygiene** | R6 commit TTL/GC | `service/src/lib.rs`, `crates/setops` | F8 | Yes (small) | ✅ done |
| **4 — Proof** | R7 sealed zero-loss soak | `loadgen.py`, `soak_verdict.py` | validates all | No | ✅ done |

Phase 1 is self-contained, needs no chain change, and eliminates the observed
"disappeared, no fill" bug plus every silent-drop path — the right first commit. Phase 2
upgrades the guarantee from "won't drop" to "deterministic." Phase 3 is state hygiene
(needs a rebuilt `jamswap-service.jam`, so it lands on a fresh genesis). Phase 4 is the
evidence. Phases 1+2 are wire-compatible with a running old-`.jam` net (they are
builder-side), so they can be rolled out by recreating the `dex` alone; Phase 3 rides the
next genesis.

---

## 6. What stays as-is (deliberately)

- `clear()` and the uniform-price/price-time algorithm — sound and determinism-tested.
- The trust model (chain re-clears, `check_round_auth`, commit-hash‖account binding) —
  correct; the builder is never trusted for fills.
- IOC *exposure* semantics (an unfilled sealed remainder never rests publicly) — kept;
  R3 re-seals the remainder rather than resting it exposed.
- Finality-based durability (SETTLE_HOLD_SECS=0 on lasair6) — this is what makes R2
  possible; don't regress it.
```
