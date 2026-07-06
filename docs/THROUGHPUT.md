# Throughput & costs (measured, per 6-second batch)

How many orders fit in one Jamswap batch, what each order type costs, and what
actually binds each privacy rung. All numbers measured in Lasair's PVM
(`spikes/crypto-gas/`, `spikes/fba-zk/`, `spikes/vdec-gas/`), per work package on
**one core** at the full-spec refine budget (5×10⁹ gas). The matching itself is never
the limit (7,476 gas cleared 3 orders) — what binds is per-order *validation*:

The 880 is how many committee-share verifications fit in one batch's gas budget.
```
5,000,000,000 gas   (one core's refine budget per 6s work package, full spec)
÷     ~5,680,000 gas (measured cost to verify ONE committee member's decryption share)
≈           880      share-verifications per batch
```
Then the /n: each sealed order needs all n members' shares verified (that's what
removes trust in the committee — every share is proven honest, per order). So one
order consumes n of your 880 verification "slots":

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
the table above), **state** is bought per-byte (JAMKB — see
[`JAMKB_IN_PRACTICE.md`](JAMKB_IN_PRACTICE.md)). A *filled* order leaves
almost no lasting state; a *resting* public order occupies 17 B of validator RAM
(~60 orders/KB), a resting sealed commitment 32 B (32/KB) — prepaid by rent and
reclaimed at expiry, so a bigger book costs rent, not gas, and the two never compete.

## Big orders accumulate liquidity across batches

A single 6-second auction rarely has enough crossing supply to fill a large order at once —
a 250-lot buy against 10-lot asks fills 10 this round. So a big order **keeps working across
successive auctions**, filling more each round until it's complete or expires, rather than
grabbing 2% and giving up. Public (and market) orders do this by **resting in the book**;
sealed orders do it privately — the builder **re-seals each round's unfilled remainder into a
fresh hidden commitment and carries it forward**, so a large sealed order accumulates fills
while staying hidden (never resting exposed). The **Execution report** shows it happening:
`filled 10 @ 1.30 · 240 working`, then `filled 10 @ 1.20 · 230 working`, and so on. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) → "Partial fills"; tested in
`offchain/tests/test_sealed_carry.py`.

---

*Back to the [README](../README.md). The wire formats and round lifecycle behind these
numbers: [`ARCHITECTURE.md`](ARCHITECTURE.md). What each privacy rung protects:
[`SEALED_ORDERS.md`](SEALED_ORDERS.md).*
