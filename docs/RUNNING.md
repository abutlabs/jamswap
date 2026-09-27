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

### Run it on any JIP-2 node — runtime deploy, no lasair, no `jamt`

On a chain that has a Bootstrap service (id 0, e.g. PolkaJam's `--chain dev`) the DEX
deploys itself: start `offchain/server.py` on the JIP-2 backend with **no `SERVICE_ID`**
and it creates the service through the Bootstrap service, provides the code with JIP-2
`submitPreimage`, then lists the default markets and registers and funds the six dev
accounts with ordinary work-items (what genesis does on a lasair net). The id is kept in
`DEPLOY_STATE` (default `/tmp/jamswap_deploy.json`); a restart reuses the service (so
does a restart without the file: a service already running this code is reused).

```sh
polkajam --chain dev dump-spec /tmp/spec.json      # the authorizer comes from its genesis
CHAIN_BACKEND=jip2 CHAIN_RPC=ws://localhost:19800 CHAIN_SPEC=/tmp/spec.json \
    python3 offchain/server.py                     # deploys, sets up, serves :8080
# or just deploy + set up, and print SERVICE_ID=<id>:
python3 offchain/deploy.py --rpc ws://localhost:19800 --chain-spec /tmp/spec.json
```

`DEX_SETUP=0` skips the markets and accounts (`1` runs them on a genesis-seeded service
too); `GENESIS_BALANCE` sets the funding per asset (display units, default 1,000,000);
`DEPLOY_SERVICE_ID` asks for an id, `DEPLOY_FRESH=1` never reuses. The API opens once
the treasury's JAMKB reserve deposit has landed (`RESERVE_WAIT_SECS`, default 120), so the
first order is not refused as under-reserved. How the Bootstrap
instruction was established is in [`offchain/deploy.py`](../offchain/deploy.py).
lasair nets keep the genesis-seeded service: lasair has no Bootstrap service or JIP-2
server yet (lasair#68, #69).

Point `CHAIN_RPC` at a node that forwards work-packages. A PolkaJam **validator**'s RPC
does not (it answers `submitWorkPackage` with "Failed to submit work-package to even a
single proxy/guarantor"); an ordinary PolkaJam node joined to the same net does, as do
`polkajam-testnet`'s RPC nodes.

On JIP-2 the service's footprint is readable (`serviceData`), so the JAMKB standard's
backpressure is live: the reserve seeded at startup (obligation + `JAMKB_RESERVE_BUFFER`,
8 KB) is outgrown after ~15 min of steady trading (a landed-round marker lives an hour),
and from then on **every new order is refused** ("service under-reserved on JAMKB").
`RESERVE_TOPUP=1` runs the beneficiary's capped top-up as a keeper: every
`RESERVE_TOPUP_SECS` (15) it tops the reserve up to its target once it has fallen half a
buffer short, one deposit in flight at a time, resent under the same nonce if it has not
landed in 60 s. Leave it off where someone else funds the reserve (docs/JAMKB_STANDARD.md).

### Run it on six PolkaJam validators — no lasair anywhere (`pj6`)

The same runtime deploy as a test net, one command (docs/NETS.md, "The DEX with no
lasair"): six PolkaJam validators with GRANDPA on a shared genesis, an ordinary PolkaJam
node as the DEX's gateway, the DEX on JIP-2 (`RESERVE_TOPUP=1`), a load generator and
netwatch.

```sh
./dex up NET=pj6              # builds, mints genesis, deploys + sets up → http://localhost:8201
./dex load NET=pj6            # drive it (PROFILE / RATE / SEALED_RATIO as for lasair6)
./dex status NET=pj6          # one head + finality across the six, and the market
./dex soak NET=pj6 3600       # A1-A4: 1 h of load, parity, the soak verdict (exit 0 = pass)
./dex down NET=pj6            # tear down, wipe the chain
```

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
`make mixed-local && make verify-mixed` (it watches every node for `VERIFY_SECS`,
default 360 s, then judges) — only then tag `client-vX.Y.Z` and let CI publish.
`verify-mixed` runs `offchain/netwatch.py` against each node's public interface
(JIP-2 for PolkaJam, the metrics endpoint for lasair) and asserts one head, liveness,
every validator credited blocks in the on-chain statistics, and peers; see
`mixed/verify.sh` for the knobs (`LAYOUT`, `NETWATCH_NODES`, `VERIFY_ARGS`).

### Monitoring the mixed network

```sh
make monitor        # mixed net + Prometheus + Grafana; dashboards on :3010, no login
make monitor-down
```

Metric sources, client-neutral first:

- **netwatch (`offchain/netwatch.py serve`, issue #15)** — one poller for every node
  of any net: JIP-2 (`bestBlock`, `finalizedBlock`, `parent`, `syncState`,
  `statistics`) for nodes that serve it, the Prometheus gauges for lasair until it
  does (lasair#68). It exports `jam_best_slot`, `jam_finalized_slot`,
  `jam_head_lag_slots`, `jam_finality_lag_slots`, `jam_head_agree` /
  `jam_final_agree` (hash agreement at the common slot), `jam_peers`, `jam_node_up`
  (all `{node, client}`), net-wide `jam_net_*` (distinct heads, one-head flag,
  divergence length) and `/verdict` (the same judgement `netwatch.py poll` prints).
- **The apples-to-apples baseline: on-chain validator statistics (GP π)**,
  decoded by netwatch from JIP-2 `statistics` (GP 0.8.0 C(13)) — per-validator
  blocks / tickets / guarantees / assurances as recorded by CONSENSUS, identical
  from any node, covering every client's validators (`jam_pi_*`).
- **lasair's native `/metrics`** (≥1.6.4, `--metrics-port`): blocks
  authored/imported, import rejects by STF reason, peers, per-peer dial failures,
  QUIC accepts/errors, Safrole tickets, the CE-133 pipeline. The dashboards show
  these in collapsed **lasair overlay** rows — optional detail, empty on a net
  without lasair.
- `monitor/exporter.py` (legacy): PolkaJam log scraping through the Docker socket.
  The monitor image runs it only while `NETWATCH_NODES` is unset.

Provisioned dashboards: **JAM network** (one-head / finality / divergence verdicts,
per-node lag and agreement, the π consensus row), **JAM clients** (the same,
averaged by `client`), **JAM node** (any node, with a selector), **JAM finality**
(A2: finalized head advancing with one hash), plus the DEX's **JAMswap service** and
**accounts & trading** boards. Dashboards are generated by
`monitor/grafana/gen_dashboards.py` — edit that, not the JSON.
Prometheus itself is on :9090.

Sealing defaults to commit–reveal (rung 3 — the permissionless base state). To opt in to
the rung-2 committee (encrypt-until-batch, simulated committee), uncomment
`ENC_MODE: "1"` under the `dex` service in `docker-compose.yml`. Rounds are sized to the
refine budget of a tiny chain (G_R = 1e9); on a full-spec chain set `REFINE_GAS: "5e9"`
there too ([`THROUGHPUT.md`](THROUGHPUT.md)).

| Your machine | What runs | Notes |
|---|---|---|
| **Linux / amd64** (Intel/AMD) | native | — |
| **Apple Silicon** (M1–M4, arm64) | native | the image is built for arm64 too |
| **Windows / WSL2** (amd64) | native | run inside a WSL2 Linux shell |
| **arm64 without an arm64 image yet** | emulated | add `--platform linux/amd64` (slower, but works) |

> **Running your own JAM node?** Jamswap is a fully self-contained JAM **service** —
> nothing is baked into the client. Any conformant node that speaks JAMNP-S (CE-133
> work-package submission, CE-129 storage reads) can host it and run the same flow.
> Build the blob yourself with `./dex rebuild` (GP 0.8.0, via `tools/jam080`). lasair is
> just the node we ship it on.

---

