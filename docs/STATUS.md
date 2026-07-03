# Jamswap — build status (what's done, what's next)

A builder's-eye checklist of the MVP. For *what the project is* start at the
[README](../README.md); for the vision and roadmap see [`PLAN.md`](PLAN.md); for
proven results see [`M1_DEMO.md`](M1_DEMO.md).

Kickoff was 2026-06-30; the core matching engine landed first, then settlement,
resting orders, sealing, and the trading layer on top.

## Done

- ✅ **Owner-signed sealed commits (2026-07-03)** — `TAG_COMMIT`/`TAG_ENC_COMMIT` carry the
  owner's signature (verified in accumulate, zero refine gas); commit/enc set entries are
  `hash‖account` and consumption matches both, so a sealed order settles only for its
  committer. Carry-forward re-seals are **allowance-gated** (one credit per genuine partial
  fill). Verified e2e: forged commits, unsigned commits, and no-allowance carries all
  rejected; partial-fill carry clears across auctions.
- ✅ **Trustless public orders (2026-07-03)** — every public order carries `pubkey+sig`
  and is **ed25519-verified per-order in `refine`** (`TAG_SMATCH`; unsigned `TAG_MATCH`
  deleted). Accumulate binds the key to the registry, enforces per-account replay floors,
  band-checks market orders, and **hash-binds the resting book** (no fabricated resting
  orders). Verified e2e on a live node: forged order REJECTED, replayed seq REJECTED,
  honest signed rounds clear/rest/settle. See `docs/SECURITY.md`.

- ✅ **`crates/match-engine`** — the FBA uniform-price clearing algorithm:
  `no_std`, integer-only, fully deterministic (so guarantors and auditors
  re-execute it byte-identically). Property-tested for value conservation,
  determinism, and per-order fill bounds.
- ✅ **`service/`** — the `no_std` JAM service: `refine` = the matching engine,
  `accumulate` = settlement. **M1 PROVEN** — it clears a real batch *in Refine on
  lasair*, deterministically (byte-identical re-runs), at **7,476 gas** for 3
  orders (~0.00015% of the refine budget). See [`M1_DEMO.md`](M1_DEMO.md).
  **Jamswap is a self-contained JAM service — nothing baked into Lasair.**
- ✅ **Phase 2 settlement** — `accumulate` now moves real **balances**: a cleared
  batch debits/credits each trader's base/quote at the uniform price; deposits fund
  accounts. Verified e2e on lasair (deposit → auction → settled balances, value
  conserved). Orders carry an `account`; the service is tagged (match vs deposit).
- ✅ **Resting limit orders** — partially/un-filled orders persist in an on-chain
  book and fill in a later round when a crossing order arrives (verified e2e: a
  lone buy rests, then a later sell crosses it and settles). A true continuous
  CLOB, not isolated auctions.
- ✅ **Sealed orders — commit–reveal (rung 3)** — in the commit round only
  `H(order ‖ nonce)` is on-chain (orders hidden); in the reveal round `refine`
  admits only orders whose hash matches a recorded commitment. Combined with the
  batch auction (no latency race), nobody can front-run within a round. Verified
  e2e: hidden commit → reveal+match settles; an uncommitted order is rejected.
  *Honest asterisk:* adds a reveal round + a non-reveal griefing vector.
- ✅ **Sealed orders — encrypt-until-batch (rung 2, the default)** — orders
  ECIES-encrypted to an off-protocol committee whose keys are committed on-chain
  (`ENC_SETUP`, gov-signed). At batch close the committee supplies Chaum-Pedersen-
  proven partial decryptions; `refine` verifies each against the committed keys and
  recovers the order with no secret — **no reveal round, no griefing**. Two
  `accumulate` checks defeat a malicious builder (committee-hash match +
  consume-or-reject ciphertext ids). Crypto in [`crates/vdec`](../crates/vdec/),
  sidecar in [`crates/committee`](../crates/committee/), proven e2e by
  [`offchain/test_enc_round.py`](../offchain/test_enc_round.py). ~n·5.6M gas/order.
  See [`SEALED_ORDERS.md`](SEALED_ORDERS.md).
- 🔬 **Sealed orders — ZK dark-pool (rung 1)** — proven in a spike
  (`zk-jam-service/spikes/fba-zk/`): a Groth16 proof of a correct, optimal,
  conservation-respecting FBA clearing verifies in `refine` at ~60M gas **flat in
  batch size**; orders never appear on-chain. Not yet integrated into Jamswap (the
  remaining work is a `MATCH_ZK` tag binding the proof to the on-chain sealed set).
- ✅ **Cancel** — owner-authenticated removal of a resting order by `(account, id)`
  (verified e2e in the demo). Order lifecycle: submit → rest → fill or cancel.
- ✅ **Multi-market** — each work-item names a market + its `base`/`quote` assets;
  markets clear **independently** (one work-package per market per round = JAM's
  per-core parallelism) into per-market books, sharing one global balance ledger.
  Demo runs two pairs (TOKA/USD @100, TOKB/USD @50) with a trader's USD shared.
- ✅ **Off-chain builder + trading UI** ([`../offchain/`](../offchain/)) — a stdlib
  API that runs the round lifecycle (collect orders → read the book → assemble +
  submit the batch) and a single-page exchange UI (order book, place order, run
  round, balances, faucet) at `:8080`.
- ✅ **Asset lifecycle** — deposit → trade → **withdraw** with conserved accounting
  and overdraft protection; a per-asset custody total whose invariant
  (Σ balances == custody) holds by construction. (Mock custody; real `on_transfer`
  backing is the Phase-3 upgrade, blocked on JAM's asset standard.)
- ✅ **Trading fee** (revenue model) — a flat 30 bps fee on matched notional accrues
  to a treasury account (per-market quote asset); conservation holds *including* the
  fee (property-tested over random fee rates).
- ✅ **Fixed-point decimal prices** — prices, quantities, and balances are integer
  *atomic* units (display × 10⁴), so orders carry 4 decimals (e.g. `1.1050`) while the
  engine stays integer-only. Settlement de-scales the quote notional exactly (buyers
  round up / sellers down, any dust to the treasury); conservation is preserved and
  property-tested over random price scales.
- ✅ **Economic simulation** ([`../sim/engine-sim`](../sim/engine-sim)) — drives the
  real engine with random order flow over thousands of rounds (`cargo run --release`),
  reports market quality (fill rate, price stability, fee revenue) and **asserts
  value conservation every round** — the economic stress test behind the
  production-ready claim.
- ✅ **Market registry** — markets are *listed* with canonical assets before
  trading; an unlisted or asset-mismatched market is rejected (verified e2e). A
  discoverable market index backs market listing.
- ✅ **Signed operations** — an account is a collision-free `u32` handle **bound to an
  ed25519 key** by a signed `TAG_REGISTER`; **withdraw / cancel / treasury-sweep are
  verified in the service** (ed25519, replay-nonce'd), order placement is signed +
  builder-verified. Forged/replayed/tampered ops are rejected (verified e2e on the
  6-validator testnet; auth unit-tested in CI). A flat **fee treasury** with a
  governance-key sweep, a **market-order slippage band** (±10%), an order-submission
  **collateral guard**, and **good-till-time order expiry** round out the trading layer.
- ✅ **JAMKB footprint meter** — a read-only tracker of the validator-RAM footprint the
  service occupies, priced in JAMKB (1 JAMKB = 1 KB). Nothing is enforced; see
  [`JAMKB.md`](JAMKB.md).
- ✅ **Sealed orders rest hidden (carry-forward)** — a sealed order with no counterparty
  is no longer immediate-or-cancel: it rests hidden on-chain and is retried each auction,
  revealed only in the round it crosses. Fixes the "sealed sells then later sealed buys
  never match" bug. Pure planner `offchain/round.py`, regression-tested in
  `offchain/tests/test_round_lifecycle.py`. See [`SEALED_ORDERS.md`](SEALED_ORDERS.md).
- ✅ **Recent-trades tape** — the trading UI shows a per-pair trade history (last ~100
  clearing prints) with volume metrics (last / volume / high / low), fed by
  `GET /api/trades`. The server records a print whenever a market's on-chain cumulative
  volume grows (robust to slot-delayed testnet settlement). The developer-only "next
  auction" countdown + manual "clear now" were removed from the end-user view (auctions
  run automatically every 6 s). Tested in `offchain/tests/test_trade_tape.py`.
- ✅ **Test layers** — matching-engine property + scenario tests (Rust), the sealed-order
  round-lifecycle / treasury / trade-tape tests (Python, in CI), and an e2e suite on a
  live node. Map: [`TESTING.md`](TESTING.md).

## Next

- ◻️ Trustless per-order signature check in `refine` (not just at the builder).
- ◻️ Fund escrow at submission (reserve funds across pending orders, not just a guard).
- ◻️ Real `on_transfer` custody against the JAM token standard.
- ◻️ Round sequencing via historical-lookup (`refine` reads the prior finalized book).
- ◻️ ZK dark-pool (rung 1) integration into Jamswap proper (`MATCH_ZK` tag).
- ◻️ Indexer + WebSocket feeds; a W3F grant application.

CI (`.github/workflows/ci.yml`) runs the matching-engine property tests
(conservation, determinism, settlement Σ-deltas == 0) on every push — the
"never regress" gate from PLAN.md §5.
