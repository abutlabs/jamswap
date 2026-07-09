# Token design: is the jamswap ledger "ERC-20 enough"?

*Written 2026-07-09, prompted by the question: balances sat at genesis
1,000,000 while trades "settled" — do USDC/DOT/JAMKB need an ERC-20-style
redesign?*

## What we have

Each asset is a row-space in the **jamswap service's own storage**:
`b"b" ‖ asset(u32) ‖ account(u32) → amount(u64 LE)`. Settlement (a round's
accumulate) debits and credits these rows atomically inside one on-chain
state transition. Authorization is ed25519 per account (registered pubkey),
replay-protected by per-account monotonic seq floors and nonces.

## The verdict: the shape is already right

On JAM, **a service IS the token contract**. The service-internal balance
table is the JAM-idiomatic equivalent of an ERC-20's `balances` mapping —
if anything it is *stronger*, because the DEX and the ledger share one
atomic state transition (no approve/transferFrom race, no reentrancy
surface: matching and settlement are the same accumulate).

The balance freeze that motivated this question was **not** a token-model
problem. It was, in order of discovery: rounds never reaching accumulate
(throughput deficit → batched work-packages, lasair `client-v1.7.6`), and
then settlements landing on branches that **lost fork choice** (no
finality → the durable-settlement hold, `SETTLE_HOLD_SECS`). A different
token layout would have frozen identically.

## What ERC-20 has that we lack (the honest gap list)

| ERC-20 property | jamswap today | gap |
|---|---|---|
| `balanceOf` | `b"b"` rows via CE-129 | none |
| `transfer` | only via trade settlement / withdraw-to-treasury | **no peer-to-peer transfer tag** |
| `totalSupply` | not tracked on-chain | **derivable only by summing rows** |
| mint discipline | faucet deposit mints freely (devnet) | **unbounded mint** |
| events/logs | pending ledger + metrics (off-chain) | on-chain events don't exist on JAM; metrics are the substitute |

## What we did now (observability as the invariant-keeper)

The `JAMswap accounts & trading` Grafana dashboard tracks per-account
balances for all three assets and enforces the two invariants that make the
ledger trustworthy **measurably**:

1. **Conservation** — `jamswap_dev_supply{asset}` (sum of dev balances) must
   be flat except for faucet mints. Any other step is a settlement bug or a
   re-org rewriting history.
2. **Durability** — `jamswap_cum_volume` must never drop; a drop means a
   re-org erased settlements. The dex now confirms a round only after its
   settlement survives on-chain for `SETTLE_HOLD_SECS` (60 s, spanning
   PolkaJam's finality ratchet), and counts observed reversions
   (`jamswap_settle_reverted_total`).

## Roadmap (when the mixed chain earns real finality)

1. **On-chain supply keys** — `b"ts" ‖ asset → u64`, updated only by
   faucet/mint and burn paths; conservation becomes exact, not derived.
2. **TAG_TRANSFER** — signed peer-to-peer transfer (canon
   `jamswap:v1:transfer`, nonce-protected); completes the ERC-20 surface.
3. **Bounded faucet** — per-account mint allowance, so devnet economics
   stop being infinite.
4. **Finality gadget (lasair)** — the real fix for durability; the 60 s
   hold is the honest stopgap until history can be pinned.

These need a service-blob rebuild (new genesis), so they ship together in a
`jamswap-service` v2 rather than piecemeal.
