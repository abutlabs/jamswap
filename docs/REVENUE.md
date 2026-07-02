# Revenue model — the self-funding treasury

Jamswap earns a trading fee, uses it to **pay its own JAM state rent (in JAMKB)**, and
lets the owner withdraw only the **surplus (profit)**. This ties the fee model directly
to the JAMKB thesis: *the exchange buys the very token that pays for its own RAM.*

## The fee (where revenue comes from)

- A **flat 30 bps** fee on the matched **quote** notional, charged to **both sides** of
  every fill (a frequent batch auction has no maker/taker distinction — everyone trades
  at the one clearing price, so the fee is symmetric).
- It accrues to a **treasury account** (`FEE_ACCOUNT`) in each market's **quote asset**
  — so USDC on `DOT/USDC` and `JAMKB/USDC`, DOT on `JAMKB/DOT`.
- Conservation holds *including* the fee: a batch moves value (fee to the treasury,
  fixed-point dust to the treasury), it never mints or burns. Property-tested in
  `crates/match-engine` over random fee rates and price scales.

## The cost (what the fee must cover first)

Running the service isn't free: its live state (order books, sealed commitments,
balances) sits in **validator RAM**, and JAMKB prices that resource at **1 JAMKB = 1 KB**.
So the service owes continuous rent:

```
rent = ceil(footprint_octets / 1024)   JAMKB      # footprint from the node's /footprint endpoint
```

The treasury must hold a **JAMKB reserve ≥ rent**. Because JAMKB is itself a trading
pair here, the operator funds that reserve by **converting fee revenue (USDC/DOT) into
JAMKB on the DEX** — a normal trade. The DEX literally earns its keep in the token that
prices its footprint.

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

## Withdrawal — owner only

- On JAM, moving funds out of the treasury requires a **governance-key-signed**
  `TAG_TREASURY` sweep (nonce-protected against replay). Only the holder of that key can
  withdraw — no one else can touch accrued fees.
- **Operator policy (enforced now):** the off-chain layer will **only relay a sweep for
  the profit amount** — a request that would dip into the JAMKB rent reserve is refused
  (`api_treasury` checks `max_withdrawable`). So "only profit is withdrawable" holds
  today at the builder, and "only the owner can withdraw" holds cryptographically on JAM.

### Beneficiary

Profit is destined for the owner's account:

```
15AWQjAZ9Ev9uhcYJdfwQzXA2VRDn2oLgZTkBzRRT7sZNDgs   (Polkadot AssetHub)
```

recorded as `PROFIT_BENEFICIARY` in `offchain/treasury.py` (and mirrored in the service
for documentation).

## Honest scope — what's real vs deferred

**Real today:**
- The 30 bps fee, treasury accrual, and conservation (property-tested).
- The rent computation and the rent-first profit split (`treasury.py`, unit-tested).
- The owner-only sweep (gov-key-authorised on JAM) and the operator-policy guard that
  refuses to withdraw into the reserve.
- The live `/api/treasury_status` readout + UI.

**Deferred (documented, not claimed as working):**
- **On-chain reserve enforcement.** The rent-first rule is currently an *operator
  policy* (off-chain). A service-level guard in `accumulate` — refusing a treasury sweep
  that drops JAMKB below a gov-reported rent figure — is the next step; it needs the
  service to be told its footprint (gov-signed) and end-to-end verification on a live
  node. (Node/protocol-level JAMKB enforcement for *all* services is a separate,
  community-level decision — see `JAMKB.md`; we deliberately don't bake that into the
  client.)
- **Auto-funding the reserve.** Converting USDC/DOT fees into JAMKB is an operator action
  (a normal trade) today, not an automatic in-`accumulate` swap.
- **Cross-chain payout to AssetHub.** The beneficiary is on Polkadot AssetHub; there is
  no JAM↔Polkadot bridge in this MVP, and the JAM governance key is not (yet) bound to a
  key the beneficiary controls. So profit does not *automatically* arrive on AssetHub —
  that payout path is future work. Until then the address records *intent* and the
  destination for a manual/bridged settlement.
