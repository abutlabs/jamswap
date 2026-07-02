# Jamswap

> A real order-book exchange that runs **trustlessly on a blockchain** — the kind of
> matching engine that until now only centralized exchanges could afford to run.

Jamswap is a **decentralized exchange (DEX)** built on [JAM](https://jam.web3.foundation).
It lets you trade one token for another the way a stock exchange or a company like
Coinbase does — using a live **order book** and a proper **matching engine** — but with
no company in the middle. You keep custody of your own funds, and every trade is
re-checked by every validator on the network.

**New here? Start with the three sections below** — what it is, how it works, and how
it hides your orders. Then [try it in one command](#try-it-in-one-command).

---

## What is it? (and why it couldn't exist before)

Almost every exchange you can name is one of two kinds:

- **A centralized exchange (CEX)** — Coinbase, Binance. Fast, real order books, but you
  hand them your money and trust them not to lose or misuse it.
- **A decentralized exchange (DEX)** — Uniswap and friends. You keep your funds, but
  they *don't* use a real order book. They use a **pricing formula** (an "AMM") instead.

Why does no DEX use a real order book? **Because running a matching engine on a
blockchain was too expensive.** Matching thousands of buy and sell orders is heavy
computation, and blockchains charge for every step — so DEXs settled for the cheap
formula-based approximation, which costs traders in worse prices and "slippage".

**JAM changes the economics.** JAM has a special phase called **Refine** designed for
heavy, parallel, deterministic computation that every validator can independently verify.
That's exactly the shape of a matching engine. So Jamswap runs a **genuine order-book
matching engine** on-chain — CEX-grade matching, DEX-grade self-custody. It's the
cleanest demonstration of something **only JAM can do**.

---

## How does it work? (ELI5)

Three ideas make Jamswap tick:

**1. It clears trades in fair batches, not a race.**
Most exchanges process orders one at a time, first-come-first-served — which turns
trading into a speed race that bots win. Jamswap instead collects every order in a
**6-second window** (matching JAM's block rhythm) and clears them **all at once at a
single fair price** (a "frequent batch auction"). Everyone in the batch trades at the
same price. There's no "first", so there's no speed game — and no room for the front-
running that plagues other chains.

**2. The matching happens where JAM is strong, settlement where it's safe.**
JAM splits work into two phases, and Jamswap maps an exchange straight onto them:

| JAM phase | Jamswap role | Think of it as… |
|-----------|--------------|-----------------|
| **Refine** | the **matching engine** — figures out who trades with whom, at what price | the trading floor |
| **Accumulate** | **settlement** — moves the actual balances between accounts | the vault / clearing house |

The matching is **deterministic** (integer-only, no randomness), so every validator
re-runs it and gets the byte-identical result — that's what makes it trustless. Then
settlement moves your tokens and records the new order book.

**3. You keep your own funds.**
JAM has no built-in wallets, so Jamswap gives your account its own cryptographic key
(held in your browser, exportable). Your orders are signed by that key; withdrawing or
cancelling is verified against it. No exchange can move your money — only you can.

Once matched, any part of your order that didn't fill can **rest in the order book** and
fill later when a matching order arrives — a true continuous exchange, not a one-shot
auction.

**Full technical architecture:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
**What's built and what's next:** [`docs/STATUS.md`](docs/STATUS.md).

---

## Hiding your orders (MEV-resistance)

A big reason on-chain trading feels rigged is **MEV**: bots watch the public queue of
pending trades and jump ahead of yours to skim a profit. The batch auction above already
removes the speed game. On top of that, Jamswap can **seal your order** so its price and
size stay hidden until the moment it clears — so nobody can react to it at all.

There are **three approaches**, a ladder from simplest to strongest:

- **Rung 3 — Commit–reveal.** You post only a locked fingerprint of your order; you
  reveal it when the auction runs. No trusted parties, but needs a reveal step. *(Shipped, available as a fallback.)*
- **Rung 2 — Encrypt-until-batch.** You encrypt your order to a committee and go offline;
  they help decrypt it only when the batch closes, with a proof they did it honestly. No
  reveal step. *(Shipped — **this is the default** when you run Jamswap.)*
- **Rung 1 — ZK dark-pool.** The auction runs privately off-chain and the chain verifies
  a single zero-knowledge proof that it cleared correctly — orders **never** appear
  on-chain. Strongest privacy, and cheapest at scale. *(Proven in a research spike; not
  yet wired into Jamswap.)*

A sealed order that finds no counterparty in the current auction doesn't vanish — it
**rests hidden** on-chain (only its commitment/ciphertext is posted) and keeps trying
each auction, revealing its terms **only in the round it actually crosses** a
counterparty. So you can place a sealed sell now and a sealed buy minutes later and
they'll match, all while their terms stay private until they clear.

**Read the full ELI5 of all three — what each protects, what it still leaks, and its
current state — in [`docs/SEALED_ORDERS.md`](docs/SEALED_ORDERS.md).** The precise trust
boundaries are in [`docs/SECURITY.md`](docs/SECURITY.md).

Whichever rung you use, the guarantee never changes: **the auction itself is always
re-verified by every validator.** Sealing changes *who can see your order and when* — not
whether it cleared honestly.

---

## A practical use for JAMKB (pricing JAM's memory)

JAM's designer, Gavin Wood, has proposed a token called **JAMKB** to price a scarce
resource: **the memory (RAM) that a service occupies across every validator**. The rule
is simple — **1 JAMKB lets a service keep 1 KB of state**. It's a proposal; nobody has a
running example of what it would actually feel like.

**Jamswap is that worked example.** A live exchange is *made of* state that sits in
validator RAM — the order book, the sealed commitments, the balances. And that state
**visibly breathes**:

- Placing an order **grows** the footprint (a sealed order writes a 32-byte commitment;
  a resting order takes 17 bytes).
- Every 6-second auction **clears** orders → the book and commitments shrink → the
  footprint **falls again**.

So Jamswap is a **live meter of JAM state being consumed and released** — and because
JAMKB is *also* one of the tradable tokens on the exchange, **the cost of state gets a
real market price**. The DEX trades the very token that would pay for the DEX's memory.
It even surfaces a genuine tradeoff: **sealed (private) orders cost more state** than
plain ones, so privacy has a measurable JAMKB price.

**The exchange even pays its own rent.** A small, cost-based trading fee funds a treasury
that must first cover the service's JAMKB state rent (`ceil(footprint ÷ 1 KB)` JAMKB);
only the **surplus** is withdrawable profit, and only by the owner. So the DEX earns fees,
buys the JAMKB that pays for its own RAM, and hands the rest to its operator — a complete
self-funding loop. Details: [`docs/REVENUE.md`](docs/REVENUE.md).

**How does a service actually get and keep its JAMKB?** That's the practical question the
proposal leaves open, so we wrote it down as a standard. A service is **deployed with an
endowment** (so it's solvent from block zero), then **self-funds through use** (fees refill
the reserve — the steady-state target), with **beneficiary top-ups** as the runway/backstop
for early life and growth. When a service holds more state than its JAMKB covers, the
standard applies **backpressure** — it refuses to grow state further until usage frees it or
the reserve is topped up. Jamswap implements all of this at the service level (endowment,
self-funding fee, `Top up reserve` control, solvency backpressure, a live footprint→JAMKB
meter). The full thesis and the day-to-day mechanics are in
[`docs/JAMKB_STANDARD.md`](docs/JAMKB_STANDARD.md).

We built the **measurement**, the worked example, and the service-level standard — but we
deliberately **do not enforce** JAMKB in the node. Pricing JAM's state is a protocol-wide
economic decision for the community, not something one client should impose. The full
understanding and the proposal-for-discussion are in [`docs/JAMKB.md`](docs/JAMKB.md).

### No order rests forever — orders pay rent to stay alive

Because every resting order sits in validator RAM, it **costs JAMKB state rent for as long
as it rests** — whether or not it ever trades. That makes an unbounded *good-till-cancelled*
order a spam/griefing vector: flood the book with far-from-market orders that never fill,
never expire, and bloat the footprint (and every auction's matching work) **forever**,
driving JAMKB usage up indefinitely for free.

So in Jamswap **there is no rest-forever order**. Every order — even "GTC" — is given an
**automatic, rent-funded expiry**:

- **Its fee funds its lifetime.** An order rests only as long as the minimum profit from its
  fee can subsidize the state rent it accrues. When that budget is exhausted, the order
  **auto-expires and its state is reclaimed** (the JAMKB it held is freed).
- **Bigger footprint dies sooner.** A sealed order's on-chain commitment (32 B) costs more
  RAM than a public order (17 B), so it burns its budget faster — **sealed orders expire
  sooner than public ones**, a direct consequence of "sealed costs more JAMKB."
- **A hard cap** bounds the maximum resting time no matter what (so nothing lingers), and a
  **per-account open-order limit** stops any single actor from stuffing the book at once.
- **You can only shorten, never extend.** Picking a TTL sets an *earlier* expiry; you can
  never rest longer than the rent-funded lifetime.

The result: the book is **self-pruning**. Spam and stale liquidity clear themselves, and
JAMKB usage from resting orders is always bounded and reclaimed — the state you occupy is
state you're paying for. The knobs (`ORDER_RENT_BUDGET_KBS`, `MAX_RESTING_SECS`,
`MAX_OPEN_ORDERS`) are documented in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); the
guards are tested in `offchain/tests/test_order_lifetime.py`. The live UI shows each order's
countdown and the current policy under **Time in force**.

---

## Try it in one command

You **don't need the JAM client's source code.** The node is pulled as a published,
**multi-arch** image (`ghcr.io/abutlabs/lasair-node`). Clone this repo and:

```sh
docker compose up            # trading UI at http://localhost:8080
```

This starts a single dev node, deploys the Jamswap service onto it (the compiled
`service/jamswap-service.jam` ships in the repo — nothing to build), and serves the
**trading UI**. Open `http://localhost:8080` and you can:

1. **Create an account** — an ed25519 keypair your browser holds (exportable/importable).
2. **Fund it** in the Faucet tab — assets are **USDC, DOT, JAMKB**, trading across three
   pairs (**DOT/USDC, JAMKB/USDC, JAMKB/DOT**).
3. **Place an order** — Buy/Sell, Limit or Market. Tick **🔒 Seal** to hide it.
4. **Watch it clear** — auctions run **every 6 seconds** automatically; a live countdown
   shows the next one. Watch the order book, the mempool, and your balances update.

Toggle the **mempool** view to see the data actually sitting in the service: open orders
are tagged 🌐 LIMIT / ⚡ MARKET (terms visible) or 🔒 SEALED (only a commitment on-chain,
terms hidden until they clear).

### Run it against real JAM consensus (6 validators)

The single node above is a dev harness. To run against a real multi-validator network —
six validators with block gossip, wall-clock leader rotation, 6-second slots, and full
state-transition import (PolkaJam-style):

```sh
docker compose -f docker-compose.testnet.yml up    # 6 validators + the same UI at :8080
```

Now every order is gossiped, included in a block, and re-executed by all six validators
byte-identically; settlement lands a slot or two later at the real 6-second cadence.

### Options

```sh
LASAIR_NODE_TAG=node-v0.3.0 docker compose up   # pin a node version instead of :latest
docker compose --profile demo run --rm demo     # run the narrated CLI walkthrough (see sim/demo.py)
```

To fall back from the default committee sealing (rung 2) to commit–reveal (rung 3),
uncomment `ENC_MODE: "0"` under the `dex` service in `docker-compose.yml`.

| Your machine | What runs | Notes |
|---|---|---|
| **Linux / amd64** (Intel/AMD) | native | — |
| **Apple Silicon** (M1–M4, arm64) | native | the image is built for arm64 too |
| **Windows / WSL2** (amd64) | native | run inside a WSL2 Linux shell |
| **arm64 without an arm64 image yet** | emulated | add `--platform linux/amd64` (slower, but works) |

> **Running your own JAM node?** Jamswap is a fully self-contained JAM **service** —
> nothing is baked into the client. Point `LASAIR_RPC` at any conformant node with a
> deploy/work-item RPC and run the same flow. Build the blob yourself with
> `cd service && jam-pvm-build -m service`. lasair is just the node we ship it on.

---

## Learn more

| Doc | What's in it |
|-----|--------------|
| [`docs/SEALED_ORDERS.md`](docs/SEALED_ORDERS.md) | The three order-hiding approaches, ELI5 — what each protects and its state today |
| [`docs/JAMKB.md`](docs/JAMKB.md) | JAMKB explained + how Jamswap is a live worked example of it |
| [`docs/JAMKB_STANDARD.md`](docs/JAMKB_STANDARD.md) | The standard — how a service receives, holds, tops up, and is held accountable for its JAMKB |
| [`docs/REVENUE.md`](docs/REVENUE.md) | The self-funding treasury — fees pay the JAMKB rent, owner takes the profit |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The full technical build: state machine, wire formats, round lifecycle |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Honest self-assessment — what's fixed, what carries an asterisk |
| [`docs/TESTING.md`](docs/TESTING.md) | The test layers — matching engine, order sequences, sealed lifecycle, e2e |
| [`docs/STATUS.md`](docs/STATUS.md) | Builder's checklist — everything built, everything next |
| [`docs/PLAN.md`](docs/PLAN.md) | The thesis, business case, and phased roadmap |
| [`docs/M1_DEMO.md`](docs/M1_DEMO.md) | The first proof: a real batch cleared in Refine on a live node |
| [`docs/LASAIR_INTERNALS.md`](docs/LASAIR_INTERNALS.md) | Deep answers on the JAM client's internals (host calls, gas, purity) |

---

## Why it's a JAM flagship

- It's the **cleanest demonstration of what JAM uniquely enables**: real on-chain order
  books exist only because JAM removes the compute limit that forced every other DEX
  into formula-based pricing.
- The **batch auction is MEV-resistant by construction** — no intra-round speed race —
  and orders can be **sealed until the batch closes**.
- We also build the JAM client it runs on (**lasair**), so we understand the whole stack
  from the matching engine down to the state machine.

**Honest caveats** (kept in view): JAM mainnet timing isn't ours to control; "trustless"
carries an asterisk in the parts still being hardened (per-order signature checks in
`refine`, real on-chain custody); and bootstrapping trading liquidity is a real grind.
See [`docs/PLAN.md`](docs/PLAN.md) §9 and [`docs/SECURITY.md`](docs/SECURITY.md).

---

## The abutlabs JAM suite

Three things we built on JAM — an independent client, and two flagship services on it —
each one-command-runnable and demonstrating something only JAM can do:

- **[lasair](https://github.com/abutlabs/lasair)** — an independent OCaml JAM client
  (+ a live multi-node testnet that runs like PolkaJam).
- **[zk-jam-service](https://github.com/abutlabs/zk-jam-service)** — anonymous,
  sybil-resistant voting; a real zero-knowledge proof verified in `refine`.
- **[jamswap](https://github.com/abutlabs/jamswap)** — this: a frequent-batch-auction
  order-book DEX; matching in `refine`, MEV-resistant, settlement on-chain.
