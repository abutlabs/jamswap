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
- ✅ **Fixed-point decimal prices** — prices, quantities, and balances are integer
  *atomic* units (display × 10⁴), so orders carry 4 decimals (e.g. `1.1050`) while the
  engine stays integer-only. Settlement de-scales the quote notional exactly (buyers
  round up / sellers down, any dust to the treasury); conservation is preserved and
  property-tested over random price scales.
- ✅ **Economic simulation** ([`sim/engine-sim`](sim/engine-sim)) — drives the real
  engine with random order flow over thousands of rounds (`cargo run --release`),
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
- ◻️ Then: trustless per-order signature check in `refine` (not just at the builder),
  fund escrow at submission, real `on_transfer` custody, round sequencing via
  historical-lookup, threshold-encryption upgrade, indexer + WebSocket feeds, a W3F
  grant application.

CI (`.github/workflows/ci.yml`) runs the matching-engine property tests
(conservation, determinism, settlement Σ-deltas == 0) on every push — the
"never regress" gate from PLAN.md §5.

## Run it on your architecture (one command, no lasair source)

You **don't need the lasair source** — the JAM node is pulled as a published,
**multi-arch** image (`ghcr.io/abutlabs/lasair-node`). Just clone this repo and:

```sh
docker compose up                  # -> Single Node, trading UI at http://localhost:8080
```

That single node is a dev harness (immediate local execution). To run against **real
JAM consensus** — **six validators** with TCP block gossip, wall-clock leader rotation,
6 s slots, and STF import (PolkaJam-style) — use the testnet compose instead:

```sh
docker compose -f docker-compose.testnet.yml up    # -> 6 validators + same UI at :8080
```

Now every order is gossiped, included in a block, and re-executed by all six validators
byte-identically; settlement lands a slot or two later (real 6 s cadence). The validator
count is **fixed at 6** — the `lasair-testnet-node` image ships a 6-validator genesis
(`NODE_INDEX` 0–5) — and the dex points at validator `v0`'s operator RPC. Both modes
serve the same UI on `:8080`; the arch notes below apply to either.

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

**Sealed orders default to encrypt-until-batch (option 2 — no reveal round).** The dex
image builds an off-protocol **committee sidecar** ([`crates/committee`](crates/committee/));
on startup it commits the committee keys on-chain and thereafter sealed orders are
ECIES-encrypted to that committee. At each auction the committee decrypts and refine
verifies a Chaum-Pedersen proof of correct decryption — so a sealed order's terms stay
hidden until it clears, with **no per-owner reveal step and no non-reveal griefing**. Set
`ENC_MODE=0` in the `dex` service to fall back to plain commit–reveal (option 3). The
verifiable-decryption crypto is in [`crates/vdec`](crates/vdec/) (host soundness tests) and
gas-measured in `zk-jam-service/spikes/vdec-gas`; e2e proof in
[`offchain/test_enc_round.py`](offchain/test_enc_round.py).

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

## How Jamswap Prototype works (the trading UI)

The UI at `:8080` (`offchain/`) is self-custodial with a JAM-native account. The flow:

1. **Create account** — JAM has no protocol-level accounts or wallets (services define
   their own model — see the wallet note below), so your account is an **ed25519 keypair
   this app holds** (WebCrypto, persisted locally, **exportable/importable**). A signed
   registration binds it to a collision-free on-chain handle. No extension required; a
   future JAM wallet standard can slot in later.
2. **Fund** in the Faucet tab — assets are **USDC, DOT, JAMKB**, tradable across all
   three pairs (**DOT/USDC, JAMKB/USDC, JAMKB/DOT**).
3. **Place an order** — **Buy/Sell**, **Limit** (you set the price) or **Market**
   (a marketable-limit within a ±10% band of the last price). Each order is **signed by
   your account key** and the signature travels with it. Tick **🔒 Seal** to hide it.
4. **Auctions clear every 6 seconds**, mirroring JAM's block cadence — a live countdown
   shows the next one; queued orders clear automatically (no button).
5. **Watch the mempool** — a toggle shows *the data sitting in the service*: each queued
   order tagged **🌐 LIMIT** / **⚡ MARKET** (terms visible) or **🔒 SEALED**. Sealed
   orders publish only a Blake2s256 **commitment** on-chain (`TAG_COMMIT`); price and
   size stay hidden until their auction reveals and clears them (`TAG_REVEAL`) — real
   commit-reveal MEV-resistance, not a mock. Sealed orders are **immediate-or-cancel**:
   they match in that auction or expire, and **never rest in the public book** exposing
   their terms. (The reveal is briefly public on-chain — persistent privacy across rounds
   needs threshold/time-lock encryption, the documented upgrade in [`docs/SECURITY.md`](docs/SECURITY.md).)
6. **Your pending orders** — *decrypted for you* (you hold the nonce), with **cancel**
   for any not yet cleared. Resting (on-chain) orders cancel via `TAG_CANCEL`.

### JAMKB — pricing the state the service consumes

Jamswap holds live state (order books, sealed commitments, balances) in **validator
RAM** — exactly the resource Gavin Wood's **JAMKB** token prices (1 JAMKB ≙ 1 KB of JAM
state footprint). **JAMKB** is our prototype of that token: the DEX tracks the footprint
it implies as a **read-only meter** (nothing is held, funded, or consumed for now), and
because JAMKB is also a trading pair, *the cost of state gets a market
price*. Placing orders grows the footprint; the 6 s auctions clear them and free it.
We build the **metrics** (a live footprint→JAMKB meter) to make this measurable and
discussable — we deliberately **don't** enforce it in the node: pricing JAM's state is a
protocol-economics decision for the community, not one a single client should bake in.
The full understanding + that proposal (for discussion, not unilateral implementation)
is in **[`docs/JAMKB.md`](docs/JAMKB.md)**.

> **Honest note on the account model (why no Talisman).** JAM is not Substrate, so no
> Polkadot wallet can add lasair as an RPC "network" — and that won't change: JAM has *no
> protocol-level accounts or transactions* ("there is no transaction in JAM chain; users only
> interact with services", per the JAM designers), so **each service defines its own account
> model**. A Substrate wallet like Talisman would only hand us an **sr25519** signature — a
> curve too costly to verify in the PVM — so it buys us nothing here. Instead the service owns
> its scheme: an account is a collision-free handle **bound to an ed25519 key** by a signed
> registration, and the browser holds that key (WebCrypto, exportable). **Withdraw, cancel, and
> the treasury sweep are verified in the service** (replay-nonce'd) — forged/replayed ops are
> rejected (verified e2e on the 6-validator testnet; unit-tested in `match-engine/src/auth.rs`).
> Order placement is signed and verified at the (trusted) off-chain builder; the trustless
> in-`refine` order check is the documented next step. Market orders are now **marketable-limit
> with a ±10% slippage band**. A future JAM wallet standard (a `jam:` WalletConnect namespace,
> or wallets signing ed25519 for services) can slot in without changing the model. See
> [`docs/SECURITY.md`](docs/SECURITY.md).

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
