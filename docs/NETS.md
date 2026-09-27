# Which net is which

The DEX runs on three nets today. All are GP 0.8.0 and finalize under GRANDPA:

- **`lasair6`** (the `./dex` default): six lasair validators. The DEX reaches them
  through lasair's JAMNP-S builder and reader bridges ([below](#the-dex-on-lasair-lasair6)).
- **`pj6`**: six stock PolkaJam validators and no lasair anywhere. The DEX reaches them
  over JIP-2 and deploys its service at startup ([below](#the-dex-on-jip-2-pj6-17)).
- **`pj-javajam`**: three PolkaJam and three JavaJAM validators, two clients finalizing
  one chain together; the pj6 stack, submitting through a PolkaJam and a JavaJAM node in
  turn ([below](#the-dex-on-polkajam--javajam-pj-javajam-18)).

The other nets are cross-client research: do different clients keep one head, finalize
together and hold the same state?

## Just run the DEX

```bash
./dex up                # lasair6: start, wait for finality → http://localhost:8081
./dex up NET=pj6        # pj6: start, deploy + set up the service → http://localhost:8201
./dex up NET=pj-javajam # PolkaJam + JavaJAM (JavaJAM native on macOS) → http://localhost:8204
./dex status            # finality + market at a glance (NET=pj6 for pj6, as for every verb)
./dex load              # optional: start the load generator (up leaves it stopped)
./dex down              # tear down + wipe the chain
./dex rebuild           # only if you changed the on-chain service (service/src)
```

Wherever the DEX runs, its code (`offchain/`) and the service blob
(`service/jamswap-service.jam`) are **bind-mounted**, so local changes are live on the
next `up`; only a change to `service/src` needs `./dex rebuild`.

## The nets

| Net | Clients (validators 0..5) | GP | Finality | DEX | Issue |
|---|---|---|---|---|---|
| `lasair6` | lasair ×6 | 0.8.0 | GRANDPA (lasair, jam-np PR #6 draft) | **runs**: lasair's bridges, service in genesis; UI :8081 | #12 |
| `pj6` | PolkaJam ×6 | 0.8.0 | GRANDPA (PolkaJam) | **runs**: JIP-2, runtime deploy; A1–A4 pass (10-min and 1-hour soaks); UI :8201 | #17 |
| `mixed` | PolkaJam ×3, lasair ×3 | 0.8.0 | none shared (PolkaJam runs `dummy`) | UI on :8090 through lasair's bridges; does not settle (no shared finality; a 45-min run in 2026-07 cleared nothing) | #2 |
| `pj-javajam` | PolkaJam ×3, JavaJAM ×3 (native on macOS) | 0.8.0 | GRANDPA on both, finalizing together (JavaJAM started first) | **runs**: JIP-2 through the PolkaJam gateway and JavaJAM's jj3, runtime deploy; A1–A4 pass (10-min soak; 1-hour: PJJJ_60_ROW); UI :8204 | #18 |
| `pj-javajam-42` | PolkaJam ×4, JavaJAM ×2 | 0.8.0 | GRANDPA on both; not run yet | configured (the pj-javajam stack), not run; UI :8205 | #18 |
| `pj-pbnjam` | PolkaJam ×5, pbnjam ×1 | 0.8.0 (pbnjam: unconfirmed) | GRANDPA: the five PolkaJam nodes finalize alone (5 of 6) | none; the pbnjam image does not start | #19 |
| `pj-pbnjam-42` | PolkaJam ×4, pbnjam ×2 | 0.8.0 (pbnjam: unconfirmed) | GRANDPA | none; blocked on the pbnjam image | #19 |
| `lasair-pj-javajam` | lasair ×2, PolkaJam ×2, JavaJAM ×2 | 0.8.0 | GRANDPA | configured (lasair's bridges), not run: needs lasair#54, #60, #66 | #20 |
| `nolasair` | PolkaJam ×2, JavaJAM ×2, pbnjam ×2 | 0.8.0 | GRANDPA | none in the profile yet; not run: needs #18, #19 | #21 |

Versions: lasair `ghcr.io/abutlabs/lasair:2.0.0`, PolkaJam `nightly-2026-09-22` (0.1.29),
JavaJAM 0.4.3, pbnjam-node `main-54226be` (the image does not state its GP version;
likely 0.8.0, see #19). Status as of 2026-09-27 on an Apple M1 Pro; details
[below](#what-the-clients-did-2026-09-26). `lasair6` and `mixed` are hand-written
compose files; the others are generated from [`nets/profiles.py`](../nets/profiles.py).

## The compose files, one line each

| File | Net | UI | What it's for |
|---|---|---|---|
| `docker-compose.lasair6.yml` | 6× lasair, GRANDPA | :8081 | The DEX on lasair (`./dex up`). |
| `nets/compose/<net>.yml` | per-index client layouts | pj6: :8201, pj-javajam: :8204 | **Generated** nets (below), including `pj6`, the DEX on PolkaJam, and `pj-javajam`, the DEX on PolkaJam + JavaJAM. `./dex up NET=<net>`. |
| `docker-compose.yml` | one lasair process | :8080 | Quickstart demo: one command, nothing to build. No finality, so fills are not durable. |
| `docker-compose.mixed.yml` | 3× lasair + 3× PolkaJam | :8090 | Consensus research: two clients co-authoring one chain. No shared finality; not a settlement net. |
| `docker-compose.mixed-dex.yml` | mixed, lasair-dominant | (overlay) | Historical (2026-07, GP 0.7.2): lasair authors nearly every block so trades settle; not re-verified at GP 0.8.0. |
| `docker-compose.pj-majority.yml` | 6× PolkaJam, GRANDPA | — | Research: lasair's finality bridge checks PolkaJam's finalized head against its own. |
| `docker-compose.monitor.yml` | overlay (mixed) | Grafana :3010 | Prometheus + Grafana on the mixed net (`make monitor`). |
| `docker-compose.lasair6-monitor.yml` | overlay (lasair6) | Grafana :3010 | Prometheus + Grafana on lasair6. |
| `docker-compose.load.yml` | overlay (mixed) | — | Load generator for the mixed net. `./dex load` covers the DEX nets. |

## Test nets: one command per net

Every net is a profile in [`nets/profiles.py`](../nets/profiles.py); `./dex nets` lists
them. Add `NET=<name>` to any `./dex` verb (default `lasair6`):

```bash
./dex up NET=pj6            # build/pull, mint the shared genesis, start every node
./dex heads NET=pj6 600     # one head + finality across every node for 600 s (default 300)
./dex status NET=pj6        # one sample of the same
./dex logs NET=pj6 pj3      # a node's log (native JavaJAM nodes too)
./dex down NET=pj6          # tear down, wipe the chain, stop native nodes
./dex gen                   # regenerate nets/compose/*.yml after editing nets/
# nets with the JIP-2 DEX (pj6, pj-javajam):
./dex load NET=pj6          # start the load generator (up leaves it stopped); noload stops it
./dex soak NET=pj6 3600     # A1-A4 in one command (below); default 600 s
```

### Layout, keys and genesis

A layout is one client per validator index: `lasair`, `pj` (`polkajam`), `pbnjam`,
`javajam` (`jj`); tiny = 6. Validator *i* is always the **standard JAM dev account i**
(JIP-5: seed = `u32-LE(i)` × 8), whichever client runs it, and that client holds exactly
that key: JavaJAM and pbnjam start with `--dev-validator i`, PolkaJam loads dev seed *i*
(`--key-seed-file`, same keys as its `--dev-validator i`), lasair runs `OWN=i`.

The shared genesis is minted by [`nets/genesis.py`](../nets/genesis.py) in the
`spec-init` container, with no lasair binary unless lasair is in the layout:

- **public keys**: [`nets/devkeys.py`](../nets/devkeys.py) derives the Ed25519 key and
  JAMNP-S peer id (JIP-5 + RFC 8032 in pure Python) and takes the Bandersnatch key from
  the published table (docs.jamcha.in/basics/dev-accounts; the test checks the
  derivation against it). With lasair in the layout, `lasair --dev-account i` must agree.
- **chain spec**: `polkajam gen-spec` (black box: config in, JIP-4 spec out). The same
  spec file goes to every client; genesis header hash `245becfe…` for every tiny layout
  (the header carries keys, not addresses).
- **addresses**: validator *i* is `10.231.<net>.(10+i):41000+100·net+i`, a static IP on
  the net's compose network (the hand-written nets keep their 172.28/29/30 subnets).
- the minter, the node table (`nodes.json`) and the service injection (`SERVICE`, lasair
  only) are the same code for the hand-written nets: lasair6 and mixed mint the same
  genesis as before (checked: every output file equal, the spec equal as JSON).

### Per-client adapters

| Client | Image / binary (pinned) | Started as validator *i* | RPC (host) |
|---|---|---|---|
| lasair | `${LASAIR_IMAGE}` (default `ghcr.io/abutlabs/lasair:2.0.0`) | mesh entrypoint, `OWN=i`, `WALL=1` next to wall-clock clients | none yet (lasair#68): the probe reads its `STATUS` log line |
| PolkaJam | `jamswap-polkajam:<PJ_RELEASE>`, built by `mixed/Dockerfile.polkajam` (target `polkajam`): the release tarball fetched at build time, sha256-pinned per release and arch | `mixed/pj-entrypoint.sh`: `--peer-id`, `--key-seed-file pj_i.seed`, `--finality-mode`, `--bootnode` | `127.0.0.1:42000+100·net+i` |
| pbnjam | `docker.io/shimonchick/pbnjam-node:main-54226be@sha256:ceb5f651…` | `--chain /shared/spec.json --dev-validator i --rpc-port … --temp` (its documented flags; `--help` can't run, see below) | same |
| JavaJAM (macOS) | native: release zip 0.4.3 + Temurin JRE 25.0.4.1, fetched at run time into `~/.cache/jamswap` (sha256-checked) by [`nets/javajam-native.sh`](../nets/javajam-native.sh) | `run --chain <spec> --dev-validator i --port … --rpc --finality-mode …` | same |
| JavaJAM (Linux) | `ghcr.io/methodfive/javajam:0.4.3@sha256:573c030b…` (amd64) / `:0.4.3-arm64@sha256:ab8d65b5…`, compose profile `javajam-docker`, heap capped (`JAVAJAM_HEAP`, default 2g; the image pins 12 GB) | same flags | same |

`JAVAJAM_RUNNER=native|docker` picks the runner (default: native on macOS, docker
elsewhere).

**JavaJAM on macOS.** Its images — and its Linux release zip in a container — die with
SIGILL in their native crypto libraries under Docker Desktop on Apple silicon (#2). So
the runner starts the macOS release on the host. A native process can reach no container
IP, so such a net is minted with `HOST_IP` (the host's LAN address, detected; override
with `HOST_IP=`): every validator's genesis address is `HOST_IP:port`, and every
container publishes its UDP port on `HOST_IP`. Container↔container traffic then hairpins
through Docker Desktop's port forwarding; the same compose file serves both modes. The
release's `bin/javajam` launcher pins `-Xms6g -Xmx6g -XX:+AlwaysPreTouch` (observed on the
java command line it starts) and ignores `JAVA_OPTS`, so three nodes would commit 18 GB:
the runner starts the release jar with the same flags, minus pre-touch, and a capped
heap (~110 MB resident per node on a tiny net). Each node runs in its own session under
[`nets/supervise.py`](../nets/supervise.py), which restarts it if it exits (as Docker's
`restart: unless-stopped` does for the containers), logs to
`~/.cache/jamswap/nets/<net>/jj<i>/`, and stops with `./dex down`.

### Keys per client

The target: **no node signs as another client's validator.** PolkaJam, JavaJAM and pbnjam
already hold only their own dev key. lasair ≤ 2.x is the exception — to guarantee and
assure on its own it derives every dev secret — until lasair#54 (one key per
validator), lasair#60 (CE-134/135 co-guaranteeing) and lasair#62 (assurances) reach a
published image. That is an explicit, per-net switch:

| `LASAIR_DEV_ALL_KEYS` | lasair nodes sign guarantees as | Default on |
|---|---|---|
| `1` | every **lasair** index of the layout (`GUARANTOR_OWN` = the lasair set; lasair6: all six, mixed: 3,4,5) | nets with a lasair node |
| `0` | their own index only (`./dex` sets `LASAIR_GUARANTOR_OWN=` empty) | nets with no lasair node (forced: nothing to share) |

With lasair 2.0.0, `0` narrows **guarantees** only: that release still derives every dev
secret and assures for any validator. The switch is also passed to each lasair node as
`LASAIR_DEV_ALL_KEYS`, for a lasair#54 entrypoint to choose `--dev-all-keys` or
`--dev-validator i`. Expect `0` to stop settlement until lasair#60/#62.

### Checking a net: `./dex heads`

[`nets/onehead.py`](../nets/onehead.py) samples every node every 6 s: best block and
finalized block over JIP-2 (`bestBlock`, `finalizedBlock`, `parent`) on PolkaJam, JavaJAM
and pbnjam; lasair's `STATUS` log line and `lasair_finalized_slot`. Each sample is
**SAME** (one best hash), **LAG** (lower heads are ancestors of the highest, checked by
walking `parent`), **FORK** (a head off the highest head's chain) or **DOWN**. Verdict
ONE HEAD: no fork longer than two samples, nobody down at the end, heads advanced, and on
a finalizing net finality advanced on every node with no conflicting finalized blocks.

### The DEX on lasair: lasair6

`./dex up` runs `docker-compose.lasair6.yml`: six lasair validators (dev accounts 0..5,
chain-paced `WALL=0`, `LASAIR_FINALITY=grandpa`) and the DEX on the chain adapter's
`jamnp` backend, because lasair serves no JIP-2 yet (lasair#68):

| Service | What it is |
|---|---|
| `lm0`..`lm5` | the validators; `spec-init` mints their genesis with the service in it |
| `builder` | `jamnp-builder`, lasair's CE-133 bridge: HTTP `/submit` → a work-package to the lasair nodes. Its packages are accepted only by lasair's own CE-133 endpoint until they are spec-valid (lasair#69) |
| `reader` | `lasair-reader`, lasair's CE-129 bridge: HTTP `/read` → a state read at lm0's head |
| `dex` | `offchain/server.py` with `SERVICE_ID=100` (seeded into genesis: lasair has no Bootstrap service yet, lasair#73), heads and finality from lm0's Prometheus gauges, `SETTLE_HOLD_SECS=0`; UI on `:8081` |
| `loadgen` | `offchain/loadgen.py` (`./dex load`) |

Two differences from pj6 follow from the backend. The reader answers at its head only,
so the DEX gates reveals and receipts by comparing block heights with the finalized
height, where on JIP-2 it reads the finalized state itself; #26 moves lasair6 to finalized reads
once the default image carries lasair#70. And lasair reports a service footprint of 0,
so the JAMKB backpressure that pj6 needs a reserve keeper for is not live here.

### The DEX on JIP-2: pj6 (#17)

`./dex up NET=pj6` starts six stock PolkaJam validators with GRANDPA and the DEX, with
no lasair image anywhere (the genesis minter is the plain `polkajam` build target) and
nothing injected into genesis. Every DEX layout without a lasair node gets this stack
(`nets/netgen.py` `dex_backend`: `jip2`; a layout with lasair gets lasair's bridges,
`jamnp`):

| Service | What it is |
|---|---|
| `rpc` | an **ordinary PolkaJam node** (no validator key, `--mode ordinary`, the net's `--finality-mode`): the DEX's gateway, JIP-2 on `127.0.0.1:42150` (the net's RPC block + 50) |
| `dex` | `offchain/server.py` with `CHAIN_BACKEND=jip2`, `CHAIN_RPC=ws://rpc:42150`, `CHAIN_SPEC=/shared/spec.json`, `RESERVE_TOPUP=1` and **no `SERVICE_ID`**: at startup it deploys `service/jamswap-service.jam` through the Bootstrap service, lists the markets and funds the six dev accounts ([`RUNNING.md`](RUNNING.md#the-dex-on-any-jip-2-node-runtime-deploy)); UI on `:8201` once the reserve has landed (~1 min) |
| `loadgen` | `offchain/loadgen.py` at the DEX (`PROFILE`, `RATE`, `SEALED_RATIO`; default trading, 12/min, 0.2) — stopped by `up`, started by `./dex load` |
| `netwatch` | `netwatch.py serve` over the six validators' JIP-2 (`127.0.0.1:9301/metrics`, `/verdict`) |

**Why a gateway node.** Every pj6 validator's RPC answers `submitWorkPackage` with
`Failed to submit work-package to even a single proxy/guarantor` (PolkaJam
nightly-2026-09-22; on both cores, for the whole run), while the same package through
an ordinary PolkaJam node on the same net is accepted and guaranteed — the topology
`polkajam-testnet` has too (validators plus RPC nodes). So builders talk to a full node,
not to a validator. A validator's other JIP-2 calls (heads, finality, storage) work, and
netwatch reads those.

**`./dex soak NET=pj6 [SECS]`** ([`nets/soak.py`](../nets/soak.py)) is the epic's shared
acceptance in one command, on a freshly started net: a 60-s `netwatch poll` with no load
that must pass first (a net that did not form exits 2, unsoaked), then loadgen on and
`netwatch poll --require-finality` over the six validators for SECS + 180 s of drain
(A1 one head, A2 finality on every node), then `netwatch parity` at the common finalized
head (A3: books, balances, custody, registry, landed-round markers on every node), then
`soak_verdict.py <the dex's order events> --chain --parity` (A4), the offered load as
the load generator counted it (at most 1 − target refused or busy), and the submission
nodes (every node the dex submitted through settled at least one round; the dex's
`jamswap_relays_total` / `jamswap_settled_via_total` by `via`). Everything lands in
`~/.cache/jamswap/soak/<net>-<UTC time>/` (`--out` to choose; `rounds.txt` lists every
settled round and its node) with a `DONE` marker; exit 0 iff all five pass.

**Why a reserve keeper.** On JIP-2 the service's footprint is readable, so the JAMKB
standard's backpressure is live (on lasair nets the footprint reads 0). The first
1-hour soak passed every check while the DEX had turned its load away for 45 of its 60
minutes: the footprint grows ~400 octets a minute under load (a landed-round marker
lives an hour), and at 173,272 octets the obligation, 170 KB, passed the 169 KB reserve
seeded at startup; from then on every order came back `400 service under-reserved on
JAMKB (short 1 KB)`. The SLO could not see it (it judges only orders the DEX accepted).
So the dex runs with `RESERVE_TOPUP=1` (the beneficiary's capped top-up, automated:
[`RUNNING.md`](RUNNING.md#the-dex-on-any-jip-2-node-runtime-deploy)),
and `./dex soak` also fails when more than 1 − target of the offered load was refused.

Results (2026-09-27, Apple M1 Pro, Docker Desktop 8 GB; fresh net per soak; loadgen
trading at RATE 12: a crossing pair every 5 s, 20 % of the sells sealed):

**10 minutes** (`./dex soak NET=pj6 600`): every check PASS — SLO 1.000000 (238
cleared, 0 missed), sealed zero-loss, one head 130/130 samples, 104 finalized slots
hash-checked, state parity on pj0..pj5, 240 orders offered and none refused.

**1 hour** (`./dex soak NET=pj6 3600`; 120 orders placed in every 5-minute window, the
keeper topped the reserve up 6 times as the footprint grew 166 → 190 KB):

```
orders seen         : 1444
clearing SLO        : 1.000000  (target 0.9999)  PASS
  cleared           : 1435
  missed            : 0  (expired/lost 0, stuck-open 0)
breakdown           : {'cleared': 1435, 'resting': 8, 'open': 1}
SEALED zero-loss    : PASS  (seen 133, terminal 132, stuck-open 0)
clear latency       : p50 30.5s  p99 357.8s
one head            : PASS  (hash (JIP-2 nodes); 630/630 samples ok, max lag 1 slots, 0 divergence episode(s), longest 0 slots vs epoch 12)
liveness            : PASS  (best advanced 630..630 slots per node over 3777.7 s)
finality            : PASS  (finalizing, required; 0 conflict(s), 0 regression(s), longest stall 1.0 slots vs 12, 591 finalized slots hash-checked)
authoring (pi)      : PASS  (blocks per validator {'0': 99, '1': 98, '2': 100, '3': 121, '4': 107, '5': 111})
state parity        : PASS  (all digests agree; service 1, 156 keys at final slot 9122807 0x65556373 (from pj0), attempt 1)
  pj0..pj5          : 88568b80eebbef01  present 133  (pinned), on all six
VERDICT (orders + chain): PASS
offered load        : PASS  (1440 orders offered, 0 refused, 0 busy)
```

The open order in each run is a lone sealed order that crossed nothing: its commit is
final and it waits hidden in the mempool for a counterparty, never revealed alone
(`offchain/round.py`); it ends filled or expired (32 min). The p99 is resting makers
filled by a later auction (latency counts from placement). In the first epoch after
genesis the gateway refuses packages ("storage access error: invalid epoch N, reference
epoch is 0"); the deploy retries through it and the API opens ~1–2 min after `up`.

### The DEX on PolkaJam + JavaJAM: pj-javajam (#18)

`./dex up NET=pj-javajam` runs three PolkaJam validators (Docker) and three JavaJAM 0.4.3
validators (native on macOS), both `--finality-mode grandpa`, and the same JIP-2 DEX
stack as pj6: the `rpc` gateway (`127.0.0.1:42450`), `dex` (UI `:8204`), `loadgen`,
`netwatch` (`127.0.0.1:9304`). `pj-javajam-42` (4 : 2) has the same stack, UI `:8205`;
it has not been run. What differs from pj6:

- **Start, in stages, then check.** The genesis minter; JavaJAM, until every JavaJAM
  node answers on its JIP-2 RPC (its JVM takes a few seconds), plus 5 s; then the
  PolkaJam validators *and the gateway*; then 20 s and a 36-s `nets/onehead.py --gateway
  --min-peers 2`: every validator and the gateway on one head, finalizing, with at least
  two peers each — if not, `up` tears the net down and starts a fresh one, up to
  `UP_TRIES` (5) times; only then the dex, loadgen and netwatch. Why each step:
  - PolkaJam casts its round-1 GRANDPA votes once, as it starts, and never re-sends them
    (the #18 diagnosis): they must reach every JavaJAM node at that moment.
  - A PolkaJam node that joins a running net follows the head but never finalizes past
    the block it synced to — an ordinary node (the gateway) and a restarted validator
    alike, and on `pj6` too (no JavaJAM: the gateway started 30 s after the validators
    stayed at its first finalized block while they finalized 29 more). The DEX reads
    finalized state through the gateway, so it must start with the validators; started
    after them, it never finalized, and no sealed order could be revealed.
  - On this Mac (Docker Desktop, every validator addressed at the host's IP so that the
    native JavaJAM can reach the containers) a PolkaJam container sometimes loses every
    peer ~15 s after it starts — all its QUIC connections time out at once — and never
    gets them back (stuck at genesis, or on one peer). Of 18 starts on 2026-09-27 (the
    host at load average 23–50), 6 formed; the check catches the rest before any load.
- **JavaJAM's RPC from containers.** A native JavaJAM serves JIP-2 on the host's
  loopback, which containers reach at `host.docker.internal` (Docker Desktop):
  `JAVAJAM_RPC_HOST`, which `./dex` sets for the native runner (the Docker runner uses
  the node's service name). netwatch reads all six validators, JavaJAM included.
- **Rounds through JavaJAM's RPC.** A JavaJAM validator takes work-packages itself
  (`submitWorkPackage` answers `null` and it forwards the package to the core's
  guarantors, PolkaJam or JavaJAM), where a PolkaJam validator refuses. So the dex
  submits through the gateway and jj3 in turn, one package each (`CHAIN_SUBMIT_RPC=
  "rpc=ws://rpc:42450 jj3=ws://host.docker.internal:42403"`; reads stay on the
  gateway; a node that refuses on every core or cannot be reached passes the package
  to the next). Each settled round names its node in the dex log (`round m1: settled
  on-chain — receipted 3 order(s), carried 0 (round 02321d3e11b234c3, via jj3)`), the
  dex counts relays and settlements by node, and `./dex soak` fails unless every
  submission node settled at least one round.
- **A package can be accepted and still never land.** JIP-2 `submitWorkPackage` succeeds
  once the package reached one guarantor. A JavaJAM guarantor whose core is busy queues
  it ("Core 0 is engaged. Queueing work package") and drops the queue when the guarantor
  rotation moves it off that core ("Dropping 1 queued work package(s) for core 0: no
  longer assigned to it"); the package is never reported. The dex used to wait
  `ROUND_GATE_SECS` (300 s) for such a round, the market blocked meanwhile. Now it reads
  JIP-2 `workPackageStatus` of the round's package once a slot and releases the round
  (orders back to the front of the mempool, kept as a zombie in case it lands after all)
  as soon as the status is `Failed`, ~1 min after submission; any other payload (a sealed
  order's commit, a deposit) is sent again as is — the service is idempotent under a
  duplicate — at most 5 times (`jamswap_package_resends_total`,
  `jamswap_round_abandoned_total{reason="package-failed"}`).

**JavaJAM 0.4.3's JIP-2, observed** (black box: every JIP-2 method called on jj3, a
validator, and on PolkaJam's pj1 on the same net, after the 10-minute soak). Served:
`parameters`, `bestBlock`, `finalizedBlock`, `parent`, `stateRoot`, `beefyRoot`,
`statistics`, `serviceData`, `serviceValue`, `servicePreimage`, `serviceRequest`,
`listServices`, `workPackageStatus`, `syncState`, `workReport` (code 2 for an unknown
hash), `fetchWorkPackageSegments` / `fetchSegments` (code 3), `submitPreimage`,
`submitWorkPackage` and `submitWorkPackageBundle` on a validator, and every
`subscribe*` method (a numeric id). Where it differs from JIP-2 / JSON-RPC 2.0 or from
PolkaJam:

- **Error objects without `"message"`**: `{"code": 0}` for a package that does not
  decode (its log: an `ArrayIndexOutOfBoundsException`), `{"code": 2, "data": …}` for
  an unknown work-report, `{"code": 3}` for segments. JSON-RPC 2.0 §5.1 requires a
  message; PolkaJam gives one ("Codec error: …", "The work-report … is not available").
- **An unknown method** is `{"code": 0, "message": "unknown error"}`; JSON-RPC's
  -32601 "Method not found" (PolkaJam's answer) tells a missing method from a failure.
- **An error with no `"id"`**: `subscribeWorkPackageStatus(hash, anchor)` without its
  `finalized` argument gets no answer the caller can match (logged: a
  NullPointerException, then an error response with no id); the client waits for its
  timeout. PolkaJam answers -32602 "Invalid params". With all three arguments it works.
- **`submitPreimage` of a preimage nobody requested** returns `null` (success) and
  logs `preimage_not_requested`; PolkaJam returns "Preimage was not requested".
- **`workPackageStatus` stays `Reported`**: a package PolkaJam's pj1 reported `Ready`
  one block after it was reported was still `Reported` on jj3 two minutes later (both
  asked at their own best block). The dex reads status on the gateway.
- **Accepted, then dropped**: a package `submitWorkPackage` accepted can be queued for a
  busy core and dropped at the next guarantor rotation (above). In the passing
  10-minute soak all 8 packages JIP-2 reported `Failed` had gone through jj3 (8 of ~29
  sent there), none of the ~31 sent through the PolkaJam gateway.

**Results** (2026-09-27, Apple M1 Pro, Docker Desktop 8 GB, the host shared with other
jobs: load average 23–37 on 10 cores, swap 9.7 of 10.5 GB in use; fresh net per soak;
loadgen as for pj6):

**10 minutes** (`./dex soak NET=pj-javajam 600`, the net formed on the first `up`):
every check PASS — SLO 1.000000 (237 cleared, 0 missed), sealed zero-loss (26 of 29
terminal, 3 open), one head 130/130 samples with every node up, 112 finalized slots
hash-checked, longest finality stall 1 slot, state parity on all six (99 keys, digest
`c094dbe8415f494e` on pj0..pj2 and jj3..jj5 at final slot 9131433), 240 orders offered
and none refused; 25 rounds settled, 13 through jj3 and 12 through the gateway; p50 /
p99 clear latency 36 s / 124 s (pj6: 30 s / 358 s over an hour). The package watch
resent 7 commits, 1 carry commit and 1 deposit and released 1 round, all first sent
through jj3.

Before the gateway started with the validators and the package watch existed, two
10-minute soaks on formed nets failed: in one the gateway never finalized and one
PolkaJam validator ran on a single peer (a 15-slot finality stall on it, A2 FAIL; 2
rounds settled in 10 minutes, 179 of 244 orders still open); in the other A1–A4 passed
but only 2 of 4 rounds settled — the other two, one through the gateway and one through
jj3, each waited out the 300-s gate — so orders cleared in batches of 100+ (p50 212 s),
and the submission-node check failed (no round had settled through the gateway).

RESULTS_60

### What the clients did (2026-09-26)

- **PolkaJam + JavaJAM (`pj-javajam`) co-author one chain** — blocks from both clients,
  one best hash on all six nodes (JavaJAM native on the Mac, PolkaJam in Docker,
  addressed through the host IP) — **but did not finalize on 2026-09-26** (fixed the
  next day by the start order: PolkaJam never re-sends its round-1 vote, see #18 and
  [the pj-javajam section](#the-dex-on-polkajam--javajam-pj-javajam-18)). Both run
  `--finality-mode grandpa` and exchange GRANDPA messages: PolkaJam logs every JavaJAM validator's
  GRANDPA view ("updated view. Now at 1, 0") and 9 incoming round-1 messages; JavaJAM
  logs "Grandpa state received for round 1, set 0" and "Grandpa vote received for round 1
  and set 0". Yet round 1 of set 0 never completes: PolkaJam's round state stays
  `prevote_ghost = genesis, estimate = genesis, finalized = None, completable = false`
  after it prevoted and precommitted, and `finalizedBlock` is genesis on every node for
  the whole soak (the same PolkaJam build finalizes within seconds as `pj6`). So the votes
  looked uncounted across clients (3 + 3 < the 5-of-6 quorum); in fact both sides
  encode and count each other's votes, and a round-1 prevote PolkaJam sent before
  JavaJAM was up was lost for good (#18).
- **JavaJAM 0.4.3 sometimes shuts itself down right after it starts**: ~0.5 s after its
  first outbound connections the netty event loop is gone ("event executor terminated"),
  and the process exits with status 0 ("Bye") after 3–9 s; seen in 4 of 9 starts in
  three-node launches and 1 of 14 single-node starts. `nets/supervise.py` restarts it
  (a `[supervise]` line in its log), and it then joins normally. After such a restart it logs (debug) ~200 `Import rejected:
  bad_state_root` in the first second of re-sync, then follows the head.
- **pbnjam-node `main-54226be`** does not start, on either arch: every run (18 restarts
  in the soak, `--help` too) exits 1 with `ENOENT: no such file or directory, open
  '/app/packages/bandersnatch-vrf/wasm-ark-vrf/ark_vrf_wasm_bg.wasm'` (Bun 1.4.2). The
  published image lacks its Bandersnatch wasm; it is the only published version
  (`latest` = `main` = the same digest). The five PolkaJam validators of `pj-pbnjam`
  still kept one head and finalized (5-of-6 GRANDPA).

## The `Makefile` still works

`make up`, `make mixed`, `make monitor` and the rest are the older entry points and
still work; `./dex` wraps the DEX nets so you don't have to remember
`-p lasair6 -f docker-compose.lasair6.yml`.

## Watching any net: `netwatch` (one head, finality, state parity)

Every net is judged by the same client-neutral tool, `offchain/netwatch.py` (issue #15).
It reads each node through its public interface only — JIP-2 (`ws://…`) where the node
serves it, lasair's Prometheus gauges (`http://…/metrics`) until lasair does (lasair#68)
— and answers the epic's shared acceptance: **A1** one head (best blocks agree by hash
within `--max-lag` slots; no fork, lag or outage longer than one epoch) and liveness,
**A2** the finalized head advances on every node with one hash per slot, **A3** the
service state digests the same on every node at the common finalized head (`parity`).

A node is `NAME,CLIENT,URL[,READER]`; `CLIENT` is a label (dashboards and verdicts group
by it), the URL scheme picks the reader, and `READER` is lasair's CE-129 bridge for
`parity` (it reads at its own head, so lasair parity is best-effort until #26 moves the
jamnp backend to finalized reads; a lasair node without a reader of its own is skipped
and listed — lasair6's one reader follows lm0, so name it on lm0 only).

```bash
# all-lasair (lasair6): by slot/height, parity through the one reader (follows lm0)
NETWATCH_NODES="lm0,lasair,http://lm0:9615/metrics,http://reader:19990 lm1,lasair,http://lm1:9615/metrics …"
# all-PolkaJam / JavaJAM / pbnjam: JIP-2 on each node's RPC port
NETWATCH_NODES="pj0,polkajam,ws://pj0:19800 pj1,polkajam,ws://pj1:19800 jj0,javajam,ws://jj0:19800 …"

python3 offchain/netwatch.py poll --duration 600 --samples /shared/chain.jsonl \
    --validators pj0,pj1,…  --require-finality       # A1/A2: exit 0 iff the verdict passes
python3 offchain/netwatch.py parity --service 100 --dex-url http://dex:8080 \
    --json --out /shared/parity.json                 # A3: book, balances, custody, cv/lp,
                                                     #     registry, landed-round markers
python3 offchain/soak_verdict.py /shared/order_events.jsonl --target 0.9999 \
    --chain /shared/chain.jsonl --parity /shared/parity.json   # A4 with the chain folded in
```

`netwatch.py serve` is the same sampler as a Prometheus exporter (`:9106/metrics`,
`/verdict`); the monitor image runs it when `NETWATCH_NODES` is set, and both
`monitor/prometheus*.yml` scrape `netwatch:9106`. A net's monitor overlay adds it as

```yaml
  netwatch:
    build: { context: ., dockerfile: monitor/Dockerfile }
    environment:
      NETWATCH_NODES: "pj0,polkajam,ws://pj0:19800 lm3,lasair,http://lm3:9615/metrics …"
      NETWATCH_VALIDATORS: "pj0,pj1,pj2,lm3,lm4,lm5"   # node behind each validator index
      NETWATCH_SAMPLES: /shared/chain.jsonl           # optional: the soak's --chain input
    networks: { <the net>: {} }
```

(replacing the mixed overlay's log-scraping `exporter` service, whose `jam_pi_*` series
would otherwise be counted twice). `make verify-mixed` runs `netwatch poll` too.

## Finality: why the DEX needs it, and what the clients share

A fill is durable only once its block is finalized: a finalized block cannot be
re-orged, so a filled order cannot be un-filled. GRANDPA finalizes with a 2/3 + 1
supermajority, 5 of 6 on these nets.

- **`lasair6` and `pj6`** each finalize within one client: all six validators run the
  same client's GRANDPA.
- **`mixed`** (3 : 3) has no shared finality gadget: PolkaJam runs `dummy`. The DEX
  does not settle there (a 45-minute run in 2026-07: 138
  work-items guaranteed, volume 0; [`SOAK_RELIABILITY.md`](SOAK_RELIABILITY.md)).
- A mixed net finalizes only when its clients count each other's votes. **`pj-javajam`**
  (3 PolkaJam : 3 JavaJAM) does, once JavaJAM is up before PolkaJam casts its one-shot
  round-1 vote (#18): the DEX runs there too.

**What the specs say.** The Graypaper names GRANDPA and the vote data (the best block's
header plus its posterior state root) and requires a block to be audited before it is
finalized. JAMNP-S has every node announce its finalized head in the UP-0 handshake
(`final`). The votes' wire format is not in JAMNP-S; it is in a public, unmerged draft,
[`zdave-parity/jam-np` PR #6 "Grandpa protocols"](https://github.com/zdave-parity/jam-np/pull/6):
CE 130 (justification request), CE 149 (vote), CE 150 (commit), CE 151 (state), CE 152
(catch-up), CE 153 (warp sync), multi-round types (Set Id, Round Number, Target = header
hash ‖ posterior state root) and the signing domain `"jam_grandpa_vote"`. Its CE numbers
were renumbered once (2025-11), so it can still change.

- lasair implements the draft (`LASAIR_FINALITY=grandpa`, on by default in lasair6;
  making it lasair's own default and the cross-client gates: lasair#66).
- PolkaJam and JavaJAM both offer `--finality-mode grandpa`. Whether a client's votes
  follow the draft is settled only by its public behaviour on a shared net, judged
  against the draft text, never against another client (#18, #20).

**Checking the result** needs no shared votes. netwatch's A2 check compares every
JIP-2 node's finalized head by hash (`finalizedBlock`). lasair's finality bridge
(`docker-compose.pj-majority.yml`) reads a peer's UP-0 `final` and checks it byte for
byte against lasair's own finalized block at that slot: 60/60 AGREE against six PolkaJam GRANDPA validators at
GP 0.8.0 (lasair `docs/GP_0_8_0_PLAN.md`, Gate 6, 2026-09-24).
