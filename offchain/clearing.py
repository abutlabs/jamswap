"""A faithful Python port of the Rust `match-engine` `clear()` — the uniform-price
batch-auction clearing, used by the builder to produce a per-order **execution report**
(a fill receipt) for the UI.

The on-chain settlement is done by `refine` (the Rust engine, in the `.jam`). This port is
NOT a second source of truth — it recomputes the SAME deterministic clearing from the SAME
inputs the builder hands to `refine`, purely so the server can attribute per-order fills for
the UI (the chain only exposes market-level `lp`/`cv`, not which order filled). It is kept
byte-for-byte equivalent to `crates/match-engine/src/lib.rs` and pinned by parity tests in
`tests/test_clearing.py` (the same scenarios as the Rust suite). See docs/ARCHITECTURE.md
→ "How the clearing price is chosen".

Orders are dicts: {"account", "oid", "side", "price", "qty"} with integer atomic price/qty
and side ∈ {BUY=0, SELL=1}.
"""
BUY, SELL = 0, 1


def clear(orders):
    """Clear a batch at ONE uniform price. Returns {"price", "volume", "fills"} where
    `fills` maps oid -> filled qty (only orders with a non-zero fill appear). Pure &
    deterministic: same input → same output, matching `refine`."""
    prices = sorted({o["price"] for o in orders})

    def demand(p):
        return sum(o["qty"] for o in orders if o["side"] == BUY and o["price"] >= p)

    def supply(p):
        return sum(o["qty"] for o in orders if o["side"] == SELL and o["price"] <= p)

    best_p, best_v, best_imb = 0, 0, float("inf")
    for p in prices:                      # candidates ascend → lowest price wins a tie
        d, s = demand(p), supply(p)
        v = min(d, s)
        imb = abs(d - s)
        # maximize matched volume; tie-break minimal imbalance; then lowest price.
        if v > best_v or (v == best_v and v > 0 and imb < best_imb):
            best_p, best_v, best_imb = p, v, imb

    if best_v == 0:
        return {"price": 0, "volume": 0, "fills": {}}
    p, v = best_p, best_v

    # eligible buys (limit ≥ p) by price-time priority: highest price, then oid.
    buys = sorted((o for o in orders if o["side"] == BUY and o["price"] >= p),
                  key=lambda o: (-o["price"], o["oid"]))
    # eligible sells (limit ≤ p): most aggressive (lowest price) first, then oid.
    sells = sorted((o for o in orders if o["side"] == SELL and o["price"] <= p),
                   key=lambda o: (o["price"], o["oid"]))

    fills = {}

    def ration(side):
        rem = v
        for o in side:
            if rem == 0:
                break
            f = min(rem, o["qty"])
            fills[o["oid"]] = fills.get(o["oid"], 0) + f
            rem -= f

    ration(buys)
    ration(sells)
    return {"price": p, "volume": v, "fills": fills}
