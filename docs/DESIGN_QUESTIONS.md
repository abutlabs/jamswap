# Open questions & design options

> Moved from the README (2026-07-16).


Jamswap is a **prototype for discussion**, not a finished protocol. Several choices are
deliberate-but-not-final, and a few genuinely belong to the JAM community rather than to any
one client. We list them openly so the trade-offs are on the table — and so the "obvious"
answer isn't quietly baked in. Each row says **what Jamswap does today**, the **alternative**,
and roughly **how big a change** it is.

### Matching engine

| Question | Today | Alternative(s) | Size of change |
|---|---|---|---|
| **Clearing price** when a whole band of prices clears the same max volume | the **competitive-equilibrium** price (min-imbalance tie-break → the resting side captures the surplus) | a **surplus-splitting midpoint** of the feasible band | one-line tie-break change (`match-engine`) |
| **Large order** that can't fully fill in one 6 s batch | **accumulates across batches** — public/market remainders rest; sealed remainders re-seal & carry forward | a single-batch **fill-or-kill / max-slippage sweep** (accept worse prices to fill more *now*) | scoped feature — a wider market band or an IOC/slippage flag |
| **Marginal allocation** when demand ≠ supply at `p*` | **price-time priority** (best price, then order id) | **pro-rata** across same-price orders | engine change (well-isolated) |

Details + the worked example: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) → "How the
clearing price is chosen" and "Partial fills".

### Order privacy (sealing)

| Question | Today | Alternative(s) | Trade-off |
|---|---|---|---|
| **How orders are hidden** until they clear | **commit–reveal** (rung 3) by default — the permissionless, no-third-party base state; **encrypt-until-batch** (rung 2, committee) as the `ENC_MODE=1` opt-in | a **ZK dark-pool** matcher (rung 1 — spiked, proven, not yet integrated) | each carries a *different* trust asterisk — a reveal-round griefing vector vs committee liveness vs prover cost |
| **Committee deployment** — today one sidecar *simulates* all n members (single-operator trust) | proven cryptography + on-chain committee anchoring; the operational model is designed but unbuilt | per-member daemons run by n independent operators, with an on-chain policy check so the builder can't use the committee as a decryption oracle | **open work** — the follow-up list lives in [`docs/COMMITTEE_DEPLOYMENT.md`](docs/COMMITTEE_DEPLOYMENT.md) |

The three-rung privacy ladder is in [`docs/SEALED_ORDERS.md`](docs/SEALED_ORDERS.md).

### JAMKB economics (mostly the community's call)

| Question | Today | Open question |
|---|---|---|
| **Total JAMKB supply / the RAM↔KB price** | a **stand-in constant** (`JAMKB_SUPPLY`) | the real cap is *total validator RAM ÷ 1 KB* — a protocol-wide measurement the community must set |
| **Where account-JAMKB + per-service obligations live** | inside the jamswap service | a shared **JAMKB system service** or **protocol-level accounts** for a testnet-wide standard |
| **Enforcement** of `footprint ∝ JAMKB` | **measured only** — deliberately **not enforced** in the node | whether/how to enforce is a protocol-economics decision, not one client's to impose |
| **Order rent → lifetime rate** (anti-bloat) | policy stand-in knobs (`ORDER_RENT_BUDGET_KBS`, `MAX_RESTING_SECS`, `MAX_OPEN_ORDERS`) | calibrate to real state-rent economics once JAMKB is priced |

Background + our thesis: [`docs/JAMKB.md`](docs/JAMKB.md) and
[`docs/JAMKB_STANDARD.md`](docs/JAMKB_STANDARD.md). We built the **measurement and a running
example** precisely to make these answerable with real numbers, not to pre-empt them.

### Fees, treasury & trust boundaries

| Question | Today | Open / next |
|---|---|---|
| **Fee shape** | a **flat, cost-based** fee per filled order in the base asset | a size-proportional fee, if the community prefers ([`docs/REVENUE.md`](docs/REVENUE.md)) |
| **Profit payout** to the beneficiary | gov-signed sweep of fee revenue, swapped on the DEX | an actual **JAM↔Polkadot bridge** to the AssetHub account (deferred — no bridge yet) |
| **Order / cancel / withdraw auth** | **trustless** — public orders ed25519-verified per-order **in `refine`** (replay-proof seq floors, hash-bound book); sealed commits **owner-signed + account-bound**; cancel/withdraw signed + nonce-protected | remaining asterisk: a carried remainder's *terms* are builder-attested until the rung-1 ZK linkage ([`docs/SECURITY.md`](docs/SECURITY.md)) |
| **Custody** | **mock** (faucet credit / funded debit, conservation-checked) | real self-custody via `on_transfer`, blocked on JAM asset-service maturity |

If any of these should be decided differently, they're small, well-isolated changes — say
which and we'll flip it (or wire in the toggle).

---

