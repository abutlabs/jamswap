# Local builder mode — run your own, trust no one

> **Status: works today** (verified e2e 2026-07-03 — two independent builders on one
> deployed service; see "Verified" below). Packaging it as a one-click desktop app is
> open work; the architecture and the env vars already exist.

The jamswap **service** is on-chain, permissionless, and controlled by nobody: matching
rules, settlement, custody, and every signature check live in the `.jam` blob the
network executes and audits. The **builder** is the replaceable off-chain layer above
it — the process that collects orders, assembles each auction's work package, and
serves the trading UI (`offchain/server.py` is both). The builder holds **zero
privilege**: the service verifies everything it submits (per-order signatures in
refine, replay floors, the hash-bound book, owner-signed sealed commitments), so
builders are *contestable infrastructure*, like Ethereum block builders or a rollup's
sequencer — or like Uniswap Labs' front-end vs the Uniswap contracts.

That means anyone can run their own:

```sh
SERVICE_ID=<deployed id> LASAIR_RPC=<any node RPC> PORT=8081 python3 offchain/server.py
# your own builder + UI at http://localhost:8081, attached to the SAME on-chain service
```

## Why you'd want to

In rung-3 sealing (commit–reveal, the default), the builder you submit through holds
your reveal preimage — so **the operator of that builder can see your resting sealed
orders** (nobody else can; on-chain there is only a hiding commitment). Run the builder
yourself and that exposure vanishes: **your preimages never leave your machine.**
Nobody — not the hosted operator, not other builders, not validators — sees your
resting sealed terms, and you control your own reveal timing. Bonus: with your key in
the running app, carried remainders of partial fills can be **owner-signed** rather
than allowance-gated, removing that documented asterisk entirely for local users.

## The tiers

| Tier | Install | Who can see your resting sealed orders | Sealed matching | Liveness need |
|---|---|---|---|---|
| **Browser + hosted builder** | none | the hosted operator (like any exchange) | vs the shared public book + the host's sealed pool | none — fire and forget |
| **Local builder (this doc)** | run the app | **no one** | vs the shared public book only | your app must be online to reveal/clear |
| **Rung 2 committee** (opt-in, [`COMMITTEE_DEPLOYMENT.md`](COMMITTEE_DEPLOYMENT.md)) | none extra | no single party (t-of-n committee) | **shared sealed mempool** — sealed-vs-sealed across all builders | none |
| **Rung 1 ZK** (spiked) | none extra | no one, ever | everything, privately | prover |

Every tier is permissionless — no asks of validators, client teams, or anyone else.

## Honest trade-offs

- **Sealed-vs-sealed doesn't cross between builders.** A builder can only reveal
  preimages it holds, so your local sealed order matches against the **shared public
  book** (on-chain, common to all builders) — never against another builder's hidden
  pool. Maximum privacy fragments sealed liquidity into pools of one. Say it plainly:
  local mode gives you *"work the public book privately"*, not a private order book.
- **Online-to-clear.** Your sealed order can only reveal while your app runs. Close the
  laptop and it rests — safe, hidden, inert. Offline sealing is exactly what rung 2's
  committee buys; the two modes are complements, not substitutes.
- **Round contention.** Multiple builders race to run a market's auctions. The
  service's book-hash binding serializes them fail-closed (a round built against a
  stale book is rejected, funds untouched — verified), but a losing round wastes its
  submitter's coretime. Fine at prototype scale; at real scale a convention helps
  (hosted builder runs the housekeeping cadence; local apps submit when they have
  something to clear).

## The rung-2 upgrade path (and one semantics shift to be honest about)

Rung 2 turns the per-builder dark pools back into **one shared sealed mempool**: the
local app encrypts orders client-side to the committee's on-chain joint key and posts
the owner-signed ciphertext; since ciphertexts live on-chain, **any builder can include
any of them blindly** — no builder ever sees plaintext, and two traders on two
different local builders can finally meet sealed-to-sealed.

The shift: a blind builder can't know which ciphertexts would cross, so included
ciphertexts are decrypted **at the batch close they enter** — and because work-package
data is public, a decrypted order's terms are exposed at that close *whether or not it
traded*. Blind-shared rung 2 therefore means "sealed until the batch you enter", not
"sealed until you actually trade". Practical mitigation: the local app knows its own
terms and watches the public book/last price, so it can time its entry to batches it
expects to cross. Restoring full rest-hidden-until-cross *with* shared sealed matching
is precisely rung 1's job (the crossing check moves inside the ZK proof).

## Verified (2026-07-03)

Two builders attached to one deployed service (hosted compose + a local
`SERVICE_ID=…` instance):

- the hosted builder's mempool showed **zero** knowledge of the local builder's sealed
  order (only the commitment on-chain);
- both builders' automatic 6 s auction loops coordinated through the shared on-chain
  book with no manual choreography;
- a sealed sell held by the local builder cleared against a public buy placed through
  the hosted builder — cross-builder settlement, fee-exact.

## Open work

- [ ] Package as a desktop app (Tauri/Electron wrapping server.py + UI) with key
      import/export — today it's a `python3`/`docker` invocation.
- [ ] Light-client or multi-node RPC reads (today the app trusts one node's RPC for
      chain reads; writes are verified on-chain regardless).
- [ ] Multi-builder round-cadence convention (avoid wasted coretime on races).
- [ ] Client-side encryption for the rung-2 path (ciphertext leaves the app, plaintext
      never does) + entry-timing heuristics.
