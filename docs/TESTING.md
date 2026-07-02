# Testing Jamswap

Testing is layered — each layer is fast, deterministic, and checks a different thing.
Run them all before shipping; CI (`.github/workflows/ci.yml`) runs the first three on
every push.

| Layer | Where | What it proves | Needs |
|-------|-------|----------------|-------|
| **1. Matching engine** | `crates/match-engine/src/lib.rs` (unit + property) | clearing optimality, conservation, determinism, per-order bounds | Rust |
| **2. Engine scenarios** | `crates/match-engine/tests/scenarios.rs` | order **sequences** across rounds — the continuous book (rest → later cross → fill) | Rust |
| **3. Round lifecycle** | `offchain/tests/test_round_lifecycle.py` | the **sealed-order lifecycle** — which orders clear now, rest hidden, or expire | Python (stdlib) |
| **3b. Treasury** | `offchain/tests/test_treasury.py` | the **self-funding treasury** — fees cover JAMKB rent first, only surplus is withdrawable profit | Python (stdlib) |
| **4. End-to-end** | `offchain/test_enc_round.py`, `offchain/test_sealed_resting_e2e.py` | the real service on a live node: honest settles, tampered/injected rejected, sealed orders rest & cross across rounds | Docker + node |

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

# Layer 3 — the sealed-order round lifecycle (pure, no node needed)
python3 -m unittest discover -s offchain/tests -v

# Layer 4 — full end-to-end on a live node (requires the committee sidecar + a node)
docker run -d --name jamtest -p 19900:19900 ghcr.io/abutlabs/lasair-node:latest
COMMITTEE_BIN=... LASAIR_RPC=http://127.0.0.1:19900 python3 offchain/test_enc_round.py
docker rm -f jamtest
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
