# Mixed-chain DEX settlement — problem, root cause, and open avenues

**Status:** partially resolved. A lasair-dominant mixed chain (`make mixed-dex`)
settles trades end-to-end today. An **equal 3 PolkaJam / 3 lasair** mixed chain
(`make mixed`) deploys the service and forms guarantees but **cannot settle trades**.
This document explains why, records the evidence, and lists avenues for making the
equal split (or any genuinely balanced mixed chain) settle trades too.

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

`verify` PASS = register / duplicate-survival / deposit / withdraw all accumulate.

**Caveat:** even in the working 5:1 case, settlement only begins once the chain reaches
**Safrole ticket-seal steady state (~1–2 epochs after launch)**. Running `verify`
immediately after `up` times out during the AURA→ticket warm-up (not a failure of the
mechanism — the chain just isn't linear yet).

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
- With `--finality-mode dummy`, is there **any** finality signal that could pin lasair's
  branch? If PolkaJam supports a real finality mode on the devnet, that might stop the
  orphaning.
- Minimum lasair authoring share for reliable settlement: 5:1 works, 4:2 doesn't — is
  there a threshold in between, and does it depend on ticket-seal vs. AURA phase?

---

## How to reproduce

```sh
# Equal split — DEX deployed, UI up, but trades DON'T settle:
make mixed                       # or: docker compose -f docker-compose.mixed.yml up
#   watch it fail to accumulate (after the chain has some slots):
docker compose -f docker-compose.mixed.yml exec -T dex python3 /app/verify.py
#   -> FAIL: timed out waiting for register to accumulate
#   inspect the guarantor:
docker logs jamswap-lm3-1 | grep -E "ce133|core_not_engaged|guaranteed"

# Lasair-dominant — trades DO settle (wait ~1-2 epochs for ticket-seal steady state):
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
