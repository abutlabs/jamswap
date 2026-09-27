# Cross-client differential — one service, independent clients, one verdict

The same `service/jamswap-service.jam` runs the same trustless scenario on independent
JAM clients, each on its own fresh chain, and the resulting service state is compared
byte for byte. If two clients disagree, one of them has a conformance bug, judged
against the Graypaper (GP 0.8.0), never against the other client, and the scenario is
a minimal reproducer by construction.

**Current result (2026-09-26, GP 0.8.0, jamswap#23):** lasair (a local build of lasair
main at `d88fc0a`) and PolkaJam 0.1.29 (`nightly-2026-09-22`) — **ALL CLIENTS AGREE**:
balance, book and book-after-forgery match byte for byte, and the forged order is
rejected on both (commit 878ea19).

## The scenario

[`differential/differential.py`](../differential/differential.py) drives it; the
scenario and assertions are client-agnostic.

| step | payload | asserted result |
|---|---|---|
| owner-signed registration | `TAG_REGISTER` | a handle, chain-assigned (reported, not compared) |
| market listing + deposit | `TAG_LIST`, `TAG_DEPOSIT` | balance bytes equal |
| **signed order** (ed25519 verified in refine) | `TAG_SMATCH` | resting-book bytes equal (the account field normalized to each lane's handle) |
| **forged order** (wrong key for the account) | `TAG_SMATCH` | book unchanged on both |

## Lanes

Each lane runs standalone and prints its state as JSON; `compare` diffs them. Both
lanes submit and read through the DEX's chain adapter (`offchain/chain.py`).

| lane | client | how |
|---|---|---|
| `lasair` | lasair | inside a lasair net (lasair6): the service is seeded in genesis (`SERVICE_ID`); the adapter's `jamnp` backend (lasair's CE-133 builder and CE-129 reader bridges) |
| `pj` | PolkaJam, black box | `differential/Dockerfile.polkajam`: the public release fetched at build time (never committed), a local `polkajam-testnet`; the service deployed over JIP-2 through the Bootstrap service (`offchain/deploy.py`), items and reads through the adapter's `jip2` backend. `PJ_DEPLOY=jamt` / `PJ_SUBMIT=jamt` use `jamt` instead (A/B checks) |

```sh
# lasair lane, inside the lasair6 network
BUILDER_URL=http://builder:19980 READER_URL=http://reader:19990 SERVICE_ID=100 \
    python3 differential.py lasair > lasair.json
# pj lane (the image's default command starts polkajam-testnet, then runs it)
python3 differential.py pj > pj.json
python3 differential.py compare lasair.json pj.json
```

Adding a lane means a client shim (`deploy`, `item`, `storage`, `poll`); for a client
that serves JIP-2, `storage` is the adapter's `read`. JavaJAM 0.4.3 (GP 0.8.0, serves
JIP-2) has not been tried as a lane yet; the other full nodes listed in #22 (JAM DUNA
and others, at GP 0.7.x) become candidates once they move to 0.8.0. Running N lanes in
one invocation is still open from #15.

## Findings along the way

- **A package anchored before its service exists is dropped silently.** `jamt item`
  right after `create-service` anchored one slot before the creation and vanished; the
  driver waits a few slots after creation. Poll state; don't trust submission receipts.
- **`jamt` hex arguments need a `0x` prefix**; bare hex is read as an ASCII string, and
  the resulting garbage payloads execute as silent no-ops.
- **Use the handle the chain assigns.** lasair6's genesis seeds dev accounts 1–6, so
  the trader got handle 7 while the order still named account 1, and lasair correctly
  refused it. The scenario now reads the assigned handle (878ea19).

## History

**First green run (2026-07-04, GP 0.7.2).** The retired `docker-compose.differential.yml`
rig deployed the service to a lasair node over lasair's old HTTP operator RPC
(`ghcr.io/abutlabs/lasair-node`, since retired) and to a PolkaJam local testnet with
`jamt`; both produced byte-identical state and rejected the same forged order:

```
check                lasair                              polkajam                            verdict
handle               01000000                            01000000                            MATCH ✓
balance              8096980000000000                    8096980000000000                    MATCH ✓
book                 010000000a0000000000350c0050c30000  010000000a0000000000350c0050c30000  MATCH ✓
book_after_forgery   (unchanged)                         (unchanged)                         MATCH ✓
```

The lanes were rewritten for QUIC-era lasair (3f1526b), moved onto the chain adapter
(#10–#13), and re-run at GP 0.8.0 (#23).
