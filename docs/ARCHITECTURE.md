# Marmalade architecture (as built)

How the working MVP is put together — the JAM service state machine, the wire
formats, the round lifecycle, and the honest trust boundaries. For the vision and
roadmap see [`PLAN.md`](PLAN.md); for the proven results see [`M1_DEMO.md`](M1_DEMO.md).

## The Refine/Accumulate split = exchange architecture

```
service Marmalade {
  refine()     = the MATCHING ENGINE  — heavy, parallel, audited, off the state path
  accumulate() = SETTLEMENT           — applies results to authoritative on-chain state
  state        = the books            — balances ledger, resting order book, commitments
}
```

- **`refine`** runs the frequent-batch-auction uniform-price clearing
  ([`crates/match-engine`](../crates/match-engine)) — integer-only and
  deterministic, so every JAM validator re-executes it byte-identically (the
  trust model). It produces a work-output; it does **not** touch live state.
- **`accumulate`** reads each work-output (via `accumulate_items()`) and commits
  it to service storage — balances, the resting book, commitments, stats.
- It's a **self-contained JAM service**: nothing is baked into Lasair. The `.jam`
  runs on any conformant node; Lasair is the cost moat (cheapest to clear), not a
  dependency.

## Work-item types (payload tag = first byte)

| Tag | Name | Payload | refine | accumulate |
|---|---|---|---|---|
| 0 | `MATCH` | plaintext orders (17 B each) | clear → settlement + resting book | settle balances, store book, bump stats |
| 1 | `DEPOSIT` | account ‖ asset ‖ amount | echo | credit balance (Phase-2 faucet; real custody = Phase 3) |
| 2 | `COMMIT` | account ‖ commitment(32) | echo | append commitment to the pending set |
| 3 | `REVEAL` | commits ‖ reveals(order‖nonce) | admit only orders whose `H(order‖nonce)` ∈ commits, then clear | (output is a `MATCH` settlement → same path) |

`refine` for `MATCH`/`REVEAL` emits: `[0]‖[settle_len:u32]‖[settlement]‖[resting book]`.

## Wire formats (little-endian, integer-only)

- **Order** (17 B): `account:u32 ‖ id:u32 ‖ side:u8(0=buy,1=sell) ‖ price:u32 ‖ qty:u32`
- **Settlement**: `price:u32 ‖ n:u32 ‖ n×(account:u32 ‖ side:u8 ‖ qty:u32)`
- **Reveal** (49 B): `order(17) ‖ nonce(32)`; **commitment** = `Blake2s256(reveal)`

## Storage layout (service state)

| Key | Value | Meaning |
|---|---|---|
| `B` ‖ account(4) | u64 | base-asset balance |
| `Q` ‖ account(4) | u64 | quote-asset balance |
| `book` | orders blob | the resting order book (carried between rounds) |
| `commits` | 32 B × n | pending order commitments (cleared on settlement) |
| `last_price`, `rounds`, `cum_volume` | u64 | round stats |

## Round lifecycle

1. **(optional) Commit** — traders submit `COMMIT H(order‖nonce)`; only hashes go
   on-chain. Orders are hidden.
2. **Match / Reveal** — the builder assembles the batch: the resting `book` + new
   orders (plaintext `MATCH`), or the `commits` set + revealed orders (`REVEAL`).
   `refine` clears the uniform-price auction; partially/un-filled orders become
   the new resting book.
3. **Settle** — `accumulate` applies conservation-checked per-account deltas
   (`settle_deltas`: buy = +base/−quote, sell = −base/+quote, **Σ = 0 per asset**),
   persists the new `book`, clears `commits`, and bumps stats.

The "builder" (the party that reads on-chain `book`/`commits` and assembles the
next payload) is, in the MVP, the test/off-chain caller. The plan's alternative —
`refine` reading the prior finalized book via historical-lookup — is a later
optimization.

## MEV-resistance

Two layers, both proven e2e:
- **Frequent batch auction** — one uniform clearing price per round removes the
  latency race that drives most CEX/AMM MEV. Everyone trades at `p*`.
- **Sealed orders (commit–reveal)** — orders are hidden (only a hash on-chain)
  until the batch seals; `refine` admits only committed orders. You cannot see an
  order in time to front-run it, nor inject one you didn't commit.

**Honest trust boundaries** (the "trustless" asterisk, kept loud):
- Matching/settlement is fully deterministic + validator-audited — no asterisk.
- Commit–reveal adds a reveal round and a **non-reveal griefing** vector; the
  reveal is public (order visible *after* reveal, but too late for that batch).
  **Threshold / time-lock encryption** (no reveal round, no griefing) is the
  stronger upgrade — scoped, not yet built.
- Deposits are a Phase-2 **faucet stub**; real self-custody (`on_transfer` +
  Σbalances == custodied reconciliation) is Phase 3.

## Safety invariants (tested)

- **Matching** (`match-engine` proptests): value conservation
  (Σ buy fills == Σ sell fills == volume), determinism (byte-identical re-runs),
  per-order fill ≤ quantity.
- **Settlement**: Σ base deltas == 0 and Σ quote deltas == 0 — a batch moves value
  between traders, never creates or destroys it (property-tested `settle_deltas`,
  used directly by the service).
