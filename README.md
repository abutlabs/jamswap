# Jamswap

> A real order-book exchange, running **trustlessly on a blockchain** — matching in
> JAM's Refine phase, MEV-resistant batch auctions, sealed orders, and settlement
> that is **final** (β-finalized: once your trade lands, no re-org can take it back).

Jamswap is a decentralized exchange built on [JAM](https://jam.web3.foundation). It
trades like a centralized exchange — a live order book and a genuine matching engine —
with no company in the middle. Why that's new, and how it works:
[`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md).

![Jamswap-demo](./docs/demo.gif)

## Run it

One command brings up the full network: **six lasair validators finalizing under
GRANDPA** (the draft JAM finality wire protocol), the DEX, and the trading UI.

```sh
./dex up        # ~5 min first boot (image pull + Safrole warmup) → http://localhost:8081
```

Then at `http://localhost:8081`:

1. **Create an account** — an ed25519 keypair your browser holds (exportable).
2. **Fund it** in the Faucet tab (USDC, DOT, JAMKB — three trading pairs).
3. **Place an order** — Limit or Market. Tick **🔒 Seal** to hide its terms until it clears.
4. **Watch it clear** — batch auctions every 6 seconds; fills show **Finalizing → Final**
   as β-finality catches up (~2–4 blocks).

The rest of the verbs:

```sh
./dex status    # finality + market at a glance
./dex load      # start the sealed-order load generator (soak testing)
./dex logs      # follow the DEX logs
./dex down      # tear down + wipe (fresh genesis next time)
./dex rebuild   # only if you changed the on-chain service (service/src/lib.rs)
```

Everything runs from the published image (`ghcr.io/abutlabs/lasair`, currently
arm64 — Apple Silicon native; amd64 riders: `LASAIR_IMAGE=ghcr.io/abutlabs/lasair:1.8.0
LASAIR_FINALITY=ce192 ./dex up` until the next multi-arch build). All networking is
spec **JAMNP-S over QUIC** — orders in as CE-133 work-packages, state out over CE-129,
finality votes on the draft GRANDPA streams (CE 149–153). No client RPC anywhere.

## What makes it special

- **A real order book on-chain.** Matching is heavy compute; JAM's Refine phase makes
  it affordable, audited, and slashable-if-wrong. No AMM price formula.
- **MEV-resistant by construction.** Orders clear in 6-second batch auctions at one
  fair price — no speed race to front-run.
- **Sealed orders.** Hide price and size until the moment of trade; a sealed order
  that doesn't cross rests hidden and keeps trying — with a zero-loss guarantee
  ([`docs/SEALED_ORDER_ROBUSTNESS.md`](docs/SEALED_ORDER_ROBUSTNESS.md)).
- **Durable settlement.** The net finalizes with a GRANDPA gadget speaking the
  [draft JAM finality wire spec](https://github.com/zdave-parity/jam-np/pull/6);
  a finalized fill can never be re-orged away (`SETTLE_HOLD_SECS=0`, zero reverts
  under chaos testing — node restarts, 2-node kills, quorum recovery).

## Other ways to run it

| Mode | Command | What it is |
|---|---|---|
| **The DEX net** (above) | `./dex up` | Six lasair validators, GRANDPA finality, durable settlement — **use this** |
| Single-node quickstart | `docker compose up` | One process, UI at `:8080`, no finality — the 60-second demo |
| Mixed-client research | `docker compose -f docker-compose.mixed.yml up` | lasair + PolkaJam co-authoring one chain — consensus research, not the DEX |
| Monitoring | `make monitor` | Prometheus + Grafana on `:3010` |

Full instructions for every mode, local source builds, and platform notes:
[`docs/RUNNING.md`](docs/RUNNING.md). Which compose file is which net (and the
cross-client finality story): [`docs/NETS.md`](docs/NETS.md).

## Learn more

| Doc | What's in it |
|-----|--------------|
| [`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md) | The full explainer: why an on-chain order book is new, batch auctions, sealed orders, throughput & JAMKB |
| [`docs/RUNNING.md`](docs/RUNNING.md) | Every run mode: quickstart, mixed-client nets, dev builds, monitoring |
| [`docs/NETS.md`](docs/NETS.md) | Which net is which + the lasair/PolkaJam finality story |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The full technical build: state machine, wire formats, round lifecycle |
| [`docs/SEALED_ORDERS.md`](docs/SEALED_ORDERS.md) | The three order-hiding rungs — what each protects |
| [`docs/SEALED_ORDER_ROBUSTNESS.md`](docs/SEALED_ORDER_ROBUSTNESS.md) | The zero-loss sealed-order guarantee: failure modes and the redesign |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Honest self-assessment — what's fixed, what carries an asterisk |
| [`docs/THROUGHPUT.md`](docs/THROUGHPUT.md) | Measured throughput & costs per 6-second batch |
| [`docs/JAMKB_IN_PRACTICE.md`](docs/JAMKB_IN_PRACTICE.md) | JAMKB with Jamswap as the live worked example |
| [`docs/DESIGN_QUESTIONS.md`](docs/DESIGN_QUESTIONS.md) | Open design choices we deliberately haven't locked in |
| [`docs/STATUS.md`](docs/STATUS.md) | Builder's checklist — everything built, everything next |
| [`docs/TESTING.md`](docs/TESTING.md) | The test layers, from matching engine to end-to-end |

## The abutlabs JAM suite

- **[lasair](https://github.com/abutlabs/lasair)** — an independent OCaml JAM client;
  runs multi-node testnets, finalizes under the draft GRANDPA spec, interoperates
  with PolkaJam on one chain.
- **[zk-jam-service](https://github.com/abutlabs/zk-jam-service)** — anonymous,
  sybil-resistant voting; a real zero-knowledge proof verified in Refine.
- **[jamswap](https://github.com/abutlabs/jamswap)** — this: the order-book DEX.
