# A practical use for JAMKB (pricing JAM's memory)

JAM's designer, Gavin Wood, has proposed a token called **JAMKB** to price a scarce
resource: **the memory (RAM) that a service occupies across every validator**. The rule
is simple — **1 JAMKB lets a service keep 1 KB of state**. It's a proposal; nobody has a
running example of what it would actually feel like.

**Jamswap is that worked example.** A live exchange is *made of* state that sits in
validator RAM — the order book, the sealed commitments, the balances. And that state
**visibly breathes**:

- Placing an order **grows** the footprint (a sealed order writes a 32-byte commitment;
  a resting order takes 17 bytes).
- Every 6-second auction **clears** orders → the book and commitments shrink → the
  footprint **falls again**.

So Jamswap is a **live meter of JAM state being consumed and released** — and because
JAMKB is *also* one of the tradable tokens on the exchange, **the cost of state gets a
real market price**. The DEX trades the very token that would pay for the DEX's memory.
It even surfaces a genuine tradeoff: **sealed (private) orders cost more state** than
plain ones, so privacy has a measurable JAMKB price.

**The exchange even pays its own rent.** A small, cost-based trading fee funds a treasury
that must first cover the service's JAMKB state rent (`ceil(footprint ÷ 1 KB)` JAMKB);
only the **surplus** is withdrawable profit, and only by the owner. So the DEX earns fees,
buys the JAMKB that pays for its own RAM, and hands the rest to its operator — a complete
self-funding loop. Details: [`REVENUE.md`](REVENUE.md).

**How does a service actually get and keep its JAMKB?** That's the practical question the
proposal leaves open, so we wrote it down as a standard. A service is **deployed with an
endowment** (so it's solvent from block zero), then **self-funds through use** (fees refill
the reserve — the steady-state target), with **beneficiary top-ups** as the runway/backstop
for early life and growth. When a service holds more state than its JAMKB covers, the
standard applies **backpressure** — it refuses to grow state further until usage frees it or
the reserve is topped up. Jamswap implements all of this at the service level (endowment,
self-funding fee, `Top up reserve` control, solvency backpressure, a live footprint→JAMKB
meter). The full thesis and the day-to-day mechanics are in
[`JAMKB_STANDARD.md`](JAMKB_STANDARD.md).

We built the **measurement**, the worked example, and the service-level standard — but we
deliberately **do not enforce** JAMKB in the node. Pricing JAM's state is a protocol-wide
economic decision for the community, not something one client should impose. The full
understanding and the proposal-for-discussion are in [`JAMKB.md`](JAMKB.md).

## No order rests forever — orders pay rent to stay alive

Because every resting order sits in validator RAM, it **costs JAMKB state rent for as long
as it rests** — whether or not it ever trades. That makes an unbounded *good-till-cancelled*
order a spam/griefing vector: flood the book with far-from-market orders that never fill,
never expire, and bloat the footprint (and every auction's matching work) **forever**,
driving JAMKB usage up indefinitely for free.

So in Jamswap **there is no rest-forever order**. Every order — even "GTC" — is given an
**automatic, rent-funded expiry**:

- **Its fee funds its lifetime.** An order rests only as long as the minimum profit from its
  fee can subsidize the state rent it accrues. When that budget is exhausted, the order
  **auto-expires and its state is reclaimed** (the JAMKB it held is freed).
- **Bigger footprint dies sooner.** A sealed order's on-chain commitment (32 B) costs more
  RAM than a public order (17 B), so it burns its budget faster — **sealed orders expire
  sooner than public ones**, a direct consequence of "sealed costs more JAMKB."
- **A hard cap** bounds the maximum resting time no matter what (so nothing lingers), and a
  **per-account open-order limit** stops any single actor from stuffing the book at once.
- **You can only shorten, never extend.** Picking a TTL sets an *earlier* expiry; you can
  never rest longer than the rent-funded lifetime.

The result: the book is **self-pruning**. Spam and stale liquidity clear themselves, and
JAMKB usage from resting orders is always bounded and reclaimed — the state you occupy is
state you're paying for. The knobs (`ORDER_RENT_BUDGET_KBS`, `MAX_RESTING_SECS`,
`MAX_OPEN_ORDERS`) are documented in [`ARCHITECTURE.md`](ARCHITECTURE.md); the
guards are tested in `offchain/tests/test_order_lifetime.py`. The live UI shows each order's
countdown and the current policy under **Time in force**.

---

*Back to the [README](../README.md). The proposal itself and our prototype plan:
[`JAMKB.md`](JAMKB.md). The service-level standard: [`JAMKB_STANDARD.md`](JAMKB_STANDARD.md).
The self-funding treasury: [`REVENUE.md`](REVENUE.md). What state costs in gas terms:
[`THROUGHPUT.md`](THROUGHPUT.md).*
