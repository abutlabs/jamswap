# Jamswap architecture (as built)

How the working MVP is put together — the JAM service state machine, the wire
formats, the round lifecycle, and the honest trust boundaries. For the vision and
roadmap see [`PLAN.md`](PLAN.md); for the proven results see [`M1_DEMO.md`](M1_DEMO.md).

## The Refine/Accumulate split = exchange architecture

```
service Jamswap {
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

Jamswap is **multi-market**: each work-item names a market (`market_id`) and the
two assets it trades (`base`, `quote`). Different markets clear independently (one
work-package per market per round — JAM's per-core parallelism) into per-market
books, sharing one global balance ledger.

| Tag | Name | Payload | refine | accumulate |
|---|---|---|---|---|
| 0 | `MATCH` | `market‖base‖quote` ‖ plaintext orders (17 B each) | clear → settlement + resting book | settle balances, store the market's book, bump stats |
| 1 | `DEPOSIT` | account ‖ asset_id ‖ amount(u64) | echo | credit `(asset_id, account)` (Phase-2 faucet; real custody = Phase 3) |
| 2 | `COMMIT` | market ‖ account ‖ commitment(32) | echo | append commitment to the market's pending set |
| 3 | `REVEAL` | `market‖base‖quote` ‖ commits ‖ reveals(order‖nonce) | admit only orders whose `H(order‖nonce)` ∈ commits, then clear | (output is a `MATCH` settlement → same path) |
| 4 | `CANCEL` | market ‖ account ‖ order_id | echo | remove the owner's matching order from the market's book |
| 5 | `WITHDRAW` | account ‖ asset_id ‖ amount(u64) | echo | debit balance + custody, **only if funded** (no overdraft) |
| 6 | `LIST` | market ‖ base ‖ quote | echo | register a market's canonical assets (+ index it). A `MATCH`/`REVEAL` for an unlisted or asset-mismatched market is **rejected**. |

`refine` for `MATCH`/`REVEAL` emits:
`[0]‖[market:u32]‖[base:u32]‖[quote:u32]‖[settle_len:u32]‖[settlement]‖[resting book]`.
Settlement moves the **market's** `base`/`quote` assets between traders.

## Wire formats (little-endian, integer-only)

- **Order** (17 B): `account:u32 ‖ id:u32 ‖ side:u8(0=buy,1=sell) ‖ price:u32 ‖ qty:u32`
- **Settlement**: `price:u32 ‖ n:u32 ‖ n×(account:u32 ‖ side:u8 ‖ qty:u32)`
- **Reveal** (49 B): `order(17) ‖ nonce(32)`; **commitment** = `Blake2s256(reveal)`

## Storage layout (service state)

| Key | Value | Meaning |
|---|---|---|
| `b` ‖ asset_id(4) ‖ account(4) | u64 | balance of an asset for an account (global, cross-market) |
| `book` ‖ market(4) | orders blob | that market's resting order book |
| `commits` ‖ market(4) | 32 B × n | that market's pending commitments (cleared on settlement) |
| `lp` ‖ market(4), `cv` ‖ market(4) | u64 | that market's last price, cumulative volume |
| `cust` ‖ asset_id(4) | u64 | custodied total of an asset (deposits +, withdrawals −) |
| `mkt` ‖ market(4) | base(4) ‖ quote(4) | a listed market's canonical assets |
| `markets` | market_id × n | the discoverable index of listed markets |

## Round lifecycle

1. **(optional) Commit** — traders submit `COMMIT H(order‖nonce)`; only hashes go
   on-chain. Orders are hidden.
2. **Match / Reveal** — the builder assembles the batch: the resting `book` + new
   orders (plaintext `MATCH`), or the `commits` set + revealed orders (`REVEAL`).
   `refine` clears the uniform-price auction; partially/un-filled orders become
   the new resting book.
3. **Settle** — `accumulate` applies conservation-checked per-account deltas
   (`settle_deltas`: buy = +base/−(quote+fee), sell = −base/+(quote−fee)), routes a
   **flat trading fee** (30 bps on each side's quote notional) to the treasury
   account, persists the new `book`, clears `commits`, and bumps stats. **Σ = 0 per
   asset including the treasury** — fees are moved, not minted.

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
- Deposits/withdrawals are a **mock custody** model (a faucet credit / a funded
  debit) with the accounting invariant **Σ(balances of an asset) == `cust`[asset]**
  holding by construction (deposit/withdraw touch balance + custody equally; trades
  conserve). **Real self-custody** — backing deposits with actual on-chain asset
  transfers via `on_transfer`, against the JAM token standard — is the Phase-3
  upgrade, blocked on JAM asset-service maturity (the plan starts on a mock).

## Safety invariants (tested)

- **Matching** (`match-engine` proptests): **clearing optimality** (`p*` maximizes
  matched volume — no candidate price clears more), value conservation
  (Σ buy fills == Σ sell fills == volume), determinism (byte-identical re-runs),
  per-order fill ≤ quantity.
- **Settlement**: Σ base deltas == 0 and Σ quote deltas == 0 *including the trading
  fee to the treasury* — a batch moves value (and fees) between accounts, never
  creates or destroys it (property-tested `settle_deltas` over random fees, used
  directly by the service).
