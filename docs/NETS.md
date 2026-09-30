# Which net is which

The DEX runs on three nets today. All are GP 0.8.0, all finalize under GRANDPA, and on
all of them every validator holds only its own key:

- **`lasair6`** (the `./dex` default): six lasair validators. The DEX reaches them
  over JIP-2, served by lasair's `lasair-reader` ([below](#the-dex-on-lasair-lasair6)).
- **`pj6`**: six stock PolkaJam validators and no lasair anywhere. The DEX reaches them
  over JIP-2 and deploys its service at startup ([below](#the-dex-on-jip-2-pj6-17)).
- **`lasair-pj`**: three lasair and three PolkaJam validators on one chain, one
  finality. lasair guarantees the DEX's work, co-signed by either client
  ([below](#the-dex-on-lasair-and-polkajam-lasair-pj-20)).

The other nets are cross-client research: do different clients keep one head, finalize
together and hold the same state? None of them is a DEX net today.

## Just run the DEX

```bash
./dex up                # lasair6: start, wait for finality → http://localhost:8081
./dex up NET=pj6        # pj6: start, deploy + set up the service → http://localhost:8201
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
| `lasair6` | lasair ×6 | 0.8.0 | GRANDPA (lasair, jam-np PR #6 draft) | **runs**: JIP-2 through lasair-reader, service in genesis; A1–A4 pass (10-min soak); UI :8081 | #12, #26 |
| `pj6` | PolkaJam ×6 | 0.8.0 | GRANDPA (PolkaJam) | **runs**: JIP-2, runtime deploy; A1–A4 pass (10-min and 1-hour soaks); UI :8201 | #17 |
| `mixed` | PolkaJam ×3, lasair ×3 | 0.8.0 | none shared (PolkaJam runs `dummy`) | UI on :8090 through lasair's bridges; does not settle (no shared finality; a 45-min run in 2026-07 cleared nothing) | #2 |
| `pj-javajam` | PolkaJam ×3, JavaJAM ×3 | 0.8.0 | GRANDPA on both: one head, but finality stays at genesis | none | #18 |
| `pj-javajam-42` | PolkaJam ×4, JavaJAM ×2 | 0.8.0 | GRANDPA on both; not run yet | none | #18 |
| `pj-pbnjam` | PolkaJam ×5, pbnjam ×1 | 0.8.0 (pbnjam: unconfirmed) | GRANDPA: the five PolkaJam nodes finalize alone (5 of 6) | none; the pbnjam image does not start | #19 |
| `pj-pbnjam-42` | PolkaJam ×4, pbnjam ×2 | 0.8.0 (pbnjam: unconfirmed) | GRANDPA | none; blocked on the pbnjam image | #19 |
| `lasair-pj` | lasair ×3, PolkaJam ×3 | 0.8.0 | GRANDPA, shared: both clients count each other's votes | **runs**: JIP-2 through lasair-reader, service in genesis; A1–A4 pass (10-min soak), state parity across both clients; UI :8206 | #20, #26 |
| `nolasair` | PolkaJam ×2, JavaJAM ×2, pbnjam ×2 | 0.8.0 | GRANDPA | none in the profile yet; not run: needs #18, #19 | #21 |

Versions: lasair `ghcr.io/abutlabs/lasair:2.1.3`, PolkaJam `nightly-2026-09-22` (0.1.29),
JavaJAM 0.4.3, pbnjam-node `main-54226be` (the image does not state its GP version;
likely 0.8.0, see #19). Status as of 2026-09-27 (lasair6, lasair-pj: 2026-09-30) on an Apple M1 Pro.
**2026-09-30, lasair 2.1.3** (JIP-3 from every lasair node, readers on JIP-2's 19800):
`lasair6` 10-minute soak PASS (130/130 one-head samples, 138 finalized slots hash-checked,
state parity on all six nodes, 242 orders, 0 refused); `lasair-pj` 10-minute soak PASS
(130/130, 130 finalized slots, the same service-state digest on 3 lasair and 3 PolkaJam
nodes, 242 orders, 0 refused); `offchain/jip2_check.py` passes against lasair's reader and
a PolkaJam node; `offchain/deploy.py` deploys a second service at runtime on `lasair6`
through the Bootstrap service in 29 s. Details
[below](#what-the-clients-did-2026-09-26). `lasair6` and `mixed` are hand-written
compose files; the others are generated from [`nets/profiles.py`](../nets/profiles.py).

## The compose files, one line each

| File | Net | UI | What it's for |
|---|---|---|---|
| `docker-compose.lasair6.yml` | 6× lasair, GRANDPA | :8081 | The DEX on lasair (`./dex up`, `./dex soak`). |
| `nets/compose/<net>.yml` | per-index client layouts | pj6: :8201, lasair-pj: :8206 | **Generated** nets (below), including `pj6`, the DEX on PolkaJam, and `lasair-pj`, the DEX on both. `./dex up NET=<net>`. |
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
# the DEX nets (lasair6, pj6, lasair-pj):
./dex load NET=pj6          # start the load generator (up leaves it stopped); noload stops it
./dex soak NET=pj6 3600     # A1-A4 in one command (below); default 600 s
```

### Layout, keys and genesis

A layout is one client per validator index: `lasair`, `pj` (`polkajam`), `pbnjam`,
`javajam` (`jj`); tiny = 6. Validator *i* is always the **standard JAM dev account i**
(JIP-5: seed = `u32-LE(i)` × 8), whichever client runs it, and that client holds exactly
that key: JavaJAM and pbnjam start with `--dev-validator i`, PolkaJam loads dev seed *i*
(`--key-seed-file`, same keys as its `--dev-validator i`), lasair runs `DEV_VALIDATOR=i`
(`--dev-validator i`, lasair#54).

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
| lasair | `${LASAIR_IMAGE}` (default `ghcr.io/abutlabs/lasair:2.1.3`) | mesh entrypoint, `DEV_VALIDATOR=i`, `WALL=1` next to wall-clock clients | JIP-2 through the node's own `lasair-reader` (lasair#68) on DEX nets; `./dex heads` reads its `STATUS` log line |
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

**No node signs as another validator.** Every client holds only its own dev key,
lasair too since 2.1.0: one key per process (lasair#54), guarantees co-signed by the
core's other validators over CE-134/135 whatever their client (lasair#60), and
assurances as itself (lasair#62). lasair's devnet mode stays behind one switch, which
`./dex` passes to every lasair net (lasair6, mixed and the generated ones); the node's
entrypoint wrapper turns it into lasair's flags:

| `LASAIR_DEV_ALL_KEYS` | a lasair node runs | it signs as |
|---|---|---|
| `0` (default) | `DEV_VALIDATOR=i` → `--dev-validator i` | validator *i* only |
| `1` | `OWN=i` → `--dev-all-keys --own i` (devnet only) | any lasair index of the layout for guarantees (`GUARANTOR_OWN`), any validator for assurances |

A lasair image older than 2.1.0 has neither `--dev-validator` nor `--dev-all-keys`: run
it with `LASAIR_DEV_ALL_KEYS=1` (its entrypoint then passes a bare `--own i`).

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
each holding only its own key, `LASAIR_FINALITY=grandpa`, chain-paced `WALL=0` at one
slot every 6 s) and the DEX on the chain adapter's `jip2` backend, through lm0's reader:

| Service | What it is |
|---|---|
| `lm0`..`lm5` | the validators; `spec-init` mints their genesis with the service in it |
| `reader`, `reader1`..`reader5` | `lasair-reader` on lm0..lm5: the JIP-2 node RPC over a WebSocket on its HTTP port (lasair#68) with reads proven against the block's state root (lasair#70), and `submitWorkPackage` to the first lasair guarantor that takes it (`LASAIR_RPC_GUARANTORS`: every node's builder endpoint), which the core's other two validators co-sign (CE-134/135) |
| `dex` | `offchain/server.py` with `CHAIN_BACKEND=jip2` on `ws://reader:19800`, `SERVICE_ID=100` (seeded into genesis: a lasair guarantor refines only the service it hosts), `RESERVE_TOPUP=1`, `SETTLE_HOLD_SECS=0`; UI on `:8081` |
| `builder` | `jamnp-builder`, lasair's HTTP → CE-133 bridge: the dex uses it only with `LASAIR_DEX_BACKEND=jamnp` (below) |
| `loadgen` | `offchain/loadgen.py` (`./dex load`) |
| `netwatch` | every node over its reader's JIP-2 (`127.0.0.1:9300`); `./dex soak` runs A1–A4 as on pj6 |

**Why 6 s slots.** Since lasair#69 the builder sets a package's context when it submits
it: the anchor must still be among the last 8 blocks, and the lookup anchor within 24
slots, when the guarantee is reported. lasair6 used to author a slot a second; at that
pace the window is 8 s, while a 48-order round takes 15–100 s to refine on a laptop (all
three co-guarantors refine it, and the auditors again). The large rounds expired
unreported (`anchor_too_old`, `lookup_anchor_too_old`) and settlement stalled after the
first minute: SLO 0.26 in a 10-minute soak (2026-09-28). At 6 s the same soak passes:

```
clearing SLO        : 1.000000  (target 0.9999)  PASS
  cleared           : 242
  missed            : 0  (expired/lost 0, stuck-open 0)
SEALED zero-loss    : PASS  (seen 23, terminal 23, stuck-open 0)
clear latency       : p50 59.0s  p99 143.7s
one head            : PASS  (hash (JIP-2 nodes); 130/130 samples ok, max lag 1 slots, 0 divergence episode(s))
finality            : PASS  (finalizing, required; 0 conflict(s), 0 regression(s), longest stall 2 slots vs 12, 134 finalized slots hash-checked)
authoring (pi)      : PASS  (blocks per validator {'0': 46, '1': 39, '2': 29, '3': 41, '4': 35, '5': 38})
state parity        : PASS  (all digests agree; service 100, 74 keys at final slot 229 0x23f56642 (from lm0), attempt 1)
VERDICT (orders + chain): PASS
offered load        : PASS  (240 orders offered, 0 refused, 0 busy)
```

(`./dex soak 600`, lasair 2.1.0 = local build of 96b40ca, Apple M1 Pro; every validator
credited with 19–22 guarantees and 36–39 assurances, each signed as itself.)

The DEX runs on lasair as on pj6 (#26): it reads the finalized state itself, and the
service footprint is readable, so JAMKB backpressure and the reserve keeper are live.
Only the service differs: it is seeded into genesis, not deployed at startup.
`LASAIR_DEX_BACKEND=jamnp` puts the dex back on lasair's HTTP bridges (`builder` and
`reader`'s `/read`), for a lasair image whose reader serves no JIP-2 (before 2.1.0);
there the reader answers at its head only, so the DEX gates reveals and receipts by
comparing heights with lm0's finalized gauge, and the footprint reads 0.

With the dex on JIP-2 (2026-09-28, `ghcr.io/abutlabs/lasair:2.1.2`, `./dex soak 600`,
another net sharing the CPU):

```
clearing SLO        : 1.000000  (target 0.9999)  PASS
  cleared           : 212
  missed            : 0  (expired/lost 0, stuck-open 0)
SEALED zero-loss    : PASS  (seen 21, terminal 18, stuck-open 0)
clear latency       : p50 33.8s  p99 128.9s
one head            : PASS  (hash (JIP-2 nodes); 130/130 samples ok, max lag 1 slots, 0 divergence episode(s))
finality            : PASS  (finalizing, required; 0 conflict(s), 0 regression(s), longest stall 2 slots vs 12, 130 finalized slots hash-checked)
state parity        : PASS  (all digests agree; service 100, 98 keys at final slot 242 0xc7d353f9 (from lm0), attempt 1)
VERDICT (orders + chain): PASS
offered load        : PASS  (241 orders offered, 0 refused, 0 busy)
```

33 orders were still open when the verdict ran, all placed in the last 90 s of load:
their round was released at the 180 s round gate and requeued, and none was open
600 s (the stuck limit).

### The DEX on lasair and PolkaJam: lasair-pj (#20)

`./dex up NET=lasair-pj` runs lasair on validators 0–2 and stock PolkaJam on 3–5, each
holding only its own key, on one genesis (`polkajam gen-spec`, the service written into
it by lasair). lasair runs wall-clock (`WALL=1`) next to PolkaJam and both run the jam-np
PR #6 GRANDPA draft; 5 of 6 votes finalize, so neither client finalizes without the
other. The DEX stack is lasair6's: the dex on lm0's `lasair-reader` over JIP-2, which
hands each work-package to a lasair guarantor only (CE-133); that guarantor shares it
with the core's other two validators over CE-134 whatever their client and collects
their signatures (CE-135). Every lasair node has its own reader, so netwatch reads all
six nodes over JIP-2 by hash, and `./dex soak NET=lasair-pj` runs A1–A4 as on pj6.

Result (2026-09-28, Apple M1 Pro; lasair 2.1.0 = local build of 96b40ca, PolkaJam
nightly-2026-09-22; fresh net; loadgen trading at RATE 12, 20 % of sells sealed):

```
clearing SLO        : 1.000000  (target 0.9999)  PASS
  cleared           : 237
  missed            : 0  (expired/lost 0, stuck-open 0)
SEALED zero-loss    : PASS  (seen 19, terminal 18, stuck-open 0)
one head            : PASS  (hash (JIP-2 nodes); 130/130 samples ok, max lag 2 slots, 0 divergence episode(s))
finality            : PASS  (finalizing, required; 0 conflict(s), 0 regression(s), longest stall 1.0 slots vs 12, 131 finalized slots hash-checked)
authoring (pi)      : PASS  (blocks per validator {'0': 23, '1': 21, '2': 24, '3': 20, '4': 19, '5': 27})
state parity        : PASS  (all digests agree; service 100, 74 keys at final slot 9137257 0xe652ea42 (from lm0), attempt 1)
  lm0 lm1 lm2 (lasair), pj3 pj4 pj5 (polkajam): 2db0b150650ea02f  present 51  (pinned)
VERDICT (orders + chain): PASS
offered load        : PASS  (240 orders offered, 0 refused, 0 busy)
```

Every DEX work-package was guaranteed by lasair; 78 of the 84 guarantees lasair made
carry a PolkaJam co-signature (e.g. validators `[0,4,5]`: one lasair, two PolkaJam), and
the chain's activity statistics credit all six validators with guarantees (22–30 each)
and assurances (40 each). The digest is read on every node at the same finalized block,
and after the run PolkaJam's `stateRoot` for its finalized block equals lasair's. That
run had the dex on lasair's HTTP bridges; with the dex on the reader's JIP-2 (#26;
`ghcr.io/abutlabs/lasair:2.1.2`, another net sharing the CPU) the same soak passes too:
SLO 1.000000 (234 cleared, 0 missed; 7 resting, 3 open), sealed zero-loss (24 seen, 21
terminal, 0 stuck), one head 130/130, 104 finalized slots hash-checked, state parity
`248d6f808bb103f0` on lm0–lm2 and pj3–pj5 (97 keys, final slot 9140069), 240 orders
offered and none refused.

### The DEX on JIP-2: pj6 (#17)

`./dex up NET=pj6` starts six stock PolkaJam validators with GRANDPA and the DEX, with
no lasair image anywhere (the genesis minter is the plain `polkajam` build target) and
nothing injected into genesis. Every DEX layout without a lasair node gets this stack
(`nets/netgen.py` `dex_backend`: `gateway`; a layout with lasair gets the dex on its
first lasair node's reader, `reader`):

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
acceptance in one command, on a freshly started net: loadgen on and `netwatch poll
--require-finality` over the six validators for SECS + 180 s of drain (A1 one head,
A2 finality on every node), then `netwatch parity` at the common finalized head (A3:
books, balances, custody, registry, landed-round markers on every node), then
`soak_verdict.py <the dex's order events> --chain --parity` (A4), and the offered load
as the load generator counted it (at most 1 − target refused or busy). Everything lands
in `~/.cache/jamswap/soak/<net>-<UTC time>/` (`--out` to choose) with a `DONE` marker;
exit 0 iff all four pass.

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

### What the clients did (2026-09-26)

- **PolkaJam + JavaJAM (`pj-javajam`) co-author one chain** — blocks from both clients,
  one best hash on all six nodes (JavaJAM native on the Mac, PolkaJam in Docker,
  addressed through the host IP) — **but never finalize.** Both run `--finality-mode
  grandpa` and exchange GRANDPA messages: PolkaJam logs every JavaJAM validator's
  GRANDPA view ("updated view. Now at 1, 0") and 9 incoming round-1 messages; JavaJAM
  logs "Grandpa state received for round 1, set 0" and "Grandpa vote received for round 1
  and set 0". Yet round 1 of set 0 never completes: PolkaJam's round state stays
  `prevote_ghost = genesis, estimate = genesis, finalized = None, completable = false`
  after it prevoted and precommitted, and `finalizedBlock` is genesis on every node for
  the whole soak (the same PolkaJam build finalizes within seconds as `pj6`). So the votes
  cross the wire but are not counted across clients (3 + 3 < the 5-of-6 quorum). Next
  (#18): which side drops which vote, judged against the PR #6 text (vote encoding and
  signing context).
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
It reads each node through its public interface only — JIP-2 (`ws://…`), which a lasair
node serves through its own `lasair-reader` (lasair#68: `ws://reader…:19800`), or a
node's Prometheus gauges (`http://…/metrics`) where neither is running — and answers the epic's shared acceptance: **A1** one head (best blocks agree by hash
within `--max-lag` slots; no fork, lag or outage longer than one epoch) and liveness,
**A2** the finalized head advances on every node with one hash per slot, **A3** the
service state digests the same on every node at the common finalized head (`parity`).

A node is `NAME,CLIENT,URL[,READER]`; `CLIENT` is a label (dashboards and verdicts group
by it), the URL scheme picks the reader. On JIP-2 every node is compared by hash and
`parity` reads every node at the same finalized block. A metrics-only node is compared by
slot and height, and `READER` is its CE-129 bridge for `parity`, read at the reader's
head (not pinned; a node without a reader of its own is skipped and listed).

```bash
# lasair (lasair6, lasair-pj): each node through its own lasair-reader's JIP-2
NETWATCH_NODES="lm0,lasair,ws://reader:19800 lm1,lasair,ws://reader1:19800 …"
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
- A mixed net finalizes only when its clients count each other's votes. **`lasair-pj`**
  (3 : 3) does: 5 of 6 votes need both clients, and the finalized head advanced on all
  six nodes with one hash per slot (131 finalized slots hash-checked in a 10-minute soak,
  #20). `pj-javajam` does not: each client logs GRANDPA messages from the other, but
  finality stays at genesis (#18).

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
