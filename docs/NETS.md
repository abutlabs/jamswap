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
| `nets/compose/<net>.yml` | per-index client layouts | — | **Generated** test nets (below): PolkaJam, JavaJAM, pbnjam and lasair in any mix. `./dex up NET=<net>`. |

## Test nets: one command per net

Every net is a profile in [`nets/profiles.py`](../nets/profiles.py); `./dex nets` lists
them. Add `NET=<name>` to any `./dex` verb (default `lasair6`, which behaves exactly as
before):

```bash
./dex up NET=pj6            # build/pull, mint the shared genesis, start every node
./dex heads NET=pj6 600     # one head + finality across every node for 600 s (default 300)
./dex status NET=pj6        # one sample of the same
./dex logs NET=pj6 pj3      # a node's log (native JavaJAM nodes too)
./dex down NET=pj6          # tear down, wipe the chain, stop native nodes
./dex gen                   # regenerate nets/compose/*.yml after editing nets/
```

| Net | Validators 0..5 | Finality | For | Status (2026-09-26, Apple M1 Pro) |
|---|---|---|---|---|
| `lasair6` | lasair ×6 | lasair GRANDPA (PR #6 draft) | the DEX | hand-written `docker-compose.lasair6.yml`, unchanged |
| `mixed` | pj ×3, lasair ×3 | none shared | #2 research | hand-written `docker-compose.mixed.yml` |
| `pj6` | pj ×6 | GRANDPA | #17 | **one head + finality**: 61/61 samples SAME over 6 min, finalized advanced on all six |
| `pj-pbnjam` | pj ×5, pbnjam | GRANDPA (pj's 5-of-6) | #19 | **pbnjam can't start** (see below); the five PolkaJam nodes keep one head and finalize |
| `pj-pbnjam-42` | pj ×4, pbnjam ×2 | GRANDPA | #19 | blocked on the pbnjam image |
| `pj-javajam` | pj ×3, JavaJAM ×3 | GRANDPA both | #18 | **one head, no finality**: 70/71 samples SAME over 7 min (the other: JavaJAM still starting); finalized stays at genesis on all six |
| `pj-javajam-42` | pj ×4, JavaJAM ×2 | GRANDPA both | #18 | untested (same adapters as `pj-javajam`) |
| `lasair-pj-javajam` | lasair ×2, pj ×2, JavaJAM ×2 + DEX | GRANDPA | #20 | needs lasair#54/#60/#66 |
| `nolasair` | pj ×2, JavaJAM ×2, pbnjam ×2 | GRANDPA | #21 | needs #18 and #19 |

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
assure on its own it derives every dev secret — until lasair#54 (one key per validator),
#60 (CE-134/135 co-guaranteeing) and #62 (assurances) land. That is now an explicit,
per-net switch:

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

### What the clients did (2026-09-26)

- **PolkaJam + JavaJAM (`pj-javajam`) co-author one chain** — blocks from both clients,
  one best hash on all six nodes (JavaJAM native on the Mac, PolkaJam in Docker,
  addressed through the host IP) — **but never finalize.** Both run `--finality-mode
  grandpa` and talk over the PR #6 streams: PolkaJam logs every JavaJAM validator's
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
  three-node launches and 1 of 14 single-node starts. `nets/supervise.py` restarts it (a `[supervise]` line in its log),
  which then joins normally. After such a restart it logs (debug) ~200 `Import rejected:
  bad_state_root` in the first second of re-sync, then follows the head.

- **pbnjam-node `main-54226be`** does not start, on either arch: every run (18 restarts
  in the soak, `--help` too) exits 1 with `ENOENT: no such file or directory, open
  '/app/packages/bandersnatch-vrf/wasm-ark-vrf/ark_vrf_wasm_bg.wasm'` (Bun 1.4.2). The
  published image lacks its Bandersnatch wasm; it is the only published version
  (`latest` = `main` = the same digest). The five PolkaJam validators of `pj-pbnjam`
  still kept one head and finalized (5-of-6 GRANDPA).

## The `Makefile` still works

`make up`, `make mixed`, `make monitor`, etc. are the older entry points and still valid —
`./dex` just wraps the one you want 95% of the time so you don't have to remember
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
`parity` (it reads at its own head, so lasair parity is best-effort until lasair#70; a
lasair node without a reader of its own is skipped and listed — lasair6's one reader
follows lm0, so name it on lm0 only).

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
   cross-client — then a real 3:3 mixed net finalizes. **UPDATE 2026-07-15: a public draft
   of exactly this exists** — [`zdave-parity/jam-np` PR #6 "Grandpa protocols"](https://github.com/zdave-parity/jam-np/pull/6)
   (opened 2025-05, actively revised through 2025-12, reviewed by the spec owner, unmerged).
   It defines CE 130 (justification request), CE 149 (vote), CE 150 (commit), CE 151
   (state), CE 152 (catch-up), CE 153 (warp sync), full multi-round GRANDPA types
   (Set Id, Round Number, Target = header hash ‖ posterior state root), and the signing
   domain `"jam_grandpa_vote"`. PolkaJam publicly offers `--finality-mode grandpa`;
   whether its wire protocol is this draft is an open question that only its public
   behaviour can settle (does a PolkaJam node accept and answer the draft's streams?),
   and that is what the cross-client finality gates test. Implementing a *published
   draft spec* is clean-room-safe (it's a public document); the risk is only that an
   unmerged draft can still change (CE numbers were renumbered 2025-11).

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
advertise + cross-check the finalized head. (2) works now; (1) is no longer hypothetical —
the jam-np PR #6 draft is implementable today, PolkaJam already speaks it, and a lasair
implementation would give a 3:3 mixed net six voters in ONE gadget (5-of-6 quorum → true
shared finality). Corroborating community signal (Let's JAM room, 2026-05): the spec author
confirms no network protocol is specified yet for BEEFY (post-finality proof aggregation)
and expects finality protocols "defined by the time M2 testing happens."
