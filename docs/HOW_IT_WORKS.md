# How Jamswap works

> Moved from the README (2026-07-16) to keep it short. This is the full explainer:
> what Jamswap is, how it works, how it hides your orders, and what it costs.

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

