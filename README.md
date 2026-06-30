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
- ◻️ Then: cancel/modify + round sequencing via historical-lookup, real custody via
  `on_transfer` (deposit/withdraw + reconciliation), threshold-encryption upgrade,
  multi-market parallelism, off-chain infra, trading UI.

## Run the demo (one command)

Watch a sealed-order batch auction clear in JAM's Refine — built from source:

```sh
docker compose up --build
```

Brings up a lasair-node and runs [`sim/demo.py`](sim/demo.py): deploy → fund
traders → **sealed** commit/reveal round → uniform-price clearing → settlement →
resting order book → MEV-resistance (an uncommitted order is rejected). Full
architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

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
