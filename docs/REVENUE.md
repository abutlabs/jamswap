# Revenue model — the self-funding treasury

Jamswap earns a trading fee, uses it to **pay its own JAM state rent (in JAMKB)**, and
lets the owner withdraw only the **surplus (profit)**. This ties the fee model directly
to the JAMKB thesis: *the exchange buys the very token that pays for its own RAM.*

## The fee (where revenue comes from)

- A **flat, cost-based fee** — `FEE_FLAT = 300` atomic = **0.03 base units** — charged
  **per filled order** (a frequent batch auction has no maker/taker distinction). It
  approximates the real per-order execution + state cost, **not** a size-proportional
  trading fee, so a 1000-unit trade and a 1-unit trade pay the same ~0.03. (The fee is
  capped at the fill so a tiny order can't be over-charged.)
- It is collected in each market's **base asset** and accrues to a **treasury account**
  (`FEE_ACCOUNT`). This is the key to the JAMKB loop: `DOT/USDC` pays fees in **DOT**,
  and both `JAMKB/*` markets pay fees **directly in JAMKB** — which lands straight in the
  state-rent reserve.
- The buyer receives `qty − fee` base, the seller delivers `qty + fee` base, and the
  treasury accrues `fee` per order. **Conservation holds** including the fee: Σ base == 0
  and Σ quote == 0 (property-tested in `crates/match-engine` over random flat fees and
  price scales; unit-tested in `wire::flat_fee_charged_in_base_and_conserves`).

> **Why not 30 bps?** 30 bps is 0.30% — a $1000 trade pays $3, an AMM-level fee. The
> intent here is cost recovery + modest margin, so the fee is a small flat amount that
> tracks the actual cost of running the service, and the profit comes from volume.

## The cost (what the fee must cover first)

Running the service isn't free: its live state (order books, sealed commitments,
balances) sits in **validator RAM**, and JAMKB prices that resource at **1 JAMKB = 1 KB**.
So the service owes continuous rent:

```
rent = ceil(footprint_octets / 1024)   JAMKB      # footprint from the node's /footprint endpoint
```

The treasury must hold a **JAMKB reserve ≥ rent**. Two things fund it:
1. **Deploy with a starting reserve.** The server seeds `INITIAL_JAMKB_RESERVE` JAMKB
   (default 100) into the treasury at startup (`ensure_reserve`), so the service is
   solvent from block one, before any fees accrue.
2. **Fees top it up.** `JAMKB/*` markets pay fees directly in JAMKB (straight into the
   reserve). `DOT/USDC` fees arrive as DOT; the beneficiary converts them to JAMKB by
   trading the `JAMKB/DOT` book — a normal DEX trade. The DEX literally earns its keep in
   the token that prices its own footprint.

## Profit = surplus above the rent reserve

```
solvent   = treasury.JAMKB ≥ rent
profit    = if solvent: { JAMKB above rent, all USDC, all DOT }
            else:       nothing               # rent is covered FIRST
```

**Rent is covered before any profit is taken.** While the JAMKB reserve is below the
rent, the treasury is entirely committed to closing that shortfall — there is no
withdrawable profit in any asset until the service is solvent. Once solvent, the JAMKB
above the reserve plus all other-asset fees are profit.

The pure logic is `offchain/treasury.py` (`jamkb_rent`, `profit_split`), unit-tested in
`offchain/tests/test_treasury.py`. The live split is exposed at
`GET /api/treasury_status` and rendered in the UI's footprint panel (rent locked /
reserve held / withdrawable profit).

## Beneficiary access — sweep, then swap

The owner (beneficiary) turns treasury profit into whatever they want by (1) **sweeping**
profit to their trading account, then (2) **swapping** JAMKB/DOT/USDC on the DEX normally.

- **Sweep.** Moving funds out of the treasury requires a **governance-key-signed**
  `TAG_TREASURY` sweep (nonce-protected against replay) — only the key-holder can withdraw.
  The sweep only ever moves **profit**: a request that would dip into the JAMKB rent
  reserve is refused (`max_withdrawable`), so the service always stays solvent.
- **Swap.** Once profit is in the owner's account, the normal trading UI swaps it across
  `JAMKB/DOT`, `JAMKB/USDC`, `DOT/USDC` — e.g. convert DOT fees to JAMKB, or realize
  profit in USDC.
- **How the sweep is signed.** By default the gov key lives off-server (in
  `crates/committee`); the UI's sweep is disabled and you sweep out-of-band. For a
  single-operator prototype where **operator == owner**, set `BENEFICIARY_SWEEP=1`: the
  server then holds the demo gov key (verified to match the service's baked `GOV_PUBKEY`
  in `test_beneficiary.py`) and the UI's "Sweep profit → your account" buttons work
  directly. ⚠️ This means anyone who can reach the server API can sweep — only enable it
  where you control access.

### Beneficiary account

Profit is destined for the owner's account:

```
15AWQjAZ9Ev9uhcYJdfwQzXA2VRDn2oLgZTkBzRRT7sZNDgs   (Polkadot AssetHub)
```

recorded as `PROFIT_BENEFICIARY` in `offchain/treasury.py` (and mirrored in the service
for documentation). On JAM the sweep is authorised by the service's governance key; a
cross-chain payout to this AssetHub address is deferred (see below).

## Honest scope — what's real vs deferred

**Real today:**
- The flat, cost-based, base-asset fee, treasury accrual, and conservation
  (property-tested + `flat_fee_charged_in_base_and_conserves`).
- The rent computation and the rent-first profit split (`treasury.py`, unit-tested).
- Deploy with a starting JAMKB reserve (`ensure_reserve`, `INITIAL_JAMKB_RESERVE`).
- The owner-only sweep (gov-key-authorised on JAM) and the operator-policy guard that
  refuses to withdraw into the reserve; the beneficiary sweep signature is verified to
  match the service's baked `GOV_PUBKEY` (`test_beneficiary.py`).
- The live `/api/treasury_status` readout + UI, and the beneficiary sweep panel.

**Deferred (documented, not claimed as working):**
- **End-to-end on a live node.** The flat-fee settlement, the seeded reserve, and the
  beneficiary sweep are verified at the unit level (conservation, gov-signature validity)
  and the service blob is rebuilt, but the full loop hasn't been run against a live node
  in this pass.
- **On-chain reserve enforcement.** The rent-first rule is currently an *operator
  policy* (off-chain). A service-level guard in `accumulate` — refusing a treasury sweep
  that drops JAMKB below a gov-reported rent figure — is the next step; it needs the
  service to be told its footprint (gov-signed) and end-to-end verification on a live
  node. (Node/protocol-level JAMKB enforcement for *all* services is a separate,
  community-level decision — see `JAMKB.md`; we deliberately don't bake that into the
  client.)
- **Auto-funding the reserve.** Converting DOT fees into JAMKB is a beneficiary action
  (a normal trade) today, not an automatic in-`accumulate` swap.
- **Cross-chain payout to AssetHub.** The beneficiary is on Polkadot AssetHub; there is
  no JAM↔Polkadot bridge in this MVP, and the JAM governance key is not (yet) bound to a
  key the beneficiary controls. So profit does not *automatically* arrive on AssetHub —
  that payout path is future work. Until then the address records *intent* and the
  destination for a manual/bridged settlement.
