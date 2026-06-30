# Marmalade — security review (self-assessment)

An honest internal review of the MVP (PLAN.md Phase 9.1). What's **fixed**, what's
**accepted/documented** (often requiring chain-level mechanisms or a production
hardening pass), and what's **sound**. "Trustless" is used precisely: the matching
and settlement are fully deterministic and validator-audited; everything else
carries the asterisk noted here.

## Fixed

- **Integer overflow in settlement notional.** `qty × price` can exceed `i64` for
  large orders and would wrap silently in release builds — a real value-corruption
  bug. **Fixed:** `settle_deltas` now computes in `i128` end-to-end (holds any
  `u32·u32` with room), and the service applies the deltas to `u64` balances with a
  clamp to `[0, u64::MAX]`. Conservation is property-tested over the `i128` path.

## Accepted / documented (production hardening needed)

- **No operation authentication.** The operator RPC accepts any work-item; nothing
  binds an order / cancel / withdraw to its `account` by signature — so a submitter
  could cancel or withdraw against an account that isn't theirs. The MVP runs in a
  single-operator/demo trust model. **Production:** each operation must be signed by
  the account's key and verified (in the service or an authenticated relay). This is
  the single most important gap before any multi-user deployment.
- **Work-item replay.** A `DEPOSIT`/`MATCH` work-item re-submitted is applied again
  (e.g. a deposit credits twice). On a real JAM chain, work-packages are unique by
  hash and included once; the service itself has no per-operation nonce. **Production:**
  a nonce per account, or reliance on chain-level inclusion uniqueness.
- **Collateralization / underflow.** Matching does not check that a trader can fund
  a fill; settlement clamps balances at 0, so an over-matched order would lose value
  (breaking storage conservation, though the *delta math* conserves). **Production:**
  reserve funds at order submission so every matched order is covered. The demos fund
  amply; the economic sim funds all traders.
- **Commit–reveal griefing + builder trust.** A committer who never reveals wastes
  their slot (non-reveal griefing) — threshold/time-lock encryption removes the
  reveal round and this vector. The off-chain **builder** assembles work-packages
  (chooses which orders/book to include); that role is inherently trusted in any
  exchange, but the **matching over the included inputs is fully validator-audited**,
  and orders are **sealed until the batch closes** (no intra-round front-running).

## Sound (and why)

- **Determinism** — integer-only matching + deterministic tie-breaks ⇒ byte-identical
  re-execution on every auditor (the existential JAM-audit property; tested).
- **Clearing optimality** — `p*` provably maximizes matched volume (property-tested).
- **Settlement conservation** — Σ base == 0 and Σ quote == 0 *including the fee to
  the treasury*; a batch moves value, never mints or burns it (property-tested over
  random fees, and asserted every round in the economic sim across thousands of rounds).
- **Market integrity** — a market must be listed with canonical assets; unlisted or
  asset-mismatched rounds are rejected (tested).
- **MEV-resistance** — frequent batch auction (one uniform price, no latency race) +
  sealed commit-reveal orders. The residual asterisk is only the commit-reveal
  griefing/reveal-round noted above.

## Before mainnet (out of MVP scope)

Signed operations + a real wallet; collateralized order submission; an external
security audit of the service + crypto; threshold/time-lock encryption; real asset
custody via `on_transfer` against the JAM token standard; economic/manipulation
simulation at production parameters; circuit breakers + an insurance fund.
