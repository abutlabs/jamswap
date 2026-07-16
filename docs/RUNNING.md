# Running Jamswap — every mode

> Moved from the README (2026-07-16). The one-shot everyone wants is `./dex up`
> (see the README); this doc covers every other way to run it — the single-node
> quickstart, the mixed lasair+PolkaJam research nets, local source builds,
> monitoring, and platform notes. `docs/NETS.md` explains which net is which.

## Try it in one command

You **don't need the JAM client's source code.** Everything chain-side runs from one
published, **multi-arch** image (`ghcr.io/abutlabs/lasair`). Clone this repo and:

```sh
docker compose up            # trading UI at http://localhost:8080
```

**All networking is spec JAMNP-S over QUIC** — in the single node here and in the
networked testnet below. Orders reach the chain as work-packages over **CE-133**, state
is read back over **CE-129**, and the service is **seeded into genesis** — there is no
client-specific HTTP node RPC anywhere.

| Compose file | Run it | Scenario |
|---|---|---|
| [`docker-compose.yml`](docker-compose.yml) | `docker compose up` | **Quickstart** — one lasair process authors all six dev validators' slots and hosts the service; a CE-133 builder and a CE-129 reader bridge the DEX to the chain. Trading UI at `:8080`; nothing to build. |
| [`docker-compose.mixed.yml`](docker-compose.mixed.yml) | `docker compose -f docker-compose.mixed.yml up` | **Networked testnet — mixed-client** — six validators split across **two independent JAM clients** (lasair + PolkaJam) co-authoring one Safrole chain over JAMNP-S/QUIC, leadership rotating across clients. The jamswap service is in the shared genesis; `make mixed-dex` settles trades on-chain, `make mixed` runs the equal-split consensus comparison — see [the section below](#run-it-on-a-mixed-client-chain--lasair-and-polkajam-one-command). |

The quickstart serves the **trading UI** on top of that chain (the compiled
`service/jamswap-service.jam` ships in the repo). Open `http://localhost:8080` and you can:

1. **Create an account** — an ed25519 keypair your browser holds (exportable/importable).
2. **Fund it** in the Faucet tab — assets are **USDC, DOT, JAMKB**, trading across three
   pairs (**DOT/USDC, JAMKB/USDC, JAMKB/DOT**).
3. **Place an order** — Buy/Sell, Limit or Market. Tick **🔒 Seal** to hide it.
4. **Watch it clear** — auctions run **every 6 seconds** automatically; a live countdown
   shows the next one. Watch the order book, the mempool, and your balances update.

Toggle the **mempool** view to see the data actually sitting in the service: open orders
are tagged 🌐 LIMIT / ⚡ MARKET (terms visible) or 🔒 SEALED (only a commitment on-chain,
terms hidden until they clear).

### Run it on a MIXED-client chain — lasair **and** PolkaJam, one command

The quickstart above runs one client. JAM's real promise is a network of
**different** client implementations agreeing on one chain. This compose runs exactly
that: six validators split across **two independent JAM clients** — [lasair](https://github.com/abutlabs/lasair)
(our OCaml client) and **PolkaJam** (Parity's) — co-authoring **one** Safrole chain,
with **leadership rotating across clients** and each client re-executing the other's
blocks to a byte-identical state root.

```sh
docker compose -f docker-compose.mixed.yml up
```

That's it — one line brings up a **multi-architecture** (Apple Silicon **and** Intel
Linux) mixed-client JAM testnet:

- `pj0 pj1 pj2` — PolkaJam validators (indices 0,1,2)
- `lm3 lm4 lm5` — lasair validators (indices 3,4,5)
- `spec-init` — mints the **shared genesis** both clients load (identical bytes → identical state root)
- `watch` — prints the chain advancing

Watch leadership rotate across clients, and confirm both agree on state:

```sh
# who authored each block — lasair's slots (val 3/4/5) interleave with PolkaJam's
docker compose -f docker-compose.mixed.yml logs lm3 lm4 lm5 | grep authored

# both clients on ONE chain: a lasair-authored block, re-derived by PolkaJam to the
# SAME state root (RPC on the host):
docker compose -f docker-compose.mixed.yml logs watch          # PolkaJam's view of the chain
```

Typical output — a single chain whose blocks alternate authorship:

```
lm5 | 🚀 authored slot 7918603 (val 5) height 1 …
lm4 | 🚀 authored slot 7918606 (val 4) height 4 …
lm3 | 🚀 authored slot 7918614 (val 3) height 12 …
      (PolkaJam authored heights 2,3,5,6,7,9,10,11 in between)
CROSS-CLIENT ROTATION — both clients co-author one chain; PolkaJam re-derives
every lasair-authored block's state root: MATCH ✓
```

**How it works, and what it proves.** Both clients load one operator-defined genesis
(`gen-spec`), whose validator set carries each node's real keys — PolkaJam's for
indices 0–2, lasair's for 3–5. Each node authors **only its own** Safrole slots (the
leader is resolved from on-chain state, so a node signs a slot *iff* it owns that
slot's leader) and imports every other slot over the **spec JAMNP-S/QUIC** transport
both clients speak. Because both are GP-v0.7.2-conformant, they agree on the fallback
leader schedule and re-execute to identical state. It's the strongest possible
interop result: two from-scratch client implementations running **one** blockchain.

**Options.**

build-local expects the private lasair checkout as a sibling of jamswap (../lasair); point elsewhere with make build-local LASAIR_SRC=/path/to/lasair.
```sh
make build-local                                                      # build a new lasair image for local use
docker build -f ../lasair/Dockerfile.mesh -t lasair:local ../lasair   # Docker equivalent
```

```sh
# use a specific published lasair client image, or your locally-built one:
LASAIR_IMAGE=ghcr.io/abutlabs/lasair:0.1.0 docker compose -f docker-compose.mixed.yml up
LASAIR_IMAGE=lasair:local                  docker compose -f docker-compose.mixed.yml up   # built from the lasair repo

# pin the PolkaJam release fetched (black-box) at build time:
PJ_RELEASE=nightly-2026-07-04 docker compose -f docker-compose.mixed.yml up

# change the client split (which indices each client owns):
LAYOUT=lasair,lasair,polkajam,polkajam,lasair,polkajam docker compose -f docker-compose.mixed.yml up
```

> **Two mixed modes.** The jamswap **service** is deployed into the shared genesis of
> the mixed chain (both clients start with it on-chain), and there are two ways to run it:
>
> - **`make mixed`** (this compose) — an **equal 3 PolkaJam / 3 lasair** split: a
>   *consensus-comparison* testbed where both clients author, seal (Safrole tickets), and
>   import each other's blocks apples-to-apples — what the Grafana dashboards measure. The
>   DEX UI is live and work-items are *guaranteed*, but trades **don't settle on-chain**:
>   a work-report only accumulates once it is *available* (a >2/3 super-majority of
>   assurances on the canonical branch within the 5-slot window), and only lasair can
>   produce those assurances — on a contested 3:3 chain its guarantee/assurance blocks
>   lose the fork-choice race before the window closes.
> - **`make mixed-dex`** — a **lasair-dominant** overlay where lasair authors the
>   canonical chain, so reports become available and **register / deposit / withdraw
>   accumulate on-chain**. PolkaJam (pj0) still runs the independent client and derives
>   the same state; it just authors negligibly. This is the mixed chain running the **full
>   DEX trading flow**. See [`docker-compose.mixed-dex.yml`](docker-compose.mixed-dex.yml)
>   for the why. (Trades settle once the chain reaches Safrole ticket-seal steady state,
>   ~1–2 epochs after launch.)
>
> The single-client quickstart above (`docker compose up`) also runs the full trading flow.

> **On PolkaJam & compliance.** PolkaJam is used **black-box**: its binary is fetched
> from the public [`paritytech/polkajam-releases`](https://github.com/paritytech/polkajam-releases)
> at image-build time on *your* machine and is never committed or redistributed. The
> lasair client image is a normal multi-arch pull. See
> [`mixed/`](./mixed) and lasair's [`docs/MIXED_CLIENT_NETWORK.md`](https://github.com/abutlabs/lasair/blob/main/docs/MIXED_CLIENT_NETWORK.md).

### Options

```sh
LASAIR_TAG=1.6.2 docker compose up              # pin the client version instead of :latest
LASAIR_IMAGE=lasair:local docker compose up     # any image ref — e.g. a local source build
```

### Dev modes (Makefile)

Public images by default; a local lasair source build on demand — so a lasair change
can be verified end-to-end BEFORE tagging a release and waiting for the ~80-min
multi-arch CI publish. Requires the (private) lasair checkout next to this repo
(override with `LASAIR_SRC=…`):

```sh
make up             # default DEX stack, published image        (docker compose up)
make mixed          # mixed net, EQUAL 3 PolkaJam / 3 lasair (consensus comparison)
make mixed-dex      # mixed net, lasair-dominant — DEX SETTLES TRADES on-chain
make local          # build ../lasair -> lasair:local -> DEX stack
make mixed-local    # same source build -> equal-split mixed net
make mixed-dex-local# same source build -> functional-DEX mixed net
make verify         # e2e smoke test against the RUNNING DEX stack (works on mixed-dex too)
make verify-mixed   # health check against the RUNNING mixed net
make down           # stop whichever stack is up
```

Pre-push flow for a lasair change: `make local && make verify`, then
`make mixed-local && make verify-mixed` (it waits for enough slots by itself) —
only then tag `client-vX.Y.Z` and let CI publish.

### Monitoring the mixed network

```sh
make monitor        # mixed net + Prometheus + Grafana; dashboards on :3010, no login
make monitor-down
```

Three metric sources, best-available per client:

- **lasair ≥1.6.4 is natively instrumented** — every node serves Prometheus
  `/metrics` itself (`--metrics-port`, on by default in the image): blocks
  authored/imported, import rejects by STF reason, height/slot, peers, per-peer
  dial failures, QUIC accepts/errors, Safrole tickets, the CE-133 pipeline.
- **PolkaJam is a black box** (no Prometheus endpoint — probed) — a stdlib-only
  exporter (`monitor/exporter.py`) derives its metrics from container logs
  (Docker socket, read-only; local dev tooling only) and its JSON-RPC
  (`bestBlock`, `finalizedBlock`, `syncState`).
- **The apples-to-apples baseline: on-chain validator statistics (GP π)**,
  decoded from the RPC `statistics` call — per-validator blocks / tickets /
  guarantees / assurances as recorded by CONSENSUS, identical from any node,
  covering both clients' validators. What the chain credited each validator
  with, not what a client says about itself.

Two provisioned dashboards: **JAM mixed network** (head slot, finality lag,
authoring rotation live, per-validator totals, faults, and the π consensus row)
and **JAM node** (per-lasair-node deep dive with a node selector). Dashboards are
generated by `monitor/grafana/gen_dashboards.py` — edit that, not the JSON.
Prometheus itself is on :9090.

Sealing defaults to commit–reveal (rung 3 — the permissionless base state). To opt in to
the rung-2 committee (encrypt-until-batch, simulated committee), uncomment
`ENC_MODE: "1"` under the `dex` service in `docker-compose.yml`.

| Your machine | What runs | Notes |
|---|---|---|
| **Linux / amd64** (Intel/AMD) | native | — |
| **Apple Silicon** (M1–M4, arm64) | native | the image is built for arm64 too |
| **Windows / WSL2** (amd64) | native | run inside a WSL2 Linux shell |
| **arm64 without an arm64 image yet** | emulated | add `--platform linux/amd64` (slower, but works) |

> **Running your own JAM node?** Jamswap is a fully self-contained JAM **service** —
> nothing is baked into the client. Any conformant node that speaks JAMNP-S (CE-133
> work-package submission, CE-129 storage reads) can host it and run the same flow.
> Build the blob yourself with `cd service && jam-pvm-build -m service`. lasair is
> just the node we ship it on.

---

