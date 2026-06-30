# Marmalade

> A frequent-batch-auction order-book DEX on JAM — a CEX-grade matching engine
> with DEX-grade self-custody, because JAM is the first chain that can actually
> *run* the matching engine.

Every on-chain exchange uses an AMM (a pricing formula) instead of a real order
book **because no blockchain could afford to run a matching engine**. JAM's
**Refine** phase is heavy, parallel, deterministic, *audited* compute — so
Marmalade runs an actual matching engine trustlessly and settles fills on-chain.
We clear as **frequent batch auctions** (one uniform price per round), which
removes the latency race that drives most CEX/AMM MEV.

Full thesis, business plan, architecture, and phased roadmap: [`docs/PLAN.md`](docs/PLAN.md).

## Status

**Kickoff (2026-06-30).** Phase 0/1 underway — the core matching engine.

- ✅ **`crates/match-engine`** — the FBA uniform-price clearing algorithm:
  `no_std`, integer-only, fully deterministic (so every JAM validator re-executes
  it byte-identically). Property-tested for value conservation, determinism, and
  per-order fill bounds.
- ✅ **`service/`** — the `no_std` JAM service: `refine` = the matching engine,
  `accumulate` = settlement. **M1 PROVEN** — it clears a real batch *in Refine on
  lasair*, deterministically (byte-identical re-runs), at **7,476 gas** for 3
  orders (~0.00015% of the refine budget). See [`docs/M1_DEMO.md`](docs/M1_DEMO.md).
  **Marmalade is a self-contained JAM service — nothing baked into Lasair.**
- ✅ **Phase 2 settlement** — `accumulate` now moves real **balances**: a cleared
  batch debits/credits each trader's base/quote at the uniform price; deposits fund
  accounts. Verified e2e on lasair (deposit → auction → settled balances, value
  conserved). Orders carry an `account`; the service is tagged (match vs deposit).
- ✅ **Resting limit orders** — partially/un-filled orders persist in an on-chain
  book and fill in a later round when a crossing order arrives (verified e2e: a
  lone buy rests, then a later sell crosses it and settles). A true continuous
  CLOB, not isolated auctions.
- ✅ **Sealed orders (MEV-resistance, Phase 4 MVP)** — commit–reveal: in the commit
  round only `H(order ‖ nonce)` is on-chain (orders hidden); in the reveal round
  `refine` admits only orders whose hash matches a recorded commitment. Combined
  with the batch auction (no latency race), nobody can front-run within a round.
  Verified e2e: hidden commit → reveal+match settles; an uncommitted order is
  rejected. *Honest asterisk:* adds a reveal round + a non-reveal griefing vector;
  threshold/time-lock encryption (no reveal round) is the stronger upgrade.
- ✅ **Cancel** — owner-authenticated removal of a resting order by `(account, id)`
  (verified e2e in the demo). Order lifecycle: submit → rest → fill or cancel.
- ✅ **Multi-market** — each work-item names a market + its `base`/`quote` assets;
  markets clear **independently** (one work-package per market per round = JAM's
  per-core parallelism) into per-market books, sharing one global balance ledger.
  Demo runs two pairs (TOKA/USD @100, TOKB/USD @50) with a trader's USD shared.
- ✅ **Off-chain builder + trading UI** ([`offchain/`](offchain/)) — a stdlib API
  that runs the round lifecycle (collect orders → read the book → assemble + submit
  the batch) and a single-page exchange UI (order book, place order, run round,
  balances, faucet) at `:8080`.
- ✅ **Asset lifecycle** — deposit → trade → **withdraw** with conserved accounting
  and overdraft protection; a per-asset custody total whose invariant
  (Σ balances == custody) holds by construction. (Mock custody; real `on_transfer`
  backing is the Phase-3 upgrade, blocked on JAM's asset standard.)
- ✅ **Trading fee** (revenue model) — a flat 30 bps fee on matched notional accrues
  to a treasury account (per-market quote asset); conservation holds *including* the
  fee (property-tested over random fee rates).
- ◻️ Then: real `on_transfer` custody, round sequencing via historical-lookup,
  threshold-encryption upgrade, indexer + WebSocket feeds, wallet/signing in the UI,
  a W3F grant application.

CI (`.github/workflows/ci.yml`) runs the matching-engine property tests
(conservation, determinism, settlement Σ-deltas == 0) on every push — the
"never regress" gate from PLAN.md §5.

## Run it (one command)

Built from source for your architecture:

```sh
docker compose up --build          # -> trading UI at http://localhost:8080
```

Brings up a lasair-node, deploys the Marmalade service, and serves the **trading
UI** + off-chain builder ([`offchain/`](offchain/)): place limit orders, run an
auction round, and watch the uniform-price clearing, the resting order book, and
your balances update — across multiple markets sharing one ledger.

The narrated CLI scenario (sealed commit/reveal round → clearing → settlement →
resting book → cancel → MEV-resistance) is also available:

```sh
docker compose --profile demo run --rm demo   # see sim/demo.py
```

Full architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## The matching engine

```sh
cd crates/match-engine && cargo test --release
```

Uniform-price sealed-bid double auction: aggregate demand/supply curves, clearing
price `p*` maximizing matched volume (tie-break: minimal imbalance, then lowest
price), and price-time-priority rationing of the marginal order. Everyone trades
at `p*`. See [`crates/match-engine/src/lib.rs`](crates/match-engine/src/lib.rs).

## Why it's a JAM flagship

- It's the **cleanest demonstration of what JAM uniquely enables**: AMMs exist
  only because of the compute limit JAM removes.
- The **batch auction is MEV-resistant by construction** (no intra-round latency
  race); orders will be encrypted until the batch seals (Phase 4).
- We build the client it runs on (**lasair**) → a structural cost moat
  (cheapest to clear auctions → lowest fees with margin).

Honest caveats (kept in view): JAM mainnet timing isn't ours to control;
"trustless" carries an asterisk until the order-encryption story is airtight; and
liquidity cold-start is a real grind. See `docs/PLAN.md` §9.
