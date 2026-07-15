# Which net is which (and why there are so many compose files)

**TL;DR — you almost always want `./dex up`.** That runs the all-lasair finality net,
the only one where sealed orders settle durably. Everything below is here so the other
`docker-compose.*.yml` files aren't a mystery — most are consensus *research*, not the DEX.

## Just run the DEX

```bash
./dex rebuild   # only if you changed the on-chain service (service/src/lib.rs)
./dex up        # start it, no auto-load, wait for finality → http://localhost:8081
./dex status    # finality + market at a glance
./dex load      # optional: start the load generator for a soak
./dex down      # tear down + wipe
```

`./dex` wraps `docker-compose.lasair6.yml`. That's the canonical net. The DEX code
(`offchain/`) and the on-chain service (`service/jamswap-service.jam`) are **bind-mounted**,
so your local changes are always live — only `service/*.jam` needs a `./dex rebuild`.

## The compose files, one line each

| File | Net | UI | What it's for |
|---|---|---|---|
| **`docker-compose.lasair6.yml`** | 6× lasair, **β-finality** | **:8081** | **THE DEX net.** Sealed orders settle durably (finality → no re-orgs). Use via `./dex`. |
| `docker-compose.yml` | default (published image) | :8080 | Quickstart demo — one command, no lasair source. No finality → sealed fills not durable. |
| `docker-compose.mixed.yml` | 3× lasair + 3× PolkaJam | :8090 | **Consensus research** — cross-client interop. 3:3 can't finalize (see below); DEX won't clear here. |
| `docker-compose.mixed-dex.yml` | mixed, lasair-dominant | (overlay) | Older "make the mixed net settle" experiment; superseded by the all-lasair net. |
| `docker-compose.pj-majority.yml` | 6× PolkaJam GRANDPA | — | The cross-client **finality bridge** test (lasair follows pj's finalized head). Research. |
| `docker-compose.monitor.yml` | overlay (mixed) | Grafana :3010 | Prometheus + Grafana on the mixed net. |
| `docker-compose.lasair6-monitor.yml` | overlay (lasair6) | Grafana :3010 | Prometheus + Grafana on the DEX net. |
| `docker-compose.load.yml` | overlay | — | Standalone load generator layer. `./dex load` already covers the common case. |

## The `Makefile` still works

`make up`, `make mixed`, `make monitor`, etc. are the older entry points and still valid —
`./dex` just wraps the one you want 95% of the time so you don't have to remember
`-p lasair6 -f docker-compose.lasair6.yml`.

## Why the DEX needs the all-lasair net (the finality story)

Settlement is durable only under **finality**: once a block is β-finalized it can't be
re-orged, so a filled order can't be un-filled. Finality is a GRANDPA-style gadget that
needs a **≥2/3+1 supermajority** of validators to agree.

- On the **3:3 mixed net**, lasair controls 3 and PolkaJam controls 3 — neither reaches
  2/3+1, so nothing finalizes and settlements can snap back. That's not a bug in either
  client; it's the BFT threshold.
- On the **all-lasair net**, all six speak lasair's finality gadget → 5-of-6 quorum →
  finalizes. That's why the DEX lives here.

### Is PolkaJam "not following the spec"? No — the finality *wire protocol* is unspecified.

Precision matters here (checked against the primary sources 2026-07-15). Finality IS
partially specified:

- **The Graypaper** (§"Grandpa and the Best Chain") says nodes "take part in the GRANDPA
  protocol as defined by [the GRANDPA paper]", names the vote data — the best block's
  header **plus its posterior state root** — and requires a block be audited before
  voting to finalize it.
- **JAMNP-S** gives every node a spec way to *announce* its result: the UP-0 handshake
  `final` field (finalized header hash + slot). That field is what our bridge reconciles.

What is **missing is the wire layer for the votes themselves** — JAMNP-S defines streams
CE-128..148 + UP-0 (blocks, state, tickets, work-packages, shards, judgments) and **no
stream for finality votes or justifications**. Concretely unspecified: the CE stream
number, the vote message encoding, the exact signed byte layout (incl. domain separation),
and round/voter-set/justification machinery. So both clients filled that gap with their
*own* private extension:

- **PolkaJam**: the Parity `finality_grandpa` crate — multi-round, set-ids, commit certs,
  over a private `SEND FIN`/`RECV FIN` stream.
- **lasair**: a single-round, state-root-bound commit over a private CE-192 stream.

Neither is non-conformant — both plausibly "take part in GRANDPA" per the Graypaper; there
is just no shared ballot format to conform to. They're **different private protocols**, so
their votes can't count toward each other's quorum.

### So how do you get finality parity across clients in different languages?

Two honest paths (from `lasair/docs/FINALITY_PLAN.md`, Phase 3b):

1. **A shared finality stream in the spec.** If JAM standardizes a β-commit message format
   + a CE stream number, every client implements the *same* wire protocol and votes count
   cross-client — then a real 3:3 mixed net finalizes. This is the proper fix, and it's
   **blocked on JAM specifying it** (reverse-engineering PolkaJam's private stream would
   break the clean-room rule).

2. **Agree on the *result*, not the votes** (what lasair built, and what's shippable today).
   Each client runs its own gadget internally, and they reconcile via the **one spec field
   that already exists** — the JAMNP-S UP-0 handshake `final` field, where every conformant
   client advertises its own finalized head hash. lasair's finality bridge reads a peer's
   advertised finalized head and checks it **byte-for-byte** against lasair's own
   independently-finalized block at that slot. "AGREE every round, identical hash" = both
   clients, in different languages, independently finalized the *same* block. Parity is
   defined as **agreement on what was finalized**, verified over a standard field — not
   identical vote gossip.

The bottom line: cross-client finality *parity* doesn't require identical vote messages; it
requires (a) each client reaching its own supermajority and (b) a spec-standard way to
advertise + cross-check the finalized head. (2) works now; (1) is the long-term convergence
once JAM specs a shared stream.
