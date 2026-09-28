# Running Jamswap — every mode

The one-shot is `./dex up` (see the README). This doc covers every mode: the two DEX
nets, the DEX on any JIP-2 node, the single-node quickstart, the mixed-client research
net, local lasair builds, monitoring and platform notes. [`NETS.md`](NETS.md) says
which net is which.

## How the DEX reaches the chain

`offchain/server.py` (the round builder, API and UI) reaches the chain only through
[`offchain/chain.py`](../offchain/chain.py). `CHAIN_BACKEND` picks the backend:

| Backend | Talks to | The service | Used on |
|---|---|---|---|
| `jip2` | a node's JIP-2 RPC at `CHAIN_RPC` (default `ws://localhost:19800`): heads and finality, service reads at the best or finalized block, and `submitWorkPackage` with a GP 0.8.0 work-package it builds (authorizer from the JIP-4 chain spec at `CHAIN_SPEC`) | deployed at startup through the Bootstrap service, or named by `SERVICE_ID` | pj6 (a PolkaJam gateway node), lasair6 and lasair-pj (the first lasair node's `lasair-reader`, lasair ≥ 2.1.0), any JIP-2 node |
| `jamnp` (default) | lasair's bridges: `BUILDER_URL` (CE-133 submit), `READER_URL` (CE-129 read at the node's head), `NODE_METRICS_URL` (heads and finality from lasair's gauges) | seeded into genesis (`SERVICE_ID`) | the quickstart, the mixed net; lasair6 and lasair-pj with `LASAIR_DEX_BACKEND=jamnp` (a lasair image before 2.1.0) |

The service blob, `service/jamswap-service.jam` (GP 0.8.0), is the same on both.
The API opens once the default markets are listed on chain (`LIST_WAIT_SECS`,
default 300): a net that has just started may refuse work for its first minute. On a
lasair net the DEX runs `jip2` on a `lasair-reader` (JIP-2 on its HTTP port, lasair#68;
proven and finalized reads, lasair#70; `submitWorkPackage` to `LASAIR_RPC_GUARANTORS`)
with `SERVICE_ID=100`: the service stays seeded into genesis, since a lasair guarantor
refines only the service it hosts (#26).

## The DEX nets

`./dex up` starts lasair6 (six lasair validators), `./dex up NET=pj6` pj6 (six PolkaJam
validators, no lasair) and `./dex up NET=lasair-pj` lasair-pj (three of each, lasair
guaranteeing the DEX's work). All finalize under GRANDPA, and every validator holds only
its own key (`LASAIR_DEV_ALL_KEYS=1` puts lasair nodes in lasair's devnet mode instead);
the README has the walkthrough and [`NETS.md`](NETS.md) the details. Every verb takes
`NET=`; for pj6:

```sh
./dex up NET=pj6              # builds, mints genesis, deploys + sets up → http://localhost:8201
./dex load NET=pj6            # drive it (PROFILE / RATE / SEALED_RATIO, as on lasair6)
./dex status NET=pj6          # one head + finality across the six, and the market
./dex soak NET=pj6 3600       # A1-A4: 1 h of load, parity, the soak verdict (exit 0 = pass)
./dex down NET=pj6            # tear down, wipe the chain
```

## The DEX on any JIP-2 node (runtime deploy)

On a chain that has a Bootstrap service (id 0, e.g. PolkaJam's `--chain dev`) the DEX
deploys itself: start `offchain/server.py` on the JIP-2 backend with **no `SERVICE_ID`**
and it creates the service through the Bootstrap service, provides the code with JIP-2
`submitPreimage`, then lists the default markets and registers and funds the six dev
accounts with ordinary work-items. The id is kept in `DEPLOY_STATE` (default
`/tmp/jamswap_deploy.json`); a restart reuses the service, and so does a restart without
the file (a service already running this code is reused). No `jamt` is needed.

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
first order is not refused as under-reserved. How the Bootstrap instruction was
established is in [`offchain/deploy.py`](../offchain/deploy.py). This path is verified on
PolkaJam; lasair nets keep the genesis-seeded service (above).

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

## Single-node quickstart: `docker compose up`

```sh
docker compose up            # trading UI at http://localhost:8080
```

Nothing to build: one lasair process from the published multi-arch image
(`ghcr.io/abutlabs/lasair`) authors all six dev validators' slots in lasair's devnet mode
(`--dev-all-keys --own 0,…,5`) and hosts the service, seeded into genesis; lasair's
CE-133 builder and CE-129 reader bridges connect the DEX to it over JAMNP-S/QUIC. There
is no finality, so fills are not durable: it is the 60-second demo, not a net.

The UI works as on the DEX nets: create an account, fund it in the Faucet tab (USDC,
DOT, JAMKB across DOT/USDC, JAMKB/USDC and JAMKB/DOT), place a Limit or Market order
(tick **🔒 Seal** to hide it), and watch the 6-second auctions clear it. The
**mempool** view shows what sits in the service: 🌐 LIMIT / ⚡ MARKET orders with their
terms, 🔒 SEALED ones as a commitment only until they clear.

## The mixed-client research net: lasair and PolkaJam on one chain

```sh
docker compose -f docker-compose.mixed.yml up
```

Six validators on one shared genesis, split across two independent clients:
`pj0 pj1 pj2` are PolkaJam (fetched black-box at build time), `lm3 lm4 lm5` are lasair.
Each node authors only its own Safrole slots and imports the others' over JAMNP-S/QUIC,
so leadership rotates across clients; `spec-init` mints the shared genesis and `watch`
prints the chain advancing.

```sh
docker compose -f docker-compose.mixed.yml logs lm3 lm4 lm5 | grep authored   # lasair's slots
docker compose -f docker-compose.mixed.yml logs watch                         # the chain, via PolkaJam's RPC
```

lasair's GP 0.8.0 release checks include a 10-minute 3:3 soak of this net with PolkaJam
`nightly-2026-09-22`: one head, 98 heads agreed, none diverged (lasair
`docs/GP_0_8_0_PLAN.md`). `make verify-mixed` judges a running net the same way (below).

**It is not a DEX net.** The DEX UI runs on `:8090` through lasair's bridges and the
service is in the shared genesis, but the two clients share no finality (PolkaJam runs
`dummy`), and a 45-minute run in 2026-07 settled nothing ([`SOAK_RELIABILITY.md`](SOAK_RELIABILITY.md)).
`make mixed-dex` layers `docker-compose.mixed-dex.yml` on top, a lasair-dominant
configuration under which trades settled in 2026-07 (GP 0.7.2); it is not re-verified
at GP 0.8.0.

The generated nets (`./dex up NET=<net>`, [`NETS.md`](NETS.md)) are the successors: any
mix of lasair, PolkaJam, JavaJAM and pbnjam, one command each.

> **On PolkaJam and compliance.** PolkaJam is used **black-box**: its binary is fetched
> from the public [`paritytech/polkajam-releases`](https://github.com/paritytech/polkajam-releases)
> at image-build time on *your* machine and is never committed or redistributed. See
> [`mixed/`](../mixed) and lasair's [`docs/MIXED_CLIENT_NETWORK.md`](https://github.com/abutlabs/lasair/blob/main/docs/MIXED_CLIENT_NETWORK.md).

## Options

```sh
LASAIR_IMAGE=lasair:local docker compose up          # any lasair image, e.g. a local source build
LASAIR_TAG=2.1.1 docker compose up                   # the quickstart's tag (default 2.1.1)
PJ_RELEASE=nightly-2026-09-22 docker compose -f docker-compose.mixed.yml up   # the PolkaJam release (default)
```

`LASAIR_IMAGE` works for every compose file and `./dex`; lasair 1.x is GP 0.7.2 and
cannot run the current service blob. `mixed/Dockerfile.polkajam` checks the release
tarball against a pinned sha256; a release with no pin is checked against the digest
the release API reports, with a warning. For another client split, use a generated net
(`./dex up NET=<net>`, [`NETS.md`](NETS.md)).

Sealing defaults to commit–reveal (rung 3, the permissionless base state). To opt in to
the rung-2 committee (encrypt-until-batch, simulated committee), uncomment
`ENC_MODE: "1"` under the `dex` service in `docker-compose.yml`. Rounds are sized to the
refine budget of a tiny chain (G_R = 1e9); on a full-spec chain set `REFINE_GAS: "5e9"`
there too ([`THROUGHPUT.md`](THROUGHPUT.md)).

## Dev modes (Makefile)

Public images by default; a local lasair source build on demand, so a lasair change can
be verified end to end before tagging a release and waiting for the ~80-min multi-arch
CI publish. Needs the (private) lasair checkout next to this repo (override with
`LASAIR_SRC=…`):

```sh
make up             # quickstart, published image                    (docker compose up)
make mixed          # mixed net, 3 PolkaJam / 3 lasair (consensus comparison)
make mixed-dex      # mixed net, lasair-dominant overlay (historical, see above)
make local          # build ../lasair -> lasair:local -> quickstart
make mixed-local    # same source build -> mixed net
make mixed-dex-local# same source build -> mixed net + lasair-dominant overlay
make verify         # e2e smoke test against the RUNNING quickstart (:8080)
make verify-mixed   # health check against the RUNNING mixed net
make test-nets      # unit tests of the net configs (nets/), no Docker
make down           # stop whichever stack is up
```

Pre-push flow for a lasair change: `make local && make verify`, then
`make mixed-local && make verify-mixed` (it watches every node for `VERIFY_SECS`,
default 360 s, then judges) — only then tag `client-vX.Y.Z` and let CI publish.
`verify-mixed` runs `offchain/netwatch.py` against each node's public interface
(JIP-2 for PolkaJam, the metrics endpoint for lasair) and asserts one head, liveness,
every validator credited blocks in the on-chain statistics, and peers; see
`mixed/verify.sh` for the knobs (`LAYOUT`, `NETWATCH_NODES`, `VERIFY_ARGS`).

## Monitoring

```sh
make monitor        # Prometheus + Grafana on a running mixed net; dashboards on :3010, no login
make monitor-down
```

lasair6 has its own overlay, `docker-compose.lasair6-monitor.yml`; on pj6, netwatch runs
as part of the net (`127.0.0.1:9301/metrics`, `/verdict`).

Metric sources, client-neutral first:

- **netwatch (`offchain/netwatch.py serve`, issue #15)** — one poller for every node
  of any net: JIP-2 (`bestBlock`, `finalizedBlock`, `parent`, `syncState`,
  `statistics`) for nodes that serve it, the Prometheus gauges for lasair until it
  does (lasair#68). It exports `jam_best_slot`, `jam_finalized_slot`,
  `jam_head_lag_slots`, `jam_finality_lag_slots`, `jam_head_agree` /
  `jam_final_agree` (hash agreement at the common slot), `jam_peers`, `jam_node_up`
  (all `{node, client}`), net-wide `jam_net_*` (distinct heads, one-head flag,
  divergence length) and `/verdict` (the same judgement `netwatch.py poll` prints).
- **On-chain validator statistics (GP π)**, decoded by netwatch from JIP-2 `statistics`
  (GP 0.8.0 C(13)) — per-validator blocks / tickets / guarantees / assurances as
  recorded by consensus, identical from any node, covering every client's validators
  (`jam_pi_*`).
- **lasair's native `/metrics`** (`--metrics-port`): blocks authored/imported, import
  rejects by STF reason, peers, per-peer dial failures, QUIC accepts/errors, Safrole
  tickets, the CE-133 pipeline. The dashboards show these in collapsed **lasair
  overlay** rows — optional detail, empty on a net without lasair.
- `monitor/exporter.py` (legacy): PolkaJam log scraping through the Docker socket.
  The monitor image runs it only while `NETWATCH_NODES` is unset.

Provisioned dashboards: **JAM network** (one-head / finality / divergence verdicts,
per-node lag and agreement, the π consensus row), **JAM clients** (the same,
averaged by `client`), **JAM node** (any node, with a selector), **JAM finality**
(A2: finalized head advancing with one hash), plus the DEX's **JAMswap service** and
**accounts & trading** boards. Dashboards are generated by
`monitor/grafana/gen_dashboards.py` — edit that, not the JSON.
Prometheus itself is on :9090.

## Platforms

| Your machine | What runs | Notes |
|---|---|---|
| **Linux / amd64** (Intel/AMD) | native | — |
| **Apple Silicon** (M1–M4, arm64) | native | lasair and PolkaJam images are arm64 too; JavaJAM runs as a native process (NETS.md) |
| **Windows / WSL2** (amd64) | native | run inside a WSL2 Linux shell |
| **arm64 without an arm64 image yet** | emulated | add `--platform linux/amd64` (slower, but works) |
