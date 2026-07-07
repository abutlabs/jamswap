# Jamswap

> An order-book exchange that runs **trustlessly on a blockchain** — the kind of
> matching engine that until now only centralized exchanges could afford to run.

Jamswap is a **decentralized exchange (DEX)** built on [JAM](https://jam.web3.foundation).
It lets you trade one token for another the way a stock exchange or a company like
Coinbase does — using a live **order book** and a proper **matching engine** — but with
no company in the middle.

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

> **Note:** JAM wallet standards aren't finalized yet (JAM is pre-launch), so the browser
> key is a stop-gap, not the architecture — when JAM wallets arrive, "your account" simply
> becomes a key your wallet holds; nothing in the service changes. The full work-around
> (why ed25519, how registration binds the key on-chain, replay protection):
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) → "Accounts & signing".

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

**Who can see your resting sealed order?** On-chain: no one (it's a hiding commitment).
Off-chain: only the builder you submitted through — with the hosted browser UI, that's
the exchange operator (same trust as any exchange). Want privacy from *everyone*,
including us? **Run your own builder** — one command, verified working, and your order
data never leaves your machine: [`docs/LOCAL_BUILDER.md`](docs/LOCAL_BUILDER.md).

**Read the full ELI5 of all three — what each protects, what it still leaks, and its
current state — in [`docs/SEALED_ORDERS.md`](docs/SEALED_ORDERS.md).** The precise trust
boundaries are in [`docs/SECURITY.md`](docs/SECURITY.md).

Whichever rung you use, the guarantee never changes: **the auction itself is always
re-verified under JAM's guarantee-and-audit protocol** (assigned validators compute it,
auditors re-execute it, fraud is slashable). Sealing changes *who can see your order and
when* — not whether it cleared honestly.

---

## Throughput, costs & JAMKB — in brief

Two resources, two meters: **compute** is bought per-slot (refine gas), **state** is
bought per-byte (**JAMKB** — JAM's proposed token pricing validator RAM at 1 JAMKB = 1 KB).

- **Throughput (measured in lasair's PVM):** a public-order batch is gas-bound at
  **~3,800 orders per 6-second batch per core**; committee-sealed orders at **~880/n**
  (n = committee size); the ZK dark-pool clears **~27,500–68,900** orders with one flat
  proof. The full tables, what binds each privacy rung, and how big orders accumulate
  fills across batches: [`docs/THROUGHPUT.md`](docs/THROUGHPUT.md).
- **JAMKB:** Jamswap aims to be a grounded example in how JAMKB will be utilized in a 
  JAM network. the order book visibly grows and shrinks the RAM footprint, every order 
  pays state rent so nothing rests forever, JAMKB itself trades on the exchange, and fees 
  fund the service's own rent — a self-funding loop: [`docs/JAMKB_IN_PRACTICE.md`](docs/JAMKB_IN_PRACTICE.md).

---

## Try it in one command

You **don't need the JAM client's source code.** Everything chain-side runs from one
published, **multi-arch** image (`ghcr.io/abutlabs/lasair`). Clone this repo and:

```sh
docker compose up            # trading UI at http://localhost:8080
```

**All networking is spec JAMNP-S over QUIC** — in the single node here and in the
networked testnet below. Orders reach the chain as work-packages over **CE-133**, state
is read back over **CE-129**, and the service is **seeded into genesis** — there is no
client-specific HTTP node RPC anywhere.

| Compose file | Run it | Scenario |
|---|---|---|
| [`docker-compose.yml`](docker-compose.yml) | `docker compose up` | **Quickstart** — one lasair process authors all six dev validators' slots and hosts the service; a CE-133 builder and a CE-129 reader bridge the DEX to the chain. Trading UI at `:8080`; nothing to build. |
| [`docker-compose.mixed.yml`](docker-compose.mixed.yml) | `docker compose -f docker-compose.mixed.yml up` | **Networked testnet — mixed-client** — six validators split across **two independent JAM clients** (lasair + PolkaJam) co-authoring one Safrole chain over JAMNP-S/QUIC, leadership rotating across clients. Consensus layer only for now — see [the section below](#run-it-on-a-mixed-client-chain--lasair-and-polkajam-one-command). |

The quickstart serves the **trading UI** on top of that chain (the compiled
`service/jamswap-service.jam` ships in the repo). Open `http://localhost:8080` and you can:

1. **Create an account** — an ed25519 keypair your browser holds (exportable/importable).
2. **Fund it** in the Faucet tab — assets are **USDC, DOT, JAMKB**, trading across three
   pairs (**DOT/USDC, JAMKB/USDC, JAMKB/DOT**).
3. **Place an order** — Buy/Sell, Limit or Market. Tick **🔒 Seal** to hide it.
4. **Watch it clear** — auctions run **every 6 seconds** automatically; a live countdown
   shows the next one. Watch the order book, the mempool, and your balances update.

Toggle the **mempool** view to see the data actually sitting in the service: open orders
are tagged 🌐 LIMIT / ⚡ MARKET (terms visible) or 🔒 SEALED (only a commitment on-chain,
terms hidden until they clear).

### Run it on a MIXED-client chain — lasair **and** PolkaJam, one command

The quickstart above runs one client. JAM's real promise is a network of
**different** client implementations agreeing on one chain. This compose runs exactly
that: six validators split across **two independent JAM clients** — [lasair](https://github.com/abutlabs/lasair)
(our OCaml client) and **PolkaJam** (Parity's) — co-authoring **one** Safrole chain,
with **leadership rotating across clients** and each client re-executing the other's
blocks to a byte-identical state root.

```sh
docker compose -f docker-compose.mixed.yml up
```

That's it — one line brings up a **multi-architecture** (Apple Silicon **and** Intel
Linux) mixed-client JAM testnet:

- `pj0 pj1 pj2` — PolkaJam validators (indices 0,1,2)
- `lm3 lm4 lm5` — lasair validators (indices 3,4,5)
- `spec-init` — mints the **shared genesis** both clients load (identical bytes → identical state root)
- `watch` — prints the chain advancing

Watch leadership rotate across clients, and confirm both agree on state:

```sh
# who authored each block — lasair's slots (val 3/4/5) interleave with PolkaJam's
docker compose -f docker-compose.mixed.yml logs lm3 lm4 lm5 | grep authored

# both clients on ONE chain: a lasair-authored block, re-derived by PolkaJam to the
# SAME state root (RPC on the host):
docker compose -f docker-compose.mixed.yml logs watch          # PolkaJam's view of the chain
```

Typical output — a single chain whose blocks alternate authorship:

```
lm5 | 🚀 authored slot 7918603 (val 5) height 1 …
lm4 | 🚀 authored slot 7918606 (val 4) height 4 …
lm3 | 🚀 authored slot 7918614 (val 3) height 12 …
      (PolkaJam authored heights 2,3,5,6,7,9,10,11 in between)
CROSS-CLIENT ROTATION — both clients co-author one chain; PolkaJam re-derives
every lasair-authored block's state root: MATCH ✓
```

**How it works, and what it proves.** Both clients load one operator-defined genesis
(`gen-spec`), whose validator set carries each node's real keys — PolkaJam's for
indices 0–2, lasair's for 3–5. Each node authors **only its own** Safrole slots (the
leader is resolved from on-chain state, so a node signs a slot *iff* it owns that
slot's leader) and imports every other slot over the **spec JAMNP-S/QUIC** transport
both clients speak. Because both are GP-v0.7.2-conformant, they agree on the fallback
leader schedule and re-execute to identical state. It's the strongest possible
interop result: two from-scratch client implementations running **one** blockchain.

**Options.**

```sh
# use a specific published lasair client image, or your locally-built one:
LASAIR_IMAGE=ghcr.io/abutlabs/lasair:0.1.0 docker compose -f docker-compose.mixed.yml up
LASAIR_IMAGE=lasair:local                  docker compose -f docker-compose.mixed.yml up   # built from the lasair repo

# pin the PolkaJam release fetched (black-box) at build time:
PJ_RELEASE=nightly-2026-07-04 docker compose -f docker-compose.mixed.yml up

# change the client split (which indices each client owns):
LAYOUT=lasair,lasair,polkajam,polkajam,lasair,polkajam docker compose -f docker-compose.mixed.yml up
```

> **Scope.** This compose demonstrates the **consensus layer** — the mixed-client
> chain jamswap runs *on*: both clients co-authoring one rotating chain and agreeing
> on state. Deploying the jamswap **service** onto that mixed chain (so DEX orders are
> guaranteed and settled across both clients) is the next milestone; today the full
> DEX trading flow runs on the single-client quickstart above (`docker compose up`).

> **On PolkaJam & compliance.** PolkaJam is used **black-box**: its binary is fetched
> from the public [`paritytech/polkajam-releases`](https://github.com/paritytech/polkajam-releases)
> at image-build time on *your* machine and is never committed or redistributed. The
> lasair client image is a normal multi-arch pull. See
> [`mixed/`](./mixed) and lasair's [`docs/MIXED_CLIENT_NETWORK.md`](https://github.com/abutlabs/lasair/blob/main/docs/MIXED_CLIENT_NETWORK.md).

### Options

```sh
LASAIR_TAG=1.6.2 docker compose up              # pin the client version instead of :latest
LASAIR_IMAGE=lasair:local docker compose up     # any image ref — e.g. a local source build
```

### Dev modes (Makefile)

Public images by default; a local lasair source build on demand — so a lasair change
can be verified end-to-end BEFORE tagging a release and waiting for the ~80-min
multi-arch CI publish. Requires the (private) lasair checkout next to this repo
(override with `LASAIR_SRC=…`):

```sh
make up             # default DEX stack, published image        (docker compose up)
make mixed          # mixed lasair+PolkaJam net, published image
make local          # build ../lasair -> lasair:local -> DEX stack
make mixed-local    # same source build -> mixed net
make verify         # e2e smoke test against the RUNNING DEX stack
make verify-mixed   # health check against the RUNNING mixed net
make down           # stop whichever stack is up
```

Pre-push flow for a lasair change: `make local && make verify`, then
`make mixed-local && make verify-mixed` (it waits for enough slots by itself) —
only then tag `client-vX.Y.Z` and let CI publish.

### Monitoring the mixed network

```sh
make monitor        # mixed net + Prometheus + Grafana; dashboards on :3000, no login
make monitor-down
```

Neither client exposes metrics natively, so a tiny exporter (`monitor/exporter.py`,
stdlib-only) derives them from what IS observable — lasair's stdout via the Docker
log API (read-only socket mount; local dev tooling only) and PolkaJam's `bestBlock`
RPC. The provisioned **JAM mixed network** dashboard shows the head slot, finality
lag, per-node heights, authoring rate split by client (the rotation claim, live),
blocks per validator, Safrole ticket pools, and the fault counters that flagged
every bug found so far (`bad_seal`, QUIC accept errors, ring-key failures, dropped
work-items). Prometheus itself is on :9090.

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
> nothing is baked into the client. Any conformant node that speaks JAMNP-S (CE-133
> work-package submission, CE-129 storage reads) can host it and run the same flow.
> Build the blob yourself with `cd service && jam-pvm-build -m service`. lasair is
> just the node we ship it on.

---

## Learn more

| Doc | What's in it |
|-----|--------------|
| [`docs/SEALED_ORDERS.md`](docs/SEALED_ORDERS.md) | The three order-hiding approaches, ELI5 — what each protects and its state today |
| [`docs/DIFFERENTIAL_TESTNET.md`](docs/DIFFERENTIAL_TESTNET.md) | **Differential milestone** (historical): the same blob on lasair + PolkaJam, byte-identical state, forgery rejected by both — green 2026-07-04; its HTTP-RPC rig has since been retired |
| [`docs/LOCAL_BUILDER.md`](docs/LOCAL_BUILDER.md) | Run your own builder + UI — full sealed-order privacy from everyone, including us (verified two-builder mode) |
| [`docs/COMMITTEE_DEPLOYMENT.md`](docs/COMMITTEE_DEPLOYMENT.md) | **Open work:** how the decryption committee goes from today's simulation to n independent operators on a real JAM testnet |
| [`docs/THROUGHPUT.md`](docs/THROUGHPUT.md) | **Measured throughput & costs** per 6-second batch — what binds each order type, and how big orders accumulate fills across batches |
| [`docs/JAMKB_IN_PRACTICE.md`](docs/JAMKB_IN_PRACTICE.md) | **JAMKB in practice** — Jamswap as the live worked example: the breathing footprint, rent-funded order expiry, the self-funding loop |
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
