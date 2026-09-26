# Testing Jamswap

Testing is layered — each layer is fast, deterministic, and checks a different thing.
Run them all before shipping; CI (`.github/workflows/ci.yml`) runs the first three on
every push. (The encrypt-until-batch attack e2e — tampered / wrong-committee / injected
rounds rejected — ran on lasair's retired HTTP operator RPC and left with it in #10; it
returns over `chain.py` with runtime deploy, #13.)

| Layer | Where | What it proves | Needs |
|-------|-------|----------------|-------|
| **1. Matching engine** | `crates/match-engine/src/lib.rs` (unit + property) | clearing optimality, conservation, determinism, per-order bounds | Rust |
| **2. Engine scenarios** | `crates/match-engine/tests/scenarios.rs` | order **sequences** across rounds — the continuous book (rest → later cross → fill) | Rust |
| **2b. Replay floors, round ids, carry credits, deposits** | `crates/match-engine/src/floors.rs`, `src/round_id.rs`, `src/carry.rs`, `src/deposit.rs` | accumulate's state-side rules, host-tested: sealed commits and public orders have **separate seq floors** (a commit at seq 20 never rejects an order at seq 15), replays of both refused, round checks fail-closed; the round id (shared fixture with the Python builder, binds every payload byte), landed-round markers that no number of later rounds evicts and that expire by age with bounded work per accept, and the round-output auth trailer (`src/wire.rs`); a **duplicated carry-commit is refused without spending a credit**, so an account's second remainder still carries (two partial fills → 2 credits → remainder 1 lands twice, the copy is refused, remainder 2 lands); a **duplicated or replayed deposit is credited once**, deposits that land out of order are each credited, one reordered deeper than the 16-nonce window is refused rather than doubled, and the 25-byte DEPOSIT layout (shared fixture with the Python builder) | Rust |
| **3. Round lifecycle** | `offchain/tests/test_round_lifecycle.py` | the **sealed-order lifecycle** — which orders clear now, rest hidden, or expire; the batch cap (`plan_batch`) counts only orders that can trade this round, never reveals a sealed order whose counterparty missed the cap, and refills a bounded number of times | Python (stdlib) |
| **3b. Treasury** | `offchain/tests/test_treasury.py` | the **self-funding treasury** — fees cover JAMKB rent first, only surplus is withdrawable profit | Python (stdlib) |
| **3c. Trade tape** | `offchain/tests/test_trade_tape.py` | the **recent-trades feed** — clearing prints recorded from cumulative-volume deltas, metrics, tick direction | Python (stdlib) |
| **3d. Clearing parity** | `offchain/tests/test_clearing.py` | the builder's Python clearing (`clearing.py`) matches the Rust engine **scenario-for-scenario** — so the fill receipts can't lie | Python (stdlib) |
| **3e. Execution reports** | `offchain/tests/test_executions.py` | the **per-order fill receipts** — filled qty @ uniform price + remainder disposition (rested / cancelled), per account | Python (stdlib) |
| **3f. JAMKB standard** | `offchain/tests/test_jamkb_standard.py` | **solvency backpressure** (refuse new state while under-reserved) + beneficiary reserve top-up | Python (stdlib) |
| **3g. Order lifetime** | `offchain/tests/test_order_lifetime.py` | **anti-bloat** — rent-funded expiry (sealed sooner than public, hard-capped), GTC never infinite, per-account open-order cap | Python (stdlib) |
| **3h. Sealed carry** | `offchain/tests/test_sealed_carry.py` | a **large sealed order accumulates fills across auctions** — its partial-fill remainder is re-sealed and carried forward (not cancelled), unless expired | Python (stdlib) |
| **3i. Round poisoning + late settlement** | `offchain/tests/test_round_poison.py` | a trader's own sealed commit no longer sinks their round; a round that can't settle is **released in seconds** to the front of the mempool; a released round that settles late is **finalized, not re-submitted** (after a second sighting, each stamped with the time of its read, by a build or a resolver sweep; a re-org on either side of a two-fork flip hands the orders to the round that won); one record per round id; a build and the resolver never interleave on a market; submit timeouts and carry-post failures lose nothing; no same-account overtaking under the batch cap; repeated seqs / out-of-band market prices never sink a round; truthful receipts for superseded and cancelled orders; the builder's round id equals the service's (SMATCH and encrypt-until-batch) | Python (stdlib) |
| **3j. Chain adapter** | `offchain/tests/test_chain.py`, `offchain/tests/test_jip2.py` | the DEX reaches the chain only through `chain.py`: the jamnp backend sends **byte-identical** requests to the pre-adapter code (bridge `/submit` + `/read`, node metrics) and reports the same finality; ChainBusy backpressure and the settle ledger; finality by height or by slot; the JIP-2 client's WebSocket framing (RFC 6455 accept vector, 7/16/64-bit lengths, fragments, ping, close, reconnect, no retry on submit) and the jip2 backend's mapping (block descriptors, `serviceValue` at best/final, the `serviceData` account record) | Python (stdlib) |
| **3k. Deposit producers** | `offchain/tests/test_deposit.py` | every DEPOSIT the builder sends (`/api/deposit`, the reserve top-up and seeding) is the 25-byte layout the service decodes (shared fixture with `deposit.rs`) with a fresh, strictly increasing nonce (even if the clock steps back); a caller-supplied nonce makes a retried request byte-identical; out-of-range nonces are refused before anything is sent | Python (stdlib) |
| **3l. Finality from the chain** | `offchain/tests/test_finality_reads.py` | durable decisions read the **state at the finalized head** where the backend can (jip2): a sealed commit is revealed once it is in the finalized commit set (not by slot arithmetic, either way round), a round is receipted once its landed marker is final (no timer while finality advances; the slot-counted hold when it stalls), and a fill's `final` flag turns true once its round is final; the derived settle hold (0 / `SETTLE_HOLD_SLOTS` / override); jamnp keeps its height rules, compared with the pre-change code as an oracle | Python (stdlib) |
| **3m. Round batch** | `offchain/tests/test_round_batch.py` | what goes into a round's batch: hidden or deferred sealed orders **never take the batch cap** ahead of public orders (jamswap#6: cap 4, four deferred sealed orders and a crossing public pair → the pair is submitted in the next auction, not starved for the orders' ~32 min life); a sealed order whose counterparty missed the cap waits hidden rather than being revealed alone; a long run of them gives way and crosses the book a round later; sealed orders that can trade keep their place in the queue; a sealed order whose counterparty is dropped or re-priced away before submit (superseded seq, unpriceable or re-priced market order, claimed by a late landing) is **not revealed** (jamswap#8); encrypt-until-batch rounds are **bounded by refine gas** (21 at n = 2, n read from the on-chain committee) and a timed-out one is rebuilt with half as many, doubling back once one lands | Python (stdlib) |
| **3n. Work-package submission** | `offchain/tests/test_workpackage.py`, `offchain/tests/test_jip2_submit.py` | the GP 0.8.0 work-package codec byte for byte (general naturals at every length boundary, refine context, work-item, package, hash; cross-checked with jam-types-py 0.8.0 when installed), the authorizer found in a JIP-4 chain spec's genesis (pools, preimage keys), and `Jip2Chain.submit`: the package it sends (anchor, lookup anchor, roots, code hash, gas, authorizer), core rotation, refusals as ChainBusy, a dropped send never retried | Python (stdlib) |
| **3o. Runtime deploy + DEX setup** | `offchain/tests/test_deploy.py`, `offchain/tests/test_dex_setup.py` | the Bootstrap CreateService payload **byte for byte** against two payloads `jamt create-service` sent to a PolkaJam 0.1.29 node we ran; the deploy flow on a fake chain whose Bootstrap service parses the instruction with its own parser and runs GP `new` (lowest free id, the code provided with `submitPreimage`, usable once at the lookup anchor; a re-run reuses the service through the state file or by its code hash; a stale state file, a Failed package resubmitted, a Bootstrap that is not the registrar, one that provides the code itself, an explicit id, a refused create, a timeout); multi-item packages (order, shared gas, limits); `dex_setup`: markets + the six dev accounts in one package (handles 1..6), deposits once per fixed nonce, a re-run sends nothing, only what is missing, lost packages resent; the API opens once the JAMKB reserve covers the footprint | Python (stdlib + PyNaCl) |
| **4. End-to-end** | `offchain/test_sealed_resting_e2e.py`, `offchain/verify.py` | the real service on a live node: sealed orders rest & cross across rounds, a public order followed by the same account's sealed order still fills; register / duplicate-survival / deposit / withdraw / a matched trade | Docker + node |

## Why layer 3 exists (the bug it caught)

A user placed sealed sells, then — seconds later — sealed buys, and **nothing
matched**. Root cause: sealed orders were *immediate-or-cancel* and the auction loop
drained the whole pending queue every 6 s, so orders placed in different 6 s windows
were never in the same batch. The matching engine (layers 1–2) was correct the whole
time; the bug was in **round orchestration** — which had no tests.

Layer 3 tests the pure planner (`offchain/round.py`) that now decides, from the
plaintext the builder holds, which sealed orders **cross** current liquidity (reveal +
clear this round) vs **don't** (rest hidden, retry next round). The regression test
[`test_lone_sealed_sells_rest_hidden_then_buys_cross`] is exactly the user's sequence.

## Run them

```sh
# Layers 1 + 2 — the matching engine (property + scenario tests)
cd crates/match-engine && cargo test --release

# Layer 3 — the off-chain builder: round lifecycle, receipts, chain adapter (no node needed)
python3 -m unittest discover -s offchain/tests -v

# Layer 4 — full end-to-end against the running stack
docker compose up -d
make verify                                                  # verify.py inside the dex
JAMSWAP_URL=http://127.0.0.1:8080 python3 offchain/test_sealed_resting_e2e.py
```

## Adding a scenario

- A new **matching** rule (prices, rationing, resting): add a case to
  `crates/match-engine/tests/scenarios.rs` — construct a book, `clear()` it, assert the
  price/volume/fills, then `resting()` and feed it into the next round.
- A new **sealed-order lifecycle** rule (when to reveal / carry / expire): add a case to
  `offchain/tests/test_round_lifecycle.py` — build a `pending` list with `buy()`/`sell()`
  (sealed) or `pbuy()`/`psell()` (public), call `run_round`, and assert
  `plan.reveal` / `plan.carry` / `plan.expired`.

Keep layer 3 **pure** (no node, no committee binary) so it stays in CI and runs in
milliseconds. Anything needing a real node belongs in layer 4.
