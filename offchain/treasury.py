"""Self-funding treasury — the service pays its own JAMKB state rent out of trading
fees, holding just enough JAMKB to back its live footprint; profit is the leftover *fee*
revenue (USDC/DOT), and only the owner may withdraw it.

Pure, deterministic, node-free logic (unit-tested in `tests/test_treasury.py`).
`server.py` uses it to (a) report the rent/reserve/profit split and (b) refuse to relay
a treasury sweep that would dip into the JAMKB rent reserve.

## The model (see docs/REVENUE.md, docs/JAMKB_STANDARD.md)

Jamswap holds live state in validator RAM. Gavin Wood's **JAMKB** token prices that
resource: **1 JAMKB lets the service keep 1 KB of state**. So running the service costs
`obligation = ceil(footprint_octets / 1024)` JAMKB, continuously — that's the cost of
doing business.

**JAMKB is FINITE and NOT mintable.** The total supply is bounded by the validators' total
RAM ÷ 1 KB (`JAMKB_SUPPLY`). Holding N JAMKB *is* the right to occupy N KB of that shared
RAM — a token you hold is RAM some other service therefore cannot. If JAMKB could be minted
freely it would price nothing; the whole point is that it's scarce. A service therefore
does not "stockpile" JAMKB — it **acquires only enough to cover its obligation** (plus a
small operational buffer), and **releases the excess** back to the pool when its footprint
shrinks. Holding far more than you occupy is not wealth — it's squatting on RAM you aren't
using, and the standard flags it (`over_reserved`) rather than counting it as profit.

The small, cost-based trading fee accrues in each market's quote asset (USDC/DOT). The
operator keeps the JAMKB reserve sized to the obligation by **buying/selling JAMKB on the
DEX itself** (the DEX trades the very token that prices its own RAM — the JAMKB thesis,
made real). **Profit is the fee revenue (USDC/DOT) above what's needed to stay solvent** —
JAMKB itself is a working reserve, never a withdrawable hoard. Rent is covered **first**:
while the reserve is below the obligation there is *no* withdrawable profit.
"""

USDC, DOT, JAMKB = 0, 1, 2

# JAMKB is a FINITE, non-mintable resource: the total supply is the validators' aggregate
# RAM budget ÷ 1 KB. This testnet constant stands in for that global cap (≈1 GiB of state
# rights). A service can only ever hold a slice of it; the sum across all services ≤ supply.
JAMKB_SUPPLY = 1_048_576   # 1 GiB / 1 KB — the whole pool, shared by every service on the testnet

# Owner / beneficiary of withdrawable profit — a Polkadot AssetHub account.
# NOTE (honest scope): on JAM the treasury sweep is authorised by the service's
# governance key, and there is no JAM<->Polkadot bridge in this MVP, so profit does not
# *automatically* land on AssetHub yet. This constant records the intended beneficiary;
# the cross-chain payout is a documented next step (docs/REVENUE.md).
PROFIT_BENEFICIARY = "15AWQjAZ9Ev9uhcYJdfwQzXA2VRDn2oLgZTkBzRRT7sZNDgs"
PROFIT_BENEFICIARY_CHAIN = "polkadot-assethub"


def jamkb_rent(footprint_octets):
    """JAMKB required to keep the service's state footprint (1 JAMKB = 1 KB, rounded
    up). Whole JAMKB tokens."""
    return (int(footprint_octets) + 1023) // 1024


def profit_split(treasury, rent_reserve):
    """Split treasury balances into the JAMKB rent reserve, the withdrawable FEE profit,
    and any over-reserved JAMKB.

    `treasury`     — {asset_id: balance} in whatever unit the caller uses (the server
                     passes atomic units = display x SCALE).
    `rent_reserve` — the JAMKB the treasury must hold to cover its obligation, in the SAME
                     unit as `treasury[JAMKB]` (the server passes `jamkb_rent(octets)*SCALE`).

    Rent is covered first: if the JAMKB balance is below `rent_reserve`, the service is
    under-reserved and **no** profit is withdrawable until the shortfall is funded.

    **JAMKB is never profit.** It's a finite RAM-right held only to back the footprint;
    any JAMKB above the obligation is `over_reserved` — idle RAM rights that should be
    *released* (sold back on the DEX), not withdrawn as wealth. Profit is the *fee* revenue
    (USDC/DOT) above solvency. Pure & deterministic; does not mutate `treasury`.
    """
    jamkb_bal = treasury.get(JAMKB, 0)
    reserve = min(jamkb_bal, rent_reserve)              # JAMKB actually backing the footprint
    shortfall = max(0, rent_reserve - jamkb_bal)        # JAMKB still needed to be solvent
    over_reserved = max(0, jamkb_bal - rent_reserve)    # idle RAM rights to release (NOT profit)
    solvent = shortfall == 0
    profit = {}
    for asset, bal in treasury.items():
        if asset == JAMKB:
            profit[asset] = 0                            # a working reserve, never a hoardable profit
        elif not solvent:
            profit[asset] = 0                            # rent not covered -> nothing withdrawable
        else:
            profit[asset] = bal                          # fee revenue (USDC/DOT) is the profit
    return {"rent_reserve": rent_reserve, "reserve_held": reserve, "shortfall": shortfall,
            "over_reserved": over_reserved, "solvent": solvent, "withdrawable": profit}


def max_withdrawable(treasury, rent_reserve, asset):
    """The most of `asset` the owner may withdraw as profit right now (0 if
    under-reserved). Used to gate the sweep relay."""
    return profit_split(treasury, rent_reserve)["withdrawable"].get(asset, 0)


def reserve_target(obligation, buffer_kb, supply=JAMKB_SUPPLY):
    """How much JAMKB the service should *aim* to hold: enough to back the footprint plus a
    small operational buffer so a burst of orders doesn't instantly trip backpressure —
    NEVER more. Capped at the finite `supply` (you cannot hold RAM rights that don't exist).
    This is the anti-hoarding cap the top-up/endowment obey. Whole JAMKB."""
    return min(int(obligation) + max(0, int(buffer_kb)), supply)


def solvency(jamkb_held, rent_reserve):
    """Is the service's state footprint backed? Returns (solvent, shortfall) — the
    JAMKB-standard invariant `held ≥ obligation`. When under-reserved the service should
    apply backpressure (refuse new state growth) until topped up or state is freed.
    See docs/JAMKB_STANDARD.md."""
    shortfall = max(0, rent_reserve - jamkb_held)
    return (shortfall == 0, shortfall)
