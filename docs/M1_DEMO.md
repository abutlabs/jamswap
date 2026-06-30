# M1 — the matching engine clears in Refine on lasair (proven)

> The "impossible thing, proven": a real order-book matching engine running
> trustlessly on-chain. This is the demo that justifies the whole DEX (PLAN.md §7).

## What was proven

The `marmalade-service` JAM service (`service/`, built with `jam-pvm-build -m
service`) was deployed to a `lasair-node` and fed a sealed batch of orders. Its
`refine` ran the frequent-batch-auction uniform-price clearing
(`crates/match-engine`) and its `accumulate` recorded the outcome on-chain.

**Batch:** buy#1 @105×5, buy#2 @100×5, sell#3 @100×10.

| Result | Value |
|---|---|
| Clearing price `p*` | **100** (maximizes matched volume) |
| Matched volume | **10** |
| Fills | #1→5, #2→5 (both buys full at the uniform price), #3→10 (sell full) |
| **`refine_gas`** | **7,476** — ~0.00015% of the 5e9 refine budget |
| **Determinism** | **byte-identical** refine output across re-runs ✓ |
| Settlement (`accumulate` → storage) | `last_price=100, last_volume=10, rounds, cum_volume` |

## Why each result matters

- **Correct uniform price + fills** — everyone trades at `p*=100`; the buyer who
  bid 105 still pays only 100 (no maker/taker spread → the flat-fee model is honest).
- **7,476 gas** — the matching engine is *cheap*. Compared to the ~56M gas a ZK
  proof verify costs, a batch of thousands of orders fits comfortably in one refine.
  This is the empirical refutation of "no chain can run a matching engine."
- **Byte-identical re-execution** — the existential property: every JAM validator
  auditing the work-package re-runs `refine` and must get the same bytes. Integer-only
  + deterministic tie-breaks deliver it.

## Architecture answer (the open question from the brief)

**Marmalade is a self-contained JAM service — nothing needs to be baked into
Lasair.** `refine` = matching engine, `accumulate` = settlement, service state =
the books. Lasair is the *cost moat* (we make it the cheapest node to clear
auctions — PLAN.md §3.9), not a functional dependency: the `.jam` runs on any
conformant JAM node.

## Reproduce

```sh
# build the service blob
cd service && jam-pvm-build -m service          # -> marmalade-service.jam

# deploy to a lasair-node and submit a batch (13 bytes/order, LE:
#   id:u32 ‖ side:u8(0=buy,1=sell) ‖ price:u32 ‖ qty:u32)
#   POST /v1/service {jam_hex}                  -> service_id
#   POST /v1/service/<id>/item {payload_hex}    -> refine_output = price‖volume‖fills
# read settlement: GET /v1/service/<id>/storage/<hex("last_price"|"rounds"|...)>
```

## Next (PLAN.md phases)

Phase 2 settlement (per-fill balance ledger + custody), Phase 3 deposits/
withdrawals, Phase 4 encrypted orders (MEV-resistance — the hard part), Phase 5
multi-market parallelism across cores, then off-chain infra + the trading UI.
