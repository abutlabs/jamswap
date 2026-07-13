# The jamswap DEX fuzzer

`offchain/dex_fuzz.py`

## Mission

Drive jamswap **and** lasair to provable correctness at scale. Populate the DEX with
orders, verify — to the atomic unit — that the exchange and the chain behaved exactly
as they must, then add more orders. **On the first divergence, halt, capture a
reproducing forensic, root-cause it, fix the offending project, and rerun deeper.**
Repeat until thousands of orders settle 100% correctly.

This is the same discipline that hardened lasair against the JAM conformance fuzzer:
a seeded, reproducible input stream at escalating penetration; an independent oracle;
invariants checked after every step; halt-on-first-divergence; and a persisted
high-water mark so each fix earns deeper coverage. The fuzzer is deliberately
adversarial toward *both* codebases — a halt is a finding in jamswap, in lasair, or in
the oracle itself (an incomplete model is also a bug worth fixing).

## Why it finds real bugs

Each level places a **balanced, all-crossing** batch: every buy is priced above every
sell, and total buy quantity equals total sell quantity. A correct frequent-batch
auction *must* clear such a batch in full and leave the book empty — so the end state
is exactly predictable without re-implementing the matching engine (the oracle cannot
share a bug with the code under test). What the oracle asserts, hardest-first:

| Invariant | What a violation means |
|---|---|
| **SERVER_FAULT** | no order is rejected with HTTP 500. A 500 is the service *breaking* on a well-formed request — a real bug — as opposed to a legitimate refusal. Halts immediately, before settlement |
| **CAPACITY** | the full balanced batch is *accepted*. HTTP 400 refusals (open-order cap, insufficient funds) are the DEX correctly applying backpressure — not a correctness bug, but the true penetration ceiling at this depth. Reported distinctly so the availability wall is never conflated with a stall or a fault |
| **LIVENESS** | the batch reaches quiescence within budget. This is the 99.99% SLO — a stall (dex round wedge, lasair availability not keeping pace) fails here with a reproducing seed |
| **CONSERVATION** (exact) | per asset, the sum over the six dev accounts **and the fee treasury** is invariant. A step = value created/destroyed, or a re-org rewriting history |
| **POSITION** (exact) | each account's base (DOT) moved by exactly (bought − sold) − fee×(its orders) |
| **FEE_ACCRUAL** (exact) | the treasury received exactly the flat fee once per order |
| **VOLUME** (exact) | cumulative on-chain volume rose by exactly the batch's crossing quantity |
| **BOOK EMPTY** | a balanced all-cross rests nothing |
| **NON-NEGATIVE** | no balance underflowed |
| **JAMKB no-mint** | trading never mints the rent asset |
| **VALUE** | each account's quote (USDC) move sits inside the price band for its gross fills |

The crucial design choice is **backpressure**: the fuzzer waits for each batch to fully
clear before offering the next. Unlike a continuous load generator, it never overwhelms
the pipeline — so it isolates *correctness* at each depth, and the depth at which it
finally stalls is the true penetration limit.

## Running it manually

The fuzzer needs a **quiescent, coherent net with the load generator stopped** — it wants
exclusive control of the six dev accounts' order sequences (a stray loadgen order bumps an
on-chain seq floor and the fuzzer's next signed order is rejected). The all-lasair net is
the coherent-consensus target where durable settlement is possible (see
`SOAK_RELIABILITY.md`); the mixed net has no shared finality and cannot clear.

**Step 1 — bring up a fresh, coherent net (no loadgen).** A fresh genesis (`down -v`) is
required whenever the service `.jam` changed; otherwise you can reuse a running net.

```sh
cd submodules/jamswap
docker compose -p lasair6 -f docker-compose.lasair6.yml down -v          # fresh genesis
docker compose -p lasair6 -f docker-compose.lasair6.yml up -d \
    spec-init lm0 lm1 lm2 lm3 lm4 lm5 builder reader dex
```

**Step 2 — wait for the net to become live (~5 min), then confirm coherence.** Genesis
starts at slot 0; the six validators sit in the sync-gate ("back-filling") until the **300 s
liveness escape**, then author from the wall clock. Poll until a block is authored — this
loop is verified to fire at ~300 s:

```sh
for i in $(seq 1 42); do
  h=$(docker logs --since 20s lasair6-lm0-1 2>&1 | grep STATUS | tail -1 \
      | grep -oE 'height=[0-9]+' | grep -oE '[0-9]+')
  [ -n "$h" ] && [ "$h" -ge 2 ] && { echo "AUTHORING at height=$h (~$((i*10))s)"; break; }
  echo "  +$((i*10))s height=${h:-0} (back-filling)"; sleep 10
done
# ... verified output ends with:  AUTHORING at height=2 (~300s)
```

Then **confirm all six nodes agree** — a fresh net is coherent (identical `head` at each
height); a net that has run for hours under load can fork and wedge (see the box below):

```sh
for n in lm0 lm1 lm2 lm3 lm4 lm5; do
  printf '%s  ' "$n"
  docker logs --since 15s lasair6-$n-1 2>&1 | grep STATUS | tail -1 \
    | grep -oE 'height=[0-9]+ head=0x[0-9a-f]{10}'
done
# verified fresh-net output — heights within 1, heads identical at the shared height:
#   lm0  height=4 head=0x6b7cbe4f78
#   lm1  height=4 head=0x6b7cbe4f78
#   lm2  height=5 head=0x2a4b0875b1      (one block ahead — just authored)
#   lm3  height=4 head=0x6b7cbe4f78
#   lm4  height=4 head=0x6b7cbe4f78
#   lm5  height=4 head=0x6b7cbe4f78
```

> **⚠ Coherence can decay under sustained load.** On a net left running ~11 h under soak +
> fuzzer backlog, the six nodes were observed to **fork into 4+ chains and wedge** (heights
> frozen at 2313 / 2373 / 2418 / 2436 / 2659 / 2659, static across a 40 s window, nodes
> heart-beating STATUS but authoring nothing). The longest-chain, no-finality net does not
> stay coherent indefinitely under load — this is the same availability/consensus wall the
> fuzzer hits as CAPACITY at L7. **If the six heights don't track together, or a level stalls
> on LIVENESS, tear down and start fresh (`down -v`) — don't fight a wedged net.** To detect a
> wedge: sample the heights twice ~40 s apart; if none advanced, it's wedged.

Optionally smoke-test with the shallowest level (verified to clear in ~24 s on a fresh net):

```sh
docker exec -e DEX_URL=http://localhost:8080 -e PYTHONUNBUFFERED=1 \
    lasair6-dex-1 python3 -u dex_fuzz.py --only-level 1
#   → "✓ level 1 clean in 24s (volume 16, conservation exact, positions exact, book empty)"
```

**Step 3 — run the escalation.** `-u` (unbuffered) so you see each level land live:

```sh
docker exec -e DEX_URL=http://localhost:8080 -e PYTHONUNBUFFERED=1 \
    lasair6-dex-1 python3 -u dex_fuzz.py --max-pairs 2000
```

It escalates through `LEVELS` until it clears everything under `--max-pairs` or halts on
the first divergence. Progress persists, so a later run resumes deeper. **Verified end-to-end
run** on a fresh net (`--max-pairs 8`):

```
dex_fuzz: seed=1000 deepest_so_far=0 pairs  dex=http://localhost:8080  reader=http://reader:19990

── level 1: 1 pairs / 2 orders, expected volume 16 DOT ──
  placed 2/2 (refused 0: 0×400 cap, 0×500 fault, 0×other); waiting to clear...
  ✓ level 1 clean in 24s (volume 16, conservation exact, positions exact, book empty)
── level 2: 3 pairs / 6 orders, expected volume 24 DOT ──
  placed 6/6 (refused 0: 0×400 cap, 0×500 fault, 0×other); waiting to clear...
  ✓ level 2 clean in 30s (volume 24, conservation exact, positions exact, book empty)
── level 3: 8 pairs / 16 orders, expected volume 53 DOT ──
  placed 16/16 (refused 0: 0×400 cap, 0×500 fault, 0×other); waiting to clear...
  ✓ level 3 clean in 27s (volume 53, conservation exact, positions exact, book empty)
stop: level 4 (20 pairs) exceeds --max-pairs 8

✓ ALL LEVELS PASSED up to 8 pairs. Raise --max-pairs / extend LEVELS to penetrate deeper.
```

Flags:

- `--max-pairs N` — stop once a level would exceed N crossing pairs.
- `--seed S` — fix the seed (0 ⇒ derived from persisted progress, advances each run).
- `--only-level L` — run exactly one level (1-based into `LEVELS`) and stop. Use with
  `--seed` to **reproduce a halt exactly**.
- `--settle-timeout T` — per-level clearing budget in seconds (the LIVENESS deadline;
  default 180). Raise it on a slow host so a slow-but-correct settle isn't misread as a stall.
- `--tolerance U` — allowed volume/position slack in whole base units (default 0 — exact).

## Monitoring while it runs

There are three surfaces, easiest first: a **Grafana dashboard** (the whole picture at a
glance), the **fuzzer's own stdout**, and **`curl`/`docker` one-liners** for spot checks.

### Grafana — the live view onto the DEX (recommended)

A provisioned dashboard, **JAMswap accounts & trading**, already draws everything worth
watching: per-dev-account balances (USDC/DOT/JAMKB for Alice…Fergie), last clearing price,
cumulative volume, mempool + in-auction depth, the order funnel (submitted → settled →
refused → timed-out → reverted), API/settlement failures, and the per-order clearing SLO.
Bring it up **alongside** the lasair6 net (same project & network, so it scrapes the dex,
builder and all six lm nodes by service DNS):

```sh
docker compose -p lasair6 \
    -f docker-compose.lasair6.yml -f docker-compose.lasair6-monitor.yml \
    up -d prometheus grafana
#   Grafana:    http://localhost:3001   (anonymous admin, opens on accounts & trading)
#   Prometheus: http://localhost:9091
```

Ports are **3001 / 9091** so this coexists with the mixed net's monitor (3000 / 9090) if
that is also up. Confirm the feed is live (verified output shown):

```sh
curl -s http://localhost:3001/api/health                    # {"database":"ok", ...}
curl -s 'http://localhost:9091/api/v1/targets?state=active' \
  | grep -o '"health":"[a-z]*"' | sort | uniq -c            # 8x "health":"up"  (dex, builder, lm0-5)
# a live query straight through Grafana's datasource proxy:
curl -sG http://localhost:3001/api/datasources/proxy/uid/prometheus/api/v1/query \
  --data-urlencode 'query=jamswap_balance{asset="DOT"}'      # per-account DOT, e.g. Alice 999990
```

Dashboards provisioned (all reachable from the top-left links): **`/d/jamswap-accounts`**
(balances & trading — the one you want), `/d/jam-service` (settlement mechanics & latency),
`/d/jam-node` (a single lasair node), `/d/jam-mixed`, `/d/jam-clients`. Panels that reference
PolkaJam show "No data" on the all-lasair net — expected; the jamswap/lasair panels are full.

### The fuzzer's stdout and CLI spot-checks

The fuzzer prints a running commentary to stdout (step 3's terminal). It polls silently
during the settle wait, so for the live net picture use Grafana or the one-liners below.

**The fuzzer's stdout** — one block per level (see the verified run above):

- the **header** (`── level N: P pairs / O orders, expected volume V DOT ──`) = the batch it's
  about to place and the DOT volume it must then see settle.
- the **placed** line (`placed O/O (refused 0: 0×400 cap, 0×500 fault, 0×other)`) = the
  acceptance outcome. `O/O refused 0` is a fully-offered balanced batch. Any non-zero count is
  already meaningful — see the CAPACITY / SERVER_FAULT rows under *Interpreting outcomes*.
- the **✓ / ╳** line = the verdict once the batch reaches quiescence (or the budget expires).

**Second terminal — the live net. Every command below was run and its output verified:**

```sh
# [1] cumulative on-chain volume — the single most useful gauge. Must climb to the target
#     then hold flat. Flat while orders are pending ⇒ settlement stalling (→ LIVENESS).
curl -s localhost:8081/metrics | grep -E \
  'jamswap_cum_volume\{market="1"\}|jamswap_resting_orders\{market="1"\}|jamswap_inflight_orders\{market="1"\}'
#   verified after levels 1-3 (16+24+53):  jamswap_cum_volume{market="1"} 93

# [2] API outcomes — {code="200"} good; a rising {code="400"} is legit backpressure
#     (CAPACITY ceiling); any {code="500"} is a real server fault.
curl -s localhost:8081/metrics | grep jamswap_api_requests_total
#   verified clean run:  jamswap_api_requests_total{code="200",route="/api/order"} 24
#                        (no code="400" / code="500" lines present)

# [3] per-account positions (display units) and the rounds settled so far.
curl -s localhost:8081/metrics | grep -E 'jamswap_balance\{account="Alice"|jamswap_settled_total'
#   verified:  jamswap_balance{account="Alice",asset="DOT"} 999990   (fee-adjusted)
#              jamswap_settled_total{op="round"} 3
#   (the flat fee to the treasury is asserted by the fuzzer's FEE_ACCRUAL invariant, not a
#    standalone gauge — the treasury handle 4294967295 isn't a named-account label here.)

# [4] the service's own view of each round (settlement, round gating, seq rejects):
docker logs -f lasair6-dex-1
#   verified sample (order counts match levels 1-3):
#     round m1: settled on-chain — receipted 2 order(s), carried 0
#     round m1: settled on-chain — receipted 6 order(s), carried 0
#     round m1: settled on-chain — receipted 16 order(s), carried 0

# [5] chain liveness — re-run the six-node agreement check from Step 2; heights must track
#     together and advance. Two samples ~40 s apart that don't move ⇒ wedged, restart.
```

A `watch -n2` around command [1] gives a live gauge you can leave open beside the run.
`jamswap_inflight_orders` cycles 0→N→0 as rounds settle; `jamswap_resting_orders` drains to
**0** at quiescence (a balanced batch rests nothing). Per-order lifecycle events also stream
to `/shared/order_events.jsonl` in the dex container (`ORDER_EVENTS_FILE`) — feed it to
`soak_verdict.py` for a pass/fail SLO number.

**Artifacts the run leaves behind** (under `/shared/fuzz/` in the dex container) — verified:

```sh
docker exec lasair6-dex-1 cat /shared/fuzz/progress.json
#   {"deepest_pairs": 8, "levels_passed": 3}
docker exec lasair6-dex-1 ls  /shared/fuzz/
#   progress.json           (+ halt_seed<S>_L<N>.json for each halt)
docker exec lasair6-dex-1 cat /shared/fuzz/halt_seed1000_L7.json   # a specific halt, if any
```

## Interpreting outcomes

**A clean level** — `✓ level N clean … (volume V, conservation exact, positions exact, book
empty)` — means every hard invariant held to the atomic unit at that depth. The escalation
continues; `deepest_pairs` advances in `progress.json`.

**A halt** stops the run and writes a forensic. It always names the invariant and the first
move:

```
╳ HALT at level 5 (50 pairs) — POSITION
  account 1 DOT delta 194900 atomic, expected 195200 (traded +20 DOT, 16 orders x 300 fee)
  forensics: /shared/fuzz/halt_seed1000_L5.json
  deepest CLEAN level so far: 20 pairs
  reproduce: dex_fuzz.py --seed 1000 --only-level 5
```

The forensic under `/shared/fuzz/` holds the seed, the full order stream, and the
expected-vs-actual state diff — everything needed to root-cause offline and to replay after
a fix. Read the halt by its invariant:

| Halt | What it means | First move — who's the suspect |
|---|---|---|
| **SERVER_FAULT** | an order got HTTP 500 — the service *broke* on a well-formed request | **jamswap** almost always. Read `docker logs lasair6-dex-1` for the traceback at that order |
| **CAPACITY** | the DEX correctly refused part of the batch (HTTP 400: open-order cap / funds) — the true backpressure ceiling, not a correctness bug | **lasair availability throughput** (the DEX can't retire orders fast enough, so the cap fills) — or raise `MAX_OPEN_ORDERS`. This is the expected wall at the current frontier |
| **LIVENESS** | the batch never reached quiescence in the budget — cumulative volume stalled below target | **lasair** (guarantee→assure→accumulate not keeping pace) or a **dex round wedge**. Check `jamswap_cum_volume` was flat and `jamswap_inflight_orders` was stuck |
| **CONSERVATION** | per-asset total over the six accounts **+ treasury** changed | **serious** — value created/destroyed, or a re-org rewrote history. Suspect lasair finality first, then service settlement math |
| **POSITION** | an account's DOT moved by ≠ (bought − sold) − fee×(its orders) | **jamswap** settlement/fee math (this is how the per-fill fee double-charge was caught) |
| **FEE_ACCRUAL** | treasury didn't grow by exactly 300 × orders introduced | **jamswap** fee application (or the oracle's fee model is stale) |
| **VOLUME** | on-chain cumulative volume rose by ≠ the batch's crossing quantity | **jamswap** matching, or a partial settle counted as full |
| **BOOK EMPTY** | a balanced all-cross left something resting | **jamswap** matching (a cross wasn't taken) — or the batch wasn't actually balanced (oracle) |
| **NON-NEGATIVE** | a balance underflowed | **jamswap** — a debit exceeded a balance without a floor |
| **JAMKB_MINT** | the rent asset supply moved across *traders* | **jamswap** token accounting (treasury reserve mint is expected on devnet and excluded) |
| **VALUE** | an account's USDC move fell outside its gross-fill price band | usually the **oracle** band is too tight; confirm before blaming the service |

**Deciding jamswap vs lasair vs oracle.** Three cross-checks: (1) **reproduce** with `--seed
S --only-level L` — a deterministic repeat is a real bug; a flaky one points at timing/lasair
liveness. (2) If it reproduces, decide whether the *oracle's expectation* is right — an
incomplete model is itself a bug to fix (that's how the fee/treasury and JAMKB findings
resolved). (3) Only once the expectation is confirmed correct do you touch the service or the
chain — **root-cause before you edit**, one change per loop, and never loosen an invariant to
turn the bar green.

`deepest_pairs` persists in `/shared/fuzz/progress.json`, so the next run resumes the
escalation where the last fix left off.

## The improvement loop (how we use it)

1. **Run** it against the current net; let it escalate until it halts.
2. **Root-cause** the halt from the forensic. Is it jamswap, lasair, or the oracle?
3. **Fix** the offending project (service `.jam` rebuild + fresh genesis if the service
   changed; lasair rebuild if the chain; oracle if the model was incomplete).
4. **Rerun** — it resumes deeper. Extend the `LEVELS` tail as the ceiling rises.

### Findings so far

| Date | Halt | Root cause | Fix |
|---|---|---|---|
| 2026-07-09 | CONSERVATION @ L1 | 300-atomic base fee → treasury; the oracle didn't model it — **jamswap correct** | oracle: model the fee + treasury + a FEE_ACCRUAL invariant |
| 2026-07-10 | POSITION @ L5 (>1 round) | the flat fee was charged **per fill**, so an order that filled across two rounds paid it twice — **jamswap bug** | service: charge the fee **once per order at introduction** (per binding), not per fill; rebuilt the `.jam` |
| 2026-07-10 | JAMKB_MINT @ L1 | the service mints JAMKB into its treasury **rent-reserve** each round; the oracle counted the treasury in the JAMKB supply — **known devnet mint**, oracle too strict | oracle: check JAMKB over **traders only** (treasury reserve mint is expected on devnet; production fix is on the TOKENS.md roadmap) |
| 2026-07-10 | LIVENESS/500 @ L7 | client-side rejections (open-order cap, insufficient funds) surfaced as **HTTP 500**, so a legitimate refusal looked like a server crash — and a partly-placed batch then failed LIVENESS misleadingly — **jamswap API bug** | server: return **HTTP 400** for validation refusals (500 reserved for real faults); fuzzer: classify 400 (CAPACITY ceiling) vs 500 (SERVER_FAULT) vs stall (LIVENESS) so the availability wall is measured cleanly. L7 now reads: 328/600 placed, 272×400 cap — the correct backpressure limit |

## Roadmap — scaling the test sets

- **Deeper LEVELS.** Extend `1500 → 3000 → 6000 → …` once each ceiling is clean; the
  next wall is expected to be lasair availability throughput (LIVENESS), which is the
  real 99.99% blocker (see `SOAK_RELIABILITY.md`).
- **New order shapes (tiers).** Beyond balanced all-cross: partial fills and resting
  makers (book-integrity invariants), cancels, market orders (band checks), sealed /
  commit–reveal orders (currently fee-free — a known simplification to close), and
  imbalanced batches (predictable residual resting depth).
- **Adversarial inputs.** Bad signatures, stale/duplicate seqs, over-cap batches,
  fabricated books — assert the service *rejects* the round and conserves state (the
  fail-closed paths).
- **Fault injection.** Kill/restart a validator mid-soak and assert the durable-
  settlement hold never lets a re-org erase a confirmed fill (CONSERVATION + VOLUME
  monotonicity across the fault).
- **CI gate.** `soak_verdict.py` already exits non-zero below target; wire the fuzzer as
  a nightly gate at the current high-water mark so regressions can't land silently.
