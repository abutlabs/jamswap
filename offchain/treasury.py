"""Self-funding treasury — the service pays its own JAMKB state rent out of trading
fees; only the surplus is withdrawable profit, and only by the owner.

Pure, deterministic, node-free logic (unit-tested in `tests/test_treasury.py`).
`server.py` uses it to (a) report the rent/reserve/profit split and (b) refuse to relay
a treasury sweep that would dip into the JAMKB rent reserve.

## The model (see docs/REVENUE.md)

Jamswap holds live state in validator RAM. Gavin Wood's **JAMKB** token prices that
resource: **1 JAMKB lets the service keep 1 KB of state**. So running the service costs
`rent = ceil(footprint_octets / 1024)` JAMKB, continuously — that's the cost of doing
business.

The 30 bps trading fee funds a treasury (in each market's quote asset, USDC/DOT). The
treasury must first hold a **JAMKB reserve** covering the rent; the operator tops it up
by converting fee revenue into JAMKB **on the DEX itself** (the DEX buys the very token
that pays for its own RAM — the JAMKB thesis, made real). Only what's left **above** the
reserve is profit, withdrawable **only by the owner** (the governance key), destined for
the owner's account.

Rent is covered **first**: while the JAMKB reserve is below the rent, there is *no*
withdrawable profit — the treasury is committed to covering the state cost, and the
shortfall must be funded before anything can be taken out.
"""

USDC, DOT, JAMKB = 0, 1, 2

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
    """Split treasury balances into the LOCKED JAMKB rent reserve and WITHDRAWABLE
    profit.

    `treasury`     — {asset_id: balance} in whatever unit the caller uses (the server
                     passes atomic units = display x SCALE).
    `rent_reserve` — the JAMKB the treasury must hold to cover rent, in the SAME unit as
                     `treasury[JAMKB]` (the server passes `jamkb_rent(octets) * SCALE`).

    Rent is covered first: if the JAMKB balance is below `rent_reserve`, the service is
    under-reserved and **no** profit is withdrawable (in any asset) until the shortfall
    is funded. Otherwise, the JAMKB above the reserve plus all other-asset fees are
    profit. Pure & deterministic; does not mutate `treasury`.
    """
    jamkb_bal = treasury.get(JAMKB, 0)
    reserve = min(jamkb_bal, rent_reserve)              # JAMKB locked to cover rent
    shortfall = max(0, rent_reserve - jamkb_bal)        # JAMKB still needed to be solvent
    solvent = shortfall == 0
    profit = {}
    for asset, bal in treasury.items():
        if not solvent:
            profit[asset] = 0                            # rent not covered -> nothing withdrawable
        elif asset == JAMKB:
            profit[asset] = bal - rent_reserve           # JAMKB above the reserve
        else:
            profit[asset] = bal                          # other-asset fees are pure profit
    return {"rent_reserve": rent_reserve, "reserve_held": reserve,
            "shortfall": shortfall, "solvent": solvent, "withdrawable": profit}


def max_withdrawable(treasury, rent_reserve, asset):
    """The most of `asset` the owner may withdraw as profit right now (0 if
    under-reserved). Used to gate the sweep relay."""
    return profit_split(treasury, rent_reserve)["withdrawable"].get(asset, 0)


def solvency(jamkb_held, rent_reserve):
    """Is the service's state footprint backed? Returns (solvent, shortfall) — the
    JAMKB-standard invariant `held ≥ obligation`. When under-reserved the service should
    apply backpressure (refuse new state growth) until topped up or state is freed.
    See docs/JAMKB_STANDARD.md."""
    shortfall = max(0, rent_reserve - jamkb_held)
    return (shortfall == 0, shortfall)
