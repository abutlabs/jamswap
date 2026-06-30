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
- ◻️ Then: full settlement ledger + custody (deposit/withdraw), encrypted
  orders (MEV-resistance), multi-market parallelism, off-chain infra, trading UI.

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
