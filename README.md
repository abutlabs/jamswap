# Jamswap

> A real order-book exchange that runs **trustlessly on a blockchain** — the kind of
> matching engine that until now only centralized exchanges could afford to run.

Jamswap is a **decentralized exchange (DEX)** built on [JAM](https://jam.web3.foundation).
It lets you trade one token for another the way a stock exchange or a company like
Coinbase does — using a live **order book** and a proper **matching engine** — but with
no company in the middle. You keep custody of your own funds, and every trade is
checked by the network: independent validators re-run the matching under JAM's
audit protocol, and settlement is re-executed by every node.

**New here? Start with the three sections below** — what it is, how it works, and how
it hides your orders. Then [try it in one command](#try-it-in-one-command).

![Jamswap-demo](./docs/demo.gif)

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
heavy, parallel, deterministic computation. Crucially, it is *not* re-run by every
validator — a small group assigned to the core computes each batch, randomly selected
auditors re-execute it, and a provably wrong result costs the signers their stake.
That's why it's cheap, and it's exactly the shape of a matching engine. So Jamswap runs a **genuine order-book
matching engine** on-chain — CEX-grade matching, DEX-grade self-custody. It's the
cleanest demonstration of something **only JAM can do**.

---

## How does it work?

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

The matching is **deterministic** (integer-only, no randomness), so *anyone* who
re-runs it gets the byte-identical result. In JAM that's what makes the audit decisive:
the validators assigned to the core clear the batch, randomly chosen auditors re-execute
it, and any mismatch is provable fraud that gets the signers slashed. Trustless —
*without* the whole network redoing the work. Then settlement moves your tokens and
records the new order book.

**3. You keep your own funds.**
JAM has no built-in wallets, so Jamswap gives your account its own cryptographic key
(held in your browser, exportable). Your orders are signed by that key; withdrawing or
cancelling is verified against it. No exchange can move your money — only you can.

> **The wallet work-around, in short:** JAM wallet standards haven't been finalized and
> publicly released yet (JAM itself is pre-launch), so to prototype the service today the
> browser generates a **temporary ed25519 account key** (WebCrypto, kept in localStorage,
> export/importable). Registering binds it on-chain to a compact account handle, and every
> action — orders, sealed commits, cancels, withdrawals — is a signed message the service
> verifies against that registered key (replay-protected by per-account sequence floors).
> This is a stop-gap, not the architecture: accounts in JAM live in *service* state, so
> when JAM wallets arrive, "your account" simply becomes a key your wallet holds — nothing
> in the service changes. Two practical notes shaped the prototype: signature checks run
> in-PVM (there's no signature host call in GP 0.7.2, our conformance target), which makes
> **ed25519** the affordable curve — Talisman's default **sr25519** accounts are expensive
> to verify there, so today the extension is used for identity/connection while the
> ed25519 key signs (the verifier already accepts `signRaw`'s `<Bytes>` framing, so a
> wallet's ed25519 account can sign directly once wired). No extension is required to
> trade the prototype.

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

- **Rung 3 — Commit–reveal.** You post only a locked fingerprint of your order; it's
  revealed only in the round it trades. No trusted parties, no extra operators, no asks
  of anyone — fully permissionless. *(Shipped — **this is the default**: the base state.)*
- **Rung 2 — Encrypt-until-batch.** You encrypt your order to a committee and go offline;
  they help decrypt it only when the batch closes, with a proof they did it honestly. No
  reveal step, and no single party can peek. *(Shipped as an **opt-in** — `ENC_MODE=1`;
  the committee is simulated today, see [`docs/COMMITTEE_DEPLOYMENT.md`](docs/COMMITTEE_DEPLOYMENT.md).)*
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
re-verified under JAM's guarantee-and-audit protocol** (assigned validators compute it,
auditors re-execute it, fraud is slashable). Sealing changes *who can see your order and
when* — not whether it cleared honestly.

---

## Throughput & costs (measured, per 6-second batch)

All numbers measured in Lasair's PVM (`spikes/crypto-gas/`, `spikes/fba-zk/`,
`spikes/vdec-gas/`), per work package on **one core** at the full-spec refine budget
(5×10⁹ gas). The matching itself is never the limit (7,476 gas cleared 3 orders) —
what binds is per-order *validation*:

The 880 is how many committee-share verifications fit in one batch's gas budget.
``` 
5,000,000,000 gas   (one core's refine budget per 6s work package, full spec)
÷     ~5,680,000 gas (measured cost to verify ONE committee member's decryption share)
≈           880      share-verifications per batch
```
Then the /n: each sealed order needs all n members' shares verified (that's what removes trust in the committee — every share is proven honest, per order). So one order consumes n of your 880 verification "slots":

```
- n = 1 → 880 orders/batch
- n = 5 → 880 ÷ 5 = 176 orders/batch
- n = 10 → 88 orders/batch
```

| Order type | Refine cost per order | Binding limit | ~Orders per batch | Scales with |
|---|---|---|---|---|
| **Public** (signed; ed25519 verified in `refine`) | 1.31 M gas | refine gas | **~3,800** | **cores** — more markets on more cores, linear |
| **Sealed — commit–reveal** (rung 3) | 2.7k gas reveal check (+1.31 M if sig-verified) | refine gas | **~3,800** | cores |
| **Sealed — encrypt-until-batch** (rung 2, default) | ~n × 5.6 M gas (n = committee size) | refine gas | **~880/n** (n=5 → ~176) | **cores × (880 ÷ n)** — inversely with committee size: every member proves per order, so a bigger committee buys trust/liveness at the direct cost of throughput; the scaling answer is rung 1 |
| **Sealed — ZK dark-pool** (rung 1, spiked) | ~0 — one 60.1 M-gas proof settles the batch, flat | input size (W_B ≈ 13.15 MiB) | **~27,500–68,900** | cores × prover capacity; on-chain cost flat in order count |

Two independent resources, two meters: **compute** is bought per-slot (coretime/gas —
the table above), **state** is bought per-byte (JAMKB, below). A *filled* order leaves
almost no lasting state; a *resting* public order occupies 17 B of validator RAM
(~60 orders/KB), a resting sealed commitment 32 B (32/KB) — prepaid by rent and
reclaimed at expiry, so a bigger book costs rent, not gas, and the two never compete.

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

### Big orders accumulate liquidity across batches

A single 6-second auction rarely has enough crossing supply to fill a large order at once —
a 250-lot buy against 10-lot asks fills 10 this round. So a big order **keeps working across
successive auctions**, filling more each round until it's complete or expires, rather than
grabbing 2% and giving up. Public (and market) orders do this by **resting in the book**;
sealed orders do it privately — the builder **re-seals each round's unfilled remainder into a
fresh hidden commitment and carries it forward**, so a large sealed order accumulates fills
while staying hidden (never resting exposed). The **Execution report** shows it happening:
`filled 10 @ 1.30 · 240 working`, then `filled 10 @ 1.20 · 230 working`, and so on. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) → "Partial fills"; tested in
`offchain/tests/test_sealed_carry.py`.

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

Now every order is gossiped and included in a block, the batch is cleared in `refine`
byte-identically, and settlement — imported and re-executed by all six validators —
lands a slot or two later at the real 6-second cadence.

### Options

```sh
LASAIR_NODE_TAG=0.4.2 docker compose up         # pin a node version instead of :latest
                                                # (GHCR tags strip the git-tag prefix: node-v0.4.2 -> 0.4.2)
docker compose --profile demo run --rm demo     # run the narrated CLI walkthrough (see sim/demo.py)
```

Sealing defaults to commit–reveal (rung 3 — the permissionless base state). To opt in to
the rung-2 committee (encrypt-until-batch, simulated committee), uncomment
`ENC_MODE: "1"` under the `dex` service in `docker-compose.yml`.

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
| [`docs/COMMITTEE_DEPLOYMENT.md`](docs/COMMITTEE_DEPLOYMENT.md) | **Open work:** how the decryption committee goes from today's simulation to n independent operators on a real JAM testnet |
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

## Open questions & design options

Jamswap is a **prototype for discussion**, not a finished protocol. Several choices are
deliberate-but-not-final, and a few genuinely belong to the JAM community rather than to any
one client. We list them openly so the trade-offs are on the table — and so the "obvious"
answer isn't quietly baked in. Each row says **what Jamswap does today**, the **alternative**,
and roughly **how big a change** it is.

### Matching engine

| Question | Today | Alternative(s) | Size of change |
|---|---|---|---|
| **Clearing price** when a whole band of prices clears the same max volume | the **competitive-equilibrium** price (min-imbalance tie-break → the resting side captures the surplus) | a **surplus-splitting midpoint** of the feasible band | one-line tie-break change (`match-engine`) |
| **Large order** that can't fully fill in one 6 s batch | **accumulates across batches** — public/market remainders rest; sealed remainders re-seal & carry forward | a single-batch **fill-or-kill / max-slippage sweep** (accept worse prices to fill more *now*) | scoped feature — a wider market band or an IOC/slippage flag |
| **Marginal allocation** when demand ≠ supply at `p*` | **price-time priority** (best price, then order id) | **pro-rata** across same-price orders | engine change (well-isolated) |

Details + the worked example: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) → "How the
clearing price is chosen" and "Partial fills".

### Order privacy (sealing)

| Question | Today | Alternative(s) | Trade-off |
|---|---|---|---|
| **How orders are hidden** until they clear | **commit–reveal** (rung 3) by default — the permissionless, no-third-party base state; **encrypt-until-batch** (rung 2, committee) as the `ENC_MODE=1` opt-in | a **ZK dark-pool** matcher (rung 1 — spiked, proven, not yet integrated) | each carries a *different* trust asterisk — a reveal-round griefing vector vs committee liveness vs prover cost |
| **Committee deployment** — today one sidecar *simulates* all n members (single-operator trust) | proven cryptography + on-chain committee anchoring; the operational model is designed but unbuilt | per-member daemons run by n independent operators, with an on-chain policy check so the builder can't use the committee as a decryption oracle | **open work** — the follow-up list lives in [`docs/COMMITTEE_DEPLOYMENT.md`](docs/COMMITTEE_DEPLOYMENT.md) |

The three-rung privacy ladder is in [`docs/SEALED_ORDERS.md`](docs/SEALED_ORDERS.md).

### JAMKB economics (mostly the community's call)

| Question | Today | Open question |
|---|---|---|
| **Total JAMKB supply / the RAM↔KB price** | a **stand-in constant** (`JAMKB_SUPPLY`) | the real cap is *total validator RAM ÷ 1 KB* — a protocol-wide measurement the community must set |
| **Where account-JAMKB + per-service obligations live** | inside the jamswap service | a shared **JAMKB system service** or **protocol-level accounts** for a testnet-wide standard |
| **Enforcement** of `footprint ∝ JAMKB` | **measured only** — deliberately **not enforced** in the node | whether/how to enforce is a protocol-economics decision, not one client's to impose |
| **Order rent → lifetime rate** (anti-bloat) | policy stand-in knobs (`ORDER_RENT_BUDGET_KBS`, `MAX_RESTING_SECS`, `MAX_OPEN_ORDERS`) | calibrate to real state-rent economics once JAMKB is priced |

Background + our thesis: [`docs/JAMKB.md`](docs/JAMKB.md) and
[`docs/JAMKB_STANDARD.md`](docs/JAMKB_STANDARD.md). We built the **measurement and a running
example** precisely to make these answerable with real numbers, not to pre-empt them.

### Fees, treasury & trust boundaries

| Question | Today | Open / next |
|---|---|---|
| **Fee shape** | a **flat, cost-based** fee per filled order in the base asset | a size-proportional fee, if the community prefers ([`docs/REVENUE.md`](docs/REVENUE.md)) |
| **Profit payout** to the beneficiary | gov-signed sweep of fee revenue, swapped on the DEX | an actual **JAM↔Polkadot bridge** to the AssetHub account (deferred — no bridge yet) |
| **Order / cancel / withdraw auth** | **trustless** — public orders ed25519-verified per-order **in `refine`** (replay-proof seq floors, hash-bound book); sealed commits **owner-signed + account-bound**; cancel/withdraw signed + nonce-protected | remaining asterisk: a carried remainder's *terms* are builder-attested until the rung-1 ZK linkage ([`docs/SECURITY.md`](docs/SECURITY.md)) |
| **Custody** | **mock** (faucet credit / funded debit, conservation-checked) | real self-custody via `on_transfer`, blocked on JAM asset-service maturity |

If any of these should be decided differently, they're small, well-isolated changes — say
which and we'll flip it (or wire in the toggle).

---

## Why it's a JAM flagship

- It's the **cleanest demonstration of what JAM uniquely enables**: real on-chain order
  books exist only because JAM removes the compute limit that forced every other DEX
  into formula-based pricing.
- The **batch auction is MEV-resistant by construction** — no intra-round speed race —
  and orders can be **sealed until the batch closes**.
- We also build the JAM client it runs on (**lasair**), so we understand the whole stack
  from the matching engine down to the state machine.

**Honest caveats** (kept in view): JAM mainnet timing isn't ours to control; orders are now
verified on-chain end-to-end (public orders per-order in `refine`, sealed commits
owner-signed — not even the builder can inject either), but "trustless" still carries an
asterisk in the parts being hardened (a carried sealed remainder's *terms* are
builder-attested until the ZK linkage; real on-chain custody); and bootstrapping trading
liquidity is a real grind. See [`docs/PLAN.md`](docs/PLAN.md) §9 and
[`docs/SECURITY.md`](docs/SECURITY.md).

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
