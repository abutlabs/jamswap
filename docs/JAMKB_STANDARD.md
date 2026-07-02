# The JAMKB Standard — how a live JAM service backs its state

> A practical standard for how a JAM service **receives, holds, tops up, and is held
> accountable for** the JAMKB that backs its state footprint. This is the "how does it
> actually work day-to-day" companion to [`JAMKB.md`](JAMKB.md) (which explains what
> JAMKB *is* and the open protocol questions). Jamswap is the reference implementation.

## The premise

A JAM service's live state — order books, balances, commitments — sits in **validator
RAM**, replicated across every validator. RAM under the reference machine is finite and
inelastic, so it must be priced. **JAMKB is that price: 1 JAMKB backs 1 KB of a service's
state footprint.** (Gavin Wood's proposal — see `JAMKB.md`.)

That gives every service one invariant to satisfy:

```
held_JAMKB(service)  ≥  obligation(service)  =  ceil(footprint_octets / 1024)
```

A service that holds enough JAMKB to cover its footprint is **solvent** — its RAM is
paid for. This doc is about the practical question: **how does a service get and keep
that JAMKB?**

## Thesis: three inflows, layered — self-funding is the goal

JAMKB enters a service three ways, and the healthy design uses **all three, in layers**:

### 1. Deployment endowment (bootstrap)
A service can't hold any state it hasn't backed, so it is **deployed with an initial
JAMKB reserve** — an endowment that covers its genesis footprint (mirrors the Gray
Paper's base deposit `B_S`). Without it a service is insolvent from block zero. The
endowment is a one-time capital placement by the deployer.

### 2. Self-funding through use (the steady-state target)
**A service should pay its own rent out of its own operation.** It charges a small,
**cost-based** fee for the value it provides, and that fee funds its JAMKB reserve. A
well-used service covers its footprint from usage and needs no subsidy — this is the
sustainable equilibrium and the *right default*, because it aligns incentives:

- State that earns its keep stays funded.
- State that isn't used enough to cover its RAM **should** be reclaimed — unused state
  shouldn't squat on validator memory.

This is the primary inflow. A mature service lives here.

### 3. Beneficiary top-up (bootstrap runway + growth backstop)
Usage rarely covers the footprint on day one, and sometimes an operator *wants* to hold
more state than current usage funds (bootstrapping liquidity, a growth push, a temporary
spike). For that, the **beneficiary injects JAMKB from capital** — a subsidy/investment
that buys runway until self-funding catches up. It's the backstop, not the everyday
source.

**The mix (my thesis):** *endowment* bootstraps, *self-funding* is the target
equilibrium every service should reach, and *beneficiary top-up* is the runway/backstop
for early life and growth. A service that can never self-fund is a service whose state
isn't worth its RAM — and the standard should make that visible, not hide it.

## Solvency & backpressure — what happens when under-reserved

If `held < obligation`, the service is **insolvent**: it's holding more RAM than it has
paid for. The standard's response is **backpressure** — the service **refuses to grow its
state further** (rejects new state-growing operations) until it is solvent again. Two
things clear the condition:

1. **Usage frees state.** As the service does its job (Jamswap's 6 s auctions clear
   orders, shrinking the book and commitments), the footprint falls until `held ≥
   obligation` — self-healing.
2. **The beneficiary tops up.** A capital injection restores solvency immediately.

Backpressure makes **unbacked state growth impossible** and gives the operator an
unambiguous signal: *fund it or shrink it.* It never blocks operations that *reduce*
state (cancels, and the auctions that clear the book), so a service can always work its
way back to solvency.

## The self-funding loop (Jamswap as the worked example)

```
        ┌──────────────── orders grow state → obligation ↑ ───────────────┐
        │                                                                  │
   traders ──fees──▶ TREASURY (JAMKB reserve) ──covers──▶ state rent       │
        ▲                    │                                             │
        │              surplus above rent = PROFIT ──▶ beneficiary         │
        │                    ▲                                             ▼
        └── beneficiary top-up (runway)          6 s auctions clear orders → footprint ↓
```

- **Fees fund the reserve.** Jamswap charges a flat, cost-based fee **in the base asset**
  per filled order. The two JAMKB markets (`JAMKB/USDC`, `JAMKB/DOT`) pay fees **directly
  in JAMKB**, landing straight in the reserve; `DOT/USDC` pays in DOT, which the
  beneficiary converts to JAMKB on the DEX. The exchange literally earns its rent in the
  token that prices its RAM. (See [`REVENUE.md`](REVENUE.md).)
- **Auctions free state.** Every 6 s clear shrinks the book and consumes sealed
  commitments → footprint falls → JAMKB is freed.
- **Only surplus is profit.** Rent is covered first; only JAMKB (and other-asset fees)
  above the obligation is withdrawable by the beneficiary.

## Tracking JAMKB in practice

There are **two distinct JAMKB quantities**, and a standard must keep them separate:

| Quantity | What it is | Where it's tracked (prototype) | Where it should live (standard) |
|---|---|---|---|
| **Service JAMKB** | the reserve a *service* holds to back its footprint | the service's treasury balance | a per-service JAMKB obligation ledger |
| **Account JAMKB** | a *user's* holdings of the JAMKB token | a balance in the service's ledger | a shared account/asset registry |
| **Obligation** | `ceil(footprint/1024)` KB the service occupies | the node's `/v1/service/<id>/footprint` | node-authoritative (already is) |

**Because account JAMKB is a tradable token, the cost of state gets a market price** —
which is the whole point: an inelastic resource with a live, discoverable price.

> **Where the ledger lives is the open question.** In this prototype every quantity lives
> inside jamswap. For a *testnet-wide* standard, account JAMKB and per-service obligations
> need a home outside any single service — a **JAMKB system service** other services call,
> or **node/protocol-level** accounts. We deliberately **do not** bake protocol-level
> enforcement into the client (it's a community economic decision — see `JAMKB.md` §7);
> we implement the **service-level** standard and the **measurement**, to make that
> decision answerable with real numbers.

## The service lifecycle (the standard, end to end)

1. **Deploy** with an endowment (`INITIAL_JAMKB_RESERVE`) → solvent from block zero.
2. **Operate** — state grows with use (orders, commitments); the obligation rises.
3. **Self-fund** — usage fees refill the reserve; a used service stays solvent on its own.
4. **Top up** — if growth outruns usage, the beneficiary injects JAMKB for runway.
5. **Backpressure** — if still under-reserved, new state-growth is refused until solvent.
6. **Reclaim** — auctions clear orders, footprint falls, JAMKB frees.
7. **Profit** — surplus above the obligation is withdrawable by the beneficiary.

## What Jamswap implements (service-level, no consensus change)

| Standard element | Implementation |
|---|---|
| Deployment endowment | `ensure_reserve()` seeds `INITIAL_JAMKB_RESERVE` JAMKB at startup |
| Self-funding | flat base-asset fee → treasury; JAMKB markets fund the reserve directly |
| Beneficiary top-up | `POST /api/reserve_topup` + a UI "Top up reserve" control |
| Solvency backpressure | `api_order` refuses new orders while under-reserved (`JAMKB_BACKPRESSURE`) |
| Profit extraction | gov-signed sweep of surplus only (`REVENUE.md`) |
| Tracking / meter | `GET /api/treasury_status` + the live UI footprint→JAMKB meter |
| Not implemented (by choice) | node/protocol-level enforcement across services (community decision) |

The pure logic (`jamkb_rent`, `profit_split`, solvency) is in `offchain/treasury.py`,
unit-tested in `offchain/tests/`. Backpressure degrades to a no-op on a node that doesn't
expose `/footprint` (obligation reads 0 → always solvent), so it never breaks a plain
dev node.
