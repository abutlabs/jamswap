# Lasair internals — answers for the sealed-order CLOB research

Answers to the ten "questions for lasair-Aiden" from the 2026-07 deep-research pass on
MEV-resistant sealed-order trading on JAM. Every claim below is grounded in the lasair
(GP v0.7.2), zk-jam-service, or jamswap source, cited as `file:line`. Lasair paths are
relative to the lasair repo root.

**TL;DR for the architecture ranking:** the threshold-committee branch (option 2) **is
reachable** on lasair — but not as a host call. A threshold-decrypt host call would be
nondeterministic, and lasair's audit/dispute model would slash the guarantors who signed
the report. The live seam is *decrypt-outside, inject-as-public-data*: a committee
decrypts off-protocol and the work-package builder attaches the plaintext (or the
decryption shares) as package-committed payload/extrinsic bytes, so refine stays a pure
function. The ZK dark-pool branch (option 1) is verify-side cheap: Groth16/BN254 verify
is measured at ~56M gas ≈ 1.1% of the full refine budget. The batch bound is **input
size**, not gas: ~25k–69k sealed orders per work-package at 500–200 B/order.

---

## Q1 — Host-call surface & crypto primitives

**There are no crypto host calls. None.** Services bring all crypto in-PVM.

- Single dispatcher for refine and accumulate: `dispatch` at `lib/pvm_host.ml:1774-1798`,
  wrapped by `execute_host_call` (`:1823-1852`). Refine passes `~acc_ctx:None
  ~refine_ctx:(Some …)` (`conformance/refine.ml:141-144`); accumulate the inverse.
- Implemented calls are exactly the GP 0.7.2 state/DA set: gas(0), fetch(1), lookup(2),
  read(3), write(4), info(5), export(7), bless(14), assign(15), designate(16),
  checkpoint(17), new(18), upgrade(19), transfer(20), eject(21), query(22), solicit(23),
  forget(24), yield(25), provide(26), log(100). IDs at `lib/pvm_host.ml:20-78`.
- **Not implemented:** historical_lookup(6) and the whole inner-PVM machine family
  (8–13) fall through to WHAT (`lib/pvm_host.ml:1798,1835-1838`).
- No ed25519-verify, Blake2, Keccak, SHA, BN254/pairing, or hash-to-curve host call
  exists anywhere. The only crypto inside the host layer is host-internal (provide
  blake2b-hashes a blob to match a request, `lib/pvm_host.ml:1750`). A stale design note
  (`notes/pvm.md:246-251`) mentioning "Ed25519 via host calls" was never implemented.
- The node-side crypto FFIs (`lib/ed25519_ffi`, `lib/bandersnatch_ffi`,
  `lib/erasure_ffi`) are consensus-only — zero references from `pvm_host.ml`, `pvm.ml`,
  or the refine/accumulate loops. `ed25519_ffi` even has `batch_verify`
  (`lib/ed25519_ffi/ed25519_ffi.ml:28`) but it is unreachable from service code.

So yes: the ~195k gas + `min_stack_size!` cost for in-PVM ed25519 is unavoidable on
lasair today. There is no host call that avoids it.

## Q2 — Gas model and measured costs

- **1 gas per instruction, flat** (`lib/pvm_decode.ml:627`, charging loop
  `lib/pvm.ml:827-833`); **10 gas per host call** (e.g. `lib/pvm_host.ml:420`), log
  charging 10 as of GP 0.7.2 (`:1110-1112`), transfer 10 + forwarded allowance
  (`:1177-1182`). This is the Gray Paper schedule, not lasair-specific.
- **Refine ceiling G_R = 1e9 tiny / 5e9 full** (`lib/spec.ml:56,75`), enforced by the
  PVM's own out-of-gas check (machine created with `gas = ri_gas_limit`,
  `conformance/refine.ml:112`) plus a step guard bounded by the gas limit
  (`conformance/refine.ml:159-165` — raised specifically for in-blob SNARK verifiers).
  Node RPC default refine budget is 5e9, overridable via `LASAIR_REFINE_GAS`
  (`node_rpc/node_rpc.ml:71-74`).
- **Measured numbers (committed):**
  - Groth16/BN254 verify (untrusted proof, subgroup-checked): **56,149,565 gas**
    (zk-jam-service `spikes/groth16-gas/README.md:12`) = 1.12% of full G_R, ~89
    verifies per full refine.
  - Full anonymous-vote refine (Groth16 + Merkle/nullifier logic): **59,855,977 gas**
    (zk-jam-service `services/voting/README.md:50`), verified e2e on a lasair-node.
  - Jamswap FBA match of 3 orders: **7,476 gas** (`docs/M1_DEMO.md:20`).
  - **ed25519 verify ~195k gas: NOT committed anywhere** — it exists only in team
    memory. Neither is a Blake2s number. → action item below.

## Q3 — PVM stack/memory model

- Stack size comes from the **3-byte LE field at offset 8** of the standard-program
  header (`lib/pvm_program.ml:403-412`) — lasair applies no default of its own; the
  8 KiB default is the polkavm linker's
  (`polkavm-gp07/crates/polkavm-linker/src/program_from_elf.rs:9919`). ed25519/BN254
  field arithmetic overflows 8 KiB; the first write below the stack region hits
  unmapped memory → `PageFault` → refine maps it to `Panicked`
  (`conformance/refine.ml:185`). That is exactly why `min_stack_size!` is required.
- **Hard cap: 2^24 − 1 ≈ 16 MiB** (3-byte field). Secondary total-layout check at
  `lib/pvm_program.ml:432-441` is nowhere near binding at 16 MiB. Current usage:
  jamswap requests 1 MiB (`service/src/lib.rs:24`), zk-jam-service 4 MiB — 16× and 4×
  headroom respectively.
- Layout: RO data at 0x10000, RW/heap above it, stack at
  `2^32 − 2·2^16 − 2^24 − P(s)` growing down, args region above the stack
  (`lib/pvm_program.ml:483-540`). A big stack shrinks heap room (heap end is one zone
  below stack start, `:583-587`).

## Q4 — Could lasair expose validator-side threshold decryption?

**As a host call: no — and our own dispute machinery is what kills it.**

- Mechanically the dispatcher is trivially extensible (flat match,
  `lib/pvm_host.ml:1774-1798`; unknown IDs are defined behavior, WHAT + 10 gas). But
  the handler would have nothing to decrypt with: `host_context`
  (`lib/pvm_host.ml:282-318`) has **no key, seed, or node-identity field** — exposing
  one means deliberately threading node secrets through `make_context`
  (`lib/pvm_host.ml:1875-1899`), a breach of the current layering. There are also no
  DKG/threshold primitives to build on (the FFIs expose sign/verify/VRF only; no BLS
  secret key exists anywhere — `lib/beefy.ml` is types-only).
- The determinism problem is concrete: the refine output blob IS the work-result digest
  (`lib/work_packages.ml:180,212-228`). A share-dependent host-call result makes each
  auditor's re-execution produce a different digest → `Mismatch` judgments →
  >1/3 negative → bad verdict → report dropped and the ≥2 signing guarantors slashed as
  culprits (`lib/judgments.ml:69-89,149-153,314-320`; disputes STF
  `conformance/disputes_stf.ml:250-253,356-359` — implemented and M1-proven). Note
  lasair's auditor re-execution comparator itself is still a stub
  (`lib/auditing.ml:240-244`), but the on-chain slashing path is real.
- **The deterministic variant exists and fits lasair today:** decryption happens
  *outside* refine, and the plaintext enters as package-committed public data —
  either work-item extrinsics (hash-committed `extrinsic_spec`,
  `lib/work_packages.ml:88-91,120`, served to refine via fetch selectors 3/4) or the
  work-item payload through the operator RPC (`node_rpc/node_rpc.ml:137-147`,
  payload_hash committed in the digest). Caveat: the extrinsic path exists in types and
  host-call plumbing but the current refine executor passes `rc_extrinsics = [||]`
  (`conformance/refine.ml:129`), so the implemented seam today is the payload path.

**Architecture consequence:** "JAM validators as threshold committee" is a *sidecar*,
not a lasair protocol change: committee members run a DKG off-protocol (over fresh keys
— not their consensus keys), publish the encryption key, traders encrypt orders to it,
and at batch-close the committee's decryption (or the shares + a verification check)
is attached to the work package as public bytes. Refine stays pure; the Gray Paper's
auditability model is untouched.

## Q5 — Is refine pure? Yes — definitively.

- `refine_input` (`conformance/refine.ml:33-43`) contains only chain-public data: code,
  core, item index, service id, payload, package hash, gas limit, service accounts.
- `host_context` (`lib/pvm_host.ml:282-318`) and `service_account` (`:234-253`) have no
  validator-specific field at all. Fetch selector 1 ("entropy") deliberately returns the
  **zero hash in refine** (`lib/pvm_host.ml:916-922`); η′ is served only in accumulate —
  and even that is consensus-deterministic.
- Validator secrets are 32-byte seeds held only in the node binaries
  (`bin/lasair_testnet_node.ml:214-215`, `bin/lasair_validator.ml:74-75`) and flow
  exclusively through `jamnp/chain.ml` → `conformance/authoring.ml` → signing FFIs
  (sealing, tickets, guarantees). That path never intersects PVM execution — there is
  no shared data structure to smuggle a key through.

**A JAM service on lasair can never hold or touch a secret. Confirmed at code level.**

## Q6 — DA layer: everything a validator ingests is public by construction

- The whole work-package bundle is erasure-coded 342-of-1023 (`lib/erasure_coding.ml:19-43`;
  production FFI `lib/erasure_ffi/erasure_ffi.ml:24-34`): every validator holds a shard,
  **any 342 (1/3) reconstruct every byte**. Exported segments persist 28 days in the
  D³L (`lib/assurance.ml:20-26`).
- Lasair's JAMNP layer implements only block-request and state-root streams
  (`jamnp/protocol.ml:19-20`); there is no shard protocol yet — and no request
  authentication at all (`jamnp/node.ml:18-28`; the QUIC spike deliberately skips cert
  validation, `rust/quic-ffi/src/lib.rs:55-80`). Even the eventual JAMNP-S key pinning
  authenticates validators as *servers*; it does not restrict who may *request*.
- Work items arrive at the node in the clear over HTTP (`node_rpc/node_rpc.ml:16,211-216`);
  refine sees extrinsics/segments as plaintext arrays (`lib/pvm_host.ml:256-272`).

**A blob "available to refine but not to the public" is not achievable through DA.**
Confidentiality must be applied before bytes enter the work package — ciphertext in,
commitment/ZK out — which is exactly jamswap's sealed-order model. The ciphertext
itself is world-readable forever; treat it as such (no long-term-secret leakage on
future key compromise ⇒ prefer forward-secure/threshold schemes over encrypt-to-one-key).

## Q7 — Work-package authoring & batch ed25519

- Authoring surfaces: block sealing via Bandersnatch VRF (`conformance/authoring.ml:41-69`),
  work-item lifecycle via HTTP RPC → `Deploy_demo.run_on_service`
  (`node_rpc/node_rpc.ml:138-161`, `conformance/deploy_demo.ml:65-123`). Guarantee
  signatures are verified node-side (`lib/guaranteeing.ml:203-209`).
- **No service-usable signature-verification host call exists** (see Q1). Every
  per-order ed25519 verify in refine is full in-PVM interpretation at 1 gas/instruction.
  At the remembered ~195k gas/verify (uncommitted — see action items), a full-spec
  refine budget of 5e9 gas admits ~25,000 verifies even before batching tricks; at
  tiny (1e9) ~5,000. Signature checking will not be the batch bound — input size is
  (Q9) — but for the ZK-matcher architecture the right move is to fold signature
  validity into the proof and verify nothing per-order on-chain.

## Q8 — Proof systems verified in a lasair refine

- **Groth16/BN254: measured, twice** — arkworks 0.5 `no_std` (not substrate-bn):
  56.1M gas (spike, trivial circuit) and 59.9M gas (production Semaphore-lite voting
  circuit, 4 public inputs, verified e2e on a lasair-node). ≈1.1–1.2% of full G_R;
  needs `min_stack_size!(4 MiB)`; `.jam` blob 78 KB vs 4 MB code limit.
- **PLONK / STARK / halo2 / Nova: never attempted** on lasair (searched both repos —
  prose mentions only). Extrapolated estimates (unvalidated, flat-gas assumption):
  KZG-PLONK ~40–120M (fine), halo2/IPA 0.3–1.1B (feasible full, tight tiny),
  STARK ~0.5–0.6B (fits full, ~50% of tiny). Nova wrapped in Groth16 collapses to the
  measured ~56M. **Groth16 remains the right choice**; a PLONK gas spike is cheap to
  run if a universal setup becomes a requirement.

## Q9 — Batch bounds for a zk-rollup matcher

| Constraint | Value | Source |
|---|---|---|
| Bundle input W_B | 13,791,360 B | `lib/definitions.ml:43`, `lib/pvm_host.ml:112` |
| Max work items I | 16 | `lib/definitions.ml:33` |
| Max extrinsic blobs T | 128 (count, not bytes) | `lib/definitions.ml:38` |
| Import/export segments W_M/W_X | 3072 each × 4104 B | `lib/definitions.ml:36-37,68` |
| Refine gas G_R | 1e9 tiny / 5e9 full | `lib/spec.ml:56,75` |
| Refine output guard | 90,000 B | `conformance/refine.ml:155` |
| Report total output W_R | 49,152 B | `lib/definitions.ml:44`, `conformance/reports_stf.ml:452-464` |
| Accumulate per report G_A | 10,000,000 (both specs) | `lib/pvm_host.ml:101`, `conformance/reports_stf.ml:443-450` |
| Accumulate per block G_T | 20M tiny / 3.5e9 full | `lib/spec.ml:55,74` |

- **The batch is input-bound:** ~13.15 MiB of bundle → **≈27,500 orders @ 500 B /
  ≈68,900 @ 200 B** via extrinsic blobs (or ~25k/63k via imported segments — maxing
  imports fills the bundle because each import charges a 4488 B footprint,
  `lib/work_packages.ml:39,312`).
- Gas is order-count-independent if matching is proven off-chain (one ~56M-gas verify).
- Output forces the zk-rollup shape regardless: refine must emit a constant-size
  commitment (new book root + fill summary), because per-order output dies at the
  48 KiB report cap; accumulate must be O(1) per batch inside 10M gas.
- Enforcement caveat: `is_bundle_size_valid` is currently only called from tests
  (`lib/work_packages.ml:317-319`) — the live RPC path doesn't enforce W_B yet.

## Q10 — Determinism gotchas (lasair-specific)

- **Wrap-everything, trap-nothing:** overflow wraps (`lib/pvm.ml:787-794`); div-by-zero
  returns GP sentinels, never traps (`lib/pvm.ml:969-1002`); shifts mask the amount.
  Rust's own zero-checks still Trap→Panic, so `checked_*` in service code as usual.
- **Unaligned access is legal** (byte-by-byte LE, `lib/pvm.ml:233-254`). Page faults
  report the access base address; a page-spanning store that faults mid-way leaves the
  earlier bytes written (`lib/pvm.ml:227-228`).
- **No nondeterministic host call exists** (no clock, no randomness; refine entropy is
  the zero hash). `timeslot` is block state, not wall clock.
- **Do not branch on the `gas` host call.** Fuzzer seed l-954496557: an 11-gas
  cross-client difference sent a service down a different branch and diverged on-chain
  state (`regression-reports/README.md:8-24`). Gas-dependent logic makes any metering
  difference a consensus divergence.
- **Bad pointers into host calls panic the whole invocation** (they don't return NONE)
  — l2-431662357. A panicked accumulate yields nothing.
- **Accumulate is gas-bounded only — no wall clock anywhere.** The public "~10 ms"
  claim maps in lasair to G_A = 10M gas per report (both specs) inside G_T per block.
  The RPC demo path defaults accumulate to 5M (`conformance/deploy_demo.ml:66`).
- `memset` opcode is unimplemented (panics, `lib/pvm.ml:858-868`) — jam-pvm-build
  doesn't emit it today, but don't hand-roll toolchains.
- Checkpoint that can't afford its own 10 gas doesn't snapshot (l-1502007736).
- Test under **both** tiny and full specs — tiny/full is a live divergence axis
  (F5/F6 were exactly this class).

---

## What this means for jamswap's sealed-order roadmap

1. **Option 3 (current commit–reveal + IOC) is the trust-minimal ceiling for pure
   on-chain.** Confirmed: no host call, no DA trick, no service-held key can do better
   without new trust.
2. **Option 2 (threshold encrypt-until-batch) is buildable now as a sidecar**: DKG
   committee off-protocol (fresh keys, not consensus keys), traders encrypt to the
   committee key, batch-close decryption injected through the existing
   `node_rpc` payload seam (later: hash-committed extrinsics once
   `rc_extrinsics` wiring lands). Removes the reveal round and non-reveal griefing;
   adds honest-majority committee trust. No lasair consensus change, no GP violation.
3. **Option 1 (ZK dark-pool matcher) is verify-side cheap** (~56M gas, 1.1% of full
   G_R, measured) — the hard part is circuit/prover engineering and the
   relayer-liveness model, not the chain.
4. Batch capacity is generous: tens of thousands of sealed orders per work-package,
   input-bound, provided refine outputs a constant-size commitment and accumulate is O(1).

## Action items surfaced by this audit

- [ ] **Benchmark and commit the in-PVM ed25519-verify and Blake2s gas numbers** — the
      ~195k figure exists only in team memory; nothing is committed anywhere.
- [ ] **lasair conformance gap:** refine's host-call set is not gated per GP — `read`/
      `write`/`new`/etc. execute in refine against the ephemeral context instead of
      WHAT, while `historical_lookup` (which GP refine allows) returns WHAT
      (`lib/pvm_host.ml:1774-1798`). Fuzzer-relevant; fix on a feature branch.
- [ ] **lasair:** live path doesn't enforce W_B (`is_bundle_size_valid` test-only).
- [ ] **lasair stale constants:** `lib/work_packages.ml:45` (4096 report cap, dead) and
      `lib/reporting.ml:237` (15e9 accumulate gas, wrong) — delete or align.
- [ ] Optional: PLONK gas spike (clone of the Groth16 spike) if universal setup is
      ever needed.
