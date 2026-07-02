"""Round planning — decide which orders clear in an auction, and which sealed
orders rest (hidden) for a later one.

This is pure, deterministic, node-free logic so it can be unit-tested exhaustively
(see `tests/test_round_lifecycle.py`). `server.py` calls `plan_round` on every 6 s
auction tick.

## Why this exists (the bug it fixes)

Sealed orders used to be **immediate-or-cancel**: every tick drained the whole
pending queue, revealed all sealed orders, cleared once, and discarded any that
didn't fill. So two sealed orders placed in *different* 6 s windows — e.g. sells
now, buys ten seconds later — could never meet. Placing a sealed sell and then a
sealed buy did nothing.

## The fix (builder-side only — no consensus/service change)

The off-chain builder holds the **plaintext** of sealed orders (it encrypts a copy
for the chain but keeps the terms in memory; only *other* users can't see them). So
it can decide locally whether a sealed order **crosses** the current liquidity, and:

- **reveal** it this round only if it crosses (there is opposing liquidity it can
  trade with), or
- **carry it forward**, still hidden on-chain (only its ciphertext/commitment is
  posted), to try again next round — until it crosses or its good-till-time expires.

This makes sealed orders genuinely *rest* (hidden), and it **strengthens** the
privacy guarantee: a sealed order's terms are revealed only in the round it actually
clears, never merely because an auction ticked.

### Why it's clearing-neutral (correctness)

A sealed order that does **not** cross is, by definition, non-marketable at any
uniform clearing price: a buy priced below every sell only adds demand at prices
where supply is zero (symmetrically for a sell above every buy). Excluding such
orders therefore cannot change the clearing price or volume. So the set the builder
submits (resting + public + crossing-sealed) clears to exactly the same result as if
the carried orders had been included — they just wouldn't have traded. The service
re-clears and settles that submitted set as usual; the carry decision is a safe
builder-side filter, not a change to the matching.
"""

BUY, SELL = 0, 1


class RoundPlan:
    """The builder's decision for one auction round.

    - `reveal`  — sealed orders that cross now; revealed + cleared this round.
    - `public`  — plaintext orders; always cleared this round (one-shot into the book).
    - `carry`   — sealed orders that don't cross; stay hidden, retried next round.
    - `expired` — sealed orders past their good-till-time that never crossed; dropped.
    """

    __slots__ = ("reveal", "public", "carry", "expired")

    def __init__(self, reveal, public, carry, expired):
        self.reveal, self.public, self.carry, self.expired = reveal, public, carry, expired

    def __repr__(self):
        return (f"RoundPlan(reveal={len(self.reveal)}, public={len(self.public)}, "
                f"carry={len(self.carry)}, expired={len(self.expired)})")


def _best_opposing(orders):
    """The most aggressive price on each side across all liquidity: the lowest sell
    and the highest buy. A buy crosses iff its price >= lowest sell; a sell crosses
    iff its price <= highest buy."""
    sells = [o["price"] for o in orders if o["side"] == SELL]
    buys = [o["price"] for o in orders if o["side"] == BUY]
    return (min(sells) if sells else None,
            max(buys) if buys else None)


def crosses(order, min_sell, max_buy):
    """Does `order` cross the current best opposing price? (Whether it would trade in
    a uniform-price auction with this liquidity — not whether it fully fills.)"""
    if order["side"] == BUY:
        return min_sell is not None and order["price"] >= min_sell
    return max_buy is not None and order["price"] <= max_buy


def plan_round(pending, resting, now=0.0):
    """Plan one auction round.

    `pending` — this round's queued orders (dicts with at least `side`, `price`, and
    a truthy `sealed` for sealed orders; sealed orders may carry an `expiry` unix ts,
    `None`/absent meaning good-till-cancelled).
    `resting` — the market's on-chain resting book (plaintext public orders), as dicts
    with `side`/`price`.
    `now` — current unix time, for good-till-time expiry of carried sealed orders.

    Returns a `RoundPlan`. Pure: does not mutate its inputs.
    """
    public = [o for o in pending if not o.get("sealed")]
    sealed = [o for o in pending if o.get("sealed")]
    # crossing is judged against ALL current liquidity (resting + this round's public
    # + sealed), because a sealed order can cross another sealed order in the same batch.
    everything = list(resting) + public + sealed
    min_sell, max_buy = _best_opposing(everything)

    reveal, carry, expired = [], [], []
    for o in sealed:
        if crosses(o, min_sell, max_buy):
            reveal.append(o)                       # crosses now -> reveal + clear
        else:
            exp = o.get("expiry")
            if exp is not None and exp <= now:
                expired.append(o)                  # never crossed within its lifetime
            else:
                carry.append(o)                    # rest hidden, retry next round
    return RoundPlan(reveal=reveal, public=public, carry=carry, expired=expired)
