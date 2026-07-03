# Sealed orders — how Jamswap hides your trade until it clears

This doc explains, from the ground up, **why** an exchange wants to hide orders,
and the **three approaches** Jamswap has built to do it — what each one protects,
what it still leaks, and **which are live today vs proven-but-not-yet-wired-in**.

For the precise trust boundaries and the bugs we found and fixed along the way, see
[`SECURITY.md`](SECURITY.md). For where each primitive lives in the code, see
[`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Why hide an order at all? (the MEV problem, ELI5)

On most blockchains, when you send a trade it sits in a **public waiting room** (the
mempool) before it's included. Anyone watching can see "this person is about to buy
a lot of X" and **jump the queue** — buy first, let your order push the price up, then
sell to you. That skim is called **MEV** (miner/maximal extractable value). It's a tax
on every trade, and it's why on-chain trading can feel rigged.

Jamswap removes MEV in **two independent ways**:

1. **The batch auction** (always on). Instead of processing orders one-by-one in a
   race, Jamswap collects every order in a 6-second window and clears them **all at
   once, at a single fair price**. There's no "first" — so there's no advantage to
   being first. This alone kills the ordering game.
2. **Sealing** (the subject of this doc). Even within a batch, we can hide the *terms*
   of your order (price, size, side) until the moment it clears — so no one can react
   to it at all.

The batch auction is the foundation; sealing is the upgrade that hides the order
itself. The three approaches below are increasingly strong ways to seal.

---

## The three approaches (from simplest to strongest)

Think of them as a **ladder**. Each rung hides more, at the cost of more machinery or
more trust. Jamswap has built all three; the table is the quick version, the sections
below are the ELI5.

| # | Approach | Hides your order until… | What it still leaks | State today |
|---|----------|-------------------------|---------------------|-------------|
| **3** | **Commit–reveal** | the round it crosses a counterparty (rests hidden until then) | briefly public *when* it clears; needs a reveal step | ✅ shipped (fallback) |
| **2** | **Encrypt-until-batch** (committee) | the round it crosses (rests hidden until then; a committee decrypts it) | order becomes public at clearing; trust a committee for liveness | ✅ shipped — **the default** |
| **1** | **ZK dark-pool** (zero-knowledge matcher) | forever — it *never* appears on-chain | nothing about individual orders; only the batch result | 🔬 proven in a spike, not yet wired into Jamswap |

> There is no rung 4. **FHE** (fully homomorphic encryption — matching directly on
> encrypted orders) is theoretically the dream, but it's ~6 orders of magnitude too
> slow to run in a block today. We assessed it and ruled it out.

---

### Rung 3 — Commit–reveal ("armed commitment")  ✅ shipped

**ELI5.** You put your order in a **locked box** and hand over only the box (a
cryptographic fingerprint, `Blake2s256(order + secret nonce)`). Nobody can see what's
inside. When the auction runs, you (or the app on your behalf) **open the box** by
revealing the order + the secret. The chain checks the order matches the fingerprint
you committed earlier, then includes it in the auction.

- **What it protects:** nobody can see or front-run your order *before* the batch
  seals. And nobody can inject an order you never committed — the matcher only accepts
  reveals whose fingerprint was recorded on-chain (we found and fixed a bug where a
  malicious builder could sneak in a fake one; see SECURITY.md → "consume-or-reject").
- **What it still leaks:** the reveal is **briefly public** at clearing time (too late
  to front-run *that* batch, but visible after). And it needs a **reveal step** — if you
  commit and then never reveal, you've wasted a slot ("non-reveal griefing").
- **Rests hidden until it crosses, and keeps working after a partial fill:** a sealed order
  that finds no counterparty is *not* revealed and *not* discarded — it stays hidden (only its
  commitment on-chain) and is retried each auction, revealing its terms only in the round it
  actually crosses (see "How sealed orders rest" below). When it *does* cross but only partially
  fills, its remainder never rests in the public book with its terms exposed (immediate-or-cancel
  on-chain); instead the builder **re-seals the remainder into a fresh commitment and carries it
  forward**, so a large sealed order accumulates fills across many auctions while staying hidden,
  until it's complete or its good-till-time expires.

This is the simplest, most trust-minimal rung: **no third party at all**. It's the
`ENC_MODE=0` fallback in Jamswap.

---

### Rung 2 — Encrypt-until-batch (committee decryption)  ✅ shipped — the default

**ELI5.** Instead of you holding the key to your locked box, you **encrypt your order
to a committee** — a small group whose public key is published on-chain. You post the
encrypted order and go offline; you don't need to come back. When the auction closes,
the committee members each contribute a piece toward decrypting every order, and the
matcher reassembles the plaintext and clears the batch.

The clever part: each committee member's decryption piece comes with a **mathematical
proof** (a Chaum-Pedersen proof) that it's the *correct* piece for the key they
committed to. The matcher verifies these proofs, so the committee **cannot lie about
what your order said** — they can only refuse to help (which just stalls, it can't
steal or alter).

- **Better than rung 3:** **no reveal round** and **no griefing** — you encrypt once
  and never have to come back online. This is why it's the default.
- **The trust:** you trust the committee to be **live** (an honest majority helps
  decrypt). You do **not** trust them for correctness — the proofs force honest
  decryption. The committee uses **fresh keys**, never validator consensus keys (a JAM
  service can't hold a secret — see [`LASAIR_INTERNALS.md`](LASAIR_INTERNALS.md)), and
  runs **off-protocol** as a sidecar.
- **What it still leaks:** once decrypted at clearing, the order is public — same as
  commit–reveal. It hides the order *until* the round it clears in, not *forever*.
- **Rests hidden until it crosses:** like rung 3, a committee-sealed order with no
  counterparty stays hidden (only its ciphertext on-chain) and is retried each auction;
  it is decrypted on-chain only in the round it crosses (see "How sealed orders rest").
- **Cost:** ~5.6M gas per order per committee member (measured). That bounds a
  per-order-verified batch to ~880/n orders — which is exactly why rung 1 exists.

**How it runs in Jamswap today:** `docker compose up` builds the committee sidecar
([`crates/committee`](../crates/committee/)), commits its keys on-chain at startup, and
routes sealed orders through it automatically. The crypto is in
[`crates/vdec`](../crates/vdec/) (with security tests); the end-to-end proof is
[`offchain/test_enc_round.py`](../offchain/test_enc_round.py) (honest orders settle;
tampered / wrong-committee / injected orders are all rejected).

---

### Rung 1 — ZK dark-pool matcher  🔬 proven in a spike, not yet integrated

**ELI5.** Run the whole auction **off-chain, in private**, then hand the chain a single
**zero-knowledge proof** that says: *"I matched these hidden orders correctly, at this
fair price, and here's cryptographic proof I didn't cheat — without showing you the
orders."* The chain verifies one small proof and trusts the result. The individual
orders **never touch the chain at all** — a true dark pool.

- **Strongest privacy:** individual orders are never revealed, not even at clearing.
  Only the batch result (price + total volume + commitments) is public. This is the
  only rung that supports **persistent hidden resting orders**.
- **What the proof guarantees:** every filled order was genuinely marketable at the
  clearing price, no fill exceeds its order size, value is conserved (nothing minted or
  burned), **and** the price is optimal — the matcher **cannot under-fill to favour
  anyone**. All proven in zero knowledge.
- **The JAM win:** verifying the proof costs **~60M gas — flat, regardless of batch
  size**. 4 orders or 4,000, the chain does the same tiny amount of work. (Compare rung
  2, which scales *per order*.) Above ~10–20 orders, one ZK proof is cheaper.
- **State today:** **proven in a spike**, not yet wired into Jamswap. The circuit,
  prover, and on-chain verifier live in the sibling repo at
  `zk-jam-service/spikes/fba-zk/` and are measured end-to-end on a real node. The
  remaining integration work is binding the proof's order-commitment to Jamswap's
  on-chain sealed-order set (a `MATCH_ZK` tag), so the proof is provably over exactly
  the orders that were committed.
- **Honest caveats:** the spike uses a fixed-seed trusted setup (a production deploy
  needs a proper ceremony or a universal-setup system like PLONK), and a fixed batch
  size (a production circuit pads to a larger N or uses recursion).

---

## How sealed orders rest (rungs 2 & 3)

A sealed order rarely finds a counterparty in the *very* auction you place it in. So it
must be able to wait — but waiting must not expose it. Here's how Jamswap does that
without a ZK matcher, and why it's safe.

**The key fact:** the off-chain builder that assembles each auction holds the
**plaintext** of your sealed order (you send it your order; it encrypts a *copy* for the
chain but keeps the terms in memory). Only *other users* can't see it. So the builder
can check, each auction, whether your sealed order **crosses** the current liquidity
(is there an opposing order it could trade with?):

- **No cross** → the builder leaves it **sealed and untouched**: only its
  commitment/ciphertext stays on-chain, its terms are never revealed, and it's retried
  next auction. It genuinely *rests hidden*.
- **Crosses** → the builder **reveals** it (rung 3) or has the committee **decrypt** it
  (rung 2) *in that auction only*, and it clears.

So a sealed sell placed now and a sealed buy placed minutes later **will** match — the
sell rests hidden until the buy arrives to cross it. And the privacy guarantee is
actually *stronger* than "hidden until the batch": your terms are revealed **only in the
round they clear**, never merely because an auction ticked.

**Why this is safe (doesn't change the price).** A sealed order that doesn't cross is,
by definition, non-marketable at any uniform clearing price — a buy below every sell only
adds demand where there's no supply (and vice-versa). Leaving it out of the auction
therefore cannot change the clearing price or volume. The matching the validators
re-run is identical to what it would be if the resting sealed order had been included;
it just wouldn't have traded. (This carry-forward logic is the pure `offchain/round.py`
planner, regression-tested in `offchain/tests/test_round_lifecycle.py` — including the
exact "sealed sells, then later sealed buys" sequence that was previously broken.)

**One honest limitation.** A sealed order that never finds a counterparty keeps a small
commitment/ciphertext on-chain until it expires (good-till-time) or you cancel it. And
because the builder holds your plaintext to do the crossing check, this rung trusts the
builder for that check (the same builder that assembles every auction — a role every
exchange has). The *matching itself* remains fully validator-audited. True
builder-independent hidden resting is rung 1 (ZK).

---

## Which should you use?

- **Want zero trusted parties and simplicity?** Rung 3 (commit–reveal). You hold your
  own secret; the tradeoff is a reveal step.
- **Want to fire-and-forget without coming back online?** Rung 2 (the default). You
  trust a committee for liveness only.
- **Want hidden resting orders with no trusted builder at all?** Rung 1 (ZK
  dark-pool) — the strongest, and the scaling answer for large batches, once integrated.
  (Rungs 2 & 3 already rest hidden, but rely on the builder for the crossing check.)

The common thread: **the matching and settlement are always fully deterministic and
re-verified under JAM's guarantee-and-audit protocol** (assigned guarantors compute,
random auditors re-execute, fraud is slashable). Sealing changes *who can see your
order and when* — it never changes the guarantee that the auction cleared honestly.
