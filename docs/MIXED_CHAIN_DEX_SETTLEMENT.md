# Mixed-chain DEX settlement — problem, root cause, and resolution

**Status: RESOLVED (2026-07-08).** The **equal 3 PolkaJam / 3 lasair** mixed chain
(`make mixed`) now settles trades end-to-end — `verify` ALL PASS (register /
duplicate-survival / deposit / withdraw), reproduced twice on a fresh chain. The fix
is the tier-1 plan from
[Research findings (2026-07-08)](#research-findings-2026-07-08--path-to-a-settling-33-chain):
a fork-choice-aware guarantor (descend-check + re-guarantee on branch loss),
**assure-any-pending-when-authoring**, and the builder fanning CE-133 out to all
three lasair nodes. Shipped in lasair **client-v1.7.0** (`ghcr.io/abutlabs/lasair:1.7.0`, the default
image), so plain `make mixed` settles out of the box. The lasair-dominant `mixed-dex` overlay is no longer needed for
settlement but remains as a near-linear-chain configuration. The body below is kept
as the root-cause record and the map of longer-term avenues (real DA, CE-135/141).

---

## TL;DR

A JAM work-report only **accumulates** (i.e. a DEX order actually mutates on-chain
state) once it becomes **available**, and availability requires a **>2/3 super-majority
of assurances** (5 of 6 on the tiny config) to land **on the canonical branch within a
5-slot window** (`U = u_timeout = 5`).

- Only **lasair** can produce those assurances — it derives every validator's key
  (all validators are standard dev accounts; see [Key symmetry](#key-symmetry)).
- On an **equal 3:3** chain, the lasair guarantor's guarantee + assurance blocks
  **race PolkaJam's blocks under longest-chain fork choice and get orphaned** before
  the 5-slot window closes. Result: guarantees form, but the report times out and
  clears, so nothing accumulates.
- When lasair authors the **canonical chain nearly exclusively** (the `mixed-dex`
  overlay: one lasair node authors validators 1–5, pj0 authors validator 0), the chain
  is effectively linear, the guarantee+assurance dance completes, and trades settle.
- A **4:2** lasair majority was tested and **still fails** — PolkaJam's ~1/3 of blocks
  fork often enough to break the window. Only near-linear works.

The tension is fundamental: **equal authoring** (what makes the mixed chain a fair
consensus comparison) is exactly what prevents a lone lasair guarantor from keeping its
availability dance on the winning branch.

---

## Background: how a DEX order reaches on-chain state

The jamswap DEX submits each order as a **CE-133 work-package** to a lasair guarantor
node (the builder posts to `LASAIR_GUARANTOR_HOST=172.28.0.13:40064` = lm3;
`docker-compose.mixed.yml:168`). To settle, that work-item must traverse the full JAM
reporting/availability/accumulation pipeline:

1. **Guarantee** — ≥2 validators assigned to a core sign the work-report. lm3 builds
   and signs this (`conformance/live_guarantee.ml:66`, `guarantee_extrinsic`), includes
   it in a block it authors. The report becomes **pending** on that core.
2. **Assurance** — ≥ `2V/3 + 1` validators attest the report is **available**, each
   signature anchored on the **including block's parent**
   (`conformance/live_guarantee.ml:138`, `assurances_extrinsic`). These go in a
   **later** block that descends from the guarantee block.
3. **Availability** — when assurance votes cross the super-majority, the STF marks the
   core available (`conformance/stf_guarantees.ml:2176-2190`,
   `validators_super_majority()` = `2*V/3 + 1`), and…
4. **Accumulation** — the now-available report runs `accumulate`, mutating service
   state (the order lands). Visible via CE-129 reads (the DEX `reader`).

The whole chain 1→4 must complete **on one canonical branch** within **`U = 5` slots**
of the guarantee (`lib/pvm_host.ml:110`, `u_timeout = 5`), or the report **times out and
is cleared** (`conformance/stf_guarantees.ml:1486`, `slot >= cr_timestamp + u_timeout`).

---

## Root cause

Two independent constraints combine to block settlement on an equal chain.

### 1. Only lasair can produce assurances

Assurance signatures are verified against the on-chain validator keys
(`conformance/stf_guarantees.ml:1758`, `bad_signature`). PolkaJam validators **do not
produce assurances** for lasair-guaranteed reports (observed: π `assurances = 0` for all
pj validators, always) — there is no real data-availability distribution, and lasair's
work-reports carry a zero `erasure_root`, so pj has nothing to attest.

Therefore **lasair must forge all the assurances**. It can, but only because every
validator is keyed to a standard dev account (see [Key symmetry](#key-symmetry)), so the
lasair guarantor legitimately holds all six secrets. The client forges assurances for
indices `[0 .. threshold-1]` regardless of which node "owns" them.

### 2. The availability dance needs the guarantor's branch to be canonical

The guarantee is in block *N*; the assurance must be in a descendant of *N* within 5
slots. On an equal 3:3 chain:

- lm3 authors block *N* with the guarantee (report pending).
- In the very next slot, PolkaJam (or the timing of propagation) authors a competing
  block that does **not** extend *N* (it forks at *N-1*).
- Under **longest-chain fork choice with no finality** (`--finality-mode dummy`), the
  chain follows whichever branch grows — often pj's — orphaning lm3's guarantee block.
- When lm3 next authors (even within 5 slots), the fork-choice head no longer descends
  from *N*, so the report isn't pending there → the assurance is rejected
  (`conformance/stf_guarantees.ml:1747`, **`core_not_engaged`**), or the report simply
  times out and clears.

**lm3 authoring only ~1/6 of slots (its single validator) makes this near-certain.**

---

## Evidence / observed symptoms

| Signal | Where | Meaning |
|---|---|---|
| `FAIL: timed out waiting for register to accumulate` | `offchain/verify.py` | order never settled |
| `[ce133] guaranteed work-item on core N (slot S); assuring next` then nothing | lm3 logs | guarantee formed, assurance never landed |
| `✗ import rejected slot S …: core_not_engaged` | lm3 logs | assurance built on a head that dropped the pending report (orphaned guarantee) |
| `✗ import rejected …: bad_attestation_parent` | lm3 logs | (fixed) assurance anchored on the guarantee block instead of the new block's parent |
| pj import log **skips** the guarantee slot (e.g. 7955637 → 7955639) | pj0 logs | pj built a competing branch; lm3's guarantee block orphaned |
| π `guarantees = 0`, `assurances = 0` for **all** validators | RPC `statistics` | no guarantee/assurance ever became canonical |

---

## What was tried (authoring split vs. settlement)

| lasair : pj authoring | Config | Chain shape | Trades settle? |
|---|---|---|---|
| 3 : 3 (equal) | `make mixed`, each lm node owns 1 validator | contested, frequent forks | ❌ guarantee orphaned / times out |
| 4 : 2 | lm3 owns 2,3,4,5; pj0,pj1 own 0,1 | pj's 1/3 forks often | ❌ still races out of the window |
| 5 : 1 | `make mixed-dex`: lm3 owns 1–5; pj0 owns 0 | near-linear (lasair authors ~all) | ✅ **all PASS** |
| **3 : 3 (equal), tier-1 fix** | `make mixed-local` with the fork-choice-aware guarantor + assure-any-pending + builder fan-out | contested, frequent forks | ✅ **all PASS** (2026-07-08, ×2) |

`verify` PASS = register / duplicate-survival / deposit / withdraw all accumulate.

**Caveat:** settlement only begins once the chain reaches **Safrole ticket-seal steady
state (~1–2 epochs after launch)**, and on the EQUAL split the first few minutes also
carry extra fork churn while the DEX bootstrap items settle — a `verify` run right
after `up` can time out on `register` even though the item accumulates a few minutes
later (observed on the published 1.7.0 image: the first run's register settled late,
the immediate re-run passed everything in-window). Not a failure of the mechanism —
give the chain a few epochs, or just re-run `verify`.

---

## Current resolution (`mixed-dex`)

`docker-compose.mixed-dex.yml` overlays the equal-split base:

- `lm3` gets `OWN=1,2,3,4,5` and `GUARANTOR_OWN=1,2,3,4,5` — it authors the
  lasair-dominant canonical chain and signs assurances as any assigned validator.
- `lm4`, `lm5`, `pj1`, `pj2` are gated behind an unused compose profile so they never
  start (they would equivocate on the indices lm3 now authors).
- `pj0` still runs the independent PolkaJam client and derives the same state — it just
  authors negligibly.

`--guarantor-own` (lasair client-v1.6.6) is the key knob: it decouples the
**assurance-signing set** from the **authoring set** (`--own`), so the guarantor can
sign as the full lasair set without equivocating on block production
(`bin/lasair_client.ml:259`, `guarantor_set`).

---

## Fixes already shipped (lasair client-v1.6.6)

These were required just to get guarantees forming and the pipeline correct; they are in
place regardless of split:

1. **Spec-field-preserving `--inject-service-spec`** — the injector now replaces only
   `genesis_state`, passing `id` / `protocol_parameters` / `bootnodes` /
   `genesis_header` through untouched (PolkaJam rejected a spec stripped to two keys:
   `"Missing or invalid 'id' field"`).
2. **`--guarantor-own`** — assurance/guarantee signing set decoupled from authoring.
3. **Assurance anchor fix** — assurances anchored on the **new block's parent**
   (`bin/lasair_client.ml:997`, `~parent_hash:b.Bt.hash`), not the older guarantee block
   (`bad_attestation_parent`).

### Key symmetry

`mixed/gen-spec.py` keys the PolkaJam validators to **standard dev accounts** (seed =
`u32-LE(i) × 8`, raw 32 bytes) instead of random `gen-keys`. **Verified**: PolkaJam
derives **byte-identical** bandersnatch and ed25519/peer_id keys from that seed as
`lasair --dev-account i`, for every index. This is what lets the lasair guarantor forge
**valid** assurances for the pj validators (their secret = a known dev account). Confirm
with: `polkajam list-keys` on a 32-byte seed file vs. `lasair --dev-account i`.

---

## Points of interest for a real fix (making a *balanced* mixed chain settle)

Ordered roughly easy→hard. Each keeps pj as a genuine co-author (the thing `mixed-dex`
gives up).

1. **Fork-choice-aware guarantor.** When lm3 owes an assurance, author it specifically
   on the **descendant of the guarantee block** rather than the global best head, and
   re-issue the guarantee if that branch loses. Today `plan_service_ext`
   (`bin/lasair_client.ml:982`) checks only that the guarantee block is *in the tree*
   (`Bt.find_node tree ghead <> None`), not that the head **descends** from it. A stricter
   check + a "stick to my availability branch" author policy could complete the dance
   before the window closes — but it deliberately forks and only helps if lasair's branch
   can win, so it likely needs to be paired with (2) or (4).

2. **Cross-lasair-node assurance coordination.** With 3 separate lasair nodes each
   authoring ~1/6, the *combined* lasair authoring rate is ~1/2, so **some** lasair node
   usually authors within 5 slots. If lm3 gossiped "report R pending on core C in block
   N, please assure" to lm4/lm5, whichever authors next could emit the (forged) assurance.
   Needs a small control-plane message between lasair nodes; the builder currently posts
   only to lm3.

3. **Longer availability window.** `U = 5` (`lib/pvm_host.ml:110`) is a consensus
   parameter — PolkaJam enforces it too, so it can't be changed unilaterally. Only viable
   if both clients agree on a larger `U` for the devnet spec. Would make the 2-block dance
   far more forgiving of infrequent authoring.

4. **Real data availability.** Distribute erasure-coded chunks (CE-137/CE-139) and set a
   real `erasure_root` so **PolkaJam genuinely assures** reports it has reconstructed.
   This removes the "only lasair can assure" constraint entirely and is the *correct*
   long-term fix — but it's the largest effort (full DA layer + pj participation).

5. **Gossip forged assurances into any author's block.** Since every key is a dev
   account, lasair's forged assurances are cryptographically valid; if pj accepted
   gossiped assurance extrinsics and included them, availability could be reached without
   lasair owning the branch. Depends on PolkaJam's willingness to include externally
   supplied assurances (black-box; unverified).

### Open questions worth pinning down

- Does PolkaJam **reject** lasair guarantee blocks, or only **orphan** them? Evidence so
  far points to **orphaning** (pj logs show no guarantee-block rejection, only benign
  `finality lagging`; the guarantee slot is simply skipped in the import sequence).
  Confirm by forcing pj to build on a known lm3 guarantee block and checking it extends it.
- ~~With `--finality-mode dummy`, is there **any** finality signal that could pin lasair's
  branch?~~ **Answered (2026-07-08):** PolkaJam (nightly-2026-07-04) has
  `--finality-mode grandpa` per `polkajam run --help` — real GRANDPA. Unusable for now:
  lasair has **no** finality implementation at all (`lib/best_chain.ml` models GRANDPA-aware
  fork choice but is dead code; the node runs pure longest-chain), and 3 pj voters can't
  reach the 2/3 GRANDPA threshold alone.
- Minimum lasair authoring share for reliable settlement: 5:1 works, 4:2 doesn't — is
  there a threshold in between, and does it depend on ticket-seal vs. AURA phase?
  **Partially answered:** the 4:2 failure is largely explained by the wedge bug below
  (one lost race pins the guarantor forever), so the "threshold" measured to date
  conflates fork-choice odds with a client bug.

---

## Research findings (2026-07-08) — path to a settling 3:3 chain

A deep pass over both codebases, the running containers, the spec blob, and the JAMNP-S
protocol spec produced four findings that reshape the avenues above.

### F1. No coordination is needed to assure: pending reports are *in state*

Avenue #2 assumed lasair nodes need a control-plane message ("please assure report R").
They don't. A pending report lives in **on-chain state (ρ, the pending-cores array)** —
so any lasair node, when it authors, can scan its **parent block's state** and forge
assurances for *every* pending report, whether or not it guaranteed it
("assure-any-pending"). Two rules that killed the naive approaches are satisfied
automatically when the author includes its own assurances:

- **anchor == parent** (`stf_guarantees.ml:1741`): the author anchors on the parent of
  the block it is building — trivially correct.
- **core_not_engaged** (`stf_guarantees.ml:1747`): the author reads pending cores from
  the exact state it extends — it can only assure genuinely pending reports.

On a 3:3 split lasair's *combined* authoring is ~1/2, so once a guarantee is canonical
the probability that **no** lasair node authors a canonical descendant within the 5-slot
window is ~(1/2)⁵ ≈ 3% per attempt — versus ~(5/6)⁵ ≈ 40% with lm3 alone today.

### F2. Bug: the guarantor wedges permanently after one lost race

`plan_service_ext` gates the assurance on `Bt.find_node tree ghead <> None`
(`bin/lasair_client.ml:987`) — a pure **membership** check against a table that never
evicts, not a *descends-from* check. Once the guarantee block is orphaned, every
subsequent own slot re-attempts the same doomed assurance (`core_not_engaged` forever).
Worse, the self-import-rejection path (`bin/lasair_client.ml:1080-1091`) never clears
`awaiting`, and the original payload is discarded after refine — so there is **no
re-guarantee path at all**. One lost race wedges the pipeline permanently. This alone
plausibly explains the 4:2 failure. Fork choice, for reference, is pure longest-chain
with lowest-header-hash tie-break (`jamnp/block_tree.ml:81-83`).

### F3. `U` is carried in the spec's `protocol_parameters` (byte offset 90)

Decoded the live spec blob from a running pj0: it is the graypaper parameter set in
alphabetical field order, and **U=5 sits at byte offset 90** (u16, between R=4/T=128 and
V=6). PolkaJam *generates* this blob (`polkajam gen-spec`) and the same binary runs
polkadot/dev/toaster chains, so it almost certainly **reads U from the spec**. Lasair
ignores the blob and hardcodes `u_timeout = 5` (`lib/pvm_host.ml:110`; mirrors at
`lib/reporting.ml:20`, `conformance/reports_stf.ml:721`) — but its own encoder
(`pvm_host.ml:170`) already writes the identical layout. So avenue #3 (longer window) is
**not** blocked on PolkaJam cooperation: bump lasair's constant, byte-patch the blob in
`gen-spec.py`, and verify pj honors it (observable in minutes: do reports still clear at
+5 slots?).

### F4. The protocol-correct path is CE-135 + CE-141, and it's half-plumbed

Per JAMNP-S: **CE 135** distributes guaranteed work-reports *guarantor → all current
validators*, and **CE 141** distributes assurances *assurer → all possible block authors*
(~2s before each slot). In spec-correct JAM, **PolkaJam-authored blocks are supposed to
carry lasair's guarantees and assurances** — the "lasair must own the branch" constraint
exists only because lasair never sends these. Current lasair state: CE-141 receive is
log-only (`bin/lasair_client.ml:888-896`), an outbound `distribute_assurance` stub exists
unused (`jamnp/transport.ml:303-306`), CE-135 is absent entirely, `erasure_root` is
hardcoded zero (`conformance/live_guarantee.ml:106`), and the availability shard store is
never populated. Whether pj includes externally received extrinsics is black-box but it
is the standard flow, and cheaply testable.

Also verified: **duplicate guarantees on different branches are benign** — the
`duplicate_package` check (`stf_guarantees.ml:1847-1889`) is computed entirely from the
branch being extended, so the same package guaranteed on two competing branches never
conflicts. Same-branch duplicates are rejected and dropped as today.

### Ranked plan

| Tier | Change | Effort | What it buys |
|---|---|---|---|
| **1a** | Fix the wedge: descend-from check + clear/re-guarantee on branch loss (stash payload with `awaiting`) | small–medium, lasair only | stops permanent wedging; retries until a branch wins |
| **1b** | **Assure-any-pending-when-authoring** (F1) | small, lasair only | any of 3 lasair authors (~1/2 of slots) completes the assurance step |
| **1c** | Builder fans CE-133 out to lm3/lm4/lm5 | tiny, jamswap only | any lasair author can also complete the guarantee step (F4 dedup makes duplicates benign) |
| 2 | Raise U in the devnet spec (F3) | small + one experiment | insurance: widens the window if 3:3 forking is nastier than modeled |
| 3 | Outbound CE-135 + CE-141 (F4) | medium | pj-authored blocks advance the dance too — protocol-correct |
| 4 | Real DA: populate shards, real `erasure_root`, pj genuinely assures | large | the honest long-term fix |

Tier 1 (a+b+c) needs **no PolkaJam changes, no consensus-parameter changes, and no new
network protocol**, and should make a genuine 3:3 chain settle with high probability per
window, with 1a's retry covering the tail.

### Tier 1: SHIPPED and verified (2026-07-08)

All three pieces landed (lasair working tree + this repo) and `verify` passes twice in a
row on a fresh equal 3:3 chain. What changed:

**lasair** (released as client-v1.7.0 / `ghcr.io/abutlabs/lasair:1.7.0`):

- `jamnp/block_tree.ml` — new `is_ancestor` (descent test; `imported` membership is NOT
  canonicality, orphans stay in the table forever). Unit-tested in
  `test/block_tree_test.ml` ("is_ancestor: descent, not membership").
- `bin/lasair_client.ml` `plan_service_ext` rewritten around three moves:
  1. **assure-any-pending** — every authored block assures ALL reports pending in the
     parent state's rho (multi-core bitfield), not just the node's own in-flight one.
     Anchor==parent and core-engagement are correct by construction; the 5-of-6
     assurer set crosses the super-majority in a single block.
  2. **re-guarantee on branch loss / timeout** — `awaiting` now carries the payload;
     when the guarantee block stops being an ancestor of the head (or its report
     cleared without landing in the accumulated ring ξ), the payload is re-queued.
     The self-import-rejected path routes on the importer's reason (captured via
     `on_reject`): `duplicate_package` drops, anything else re-queues — the old path
     dropped unconditionally AND left `awaiting` pointing at the dead branch forever.
  3. **drain-pop dedup** — queued items already reported/accumulated on the branch
     being extended are dropped without costing the slot (guaranteeing one would get
     the whole authored block rejected as `duplicate_package`, forfeiting that slot's
     assurances too). Found live: stale duplicates ahead of a deposit each burned an
     authored slot and pushed settlement past the verify window.
- `bin/jamnp_builder.ml` — `LASAIR_GUARANTOR_HOST`/`PORT` accept comma-separated
  lists; every `/submit` fans the work-package out to ALL targets.

**jamswap**:

- `docker-compose.mixed.yml` — lm4/lm5 get `GUARANTOR_OWN: "3,4,5"`; the builder
  targets `172.28.0.13,.14,.15` on ports `40064,40065,40066` (each node's PORT+1).
- `docker-compose.mixed-dex.yml` — overlay pins the builder back to lm3 only (lm4/lm5
  don't run there).
- `offchain/verify.py` — accumulate-wait window widened to 90 s (`VERIFY_WAIT_TRIES`
  overridable): on the contested chain settlement legitimately takes ~4+ slots.

Observed on the live equal split: guarantees forming on lm3/lm4/lm5, `guarantee block
0x.. lost fork choice; re-queueing work-item` firing and recovering, duplicate drops,
and all four verify steps accumulating. Tiers 2–4 above remain as follow-ups: raising
`U` is now optional insurance, CE-135/141 distribution is the protocol-correct
evolution, real DA the long-term fix.

---

## How to reproduce

```sh
# Equal split with the tier-1 fix — trades SETTLE (lasair >= 1.7.0, the default
# image; wait ~1-2 epochs for ticket-seal steady state):
make mixed
docker compose -f docker-compose.mixed.yml exec -T dex python3 /app/verify.py
#   -> ALL PASS: register / duplicate-survival / deposit / withdraw
#   watch the mechanism work:
docker logs jamswap-lm4-1 | grep ce133
#   -> guaranteed / lost fork choice; re-queueing / already in this branch's pipeline

# Equal split on a PRE-FIX published lasair image — reproduces the original failure:
LASAIR_IMAGE=ghcr.io/abutlabs/lasair:1.6.2 make mixed
docker compose -f docker-compose.mixed.yml exec -T dex python3 /app/verify.py
#   -> FAIL: timed out waiting for register to accumulate
docker logs jamswap-lm3-1 | grep -E "ce133|core_not_engaged|guaranteed"

# Lasair-dominant (near-linear chain; no longer needed for settlement):
make mixed-dex
docker compose -f docker-compose.mixed.yml exec -T dex python3 /app/verify.py
#   -> ALL PASS: register / duplicate-survival / deposit / withdraw
```

Confirm cross-client key symmetry:

```sh
docker run --rm --entrypoint lasair lasair:local --dev-account 0     # bandersnatch/peer_id
docker run --rm --entrypoint sh jamswap-pj-mixed:local -c '
  mkdir -p /root/.config/polkajam/polkadot/keys
  head -c 32 /dev/zero > /root/.config/polkajam/polkadot/keys/dev0.seed
  polkajam list-keys'                                                 # identical keys
```

---

## Key code references

**lasair** (`../lasair`, client-v1.6.6):

| Ref | What |
|---|---|
| `lib/pvm_host.ml:110` | `u_timeout = 5` — the availability window |
| `conformance/stf_guarantees.ml:2176-2190` | availability threshold (`> 2/3·V`) |
| `conformance/stf_guarantees.ml:1486` | report timeout/clear (`slot >= cr_timestamp + u_timeout`) |
| `conformance/stf_guarantees.ml:1741` | `bad_attestation_parent` (assurance anchor must equal header.parent) |
| `conformance/stf_guarantees.ml:1747` | `core_not_engaged` (assurance for a non-pending core) |
| `conformance/stf_guarantees.ml:1758` | assurance `bad_signature` (keys checked against on-chain kappa) |
| `conformance/live_guarantee.ml:66` | `guarantee_extrinsic` (+ `?own` controllable-signer filter) |
| `conformance/live_guarantee.ml:132,138` | `assurance_threshold`, `assurances_extrinsic` |
| `bin/lasair_client.ml:259` | `guarantor_set` (from `--guarantor-own`, decoupled from `--own`) |
| `bin/lasair_client.ml:982-1024` | `plan_service_ext` — the guarantee/assurance two-block dance |
| `bin/lasair_client.ml:997` | assurance anchored on `b.Bt.hash` (new block's parent) |

**jamswap** (this repo):

| Ref | What |
|---|---|
| `docker-compose.mixed.yml:168` | builder → guarantor host (lm3 CE-133 endpoint) |
| `docker-compose.mixed.yml` (lm3 env) | `GUARANTOR_OWN` on the equal-split guarantor |
| `docker-compose.mixed-dex.yml` | lasair-dominant overlay (the working config) |
| `mixed/gen-spec.py` (`dev_seed_file`) | pj validators keyed to standard dev accounts |
| `offchain/verify.py` | the e2e register/duplicate/deposit/withdraw test |

---

## Glossary

- **Guarantee** — validators' signed attestation that a work-report is valid; makes the
  report *pending* on a core.
- **Assurance** — validators' signed attestation that a pending report's data is
  *available*; ≥ `2V/3+1` of them make the core *available*.
- **Accumulate** — the available report's on-chain state transition (the DEX order lands).
- **`U` / `u_timeout`** — slots a pending report has to become available before it's
  cleared (5 on tiny).
- **Tiny config** — `V=6` validators, `C=2` cores, `E=12`-slot epoch.
