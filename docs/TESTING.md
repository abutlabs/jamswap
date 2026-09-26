# Testing Jamswap

Testing is layered — each layer is fast, deterministic, and checks a different thing.
Run them all before shipping; CI (`.github/workflows/ci.yml`) runs the first three on
every push. (The encrypt-until-batch attack e2e — tampered / wrong-committee / injected
rounds rejected — ran on lasair's retired HTTP operator RPC and left with it in #10; it
returns over `chain.py` with runtime deploy, #13, and spec-valid submission, #11.)

| Layer | Where | What it proves | Needs |
|-------|-------|----------------|-------|
| **1. Matching engine** | `crates/match-engine/src/lib.rs` (unit + property) | clearing optimality, conservation, determinism, per-order bounds | Rust |
| **2. Engine scenarios** | `crates/match-engine/tests/scenarios.rs` | order **sequences** across rounds — the continuous book (rest → later cross → fill) | Rust |
| **2b. Replay floors, round ids, carry credits** | `crates/match-engine/src/floors.rs`, `src/round_id.rs`, `src/carry.rs` | accumulate's state-side rules, host-tested: sealed commits and public orders have **separate seq floors** (a commit at seq 20 never rejects an order at seq 15), replays of both refused, round checks fail-closed; the round id (shared fixture with the Python builder, binds every payload byte), landed-round markers that no number of later rounds evicts and that expire by age with bounded work per accept, and the round-output auth trailer (`src/wire.rs`); a **duplicated carry-commit is refused without spending a credit**, so an account's second remainder still carries (two partial fills → 2 credits → remainder 1 lands twice, the copy is refused, remainder 2 lands) | Rust |
| **3. Round lifecycle** | `offchain/tests/test_round_lifecycle.py` | the **sealed-order lifecycle** — which orders clear now, rest hidden, or expire | Python (stdlib) |
| **3b. Treasury** | `offchain/tests/test_treasury.py` | the **self-funding treasury** — fees cover JAMKB rent first, only surplus is withdrawable profit | Python (stdlib) |
| **3c. Trade tape** | `offchain/tests/test_trade_tape.py` | the **recent-trades feed** — clearing prints recorded from cumulative-volume deltas, metrics, tick direction | Python (stdlib) |
| **3d. Clearing parity** | `offchain/tests/test_clearing.py` | the builder's Python clearing (`clearing.py`) matches the Rust engine **scenario-for-scenario** — so the fill receipts can't lie | Python (stdlib) |
| **3e. Execution reports** | `offchain/tests/test_executions.py` | the **per-order fill receipts** — filled qty @ uniform price + remainder disposition (rested / cancelled), per account | Python (stdlib) |
| **3f. JAMKB standard** | `offchain/tests/test_jamkb_standard.py` | **solvency backpressure** (refuse new state while under-reserved) + beneficiary reserve top-up | Python (stdlib) |
| **3g. Order lifetime** | `offchain/tests/test_order_lifetime.py` | **anti-bloat** — rent-funded expiry (sealed sooner than public, hard-capped), GTC never infinite, per-account open-order cap | Python (stdlib) |
| **3h. Sealed carry** | `offchain/tests/test_sealed_carry.py` | a **large sealed order accumulates fills across auctions** — its partial-fill remainder is re-sealed and carried forward (not cancelled), unless expired | Python (stdlib) |
| **3i. Round poisoning + late settlement** | `offchain/tests/test_round_poison.py` | a trader's own sealed commit no longer sinks their round; a round that can't settle is **released in seconds** to the front of the mempool; a released round that settles late is **finalized, not re-submitted** (after a second sighting; a re-org on either side of a two-fork flip hands the orders to the round that won); one record per round id; a build and the resolver never interleave on a market; submit timeouts and carry-post failures lose nothing; no same-account overtaking under the batch cap; repeated seqs / out-of-band market prices never sink a round; truthful receipts for superseded and cancelled orders; the builder's round id equals the service's (SMATCH and encrypt-until-batch) | Python (stdlib) |
| **3j. Chain adapter** | `offchain/tests/test_chain.py`, `offchain/tests/test_jip2.py` | the DEX reaches the chain only through `chain.py`: the jamnp backend sends **byte-identical** requests to the pre-adapter code (bridge `/submit` + `/read`, node metrics) and reports the same finality; ChainBusy backpressure and the settle ledger; finality by height or by slot; the JIP-2 client's WebSocket framing (RFC 6455 accept vector, 7/16/64-bit lengths, fragments, ping, close, reconnect, no retry on submit) and the jip2 backend's mapping (block descriptors, `serviceValue` at best/final, the `serviceData` account record) | Python (stdlib) |
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
