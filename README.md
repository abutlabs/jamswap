# Jamswap

> A frequent-batch-auction order-book DEX on JAM — a CEX-grade matching engine
> with DEX-grade self-custody, because JAM is the first chain that can actually
> *run* the matching engine.

Every on-chain exchange uses an AMM (a pricing formula) instead of a real order
book **because no blockchain could afford to run a matching engine**. JAM's
**Refine** phase is heavy, parallel, deterministic, *audited* compute — so
Jamswap runs an actual matching engine trustlessly and settles fills on-chain.
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
  **Jamswap is a self-contained JAM service — nothing baked into Lasair.**
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
- ✅ **Economic simulation** ([`sim/engine-sim`](sim/engine-sim)) — drives the real
  engine with random order flow over thousands of rounds (`cargo run --release`),
  reports market quality (fill rate, price stability, fee revenue) and **asserts
  value conservation every round** — the economic stress test behind the
  production-ready claim.
- ✅ **Market registry** — markets are *listed* with canonical assets before
  trading; an unlisted or asset-mismatched market is rejected (verified e2e). A
  discoverable market index backs market listing.
- ◻️ Then: real `on_transfer` custody, round sequencing via historical-lookup,
  threshold-encryption upgrade, indexer + WebSocket feeds, wallet/signing in the UI,
  a W3F grant application.

CI (`.github/workflows/ci.yml`) runs the matching-engine property tests
(conservation, determinism, settlement Σ-deltas == 0) on every push — the
"never regress" gate from PLAN.md §5.

## Run it on your architecture (one command, no lasair source)

You **don't need the lasair source** — the JAM node is pulled as a published,
**multi-arch** image (`ghcr.io/abutlabs/lasair-node`). Just clone this repo and:

```sh
docker compose up                  # -> trading UI at http://localhost:8080
```

| Your machine | What runs | Notes |
|---|---|---|
| **Linux / amd64** (Intel/AMD) | native | — |
| **Apple Silicon** (M1–M4, arm64) | native | the image is built for arm64 too |
| **Windows / WSL2** (amd64) | native | run inside a WSL2 Linux shell |
| **arm64 without an arm64 image yet** | emulated | add `--platform linux/amd64` (slower, but works) |

It pulls the node, deploys the Jamswap service onto it, and serves the **trading
UI** + off-chain builder ([`offchain/`](offchain/)): place limit orders, run an
auction round, and watch the uniform-price clearing, the resting order book, and
your balances update — across multiple markets sharing one ledger. The committed
service blob (`service/jamswap-service.jam`) means it runs straight from a clone;
nothing to compile.

Pin a node version instead of `:latest`:

```sh
LASAIR_NODE_TAG=node-v0.3.0 docker compose up
```

The narrated CLI scenario (sealed commit/reveal round → clearing → settlement →
resting book → cancel → MEV-resistance) is also available:

```sh
docker compose --profile demo run --rm demo   # see sim/demo.py
```

> **JAM-client teams:** this is a fully self-contained JAM **service** — nothing is
> baked into the client. If you have your own JAM node with a deploy/work-item RPC,
> point `LASAIR_RPC` at it and run the same flow. lasair is just the node we ship it
> on; the service is portable. Build the blob yourself with
> `cd service && jam-pvm-build -m service`.

Full architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## How Jamswap works (the trading UI)

The UI at `:8080` (`offchain/`) is wallet-native. The flow:

1. **Connect Talisman** (or any injected Polkadot wallet) — your real accounts, not
   `Account 1`. The on-chain account id is derived from your address.
2. **Fund** in the Faucet tab — assets are **USDC, DOT, JSMBK**, tradable across all
   three pairs (**DOT/USDC, JSMBK/USDC, JSMBK/DOT**).
3. **Place an order** — **Buy/Sell**, **Limit** (you set the price) or **Market**
   (takes the clearing price). Each order pops a **wallet confirmation** (`signRaw`)
   before it joins the batch. Tick **🔒 Seal** to hide it.
4. **Auctions clear every 6 seconds**, mirroring JAM's block cadence — a live countdown
   shows the next one; queued orders clear automatically (no button).
5. **Watch the mempool** — a toggle shows *the data sitting in the service*: each queued
   order tagged **🌐 LIMIT** / **⚡ MARKET** (terms visible) or **🔒 SEALED**. Sealed
   orders publish only a Blake2s256 **commitment** on-chain (`TAG_COMMIT`); price and
   size stay hidden until the batch reveals and clears (`TAG_REVEAL`) — real
   commit-reveal MEV-resistance, not a mock.
6. **Your pending orders** — *decrypted for you* (you hold the nonce), with **cancel**
   for any not yet cleared. Resting (on-chain) orders cancel via `TAG_CANCEL`.

### JSMBK and JAMKB — pricing the state the service consumes

Jamswap holds live state (order books, sealed commitments, balances) in **validator
RAM** — exactly the resource Gavin Wood's **JAMKB** token prices (1 JAMKB ≙ 1 KB of JAM
state footprint). **JSMBK** is our prototype of that token: it backs the service's
footprint, and because it's also a trading pair, *the cost of state gets a market
price*. Placing orders grows the footprint; the 6 s auctions clear them and free it.
We build the **metrics** (a live footprint→JAMKB meter) to make this measurable and
discussable — we deliberately **don't** enforce it in the node: pricing JAM's state is a
protocol-economics decision for the community, not one a single client should bake in.
The full understanding + that proposal (for discussion, not unilateral implementation)
is in **[`docs/JAMKB.md`](docs/JAMKB.md)**.

> **Honest note on the wallet:** lasair is a JAM node, not a Substrate chain, so
> Talisman can't add it as an RPC "network" (JAM ≠ Substrate). What's real and works:
> Talisman supplies your accounts and signs each order (a genuine wallet confirmation).
> Verifying those signatures **in the service** (signed operations) is the next step —
> see [`docs/SECURITY.md`](docs/SECURITY.md). Market orders take the uniform clearing
> price, which is well-behaved with a populated book and extreme in a thin one.

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

---

## The abutlabs JAM suite

Three things we built on JAM — an independent client, and two flagship services on
it — each one-command-runnable and demonstrating something only JAM can do:

- **[lasair](https://github.com/abutlabs/lasair)** — an independent OCaml JAM client
  (+ a live multi-node testnet that runs like PolkaJam).
- **[zk-jam-service](https://github.com/abutlabs/zk-jam-service)** — anonymous,
  sybil-resistant voting; a real ZK proof verified in `refine`.
- **[jamswap](https://github.com/abutlabs/jamswap)** — this: a frequent-batch-auction
  order-book DEX; matching in `refine`, MEV-resistant, settlement on-chain.
