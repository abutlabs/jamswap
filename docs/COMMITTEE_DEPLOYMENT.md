# Committee deployment — from simulation to a real JAM testnet

> **Status: OPEN QUESTION / future work.** What ships today is a *simulation* of the
> decryption committee, sufficient to prove the cryptography end-to-end. This document
> is honest about that gap, specifies the production deployment model, and lists the
> concrete work needed to close it. The service, the proofs, and every on-chain format
> need **zero changes** — the boundary was designed so only the off-chain side evolves.

First, the disambiguation that keeps coming up: **the committee is OUR app-side role,
not a JAM client and not validators.** `crates/committee` is the Rust sidecar that plays
the n decryption-committee members for encrypt-until-batch sealing (rung 2 in
[`SEALED_ORDERS.md`](SEALED_ORDERS.md)). It has no relationship to PolkaJam/lasair
validators — they never run it, never know it exists.

## What exists today (and what it actually proves)

One binary simulates all n members: fixed deterministic seeds, every key share generated
inside a single process. That means **the operator running the sidecar is, in truth, a
single trusted decryptor** — fine for a demo, not a deployment.

What the simulation *does* prove, because these parts are real and verified e2e on a
live node:

- the **cryptography**: ECIES to an additive joint key; per-member Chaum–Pedersen
  decryption proofs; `refine` verifies every proof and recovers orders **with no secret
  ever existing on-chain** (~5.6 M gas per share — see the throughput table in the
  README);
- the **on-chain anchoring**: committee keys are committed via the gov-signed,
  nonce-protected `ENC_SETUP`, and every round must hash-match the on-chain set — a
  builder cannot swap committees (tested: rejected);
- the **fail-closed round**: tampered proofs, wrong committees, and uncommitted
  ciphertexts all reject the round without settlement (tested: rejected).

So the trust gap is *operational* (who holds the shares), not cryptographic.

## Production deployment model

```
                     A REAL JAM TESTNET (any mix of clients: lasair, polkajam, …)
                     validators know NOTHING about any of this
        ═══════════════════════════════════════════════════════════════════
              ▲ read encset / committee state          ▲ work packages
              │ via any node's RPC (verification)      │ (builder → assigned core)
  ┌───────────┴───────────────┐            ┌───────────┴──────────┐
  │ COMMITTEE — n operators,  │            │ BUILDER (exchange    │
  │ separate orgs & machines  │◀──────────▶│ operator)            │
  │                           │ batch-close│                      │
  │ member_1  [key share 1]   │ share      │ fan-out request:     │
  │ member_2  [key share 2]   │ requests   │ POST /decrypt_share  │
  │   …          (HSM/file)   │ + replies  │ {batch, ciphertexts} │
  │ member_n  [key share n]   │            │ collect ≥ t replies  │
  └───────────────────────────┘            └──────────────────────┘
```

**1. Keys.** Each member generates its own keypair locally; the secret never leaves its
machine. For the current additive n-of-n ECIES this needs **no ceremony at all** — the
joint encryption key is the sum of the member public keys, so there is no dealer and no
moment where the joint secret exists anywhere. Upgrading to t-of-n (for liveness)
requires an interactive DKG with verifiable secret sharing — a known protocol; the
on-chain side is unchanged and the per-share proof/gas cost is identical (the vdec
design takes t-of-n as a drop-in).

**2. Registration.** Already built: governance collects the n public keys and posts the
gov-signed `ENC_SETUP` ([n][pks], nonce-protected). Rotation = post a new set, drain
outstanding ciphertexts across (they were encrypted to the old joint key), switch.

**3. Runtime.** The binary becomes a small daemon each operator hosts — a container
next to any JAM node RPC they trust. At batch close the builder fans out
`decrypt_share` requests; each member independently returns its share + proof; the
builder collects ≥ t replies and assembles the work package. Members are stateless
apart from their key: crash one, restart it, nothing on-chain notices.

**4. The policy check — the piece that matters most and does NOT exist yet.** The
simulation decrypts whatever it is handed, whenever. A real member must enforce,
*before producing any share*:

- this ciphertext's id is in the **on-chain encset** (i.e. its owner committed it), and
- the batch it belongs to is **actually closing now** (not mid-window).

Each member checks this independently against a JAM node's RPC (one storage read per
ciphertext). Without it, a malicious builder can use the committee as a decryption
oracle and peek at sealed orders mid-window — exactly the MEV this design prevents.
With it, even the builder cannot decrypt early unless it corrupts t members. This is
the single biggest gap between the sim and a deployable member, and it is cheap to
build.

**5. Trust and incentives, stated plainly.** The proofs mean members can never *forge*
a decryption — refine verifies that forever. What remains:

| Failure mode | Mitigation |
|---|---|
| Liveness (members offline) | t-of-n threshold: any t of n online keeps sealing alive; commit–reveal (rung 3) remains the zero-committee fallback |
| Early decryption / collusion (t members peek) | independent operators (separate orgs/jurisdictions); detectable socially if leaked orders get front-run; removal at next rotation |
| No on-chain slashing (members are off-protocol) | payment from the fee treasury is the carrot; the role is the bond — same trust class as Ethereum's builder/relay ecosystem, with stronger cryptographic guardrails |

**6. Who the members would be on a real testnet.** The natural first recruits are
**other JAM implementer teams** — "run our committee daemon next to your node" is a tiny
ask, gives each team skin in the interop game, and a committee spanning independent
teams is decentralized in the way that matters (separate orgs) while their validators
stay completely app-agnostic. Market makers who want sealed liquidity are the second
cohort: they have a direct interest in the committee's honesty.

## Open work (the follow-up list)

- [ ] **`committee serve` mode**: per-member daemon (one key share, HTTP endpoint) —
      the share/proof logic already exists per-member; this is a subcommand + transport.
- [ ] **The policy check**: verify ciphertext-id ∈ on-chain encset + batch-close timing
      against a node RPC before sharing. *Security-critical; without it the committee is
      a decryption oracle for the builder.*
- [ ] **Builder fan-out**: replace the local shell-out (`committee_run`) with HTTP
      fan-out to n endpoints and a ≥ t collect (timeouts, retries).
- [ ] **t-of-n DKG**: interactive keygen with verifiable secret sharing, replacing
      additive n-of-n; same proofs, same gas.
- [ ] **Rotation drill**: exercise `ENC_SETUP` re-registration + ciphertext drain on the
      testnet end-to-end.
- [ ] **Payment plumbing**: route a fee-treasury share to member accounts (the treasury
      and sweep machinery exist; the split policy doesn't).
- [ ] **Member recruitment**: implementer teams / market makers, once a mixed-client
      testnet exists (see the README's roadmap notes).

And the standing perspective: rung 2's committee lifecycle is real operational weight —
it is part of why the **ZK matcher (rung 1) is the end-state**, where one proof replaces
all per-order committee verification and the committee shrinks to key custody or folds
into the prover. If the lifecycle above proves heavier than rung 2's fire-and-forget UX
is worth, the honest move is to skip from rung 3 straight to rung 1; nothing in the
service would change.
