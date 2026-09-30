# How Jamswap works

> Moved from the README (2026-07-16) to keep it short. This is the full explainer:
> what Jamswap is, how it works, how it hides your orders, what it costs, and where it
> runs.

## What is it? (and where it sits among on-chain order books)

Most decentralized exchanges don't run an order book. They use a **pricing formula** (an
automated market maker, AMM), because matching orders on a chain is expensive: on most
chains every validator re-executes every transaction.

Some exchanges do run real order books on-chain, and how they pay for it is the useful
part:

- **A chain of their own.** Hyperliquid built a layer-1 blockchain for trading: the order
  book is part of its protocol, every validator runs the matching engine, and blocks come
  in well under a second. dYdX (v4) is an app-chain whose validators match orders held in
  memory. Injective, and years earlier BitShares and Stellar, built order books into their
  chains too.
- **A fast general-purpose chain.** Serum (later OpenBook) and Phoenix run order books as
  programs on Solana, within its per-transaction compute limits.

**What JAM changes.** Those exchanges use *replicated execution*: every validator runs
the matching engine on every order. That works for matching alone, which is cheap per
order. JAM splits the work in two. **Refine** (in-core) is heavy, stateless computation run
by the few validators assigned to a core, re-executed by randomly selected auditors, with a
provably wrong result costing the signers their stake; so it carries the security of the
whole validator set while only a few execute it. **Accumulate** (on-chain) is the small,
stateful part every validator runs. The model is Polkadot's parachain validation,
generalised: any service can use cores, not only a whole parachain.

That matters for an exchange when the per-trade work is **verifiable and heavy**, which is
what jamswap does beyond matching:

- **Signed orders:** Refine checks every public order's ed25519 signature, about 5.29M gas each
  under GP 0.8.0, so one core clears about 945 public orders per 6-second batch
  ([`THROUGHPUT.md`](THROUGHPUT.md)), and more markets use more cores.
- **Sealed orders:** in encrypt-until-batch mode (opt-in; the committee is simulated
  today) Refine verifies a proof that each order was decrypted correctly and derives the
  plaintext itself ([`SEALED_ORDERS.md`](SEALED_ORDERS.md)).
- **Zero-knowledge batches:** a research spike settles a whole batch with one proof of
  about 260M gas, flat in the number of orders; not yet wired in.

A chain that replicates its order book pays for all of that on every validator. On JAM,
one core's validators and its auditors pay for it, and jamswap is one service among many,
not a chain of its own. The price is that Refine is stateless: it cannot read the live
book, so each round carries the resting book in byte for byte, and Accumulate, which can
read state, rejects the round unless the carried book's hash matches the one on-chain.
And jamswap clears in **batch auctions** rather than a continuous book, a different
market design (below).

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
> [`ARCHITECTURE.md`](ARCHITECTURE.md) → "Accounts & signing".

Once matched, any part of your order that didn't fill can **rest in the order book** and
fill later when a matching order arrives — a true continuous exchange, not a one-shot
auction.

**Full technical architecture:** [`ARCHITECTURE.md`](ARCHITECTURE.md).
**What's built and what's next:** [`STATUS.md`](STATUS.md).

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
  the committee is simulated today, see [`COMMITTEE_DEPLOYMENT.md`](COMMITTEE_DEPLOYMENT.md).)*
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
data never leaves your machine: [`LOCAL_BUILDER.md`](LOCAL_BUILDER.md).

**Read the full ELI5 of all three — what each protects, what it still leaks, and its
current state — in [`SEALED_ORDERS.md`](SEALED_ORDERS.md).** The precise trust
boundaries are in [`SECURITY.md`](SECURITY.md).

Whichever rung you use, the guarantee never changes: **the auction itself is always
re-verified under JAM's guarantee-and-audit protocol** (assigned validators compute it,
auditors re-execute it, fraud is slashable). Sealing changes *who can see your order and
when* — not whether it cleared honestly.

---

## Throughput, costs & JAMKB — in brief

Two resources, two meters: **compute** is bought per-slot (refine gas), **state** is
bought per-byte (**JAMKB** — JAM's proposed token pricing validator RAM at 1 JAMKB = 1 KB).

- **Throughput (GP 0.8.0 gas, measured in lasair's PVM and cross-checked against the
  polkavm interpreter):** a public-order batch is
  gas-bound at **~945 orders per 6-second batch per core**; committee-sealed orders at
  **~267/n** (n = committee size); the ZK dark-pool clears **~27,500–68,900** orders with one flat
  proof. The full tables, what binds each privacy rung, and how big orders accumulate
  fills across batches: [`THROUGHPUT.md`](THROUGHPUT.md).
- **JAMKB:** Jamswap aims to be a grounded example in how JAMKB will be utilized in a 
  JAM network: the order book visibly grows and shrinks the RAM footprint, every order 
  pays state rent so nothing rests forever, JAMKB itself trades on the exchange, and fees 
  fund the service's own rent — a self-funding loop: [`JAMKB_IN_PRACTICE.md`](JAMKB_IN_PRACTICE.md).

---

## Where it runs

Jamswap is one JAM service (`service/jamswap-service.jam`, GP 0.8.0), written to the
Graypaper and not to any client, plus an off-chain builder. The builder reaches the
chain through one interface ([`offchain/chain.py`](../offchain/chain.py)): the JIP-2
node RPC, which lasair (through `lasair-reader`) and PolkaJam both serve. Today the DEX
runs on three nets: six lasair validators (`lasair6`); six stock PolkaJam validators
with no lasair anywhere (`pj6`), where it deploys itself through the chain's Bootstrap
service; and three of each on one chain (`lasair-pj`), where the service state is
byte-identical on both clients at the finalized head. Which net is which:
[`NETS.md`](NETS.md). Bring your own client or service:
[`COLLABORATE.md`](COLLABORATE.md).

---

## Why it matters for JAM

- It shows what **Refine** is for: verifiable work on every trade (signed, sealed, and
  eventually proven in zero knowledge) that a replicated order-book chain would pay for on
  every validator, done by one core and its auditors, in a service among many rather than
  an app-chain of its own (the route Hyperliquid and dYdX took).
- The **batch auction is MEV-resistant by construction** — no intra-round speed race —
  and orders can be **sealed until the batch closes**.
- It is **client-neutral**: the same service runs on lasair and on stock PolkaJam, and
  we also build a JAM client (**lasair**), so we understand the whole stack from the
  matching engine down to the state machine.

**Honest caveats** (kept in view): JAM mainnet timing isn't ours to control; orders are now
verified on-chain end-to-end (public orders per-order in `refine`, sealed commits
owner-signed — not even the builder can inject either), but "trustless" still carries an
asterisk in the parts being hardened (a carried sealed remainder's *terms* are
builder-attested until the ZK linkage; real on-chain custody); and bootstrapping trading
liquidity is a real grind. See [`PLAN.md`](PLAN.md) §9 and
[`SECURITY.md`](SECURITY.md).

---

