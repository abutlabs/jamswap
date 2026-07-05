# Mixed-client differential testnet — one service, independent clients, one verdict

> **First green run: 2026-07-04.** The same `service/jamswap-service.jam` deployed to a
> lasair node and a PolkaJam local testnet (both GP 0.7.2), driven through the identical
> trustless scenario, produced **byte-identical on-chain state** — and both PVMs rejected
> the same forged order.

```sh
docker compose -f docker-compose.differential.yml up --build --abort-on-container-exit
```

## What it proves (and why it beats static vectors)

Conformance suites test clients against *fixed* vectors. This rig tests them against a
**live application**: real ed25519 verification inside refine, real state writes in
accumulate, real adversarial inputs. If two independent implementations of GP 0.7.2
execute the same service identically down to the byte, that's evidence about both
clients *and* about the service's portability. If they ever disagree, one of them has a
conformance bug — and we have a minimal reproducer by construction.

The scenario each client runs (from `differential/driver.py`):

| step | payload | asserted result |
|---|---|---|
| owner-signed registration | `TAG_REGISTER` | handle bytes equal |
| market listing + deposit | `TAG_LIST`, `TAG_DEPOSIT` | balance bytes equal |
| **signed order** (ed25519 verified in refine) | `TAG_SMATCH` | resting-book bytes equal |
| **forged order** (wrong key for the account) | `TAG_SMATCH` | book unchanged on both |

Verdict table from the first green run:

```
check                lasair                              polkajam                            verdict
handle               01000000                            01000000                            MATCH ✓
balance              8096980000000000                    8096980000000000                    MATCH ✓
book                 010000000a0000000000350c0050c30000  010000000a0000000000350c0050c30000  MATCH ✓
book_after_forgery   (unchanged)                         (unchanged)                         MATCH ✓
```

## What this is NOT (yet): a shared consensus network

The two lanes are **separate chains** running the same service. A single network mixing
clients is gated on transport interop: lasair's testnet gossips over its own TCP
protocol, while PolkaJam (and the other teams' nets) speak **JAMNP-S over QUIC**. That's
lasair M2/M3 client work, not compose plumbing. When JAMNP-S lands in lasair, these
lanes merge into one chain and the byte-comparison becomes consensus itself.

## Lanes

| lane | client | how | status |
|---|---|---|---|
| 1 | **lasair** (ours, OCaml) | `ghcr.io/abutlabs/lasair-node`, HTTP operator RPC | ✅ green |
| 2 | **PolkaJam** (Parity, Rust, binary-only) | public release fetched at image build (never committed — black-box use, see lasair `docs/DISCLOSURES.md`); local `polkajam-testnet` + `jamt` CLI | ✅ green |
| 3 | **JAM DUNA** (`jam-duna/jamtestnet`) | published `jamduna` binary (linux/amd64) + chainspec tooling + JSON-RPC :19800-19805, GP 0.7.2 | 🔜 best next candidate — needs its RPC's service-deploy/work-item surface verified; amd64-only (emulated on arm64) |
| 4 | **TurboJam** (r2rationality, C++) | source-build Dockerfiles upstream; JIP-2 RPC | ⏸ deferred — no prebuilt release, work-item interface unverified |

Adding a lane = implement the two-method client shim in `differential/driver.py`
(`deploy`, `item`, `storage`) against that client's operator interface. The scenario and
assertions are client-agnostic.

## Operational findings (the rig already paid rent)

- **Anchor lag drops packages silently**: a work package anchored on a block where the
  target service doesn't exist yet is dropped with no error — `jamt item` immediately
  after `create-service` anchored one slot before the creation and vanished. The driver
  now waits a few slots post-creation. (Same class of lesson as lasair's "no partial
  credit" fuzzing: distributed pipelines fail silently; poll state, don't trust
  submission receipts.)
- **`jamt` hex arguments need `0x` prefixes** — bare hex is interpreted as an ASCII
  string (payloads submitted as garbage tags execute as no-ops, again silently).
- PolkaJam release pinning: `PJ_RELEASE=nightly-2026-07-04 docker compose ... up`
  (verified against `nightly-2026-06-29`/0.1.28 and `nightly-2026-07-04`).

## Where this goes

1. **Lane 3 (JAM DUNA)** — third independent implementation, same verdict table.
2. **Scenario depth** — sealed rounds (commit–reveal + committee), partial-fill carry,
   market-order band checks: the full `sim/demo.py` matrix, asserted cross-client.
3. **JAMNP-S in lasair** — the lanes become one chain; differential-by-comparison
   becomes differential-by-consensus, and jamswap runs on a genuinely mixed validator
   set with zero changes (the service is already proven client-portable).
