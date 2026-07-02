# Jamswap — security review (self-assessment)

An honest internal review of the MVP (PLAN.md Phase 9.1). What's **fixed**, what's
**accepted/documented** (often requiring chain-level mechanisms or a production
hardening pass), and what's **sound**. "Trustless" is used precisely: the matching
and settlement are fully deterministic and validator-audited; everything else
carries the asterisk noted here.

## Fixed

- **Integer overflow in settlement notional.** `qty × price` can exceed `i64` for
  large orders and would wrap silently in release builds — a real value-corruption
  bug. **Fixed:** `settle_deltas` now computes in `i128` end-to-end (holds any
  `u32·u32` with room), and the service applies the deltas to `u64` balances with a
  clamp to `[0, u64::MAX]`. Conservation is property-tested over the `i128` path.
- **Operation authentication (withdraw / cancel / treasury).** An account is now a
  collision-free `u32` **handle bound to an ed25519 public key** via a signed
  `TAG_REGISTER` (sequential handles — no birthday collisions, and the id is a
  commitment to a signable key, not a public hash of the address). **Withdraw**,
  **cancel** (of a resting order), and the **treasury sweep** are verified *in the
  service* (`ed25519-compact`, `verify_strict`-equivalent, `<Bytes>`-framing aware) —
  a forged or wrong-key operation is rejected. Verified e2e on the 6-validator testnet
  and unit-tested in `match-engine/src/auth.rs` (accept-valid / reject-tampered /
  reject-wrong-key / `<Bytes>`-wrapped). ed25519 verify traps on the PVM without a
  larger stack — `min_stack_size!(1 MiB)` fixes it (as the sibling zk-jam-service does
  for pairing).
- **Replay protection.** Each account carries an on-chain **nonce**; withdraw and cancel
  bind their signed message to it and it advances on every authorised op, so a captured
  operation can't be replayed. The treasury sweep uses a dedicated governance nonce.
- **Order collateralization (guard).** Order submission now refuses an order the account
  can't currently fund (a buy needs `qty·price` of quote, a sell needs `qty` of base) —
  see "still open" below for the trustless version.

## Accepted / documented (production hardening needed)

- **Order-placement authentication is builder-side, not yet in-`refine`.** Orders are
  signed by the account key and verified at the **off-chain builder** (a role SECURITY.md
  already treats as trusted), not yet re-verified trustlessly inside `refine`. So a
  malicious *builder* could still place an order on your behalf (it can't extract funds —
  withdraw is trustless-authenticated). **Production:** carry each order's `pubkey+sig`
  into the batch and verify per-order in `refine` (the research-blessed path; ed25519
  verify is cheap in refine's gas budget). The primitive (`match_engine::auth`) is in place.
- **Order collateralization is a guard, not an escrow.** The submission check reads the
  *current* balance; it doesn't reserve funds across multiple pending orders, and the
  settlement clamp at 0 still means an over-matched order could lose value on-chain.
  **Production:** reserve/escrow funds at submission so every matched order is covered.
- **Work-item replay (non-signed paths).** A `DEPOSIT`/`MATCH` work-item re-submitted is
  applied again. Deposits are a permissionless faucet (additive, no theft); the signed
  paths are nonce-protected. **Production:** chain-level inclusion uniqueness, or extend
  nonces to the match path.
- **Commit–reveal griefing + builder trust.** A committer who never reveals wastes
  their slot (non-reveal griefing) — threshold/time-lock encryption removes the
  reveal round and this vector. The off-chain **builder** assembles work-packages
  (chooses which orders/book to include); that role is inherently trusted in any
  exchange, but the **matching over the included inputs is fully validator-audited**,
  and orders are **sealed until the batch closes** (no intra-round front-running).
  The builder **cannot fabricate an order that was never committed** (this was a
  real bug: `refine` checks reveals against the *builder-supplied* commits blob,
  and `accumulate` used to consume commitments without verifying they existed
  on-chain — a builder could pad the blob with a fake hash and settle an
  uncommitted order. Fixed consume-or-reject: `accumulate` now requires every
  revealed commitment to match a distinct on-chain entry, else the whole round is
  dropped before settlement; proven e2e — the pre-fix blob settles the injected
  order, the fixed blob rejects the round while honest rounds still clear).
  Residual builder power is censorship/ordering within a round, never fabrication.
- **What sealing does and doesn't hide (precise model).** A sealed order is hidden
  (only its Blake2s256 commitment / ciphertext is on-chain) **until the auction it
  crosses a counterparty in** — at that auction it is revealed on-chain to be matched, so
  the reveal is **transiently public**. A sealed order that finds no crossing liquidity
  **rests hidden** (carried forward by the off-chain builder; only its commitment stays
  on-chain) and is retried each auction until it crosses or its good-till-time expires —
  so a sealed sell placed now can match a sealed buy placed rounds later, while both stay
  private until they clear. *(This fixed a real usability bug: sealed orders used to be
  immediate-or-cancel and the auction loop drained the pending queue every tick, so
  orders placed in different 6 s windows never met. The carry-forward planner
  — `offchain/round.py`, tested in `offchain/tests/test_round_lifecycle.py` — reveals a
  sealed order only in the round it crosses.)* Any unfilled remainder of a *revealed*
  order is still immediate-or-cancel, so a sealed order **never persists in the public
  book with its terms exposed** (an earlier bug where a revealed sealed order rested
  publicly was fixed by emitting no resting book on the reveal path). **Residual trust:**
  the builder holds the plaintext to run the crossing check (the same builder role every
  exchange has, and the check is clearing-neutral — non-crossing orders can't change the
  uniform price); the matching itself stays fully validator-audited. Builder-independent
  hidden resting needs the **ZK/MPC matcher (option 1)**.
- **Encrypt-until-batch (option 2, no reveal round) — BUILT.** Orders are ECIES-encrypted
  to an off-protocol committee key committed on-chain (`ENC_SETUP`, gov-signed; the
  committee uses fresh keys, never validator consensus keys). At batch close the committee
  supplies a Chaum-Pedersen-proven partial decryption per member; `refine` verifies every
  proof against the committed keys and recovers each order **with no secret**, so there is
  **no reveal round and no non-reveal griefing** (the owner need not be online at match
  time). Two `accumulate` checks defeat a malicious builder: the round must use the
  on-chain committee (committee-hash match) and every ciphertext must be committed
  (consume-or-reject). Trust is honest-committee for **liveness** only — the DDH proof
  forces honest plaintext, so the committee cannot forge or alter an order, only withhold
  decryption (censorship). Cost ~n·5.6M gas/order (measured). Crypto in `crates/vdec`,
  committee in `crates/committee`, proven e2e by `offchain/test_enc_round.py`. The residual
  gap vs a true dark pool (option 1) is that a decrypted order is public at clearing, same
  as commit–reveal — persistent hidden *resting* orders still require the ZK/MPC matcher.

## Sound (and why)

- **Determinism** — integer-only matching + deterministic tie-breaks ⇒ byte-identical
  re-execution on every auditor (the existential JAM-audit property; tested).
- **Clearing optimality** — `p*` provably maximizes matched volume (property-tested).
- **Settlement conservation** — Σ base == 0 and Σ quote == 0 *including the fee to
  the treasury*; a batch moves value, never mints or burns it (property-tested over
  random fees, and asserted every round in the economic sim across thousands of rounds).
- **Market integrity** — a market must be listed with canonical assets; unlisted or
  asset-mismatched rounds are rejected (tested).
- **MEV-resistance** — frequent batch auction (one uniform price, no latency race) +
  sealed commit-reveal orders. The residual asterisk is only the commit-reveal
  griefing/reveal-round noted above.

## Before mainnet (out of MVP scope)

Signed operations + a real wallet; collateralized order submission; an external
security audit of the service + crypto; threshold/time-lock encryption; real asset
custody via `on_transfer` against the JAM token standard; economic/manipulation
simulation at production parameters; circuit breakers + an insurance fund.
