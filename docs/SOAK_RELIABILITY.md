# Soak reliability: measuring per-order clearing, and the path to 99.99%

*Written 2026-07-09. Prompted by: "I want soak tests with robust 99.99% clearing
and accumulation of trades… measure each order to make sure it behaves as
expected… do you need to improve your telemetry to do it? how can you get to
production-grade parity and reliability?"*

Short answers:

1. **Do you need better telemetry?** Yes — and it now exists. Per-order lifecycle
   tracking (`offchain/order_telemetry.py`) follows every order from placement to a
   durable terminal state and computes the clearing SLO. `soak_verdict.py` turns the
   event log into a pass/fail against a target.
2. **Can you get 99.99% clearing?** Yes — on a **coherent-consensus** chain. On the
   all-lasair net trades clear and accumulate durably. On the **mixed lasair+PolkaJam
   net it is structurally impossible** without a cross-client finality gadget, and the
   telemetry proves exactly why.

## What the telemetry measures

Each order walks a small state machine, and every transition is a Prometheus
counter update **and** a JSONL line (`ORDER_EVENTS_FILE`):

    placed ─┬─► rested ───────────────► (terminal: never crossed — not an SLO input)
            └─► rounded ─┬─► settled ──► (terminal: filled / partial-carried — SLO hit)
                         ├─► reverted ─► rounded   (a re-org ate the settling branch)
                         ├─► requeued ─► placed    (round never landed in time)
                         └─► expired ──► (terminal: marketable & unfilled — SLO MISS)

`jamswap_order_clearing_slo = cleared / (cleared + missed)` over **marketable**
orders — the ones a correct chain was obliged to fill (they crossed standing
liquidity at placement). A resting order that never found a counterparty is not
counted against the chain. `soak_verdict.py <events.jsonl> --target 0.9999` exits 0
iff the SLO clears the bar and no order is stuck open past a grace window.

Grafana: the **JAMswap accounts & trading** dashboard gained an SLO row — the
headline gauge, cleared/missed, clear-latency p50/p99, outcomes-per-minute, retries
(reverts + timeouts), and open-orders-by-phase.

## Why the mixed net cannot hit 99.99% (measured, not asserted)

The 5-of-6 availability threshold means a work-report needs 5 assurances. lasair
reaches it by self-signing assurances for validator indices 0–4 when one of its own
nodes authors the carrying block — **PolkaJam never assures a lasair report** (no
cross-client erasure-coded data-availability). So a settlement lands only if:

1. a lasair node authors within U=5 slots of the guarantee, **and**
2. that block stays on the canonical chain.

But the two client families do not share a fork choice: lasair follows longest-chain;
PolkaJam follows first-seen with a dummy-finality ratchet it never re-orgs. lasair's
settlement blocks routinely lose canonicity from the reader's view. Measured on the
running mixed net (45 min): **138 work-items guaranteed, cumulative volume 0, every
balance still at genesis.** The per-order telemetry over a fresh window: 30 placed,
30 rounded, 4 reverted, 2 requeued, **0 cleared**. Orders pile up in `rounded` and
never terminate — the exact signature of "no shared finality."

This is not a token-model bug, a throughput bug, or a matching bug (those were the
earlier fixes: batched work-packages, the durable-settlement hold). It is a
**consensus** property. The honest fix is a cross-client finality gadget; the 150 s
`SETTLE_HOLD_SECS` is a stopgap that rides out epoch-scale re-orgs, not a cure.

## The all-lasair reliability net (`docker-compose.lasair6.yml`)

Six lasair validators (standard JAM dev accounts, indices 0–5), one client, one
fork-choice rule. No PolkaJam, so no cross-client disagreement: guarantees stay
canonical, the availability dance completes, and settlements are durable.

    LASAIR_IMAGE=lasair:local docker compose -p lasair6 -f docker-compose.lasair6.yml up -d
    curl -s localhost:8081/api/orders_slo          # live SLO
    docker exec lasair6-dex-1 python3 soak_verdict.py /shared/order_events.jsonl --target 0.9999

Bring-up note: with a genesis at slot 0 and wall-clock slots, all six nodes sit in
the sync-gate ("back-filling, not authoring") until the 300 s liveness escape, then
author from the current wall slot. After that the chain advances one block per slot,
batches up to 6 work-items per report, and clears. Because re-orgs are shallow, the
dex runs a short `SETTLE_HOLD_SECS=18` (3 slots) for fast confirmation — set per-net
in the compose, measured safe by `jamswap_settle_reverted_total`.

### What this net PROVES, and the bug it exposes next

**Consensus is coherent — measured.** All six nodes report an identical head and
state root at every height (verified at h=103: same head, same root, same slot on
lm0–lm5). The CE-133 pipeline flows: "guaranteed 6 work-item(s) → 6 accumulated",
steadily. This is the property the mixed net cannot have, and it is the precondition
for durable settlement. The diagnosis holds: coherent consensus makes accumulation
durable; cross-client divergence makes it impossible.

**Then two settlement bugs cap clearing — both fixed this session, 14× improvement.**
`order_telemetry` made them visible (cleared plateaus while rounded/requeued climb):

1. **seq-floor cascade (fixed).** The service enforces a per-account monotonic seq
   (lib.rs `check_orders`): every signed order's seq must strictly beat its account's
   on-chain floor, the floor rises to each order's seq *in round order*, and one stale
   order rejects the **whole round untouched** (fail-closed). A timed-out round
   re-queues its orders out of seq order → a later round carries a stale-seq order →
   wholesale reject → the same payload re-submits and the node drops it as "already in
   pipeline" → permanent stall. Fix: `_seq_sanitize()` sorts a round's signed orders by
   (account, seq) and drops any at/below the on-chain floor (`b"sq"‖handle`) as
   permanently dead. Result: cv unstuck from 15.
2. **giant-round wedge (fixed).** One round settles per market at a time; a 253-order
   round that fails to settle wedges the market for the gate window while it cycles.
   Fix: `MAX_ROUND_ORDERS` is env now (48 on the all-lasair net). Result: the backlog
   drained and **cv reached 214** (14× the stuck plateau), with orders resting on-chain.

**The residual blocker — availability throughput (open).** Sustained 99.99% is not yet
reached. Under continuous load the lasair pipeline reports *"report timed out
unassured"*: a work-report is guaranteed but the availability/assurance dance does not
land an assurance within U=5 slots often enough, so the report never accumulates and
re-queues. This is NOT consensus (the six nodes stay bit-identical) and NOT the seq
bug (fixed) — it is the guarantee→assure→accumulate throughput not keeping pace with
the DEX's offered round rate. It is the next target: raise the assurance-inclusion rate
(assure every pending report on every authored block, across all six guarantors) so
availability completes inside U on every round.

## Production-grade parity & reliability — the roadmap

| Goal | Status | Blocker / next step |
|---|---|---|
| Per-order SLO measurement | **done** | `order_telemetry` + `soak_verdict` + dashboard |
| Durable clearing on a coherent chain | **demonstrated** | all-lasair net clears & accumulates |
| 99.99% on the mixed net | **blocked** | cross-client finality gadget (GRANDPA/BEEFY-style) |
| Faster confirmation | tunable | `SETTLE_HOLD_SECS` per consensus; shallow re-orgs → 18 s |
| k8s soak with per-order verdict | next | `k8s/loadgen.yaml` + `soak_verdict.py` as the gate |
| ERC-20 completeness (supply keys, transfer) | roadmap | service v2 (docs/TOKENS.md) |

The route to a green 99.99% soak: run the load against the **all-lasair** net (or a
finality-equipped mixed net), let `soak_verdict.py` assert the SLO, and watch the
dashboard's SLO gauge sit at 1.0 with `jamswap_settle_reverted_total` flat.

## The DEX fuzzer (`offchain/dex_fuzz.py`) — how we drive both projects to correct

Same discipline as the JAM conformance fuzzer that hardened lasair: a seeded,
reproducible order stream at ESCALATING scale, hard invariants after every level,
and HALT-ON-FIRST-DIVERGENCE with a forensic dump. Each bug we fix lets the next
run reach a deeper level; the goal is thousands of orders settling 100% correctly.

Each level places a **balanced, all-crossing** batch (every buy above every sell,
total buy qty == total sell qty) that a correct FBA must clear in full, making the
end state exactly predictable. The invariants, checked hardest-first:

  * **LIVENESS** — the batch reaches quiescence within the settle budget. This is
    the 99.99% SLO; a stall (dex round wedge / lasair availability not keeping up)
    fails here with a reproducing seed.
  * **CONSERVATION** (exact) — per asset, the six-account supply is invariant.
  * **POSITION** (exact) — each account's DOT moves by exactly (bought − sold).
  * **VOLUME** (exact) — cv rises by exactly the batch's crossing quantity.
  * **BOOK EMPTY**, **NON-NEGATIVE**, **JAMKB no-mint**, **VALUE** (USDC move
    inside the price band for each account's gross fills).

Unlike the continuous loadgen, the fuzzer applies **backpressure** — it waits for
each batch to fully clear before the next — so it tests correctness at each depth
without overwhelming the pipeline, and the depth at which it finally stalls IS the
current penetration limit.

    # run against a FRESH, quiescent all-lasair net with the loadgen stopped:
    docker compose -p lasair6 -f docker-compose.lasair6.yml stop loadgen
    docker exec -e DEX_URL=http://localhost:8080 lasair6-dex-1 python3 dex_fuzz.py --max-pairs 2000
    # a halt prints the invariant, the seed, and a forensic file under /shared/fuzz;
    # reproduce it exactly:
    docker exec lasair6-dex-1 python3 dex_fuzz.py --seed <S> --only-level <N>

Progress (`deepest_pairs`) persists in `/shared/fuzz/progress.json`, so each rerun
picks up the escalation where the last fix left off.
