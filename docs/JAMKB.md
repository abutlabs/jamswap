# JAMKB on Jamswap — understanding the proposal, and a prototype plan

> Jamswap is a JAM service that holds **live state** — order books, sealed
> commitments, balances. That state sits in **validator RAM**. JAMKB is Gavin Wood's
> proposed token for exactly this resource. This doc is (a) our understanding of the
> JAMKB proposal, (b) how Jamswap is a concrete worked example of it, and (c) the
> **metrics we built to make JAMKB measurable and discussable** with a running example.
>
> **Scope, deliberately.** We build the *measurement and the worked example* — what a
> service's footprint actually is, and what it would cost in JAMKB. We do **not**
> implement protocol-level enforcement in the node. Pricing JAM's state footprint is a
> **protocol-economics decision for the JAM community**, not one any single client
> should bake in unilaterally (it would also diverge lasair from conformance). The
> enforcement design below is written up as an **open proposal to inform that
> discussion** — not a roadmap we execute on our own.

Source: *"DOT, DAO and the need for JAMKB"* (Polkadot Network, Medium).

---

## 1. What JAMKB is

**JAMKB is a single-purpose JAM *state-footprint* resource-access token.** The rule
is deliberately simple and fixed-rate:

> **1 JAMKB held by a service ⇒ that service may keep 1 KB in JAM's state footprint,
> for as long as the token is held.**

- A JAM node keeps "footprint" state in **RAM at all times, across every validator**.
  RAM under reference hardware is **finite, well-specified, and inelastic** — so it
  must be priced.
- Proposed shape: **fixed supply ≈ 21 M JAMKB** (assuming ~20 GB total footprint),
  **DAO-owned at genesis**, drip-fed into permissionless markets, free grants to core
  devs. **No dynamic pricing curve** (Wood rejects that as protocol complexity); the
  1 KB↔1 JAMKB ratio can only *increase* (more KB per token) if RAM gets cheaper, no
  hard fork.
- To move JAMKB *out* of a service you must **first clear the 1 KB of state** it backs.
  So the token is a *claim on occupied RAM* — you can't free the token without freeing
  the memory.

**Why a dedicated token (not DOT):** DOT is used for staking, coretime, and DAO
control, so "the supply which could be used for JAM's Service State is utterly
uncertain." A dedicated token makes supply == priced state capacity, 1:1.

## 2. What JAMKB replaces — JAM's footprint deposit (Gray Paper grounding)

JAM **already** prices state, in DOT, via a per-service **threshold balance**. A
service account must keep its balance `a_b` at or above (GP §accounts, eq. deposits):

```
a_t  =  B_S  +  B_I · a_items  +  B_L · a_octets
     =  100  +   10 · items    +    1 · octets        (GP constants, DOT base units)
```

| GP constant | symbol | value | meaning |
|---|---|---|---|
| base deposit  | `B_S` (`C_basedeposit`) | 100 | minimum balance for *any* service |
| item deposit  | `B_I` (`C_itemdeposit`) | 10  | per storage **item** (key) |
| octet deposit | `B_L` (`C_bytedeposit`) | 1   | per **octet** of stored data |

So **footprint = (number of storage items, total octets)** and the service must be
collateralised against it. **JAMKB swaps the unit of account**: instead of
`a_b ≥ a_t` in DOT, the rule becomes

```
JAMKB_held(service)  ≥  ceil( footprint_octets / 1024 )      # 1 JAMKB = 1 KB
```

(One can keep the `100 + 10·items` overhead as a JAMKB-denominated base + per-item
term, or simplify to pure KB. We prototype the pure-KB form and note the GP overhead.)

## 3. Jamswap is the worked example

Jamswap's on-chain (in-validator-RAM) footprint **is** its state:

| storage item | key | grows with |
|---|---|---|
| balances | `b ‖ asset(4) ‖ account(4)` → 8 B | # funded accounts |
| resting book | `book ‖ market(4)` → 17 B/order | unmatched limit orders |
| **sealed commitments** | `commits ‖ market(4)` → 32 B/order | pending sealed orders |
| market registry, `lp`, `cv`, `cust` | small | # markets |

**This is the "wow":** placing orders **grows** the footprint (a sealed order writes a
32 B commitment to validator RAM; an unmatched limit order rests as 17 B). Every **6 s
auction** (matching JAM's 6 s block cadence) **clears** orders → book and commitments
shrink → **footprint falls → JAMKB is freed**. The DEX becomes a live, visible meter of
JAM state being consumed and released — and **JAMKB is the prototype token** for that
resource. Because JAMKB is *also* a trading pair on the DEX, the **cost of state has a
real market price** (in USDC/DOT) — precisely the inelastic-resource pricing JAMKB
exists to create. The DEX trades the very token that pays for the DEX's RAM.

## 4. The prototype's JAMKB rule

```
footprint_octets(service) = Σ over storage items ( len(key) + len(value) )
footprint_items(service)  = count of storage items
JAMKB_required(service)   = ceil( footprint_octets / 1024 )          # 1 JAMKB = 1 KB
solvent                   = JAMKB_held(service) ≥ JAMKB_required(service)
```

`JAMKB_held` is the service's JAMKB balance (a special account = the service itself).
A round that **adds** sealed commitments / resting orders **raises** `JAMKB_required`;
clearing **lowers** it. Insolvency (footprint > JAMKB held) is the condition the
protocol must react to.

> **Implemented as a read-only tracker (for now).** The prototype surfaces
> `JAMKB_required` as a live meter only — nothing is held, funded, or consumed, and
> `solvent` is not enforced. The held-reserve / solvency model above is the *deferred*
> design (see Phase 2); re-enabling it is a contained change once the protocol question
> in §7 is settled.

## 5. Multiphase plan (build order — safe first, invasive last)

**Phase 0 — DEX foundation (no JAMKB enforcement; clearly-scoped UX).**
Tokens → **USDC, DOT, JAMKB**; **all pairs** (DOT/USDC, JAMKB/USDC, JAMKB/DOT);
**limit + market** order types; **6 s auto-auction** (a server tick clears every market
every 6 s, like block production); a **mempool toggle** that reveals the data sitting in
the service (sealed + unsealed); **users view/decrypt their own** pending orders (they
hold the nonce) and **cancel** un-processed ones. *Status: building now.*

**Phase 1 — Footprint instrumentation (read-only, honest).**
lasair exposes a service's footprint over the operator RPC:
`GET /v1/service/<id>/footprint → { items, octets }` (sum the service's storage trie).
The UI renders a **JAM state-footprint meter**: items, octets, KB — *actual validator
RAM this service occupies right now*. No token, no enforcement — just the truth, live.

**Phase 2 — JAMKB accounting layer (service + UI, still no protocol enforcement).** ✅ done
Define `JAMKB_required = ceil(octets/1024)`; the UI surfaces it as a **read-only tracker**
alongside items and octets, updating live as orders accrue and clear. JAMKB is **not
consumed for now** — nothing is held, funded, or debited; the meter just measures the
footprint. (An earlier iteration modelled a held reserve with headroom/solvency and a
"top up reserve" action; that consumption model is deferred until the protocol question
below is settled.) This surfaces the economics end-to-end **without** changing consensus.

> **Phases 0–2 are where we stop building.** They make JAMKB *measurable and
> discussable* on a running system — which is the contribution we want to bring to the
> community conversation. What follows is a **proposal sketch, not a roadmap.**

**Phase 3 — Protocol-level enforcement (PROPOSAL ONLY — needs community agreement).**
*We are deliberately not implementing this.* Pricing/enforcing the state footprint is a
JAM-wide economic decision; a single client shouldn't bake it in (and doing so would
diverge lasair from conformance). The sketch, for discussion: a node could enforce the
JAMKB rule at accumulate time — a `set_storage` that pushes `footprint_octets/1024 >
JAMKB_held` is **rejected** (mirrors GP's `a_b ≥ a_t`, but in JAMKB), via a per-service
JAMKB ledger + a guard in the accumulate transition. **Questions for the community:**
where does the JAMKB ledger live (a system service? the account model?); is it a hold or
a per-block rent; how is the 1 KB↔1 JAMKB ratio governed; how does it interact with the
existing DOT deposit `a_t`? Our metrics (Phases 0–2) exist to make these answerable with
real numbers.

**Phase 4 — Documentation + proposal write-up.** ✅ ongoing
This file + the README "How Jamswap works" section, kept in lockstep.

## 6. Open design questions (our proposals to refine)

1. **Hold vs rent.** The article is *hold-to-occupy* (JAMKB locked while state exists),
   not a per-block burn. We follow hold semantics. A rent/decay variant (footprint
   costs JAMKB-flow over time) is a richer but more opinionated model — note, don't build.
2. **Who supplies the JAMKB?** Options: (a) the **service** holds a JAMKB reserve and
   each order's submitter tops it up (state is a cost of trading); (b) a **per-order
   bond** returned when the order clears/cancels (aligns incentives — you pay JAMKB
   while your order occupies RAM, refunded when it leaves). We lean to (b): it makes
   the 6 s clearing a *JAMKB-refund* event, the cleanest demo.
3. **Base + item overhead.** Keep GP's `100 + 10·items` as a JAMKB-denominated floor,
   or pure-KB? Prototype pure-KB; expose items so the overhead is visible.
4. **Sealed orders cost the most state** (32 B commitment each, on-chain until reveal) —
   so MEV-resistance has a *quantified JAMKB cost*. That tradeoff (privacy ⇒ more
   footprint ⇒ more JAMKB) is a genuine, novel result this prototype can show.
5. **Ratio increases.** Model the "1 JAMKB → 1.x KB if RAM cheapens" lever as a node
   constant so the UI can show capacity growing without a hard fork.
```
