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
  deterministic, so any re-execution is byte-identical. That's the trust model:
  the guarantors assigned to the core compute it, randomly selected auditors
  re-run it, and a mismatch is slashable — not universal re-execution. It
  produces a work-output; it does **not** touch live state.
- **`accumulate`** reads each work-output (via `accumulate_items()`) and commits
  it to service storage — balances, the resting book, commitments, stats.
- It's a **self-contained JAM service**: nothing is baked into Lasair. The `.jam`
  runs on any conformant node; Lasair is simply the client we dogfood it on, not a
  dependency.

## Work-item types (payload tag = first byte)

Jamswap is **multi-market**: each work-item names a market (`market_id`) and the
two assets it trades (`base`, `quote`). Different markets clear independently (one
work-package per market per round — JAM's per-core parallelism) into per-market
books, sharing one global balance ledger.

| Tag | Name | Payload | refine | accumulate |
|---|---|---|---|---|
| 0 | — | **RETIRED** (was unsigned `MATCH` — deleted so there is no unsigned downgrade path) | — | — |
| 1 | `DEPOSIT` | account ‖ asset_id ‖ amount(u64) | echo | credit `(asset_id, account)` (Phase-2 faucet; real custody = Phase 3) |
| 2 | `COMMIT` | market ‖ account ‖ commitment(32) ‖ seq(8) ‖ **owner sig(64)** | echo | verify the owner's signature + seq floor, then append `commitment‖account` to the market's set |
| 3 | `REVEAL` | `market‖base‖quote` ‖ commits ‖ reveals(order‖nonce) ‖ *public section* | admit only orders whose `H(order‖nonce)` ∈ commits, verify the public section, then clear | auth-trailer checks (below) **and** consume-or-reject the commitments, then settle |
| 4 | `CANCEL` | market ‖ account ‖ order_id | echo | remove the owner's matching order from the market's book |
| 5 | `WITHDRAW` | account ‖ asset_id ‖ amount(u64) | echo | debit balance + custody, **only if funded** (no overdraft) |
| 6 | `LIST` | market ‖ base ‖ quote | echo | register a market's canonical assets (+ index it). A round for an unlisted or asset-mismatched market is **rejected**. |
| 9 | `ENC_SETUP` | n ‖ committee_pks(n·32) ‖ nonce(8) ‖ sig(64) | echo | **gov-signed**: commit the encrypt-until-batch committee keys on-chain (nonce-protected) |
| 10 | `ENC_COMMIT` | market ‖ C1(32) ‖ body(17) ‖ account ‖ seq(8) ‖ **owner sig(64)** | echo | verify the owner's signature (over `id = H(C1‖body)`) + seq floor, then append `id‖account` to the encset |
| 11 | `ENC_ROUND` | committee keys ‖ ciphertexts ‖ proven partials ‖ *public section* | verify every partial's proof against the committee keys, decrypt each order, verify the public section, clear | verify committee-hash == on-chain committee, auth-trailer checks, consume-or-reject the ciphertext ids, then settle |
| 12 | `SMATCH` | `market‖base‖quote` ‖ *public section* | **verify each order's ed25519 sig** (and limit price == signed price), then clear | auth-trailer checks (below), then settle + store the book |
| 13 | `CARRY_COMMIT` | market ‖ account ‖ commitment(32) | echo | **allowance-gated** re-seal of a partially-filled sealed order's remainder (one credit per genuine partial fill, minted by the settling round) |
| 14 | `CARRY_ENC_COMMIT` | market ‖ C1(32) ‖ body(17) ‖ account | echo | same allowance gate, encrypt-until-batch mode |

**The signed public section (trustless orders — every round type carries it).** New public
orders travel as `order(17) ‖ flags ‖ signed_price ‖ seq(8) ‖ pubkey(32) ‖ sig(64)`; the
section is `[ns][signed orders][np][pruned (account,oid) pairs][on-chain book, byte-exact]`.
`refine` verifies each signature statelessly against the carried pubkey; the round's output
ends with an **auth trailer** (`bindings ‖ H(input book)`) that `accumulate` — which can
read state — checks: pubkey == the account's registered key, `seq` strictly above the
account's monotonic floor (replay-proof), market-order price within ±10% of the on-chain
last price, and the input-book hash == the on-chain book (no fabricated resting orders).
Any failure rejects the round fail-closed. Cost: ~1.31M gas/order (measured) → a signed
batch is gas-bound at ~3,800 orders; the ZK matcher folds all signatures into one proof.

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
   resting book. A revealed sealed order's remainder is immediate-or-cancel *on-chain* (kept
   off the public book), but the builder **re-seals and carries it forward** so it keeps
   working across auctions (see "Partial fills" below).
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

### Partial fills — a big order keeps working across batches

A batch clears **all-or-part at one price**, and a single 6 s auction rarely holds enough
crossing supply to fill a large order at once (a 250-lot buy against 10-lot asks fills 10 this
round). So an order **accumulates fills over successive auctions** rather than filling in one
shot — what happens to each round's remainder depends on the order type:

- **Public limit** — the remainder **rests in the on-chain book** (visible) and keeps filling
  as new counterparties arrive. A *market* order is submitted as a marketable limit (last price
  ± a band), so its remainder likewise rests at that band price and keeps working.
- **Sealed** — the revealed order's remainder is **immediate-or-cancel *on-chain*** (the
  service excludes it from the public book so its terms are never left exposed — see
  `reveal_output`). To give sealed orders the *same* cross-batch persistence, the **builder
  re-seals the remainder into a fresh hidden commitment and carries it forward**. So a 250-lot
  sealed buy fills 10 now and carries 240 (still hidden) into the next auction, filling more
  each round until it's complete or its good-till-time expires — it is **not** cancelled. Only
  an *expired* remainder is dropped.

This means large orders behave sensibly: they **group liquidity across many batches** into one
persistent order, publicly or privately, instead of losing the unfilled part.

The chain only exposes market-level `lp`/`cv`, not which order filled — so the **builder
produces a per-order fill receipt**. `offchain/clearing.py` is a faithful Python port of the
Rust engine (pinned to it by `tests/test_clearing.py`); `server.record_executions` reuses the
exact clearing it handed to `refine` and attributes fills to each trader's order. The UI's
**Execution report** panel polls `GET /api/executions?account=…` and shows, per order,
`filled <qty> @ <uniform price>` plus the remainder's disposition (`working` / `rested` /
`cancelled`) — so you watch a big order fill 10 at a time across auctions. It also corrects the
common misread that a 500-buy filled "100 @ 1.10 + 100 @ 1.20" when it in fact filled **200 @
one uniform price**. Tested in `tests/test_sealed_carry.py`.

## Order lifetime — rent-funded expiry (anti-bloat)

A resting order occupies validator RAM, so it **accrues JAMKB state rent continuously**,
whether or not it ever trades. An unbounded good-till-cancelled order is therefore a
griefing vector: spam far-from-market orders that never fill, never expire, and grow the
footprint (and every round's matching work) forever. Jamswap's rule: **no order rests
forever.** Every order gets an automatic expiry, and the existing reclaim path
(`prune_expired` for public orders, `plan_round.expired` for carried sealed ones) frees the
state when it lapses.

```
footprint_bytes   = 32 (sealed commitment)  | 17 (public resting order)
lifetime_secs     = ORDER_RENT_BUDGET_KBS / (footprint_bytes / 1024)   # rent the fee's min-profit funds
effective_expiry  = now + min(user_ttl or ∞, lifetime_secs, MAX_RESTING_SECS)
```

- **Fee-funded.** `ORDER_RENT_BUDGET_KBS` is the KB·seconds of state rent an order's minimum
  profit is willing to subsidize; lifetime is `budget ÷ footprint`, so a bigger footprint
  runs out sooner → **sealed (32 B) expires before public (17 B)**. (A policy constant, like
  `JAMKB_SUPPLY` — the community calibrates the real rate.)
- **Hard cap.** `MAX_RESTING_SECS` bounds the maximum resting time unconditionally.
- **Shorten-only.** A user TTL can pull the expiry *earlier*, never later than the rent cap.
- **Per-account cap.** `MAX_OPEN_ORDERS` limits live orders (mempool + resting book) per
  account per market, bounding one actor's instantaneous bloat.

The book is thus **self-pruning**: JAMKB usage from resting orders is always bounded and
reclaimed. `/api/state.order_life` surfaces the policy and `/api/mine[].expires_in` the
per-order countdown for the UI. Tested in `offchain/tests/test_order_lifetime.py`.

## Accounts & signing — the wallet stop-gap

JAM wallet standards haven't been finalized and publicly released yet (JAM itself is
pre-launch), so to prototype the service today the browser generates a **temporary
ed25519 account key** (WebCrypto, kept in localStorage, export/importable). Registering
binds it on-chain to a compact account handle, and every action — orders, sealed
commits, cancels, withdrawals — is a signed message the service verifies against that
registered key (replay-protected by per-account sequence floors). This is a stop-gap,
not the architecture: accounts in JAM live in *service* state, so when JAM wallets
arrive, "your account" simply becomes a key your wallet holds — nothing in the service
changes. Two practical notes shaped the prototype: signature checks run in-PVM (there's
no signature host call in GP 0.7.2, our conformance target), which makes **ed25519**
the affordable curve — Talisman's default **sr25519** accounts are expensive to verify
there, so today the extension is used for identity/connection while the ed25519 key
signs (the verifier already accepts `signRaw`'s `<Bytes>` framing, so a wallet's
ed25519 account can sign directly once wired). No extension is required to trade the
prototype.

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
