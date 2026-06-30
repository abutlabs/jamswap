# Marmalade — a frequent-batch-auction order-book DEX on JAM

> Working codename: **Marmalade** (a preserve in the jam family; "market" is in
> there if you squint). Provisional — the clearing price in our auction is the
> "set point," and jam famously *sets*, so **Setpoint** is the alternate name on
> the table. Decide before public anything.

**Status:** PLANNING (v0.1, drafted while the lasair conformance fuzzer runs).
**Owners:** Aodh + Aiden.
**Horizon:** multi-session, multi-month. Gated externally on JAM rollout (see §9).

---

## 0. One-paragraph thesis

Every on-chain exchange uses an AMM (a closed-form pricing formula) instead of a
real order book **because no blockchain could run a matching engine** — matching
thousands of orders is a real algorithm, and on-chain compute couldn't afford it.
AMMs are a *workaround for a compute limit*, and they pay for it with impermanent
loss, capital inefficiency, and being MEV piñatas. JAM removes the limit: its
**Refine** phase is heavy, parallel, deterministic, *audited* (trustless)
compute. So we can run an actual **matching engine** trustlessly and settle fills
on-chain — a CEX-grade order book with DEX-grade self-custody. We clear as
**frequent batch auctions** (one uniform clearing price per round), which is not a
compromise but an upgrade: it removes the latency race that drives most CEX/AMM
MEV. Marmalade is that exchange, and we have a structural edge because **we build
the client it runs on** (lasair) — letting us run the auctions cheaper than anyone
who didn't write their own node.

---

## 1. Product vision & differentiation

**What it is:** a self-custodial, MEV-resistant, central-limit-order-book exchange
where matching runs in JAM's Refine and settlement in Accumulate. Orders are
encrypted until the batch seals, so nobody — not even the node running the match —
can front-run within a round.

**The one-sentence pitch:** *the first exchange with a CEX's matching engine and a
DEX's custody, because JAM is the first chain that can actually run the matching
engine.*

**Differentiation map:**

| Venue | Matching | Custody | MEV exposure | Why we win |
|---|---|---|---|---|
| AMM DEX (Uniswap) | none — a formula | self | high (sandwiching) | real price discovery; no IL; limit orders |
| CLOB DEX on a fast L1 (Hyperliquid, Phoenix) | off-chain or app-specific sequencer | self-ish | sequencer-trust / latency games | trustless *audited* matching, no privileged sequencer |
| CEX (Binance, Coinbase) | off-chain engine | **custodial** | operator-trust | non-custodial; verifiable matching |
| Marmalade | **in-Refine, audited** | self | batch-auction + encryption → minimal | trustless matching + encryption + our cost moat |

**What we are NOT:** we are not "AWS for compute," not microsecond HFT (Refine
settles per block), and not a place to run arbitrary smart contracts. We do one
thing — clear order books trustlessly — and we do it where it was previously
impossible.

---

## 2. Business plan

### 2.1 The cost structure nobody else has to think about: coretime is COGS

On JAM, running an auction **costs coretime** (you pay for the compute that clears
the batch). This is a genuine cost of goods sold per trade — something no AMM has,
because an AMM is just a formula evaluated inside a normal transaction.

- **Marginal cost of a cleared trade** ≈ its share of the coretime spent refining
  the batch it was in.
- **Revenue per trade** ≈ trading fee (bps) × notional.
- **Per-market profit** ≈ Σ fees − coretime spent on that market's auctions.

Two consequences drive the whole business:

1. **Empty rounds are pure loss.** A market with no flow still burns coretime if
   you auction it every block. So the operating discipline is: **only run an
   auction when there's flow** (skip empty rounds; batch low-volume markets into
   shared cores; charge listing fees / minimum-activity requirements).
2. **Whoever runs the auctions cheapest wins.** Lower COGS per trade means you can
   undercut on fees *and* keep margin. This is where lasair becomes a moat, not
   just a client (§4.10).

### 2.2 Revenue model

Layered, in rough order of when they come online:

1. **Trading fees (primary).** A flat fee in bps on matched notional (FBA has no
   maker/taker distinction — everyone clears at one price, so a flat, transparent
   fee is the honest design). Start ~1–5 bps; tune against COGS.
2. **Listing fees / market sponsorship.** Creating a new market consumes ongoing
   coretime; charge to list, or require a sponsor to fund the market's coretime.
3. **Coretime efficiency margin.** Because we run an optimized node (§4.10), our
   cost to clear is below a naive operator's — that spread *is* margin.
4. **Premium data / API.** Low-latency feeds, historical data, and order-book
   analytics for algo traders and market makers.
5. **Token + fee switch (later, optional, treat skeptically).** A protocol token
   capturing a slice of fees and used for liquidity incentives. **Honest caveat
   (see the DOT lesson): a token succeeding as infra ≠ the token capturing
   value.** Only do this if there's a real value-accrual design, not as a
   fundraise. Liquidity mining can bootstrap volume but is a sugar high.
6. **Insurance-fund yield / float** (minor, and regulatorily sensitive — flag).

### 2.3 The lasair moat (why this isn't just another DEX)

We control the node software. That is rare and it compounds:

- We can make lasair **fastest at refining Marmalade work-packages** → lowest COGS
  per trade → structural fee advantage.
- We run the **work-package builder** ourselves → reliable inclusion, optimal
  batching, and capture of whatever (intentionally minimal) builder value exists.
- Our existing perf work (flambda -O3, O(log n) state, memoized roots) **directly
  lowers the cost of every auction**.
- Independent client + flagship app is a strong **grant and fellowship story**.

### 2.4 Funding strategy (non-dilutive first)

1. **W3F / JAM ecosystem grants.** Infra + a flagship trustless DEX is squarely
   fundable; ties to the existing fellowship narrative. Pursue first.
2. **Polkadot treasury proposals** (ecosystem tooling).
3. **Build-in-public** → the Aiden story (an AI building the client *and* the
   first real service on it) is itself fundraising/marketing.
4. Token/VC only much later, only if warranted, only with a real accrual model.

### 2.5 Go-to-market

- **First markets:** a handful of high-conviction pairs where trustless matching
  and MEV-resistance are obviously valuable (e.g., a blue-chip pair, a stable
  pair). Depth > breadth at launch.
- **First users:** sophisticated traders and MMs who feel CEX-custody risk and
  AMM-MEV pain. They value verifiable fairness.
- **Wedge narrative:** "the auction nobody can front-run."

### 2.6 Risks (business)

- **JAM mainnet timing** — the single biggest external dependency (§9). We can
  build and demo on testnets; we cannot earn fees until JAM is live.
- **Liquidity cold-start** — the classic chicken-and-egg. Mitigation: few markets,
  MM partnerships, possibly incentives, and the genuine UX win of no-sandwiching.
- **Regulatory** — a non-custodial matching venue still attracts scrutiny;
  jurisdiction, front-end exposure, and token design are legal questions to get
  ahead of (not legal advice — engage counsel before mainnet/token).
- **Token value-accrual** — see §2.2.5; do not assume it.
- **Competition** — other JAM teams will see the same opportunity; the lasair cost
  moat and first-mover client integration are our defenses.

---

## 3. Technical architecture overview

### 3.1 JAM service model (recap, so the doc is self-contained)

A JAM **service** = code (PVM/RISC-V bytecode) + key-value state + balance, with
protocol-called entry points. The two that matter:

- **`refine`** — runs *in-core*, off-chain, parallel, large gas (~5B full spec).
  Heavy compute. **Cannot write live state**; operates on the work-package payload
  plus **historical lookups** (reads recently-finalized state). Output: a
  `work-result` blob. Re-executed/audited by validators → trustless.
- **`accumulate`** — runs *on-chain*, sequential, smaller gas. Commits work-results
  to service state. The authoritative ledger update.
- **`on_transfer`** — handles value transferred in from other services (deposits).

Supporting concepts: **work-package** (unit submitted to a core, carries
work-items, gets erasure-coded for **data availability**); **guarantor** (runs
refine); **builder** (assembles work-packages); **auditing/ELVES** (random
re-execution = the trust).

### 3.2 The Refine/Accumulate split *is* exchange architecture

Real exchanges separate the **matching engine** (compute) from the **ledger**
(state/settlement). JAM gives us that split natively:

```
service Marmalade {
  state:        balances ledger + resting order book + market registry
  refine():     run the FBA matching engine on a sealed batch   // MATCHING ENGINE
  accumulate(): apply fills, update balances & resting book      // SETTLEMENT
  on_transfer(): credit deposits                                 // CUSTODY IN
}
```

- **Refine = matching engine** (heavy, audited, trustless).
- **Accumulate = settlement ledger** (light, on-chain, authoritative).
- **State = the books** (balances + resting orders + markets).

### 3.3 The clearing mechanism (spec the math precisely)

**Uniform-price sealed-bid double auction (a.k.a. a call auction / "fixing"):**

1. Buyers submit `(limit_price, quantity)` bids; sellers submit `(limit_price,
   quantity)` asks. Prices on an integer tick grid; quantities integer.
2. Build the **aggregate demand curve** D(p) = total quantity bid at limit ≥ p
   (non-increasing in p) and **aggregate supply curve** S(p) = total quantity
   asked at limit ≤ p (non-decreasing in p).
3. **Clearing price p\*** = the price that maximizes matched volume
   `min(D(p), S(p))`; tie-break by the standard call-auction rules (minimize
   imbalance, then a fixed reference). All deterministic integer comparisons.
4. **Fills:** every order strictly better than p\* fills fully; orders at the
   margin (limit == p\*) are **pro-rated** with a *deterministic* tie-break
   (price-time priority by `(round_seq, order_id)`, or a hash-based lottery seeded
   by JAM VRF entropy — chosen for determinism, §6). Unfilled remainder either
   rests (limit orders) or is cancelled (immediate-or-cancel).
5. **Everyone trades at p\*** (uniform price). No maker/taker spread to capture →
   flat fee is the honest model.

Properties to prove (Phase 1 tests): value conservation, no-arbitrage at p\*,
monotonic fills, and **bit-exact determinism** (the whole thing must re-execute
identically on every auditor — integer-only, no floating point: this is JAM's home
turf, unlike the AI ideas).

### 3.4 Asset & custody model

CEX-style internal ledger (simplest correct design):

- **Deposit:** user transfers asset into the Marmalade service → `on_transfer`
  credits an internal balance.
- **Trade:** internal balance debits/credits only — fast, cheap, no token movement
  per trade.
- **Withdraw:** debit internal balance → transfer asset out.
- **Invariant:** Σ internal balances per asset == custodied amount (reconciled
  every block; a violation halts trading).

Open dependency: how assets are represented on JAM (token services / a canonical
asset standard) is part of the broader ecosystem and may not be mature early — so
Phase 3 starts against a **mock token service** and adopts the real standard when
it lands.

### 3.5 Front-running resistance / encryption (the hard part — don't hand-wave)

**Threat:** a guarantor *runs* refine, so it *sees the work-package contents*. If
orders are plaintext, the guarantor (or anyone watching the order relay) can insert
their own order into the same batch with knowledge of yours.

The **batch model already removes the latency race** (one price per round, no
"be faster" game — this is why FBAs are MEV-resistant by construction). What
remains is **information leakage** before the batch seals. Options, with honest
trade-offs:

- **Commit–reveal:** users commit a hash, reveal next round. Simple, no committee,
  but adds a round of latency and a non-reveal griefing vector.
- **Threshold encryption:** orders encrypted to a committee key; decrypted only
  after the batch seals. Stronger UX (one round), but introduces a **committee
  trust assumption** and liveness dependency.
- **Time-lock / delay encryption (VDF-based):** decryptable only after a wall-clock
  delay; no committee. Elegant, but VDF maturity/cost is a question.

**Decision deferred to Phase 4** after a spike. Whatever we pick, the residual
trust assumption gets documented loudly — "trustless" earns an asterisk until the
encryption story is airtight. The *matching* stays fully deterministic/audited;
only the *decryption* carries the asterisk.

### 3.6 Multi-market scaling

One work-package → one core per round. So:

- **Per-market parallelism:** each pair clears as its own work-package on its own
  core, all feeding the one Marmalade service's `accumulate`. With 341 cores,
  hundreds of markets clear *in parallel* each block.
- Same service code, many work-packages. Coretime budgeting (§2.1) decides which
  markets auction which rounds.

### 3.7 Off-chain infrastructure

- **Order relay / encrypted mempool** — collect & gossip encrypted orders for the
  current round.
- **Work-package builder** — assemble the sealed batch, submit to guarantors. *We
  run this* (lasair edge).
- **Indexer** — read chain state into a queryable order book, trade history,
  balances.
- **API (REST + WebSocket)** — order-book snapshots, fills stream, account state,
  order submission — what the UI consumes.

### 3.8 Frontend

A polished trading interface: live order book + depth chart, price chart, order
entry (limit/market/IOC), portfolio & balances, order/fill history, deposit/
withdraw flows, wallet + signing, and a UX that hides the encrypt→commit→reveal
mechanics so it *feels* like a normal exchange.

### 3.9 How lasair optimizes the order book (the moat, in detail)

Because we write the client, we can do what no DEX-on-someone-else's-chain can:

- **L1 — PVM hot-path optimization.** The matching engine's inner loops (sorting,
  comparisons, curve aggregation) are the same ops every round. Specialize lasair's
  PVM execution for them (JIT or targeted interpretation) → cheaper refine.
- **L2 — Fastest guarantor.** Be the node most able to refine Marmalade
  work-packages quickly and reliably → dependable inclusion.
- **L3 — Builder integration.** Batch assembly inside lasair → optimal, low-latency
  work-package construction.
- **L4 — Host-side state caching.** Memoize the resting order book / historical
  lookups so refine's reads are cheap.
- **L5 — General perf compounding.** Every lasair perf win (flambda -O3, O(log n)
  state, memoized Merkle roots) lowers COGS per trade.
- **L6 — Co-design.** We can shape the service's data layout to match what lasair
  executes fastest — a luxury you only have when you own both sides.

This is the durable advantage: **same protocol, lower marginal cost.**

---

## 4. Phased roadmap

Each phase has subtasks and an **exit criterion** (the thing that must be true to
move on). Phases 6 (infra) and 7 (UI) parallelize with each other and with later
backend phases. A **Track L** (lasair optimization) runs continuously alongside.

### Phase 0 — Foundations & feasibility spike  *(~1–2 weeks)*
- 0.1 Finalize this plan; pick the name; lay out the repo (`service/`, `offchain/`,
  `web/`, `docs/`, `sim/`).
- 0.2 Stand up a JAM service dev environment: jam-pvm-sdk (Rust→PVM) toolchain +
  a way to invoke `refine`/`accumulate` locally — ideally **driven through
  lasair's existing STF/PVM harness** so we dogfood from day one.
- 0.3 **Determinism spike:** a trivial `refine` that integer-sorts a list; run it
  twice on lasair; prove bit-identical. De-risks the core assumption before any
  real work.
- 0.4 Write the formal clearing-algorithm spec (§3.3) with worked examples.
- **Exit:** a hello-world `refine` runs deterministically on lasair; clearing spec
  written and reviewed.

### Phase 1 — Matching engine MVP (Refine), plaintext, single market  *(~3–4 weeks)*
- 1.1 Order/fill binary data structures + codec.
- 1.2 Implement the FBA uniform-price clearing algorithm in Rust→PVM (integer-only):
  curve aggregation, clearing price, fills, deterministic marginal pro-rata.
- 1.3 Property tests: conservation, monotonicity, clearing-price correctness, and
  **determinism (two runs byte-identical)**.
- 1.4 Run `refine` on lasair with a real batch payload; verify byte-exactness.
- 1.5 Benchmark: orders-per-gas-budget; build the scaling/cost profile.
- **Exit:** a real matching engine clears N orders deterministically in Refine on
  lasair, correct uniform price + fills.

### Phase 2 — Settlement & state (Accumulate), full single-market lifecycle  *(~3–4 weeks)*
- 2.1 Service state layout: balances ledger, resting book, market registry, round
  counters.
- 2.2 `accumulate`: ingest work-result, update balances, persist resting orders.
- 2.3 Order-submission path: order → work-item → sealed batch.
- 2.4 Resting limit orders; `refine` reads the resting book via historical lookup.
- 2.5 Round sequencing so each round reads the prior round's **finalized** book
  (consistency).
- 2.6 Cancel/modify.
- 2.7 End-to-end single-market test on the lasair harness.
- **Exit:** a full *plaintext* single-market DEX works end-to-end locally.

### Phase 3 — Custody: deposits, withdrawals, asset model  *(~2–3 weeks)*
- 3.1 Asset-representation decision; build/adopt a **mock token service** to start.
- 3.2 Deposit (`on_transfer` → credit) and 3.3 withdrawal (debit → transfer-out).
- 3.4 Accounting invariants + per-block reconciliation; halt-on-violation.
- **Exit:** deposit → trade → withdraw with conserved accounting.

### Phase 4 — Encrypted / MEV-resistant trading  *(~4–6 weeks — the hard one)*
- 4.1 Formal threat model (guarantor, relay, builder).
- 4.2 Evaluate commit-reveal vs threshold vs time-lock (spike each).
- 4.3 Choose & spec; document the residual trust assumption.
- 4.4 Client-side encryption + deterministic/verifiable decryption path.
- 4.5 Integrate into the round lifecycle (collect encrypted → seal → decrypt →
  refine).
- 4.6 Adversarial tests: attempt front-running; prove resistance; write up the
  asterisk honestly.
- **Exit:** orders private until seal; no intra-round front-running; trust
  assumptions documented.

### Phase 5 — Multi-market & scaling  *(~2–3 weeks)*
- 5.1 Market registry + per-market state isolation.
- 5.2 Parallel auctions: one work-package per market per round across cores.
- 5.3 Coretime budgeting + skip-empty-rounds logic (the §2.1 discipline).
- 5.4 Load/scaling tests.
- **Exit:** multiple markets clear in parallel in one block.

### Phase 6 — Off-chain infrastructure  *(~4–6 weeks, parallel)*
- 6.1 Encrypted order relay/mempool.
- 6.2 Work-package builder (lasair-integrated).
- 6.3 Indexer.
- 6.4 REST + WebSocket API.
- 6.5 Monitoring/reliability.
- **Exit:** a service-backed API the UI can build on.

### Phase 7 — Frontend / polished UI  *(~6–8 weeks, parallel)*
- 7.1 UX/visual design (use the frontend-design guidance — distinctive, not
  templated).
- 7.2 Wallet integration + order signing.
- 7.3 Real-time order book & fills (WebSocket).
- 7.4 Encrypted-order UX (hide the crypto).
- 7.5 Polish, branding, responsive.
- 7.6 Wire end-to-end against testnet.
- **Exit:** a polished trading UI, usable end-to-end.

### Phase 8 — Testnet deployment & integration  *(~3–4 weeks; gated on JAM testnet)*
- 8.1 Deploy the service to a JAM testnet / lasair devnet.
- 8.2 Run our optimized lasair guarantor + builder.
- 8.3 Integration, bug bash, alpha testers.
- **Exit:** live on testnet, real users trading test assets.

### Phase 9 — Security, audit, economic hardening  *(~6–8 weeks; overlaps)*
- 9.1 Internal review (security-review + code-review skills).
- 9.2 External audit (service code + crypto).
- 9.3 Economic simulation (market quality, manipulation resistance, fee model).
- 9.4 Insurance fund / circuit breakers.
- 9.5 Bug bounty.
- **Exit:** audited + economically stress-tested.

### Phase 10 — Mainnet launch & business ops  *(gated on JAM mainnet)*
- 10.1 Mainnet deploy.
- 10.2 Liquidity bootstrapping / MM partnerships.
- 10.3 Fee activation, treasury, token decision.
- 10.4 Growth, listings, ops.
- **Exit:** live, generating fees.

### Track L — lasair optimization  *(continuous, alongside all phases)*
- L1 PVM matching hot-path optimization · L2 fastest guarantor · L3 builder
  integration · L4 host-side state caching · L5 perf compounding · L6 service/client
  co-design. (See §3.9.)

---

## 5. Determinism & testing strategy

Determinism is existential (auditors must re-execute byte-identically), and it's
the thing most likely to bite. Standing rules:

- **Integer-only** matching; no floating point anywhere in `refine`.
- **Deterministic tie-breaks** everywhere (marginal pro-rata, ordering) — seeded by
  `(round_seq, order_id)` or JAM VRF entropy, never by iteration order of a hash
  map.
- **The determinism harness** (built in Phase 0, reused throughout): run `refine`
  twice on independent lasair instances over the same input; assert byte-equality.
  This is the same idea as the conformance gate and the AI-classifier test.
- **Property-based tests** for the clearing math (conservation, monotonicity).
- **Adversarial tests** for encryption/MEV (Phase 4) and accounting (Phase 3).
- **Economic simulation** (Phase 9) — market microstructure, manipulation, fees.
- Everything runs in a CI gate, lasair-style ("never regress a high-water mark").

---

## 6. External dependencies & risks (technical)

- **JAM rollout timing** — biggest unknown; gates Phases 8/10. We build/demo on
  testnets and be ready when mainnet lands. Do not build the business plan around a
  date we don't control.
- **JAM asset standard / token services** — may be immature early → start on mocks
  (§3.4).
- **jam-pvm-sdk maturity** — the Rust→PVM service toolchain; track upstream.
- **Encryption primitive maturity** — threshold/VDF tooling (§3.5).
- **Refine gas economics** — how many orders fit per budget shapes throughput and
  COGS (measured in Phase 1).

---

## 7. Rough timeline & milestones

Order-of-magnitude, single-pair-of-hands pace, *excluding* JAM-mainnet gating:

- **M1 (~month 1):** Phase 0–1 — matching engine clears deterministically in Refine
  on lasair. *This is the "impossible thing, proven" milestone — the demo that
  justifies everything.*
- **M2 (~months 2–3):** Phase 2–3 — full single-market lifecycle with custody,
  plaintext, end-to-end locally.
- **M3 (~months 3–5):** Phase 4 — encrypted trading working.
- **M4 (~months 5–7):** Phase 5–6 — multi-market + off-chain infra/API.
- **M5 (~months 6–9):** Phase 7 — polished UI against testnet.
- **M6 (~months 8–11):** Phase 8–9 — testnet live + audited.
- **M7 (JAM-mainnet-gated):** Phase 10 — mainnet + fees.

These overlap (infra/UI parallelize). Treat as sequence-of-dependencies, not a
calendar promise.

---

## 8. Open decisions (resolve as we go)

1. **Name:** Marmalade vs Setpoint vs other.
2. **Encryption scheme** (Phase 4): commit-reveal vs threshold vs time-lock.
3. **Marginal allocation:** price-time priority vs VRF-seeded pro-rata lottery.
4. **Token: yes/no/when** — default *no* until a real accrual model exists.
5. **Asset model** — adopt the JAM token standard vs internal-ledger-only.
6. **Round cadence** — every block vs every N blocks vs flow-triggered.
7. **Service language** — Rust→PVM (default) vs anything else viable.
8. **Where lasair-as-node lives** — fold the DEX node role into lasair, or a
   separate operator build sharing lasair's core.

---

## 9. Why this is worth doing (the honest version)

- It's the **cleanest demonstration of what JAM uniquely enables** — point at AMMs,
  say "those exist only because of the limit JAM removes," and the whole value prop
  is legible in one sentence.
- It **dogfoods lasair** harder than any fuzzer can — a real service surfaces real
  client bugs and real perf needs.
- The **lasair cost moat** is genuine and rare (we own the node).
- The **story** (an AI built the client *and* the first real exchange on it) funds
  itself in attention and grants.
- And it's a **great use of fuzzer downtime** — long-horizon, high-ceiling, and
  every phase produces something demonstrable.

Honest caveats kept in view throughout: JAM mainnet timing is not ours to control;
"trustless" carries an asterisk until the encryption is airtight; a successful
exchange and a valuable token are *separate* bets; and liquidity cold-start is a
real, unglamorous grind. We build it eyes-open.

---

*Next action when we pick this up: Phase 0.3 — the determinism spike. The smallest
thing that proves the biggest claim (a matching kernel re-executing byte-identically
in Refine on lasair). Everything else layers onto that.*
