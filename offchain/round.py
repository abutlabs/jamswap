"""Round planning — decide which orders clear in an auction, and which sealed
orders rest (hidden) for a later one.

This is pure, deterministic, node-free logic so it can be unit-tested exhaustively
(see `tests/test_round_lifecycle.py`). `server.py` calls `plan_batch` on every 6 s
auction tick: `plan_round` over the whole queue, with the batch cap applied only to the
orders that can trade this round (so sealed orders that must wait never take a place in
the batch — see `plan_batch`).

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

`plan_round` decides reveal-vs-carry for *not-yet-crossed* orders. The complementary
case — a revealed order that crosses but only **partially fills** — is handled in
`server.api_round`: its remainder is re-sealed (fresh commitment) and re-queued, so a
large sealed order accumulates fills across many auctions instead of losing the unfilled
part to on-chain immediate-or-cancel. Both paths respect the good-till-time expiry.

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

    - `reveal`   — sealed orders that cross now; revealed + cleared this round.
    - `public`   — plaintext orders; always cleared this round (one-shot into the book).
    - `carry`    — sealed orders that don't cross; stay hidden, retried next round.
    - `expired`  — sealed orders past their good-till-time that never crossed; dropped.
    - `deferred` — sealed orders whose commit isn't on-chain (finalized) yet; held OUT of
                   this batch entirely so they neither reveal nor make another order appear
                   to cross against liquidity that won't be submitted. Retried next round.
    - `overflow` — orders that could trade but didn't fit this round's batch (`plan_batch`);
                   they wait for a later round. Always empty from `plan_round`.
    """

    __slots__ = ("reveal", "public", "carry", "expired", "deferred", "overflow")

    def __init__(self, reveal, public, carry, expired, deferred=None, overflow=None):
        self.reveal, self.public, self.carry, self.expired = reveal, public, carry, expired
        self.deferred = deferred if deferred is not None else []
        self.overflow = overflow if overflow is not None else []

    def __repr__(self):
        return (f"RoundPlan(reveal={len(self.reveal)}, public={len(self.public)}, "
                f"carry={len(self.carry)}, expired={len(self.expired)}, "
                f"deferred={len(self.deferred)}, overflow={len(self.overflow)})")


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


def plan_round(pending, resting, now=0.0, sealed_ready=None):
    """Plan one auction round.

    `pending` — this round's queued orders (dicts with at least `side`, `price`, and
    a truthy `sealed` for sealed orders; sealed orders may carry an `expiry` unix ts,
    `None`/absent meaning good-till-cancelled).
    `resting` — the market's on-chain resting book (plaintext public orders), as dicts
    with `side`/`price`.
    `now` — current unix time, for good-till-time expiry of carried sealed orders.
    `sealed_ready` — predicate `o -> bool`: is this sealed order's commit already
    on-chain (finalized) so it can actually be revealed and settled THIS round? A
    sealed order that isn't ready is held out of the crossing view entirely
    (`deferred`), so the planner and the on-chain reveal-gate share ONE view of
    liquidity. Default (`None`): treat every sealed order as ready (unit tests /
    encrypt-until-batch simulation).

    ## Why the readiness gate lives HERE (the "revealed alone" bug it fixes)

    Crossing must be judged against exactly the liquidity that will be in the submitted
    batch. If the planner judged crossing against a sealed order whose commit hasn't
    landed, it could reveal an order that then clears *alone* (its counterparty removed
    by the on-chain gate) — leaking that order's terms AND dropping it as immediate-or-
    cancel. Gating readiness before `_best_opposing` makes `revealed ⟹ a real
    counterparty is in the batch`, so a revealed order trades.

    Returns a `RoundPlan`. Pure: does not mutate its inputs.
    """
    if sealed_ready is None:
        sealed_ready = lambda o: True
    public = [o for o in pending if not o.get("sealed")]
    sealed = [o for o in pending if o.get("sealed")]
    ready = [o for o in sealed if sealed_ready(o)]
    not_ready = [o for o in sealed if not sealed_ready(o)]
    # crossing is judged against the liquidity that will actually be submitted this
    # round: resting book + this round's public orders + COMMIT-READY sealed orders.
    # A sealed order can cross another sealed order in the same batch — but only if both
    # commits are on-chain, so both will be in the submitted set.
    everything = list(resting) + public + ready
    min_sell, max_buy = _best_opposing(everything)

    reveal, carry, expired, deferred = [], [], [], []
    for o in ready:
        if crosses(o, min_sell, max_buy):
            reveal.append(o)                       # crosses now -> reveal + clear
        else:
            exp = o.get("expiry")
            if exp is not None and exp <= now:
                expired.append(o)                  # never crossed within its lifetime
            else:
                carry.append(o)                    # rest hidden, retry next round
    for o in not_ready:
        exp = o.get("expiry")
        if exp is not None and exp <= now:
            expired.append(o)                      # GTT elapsed while waiting for its commit
        else:
            deferred.append(o)                     # commit not on-chain yet; wait, don't reveal
    return RoundPlan(reveal=reveal, public=public, carry=carry,
                     expired=expired, deferred=deferred)


FIT_PASSES = 4   # batch refills before unproven sealed orders give way (see plan_batch)


def plan_batch(pending, resting, fit, now=0.0, sealed_ready=None):
    """Plan one auction round whose batch is bounded: `plan_round`, with the cap applied
    only to the orders that can join this round.

    `fit(candidates) -> (batch, overflow)` is the capacity policy (the server's
    `_cap_batch`: the order count, and never an account's newer public order ahead of its
    older one). `candidates` are in queue order, and `fit` must be prefix-closed: taking
    orders out of the candidates never drops one it admitted before (the server's cap takes
    a prefix of the queue).

    ## Who competes for the cap (jamswap#6)

    Public orders, and ready sealed orders that cross the liquidity of the WHOLE queue. A
    sealed order that is deferred (commit not final yet) or hidden (it crosses nothing even
    against every queued order, so against no part of it) cannot join this round whatever
    else does: it waits outside the cap. The server used to cap the queue first and plan
    only the capped part, re-queueing such orders AHEAD of the overflow every auction —
    48 of them (the lasair6 cap) kept every public order out of every round for their whole
    ~32 min life.

    A sealed order in the batch whose only counterparties were left over the cap crosses
    nothing in the batch: it is taken back out (it waits, still hidden — never revealed
    alone) and the batch is refilled. Crossing is mutual, so an order that crosses nothing
    is no other order's counterparty: taking it out uncrosses nothing, and refilling only
    adds liquidity, so every pass keeps the previous pass's reveals. After FIT_PASSES
    passes the sealed orders no batch has shown to cross give way to the rest, which bounds
    the work and keeps a long run of such orders at the head of the queue from holding
    public orders out (they cross the book those orders leave, a round later).

    Returns a `RoundPlan`: `reveal`/`public` are the batch, `overflow` what waits for a
    later round. Pure: does not mutate its inputs.
    """
    full = plan_round(pending, resting, now, sealed_ready)
    crossing = {id(o) for o in full.reveal}
    carry, expired = list(full.carry), list(full.expired)
    for n in range(FIT_PASSES + 1):
        if n == FIT_PASSES:
            crossing = {id(o) for o in plan.reveal}     # only proven reveals compete now
        cands = [o for o in pending if not o.get("sealed") or id(o) in crossing]
        batch, _ = fit(cands)
        plan = plan_round(batch, resting, now)          # its sealed orders are all ready
        stuck = plan.carry + plan.expired               # counterparty left over the cap
        if not stuck:
            break
        carry += plan.carry
        expired += plan.expired
        crossing -= {id(o) for o in stuck}
    placed = {id(o) for o in plan.reveal + plan.public + carry + expired + full.deferred}
    return RoundPlan(reveal=plan.reveal, public=plan.public, carry=carry, expired=expired,
                     deferred=full.deferred,
                     overflow=[o for o in pending if id(o) not in placed])
