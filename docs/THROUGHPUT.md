# Throughput & costs (measured, per 6-second batch)

How many orders fit in one Jamswap batch, what each order type costs, and what
actually binds each privacy rung. All numbers are **GP 0.8.0 gas**, per work package
on **one core** at the full-spec refine budget (5×10⁹ gas). They were measured in
lasair's PVM from zk-jam-service's gas spikes (`spikes/crypto-gas`, `vdec-gas`,
`groth16-gas`, `fba-zk`) rebuilt for 0.8.0 by `tools/jam080/measure-gas.sh`. The
polkavm reference interpreter reproduces each figure exactly (lasair
`scripts/pvm-refine-differential.sh`). GP 0.8.0 prices gas per basic block with a
pipeline model, not 1 gas per instruction, so these are about 4× the GP 0.7.2
figures this page used to quote.

The matching itself is never the limit (3 orders cleared in 7,476 gas under GP 0.7.2,
not re-measured). What binds is per-order *validation*.

The 267 below is how many committee-share verifications fit in one batch's gas budget.
```
5,000,000,000 gas   (one core's refine budget per 6s work package, full spec)
÷    18,722,000 gas (measured cost to verify ONE committee member's decryption share)
≈           267      share-verifications per batch
```
Then divide by n. Each sealed order needs all n members' shares verified, which is
what removes trust in the committee: every share is proven honest, per order. So one
order uses n of the 267 verification slots:

```
- n = 1 → ~260 orders/batch   (19.2 M gas per order, measured)
- n = 5 → ~53 orders/batch    (94.1 M gas per order, measured)
- n = 10 → ~26 orders/batch
```

| Order type | Refine cost per order | Binding limit | ~Orders per batch | Scales with |
|---|---|---|---|---|
| **Public** (signed; ed25519 verified in `refine`) | 5.29 M gas | refine gas | **~945** | **cores**: more markets on more cores, linear |
| **Sealed — commit–reveal** (rung 3) | 10.1k gas reveal check (+5.29 M if sig-verified) | refine gas | **~945** | cores |
| **Sealed — encrypt-until-batch** (rung 2, default) | ~n × 18.7 M gas (n = committee size) | refine gas | **~267/n** (n=5 → ~53) | **cores × (267 ÷ n)**. Inverse in committee size: every member proves per order, so a bigger committee buys trust and liveness at the direct cost of throughput. The scaling answer is rung 1 |
| **Sealed — ZK dark-pool** (rung 1, spiked) | ~0: one 260 M-gas proof settles the batch, flat | input size (W_B ≈ 13.15 MiB) | **~27,500–68,900** | cores × prover capacity; on-chain cost flat in order count |

The builder plans each round against the refine budget: at most `ROUND_GAS_BUDGET` = 80%
of `REFINE_GAS` (G_R; default 1e9, tiny — set 5e9 on a full-spec chain), summed over its
orders at the per-kind costs above, as well as at most `MAX_ROUND_ORDERS` orders. For
encrypt-until-batch that is 21 sealed orders per round at n = 2 on tiny (n is read from the
on-chain committee); a round of them that times out is rebuilt with half as many, and the
limit doubles back as rounds land (jamswap#8 — an oversized round never landed and was
rebuilt at the same size, wedging the market).

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
