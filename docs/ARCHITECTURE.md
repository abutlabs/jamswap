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
| 9 | `ENC_SETUP` | n ‖ committee_pks(n·32) ‖ nonce(8) ‖ sig(64) | echo | **gov-signed**: commit the encrypt-until-batch committee keys on-chain (nonce-protected) |
| 10 | `ENC_COMMIT` | market ‖ C1(32) ‖ body(17) | echo | append `id = H(C1‖body)` to the market's encrypted-order set |
| 11 | `ENC_ROUND` | committee keys ‖ ciphertexts ‖ proven partials ‖ plaintext (see below) | verify every partial's proof against the committee keys, decrypt each order, clear | verify committee-hash == on-chain committee **and** consume-or-reject the ciphertext ids, then settle |

**Encrypt-until-batch (option 2, sealed orders with no reveal round).** Orders are
encrypted (ECIES) to an **off-protocol committee** key committed on-chain via `ENC_SETUP`
(fresh committee keys — *never* validator consensus keys; a JAM service can't hold a
secret). A trader posts a ciphertext with `ENC_COMMIT`. At batch close the committee
produces, for each ciphertext, a partial decryption `S_i = sk_i·C1` carrying a
Chaum-Pedersen proof that it's the correct share for the committed `PK_i`; the builder
assembles these into an `ENC_ROUND`. `refine` verifies every proof, recovers each order
with **no secret**, and clears — and because refine is a pure function of its payload, two
`accumulate`-side checks stop a malicious builder: (1) the committee keys the round used
must hash-match the on-chain committee (else a swapped committee could steer decryption),
and (2) every ciphertext id must already be in the on-chain encrypted-order set
(consume-or-reject, same defence as `REVEAL`). This removes commit–reveal's reveal round
and non-reveal griefing; trust is honest-committee for *liveness* only (the DDH proof
forces honest plaintext). Verifiable-decryption cost is ~n·5.6M gas/order (measured;
zk-jam-service `spikes/vdec-gas/`), bounding a per-order-verified batch to ~880/n orders.
Crypto lives in `crates/vdec`; the committee sidecar is `crates/committee`; proven e2e by
`offchain/test_enc_round.py` (honest settles; tampered / wrong-committee / injected all
rejected).

`refine` for `MATCH`/`REVEAL` emits:
`[0]‖[market:u32]‖[base:u32]‖[quote:u32]‖[settle_len:u32]‖[settlement]‖[resting book]`.
Settlement moves the **market's** `base`/`quote` assets between traders.

## Wire formats (little-endian, integer-only)

- **Order** (17 B): `account:u32 ‖ id:u32 ‖ side:u8(0=buy,1=sell) ‖ price:u32 ‖ qty:u32`
- **Settlement**: `price:u32 ‖ n:u32 ‖ n×(account:u32 ‖ side:u8 ‖ qty:u32)`
- **Reveal** (49 B): `order(17) ‖ nonce(32)`; **commitment** = `Blake2s256(reveal)`

Prices, quantities, and balances are integer **atomic** units = display × `SCALE`
(`SCALE = 10_000` → 4 decimals), so a fractional price like `1.1050` is carried as the
integer `11050`. The matching engine stays integer-only; **settlement** de-scales the
quote notional by one factor of `SCALE` (`qty·price / SCALE`). The off-chain layer
scales on ingest and de-scales on read, so the UI speaks plain decimals end-to-end.

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
   orders (plaintext `MATCH`), or the `commits`/ciphertext set + revealed orders
   (`REVEAL`/`ENC_ROUND`). Only sealed orders that **cross** the current liquidity are
   revealed this round; non-crossing sealed orders are carried forward, still sealed
   on-chain (the pure `offchain/round.py` planner decides this from the plaintext the
   builder holds — see `docs/SEALED_ORDERS.md` → "How sealed orders rest"). `refine`
   clears the uniform-price auction; partially/un-filled *public* orders become the new
   resting book (a revealed sealed order's remainder is immediate-or-cancel).
3. **Settle** — `accumulate` applies conservation-checked per-account deltas
   (`settle_deltas`: buy = +base/−(quote+fee), sell = −base/+(quote−fee); quote notional
   = `qty·price / SCALE`, buyers rounding up and sellers down so any fixed-point dust
   flows to the treasury — exact when quantities are whole units), routes a **flat,
   cost-based trading fee in the base asset** (per filled order, capped at the fill) to
   the treasury account, persists the new `book`, clears `commits`, and bumps stats.
   **Σ = 0 per asset including the treasury** — fees (and rounding dust) are moved, not
   minted.

The "builder" (the party that reads on-chain `book`/`commits` and assembles the
next payload) is, in the MVP, the test/off-chain caller. The plan's alternative —
`refine` reading the prior finalized book via historical-lookup — is a later
optimization.

## How the clearing price is chosen (`clear()` in `match-engine`)

Every order in a batch clears at **one uniform price `p*`**. The engine considers only
the **distinct submitted limit prices** as candidates (the optimum always sits on one of
them), and for each candidate `p` computes:

- **demand** `D(p)` = Σ quantity of buys with limit **≥ p**, and
- **supply** `S(p)` = Σ quantity of sells with limit **≤ p**.

It picks the `p` that **maximizes matched volume** `min(D, S)`; ties are broken by
**minimal imbalance** `|D − S|`, then by lowest price (deterministic). Eligible orders
then fill to that volume by **price-time priority** (best price first, then order id),
so a marginal order may be partially filled but never over-filled.

**Consequence — you pay the equilibrium price, not your limit.** A limit price is the
*worst* price you'll accept, never the price you pay. The clearing price lands where
supply meets demand, and every fill in the batch gets it.

### Worked example (buy lands between resting asks)

Resting asks: `100@1.10`, `100@1.20`, `100@1.30`. A new **buy `100@1.25`** arrives.

| candidate `p` | `D(p)` | `S(p)` | volume | imbalance |
|---|---|---|---|---|
| **1.10** | 100 | 100 | **100** | **0** |
| 1.20 | 100 | 200 | 100 | 100 |
| 1.25 | 100 | 200 | 100 | 100 |
| 1.30 | 0 | 300 | 0 | — |

Any price in `[1.10, 1.25]` clears the same 100 units, so volume ties at 100. The
tie-break picks **1.10** — the unique price where `D == S` (zero imbalance), the true
competitive equilibrium. At 1.20/1.25 supply *exceeds* demand (200 offered vs 100
wanted), so those aren't equilibrium prices.

**Result:** all **100 DOT trade at `1.10`**, filled entirely against the cheapest ask;
the buyer gets **0.15 × 100 = 15 quote** of price improvement over their 1.25 limit, and
the `1.20`/`1.30` asks don't trade (demand was exhausted by cheaper liquidity). This is
locked in as the regression test `buy_between_asks_clears_at_the_marginal_ask_only`.

> **Design note.** Jamswap clears at the competitive-equilibrium price (the
> buyer-favorable end of the feasible band here), *not* a midpoint that splits the
> surplus. This is deterministic and principled — the price never sits where supply
> exceeds demand — but in a one-sided batch the resting side captures none of the
> surplus. A surplus-splitting (midpoint) rule is a one-line tie-break change if the
> community prefers it.

### Partial fills and the execution report

A batch clears **all-or-part at one price**, so an order can fill partially. What happens to
the unfilled remainder depends on the order type:

- **Public limit** — the remainder becomes/stays a **resting** order in the book (visible,
  waiting for a later counterparty). A *market* order is submitted as a marketable limit (at
  the last price ± a band), so any remainder likewise rests at that band price.
- **Sealed** (revealed to clear) — the remainder is **immediate-or-cancel**: it is **dropped**,
  never left resting-and-exposed. (So a sealed buy for 500 that finds only 200 of crossing
  supply fills 200 and cancels 300 — the book stays empty of it.)

The chain only exposes market-level `lp`/`cv`, not which order filled — so the **builder
produces a per-order fill receipt**. `offchain/clearing.py` is a faithful Python port of the
Rust engine (pinned to it by `tests/test_clearing.py`); `server.record_executions` recomputes
the exact batch it handed to `refine` and attributes fills to each trader's order. The UI's
**Execution report** panel polls `GET /api/executions?account=…` and shows, per order:
`filled <qty> @ <uniform price>` plus the remainder's disposition (`rested` / `cancelled`).
This is what makes the matching engine's behaviour legible in real time — and it corrects the
common misread that a 500-buy filled "100 @ 1.10 + 100 @ 1.20" when it in fact filled **200 @
one uniform price**.

## MEV-resistance

Two layers, both proven e2e:
- **Frequent batch auction** — one uniform clearing price per round removes the
  latency race that drives most CEX/AMM MEV. Everyone trades at `p*`.
- **Sealed orders** — orders are hidden (only a hash/ciphertext on-chain) and **rest
  hidden** until the round they cross a counterparty; `refine` admits only committed
  orders. You cannot see an order in time to front-run it, nor inject one you didn't
  commit. A sealed order's terms are revealed only in the round it clears (the builder's
  crossing check carries non-crossing sealed orders forward, still sealed).

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
  fee and any fixed-point rounding dust to the treasury* — a batch moves value, never
  creates or destroys it (property-tested `settle_deltas` over random fees **and price
  scales**, used directly by the service).
