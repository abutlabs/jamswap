# Jamswap

> An order-book exchange that runs as a JAM service: matching in JAM's Refine phase,
> MEV-resistant batch auctions, sealed orders, and settlement that is **final** once the
> chain finalizes it (no re-org can take a final fill back).

Jamswap is a decentralized exchange built on [JAM](https://jam.web3.foundation). It
trades like a centralized exchange — a live order book and a genuine matching engine —
with no company in the middle. It is an ordinary JAM service, not tied to one client:
the same service runs on lasair and on stock PolkaJam.

Other exchanges match orders on-chain too (Hyperliquid and dYdX each run a chain of their
own for it). What JAM adds is room for **verifiable work on every trade**: jamswap checks
every public order's signature in Refine, can decrypt sealed orders there with proofs of correct
decryption, and could settle a batch with one zero-knowledge proof. A chain that
replicates its order book pays for that work on every validator; on JAM one core's
validators and its auditors pay for it, and jamswap is one service among many rather than
a chain of its own. How it works, and how it compares with other on-chain order books:
[`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md).

**Build JAM together.** jamswap runs on nets of any mix of clients. Bring your client
(check your node with `python3 offchain/jip2_check.py ws://<node>:19800`) or your service
(built for GP 0.8.0 with `tools/jam080`, deployed at runtime over JIP-2):
[`docs/COLLABORATE.md`](docs/COLLABORATE.md).

![Jamswap-demo](./docs/demo.gif)

## Run it

```sh
./dex up           # six lasair validators + the DEX → http://localhost:8081 (~5 min first boot)
```

That starts `lasair6`: six [lasair](https://abutlabs.github.io/jam-learning/lasair/) validators
finalizing under GRANDPA (the draft JAM finality wire protocol), the DEX and the
trading UI. To run the same DEX on six stock PolkaJam validators, with no lasair
anywhere:

```sh
./dex up NET=pj6   # the DEX deploys its service at startup → http://localhost:8201
```

Then in the UI:

1. **Create an account** — an ed25519 keypair your browser holds (exportable).
2. **Fund it** in the Faucet tab (USDC, DOT, JAMKB — three trading pairs).
3. **Place an order** — Limit or Market. Tick **🔒 Seal** to hide its terms until it clears.
4. **Watch it clear** — batch auctions every 6 seconds; fills show **Finalizing → Final**
   as finality catches up (a few blocks).

On pj6 the browser-account flow is not yet verified; the six dev accounts (Alice …
Fergie in the account menu) are funded at startup, and pj6's soaks trade through them.

The rest of the verbs (add `NET=pj6` for pj6):

```sh
./dex status    # finality + market at a glance
./dex load      # start the load generator (soak testing)
./dex logs      # follow the DEX logs
./dex down      # tear down + wipe (fresh genesis next time)
./dex rebuild   # only if you changed the on-chain service (service/src)
```

To watch a net or a soak live in Grafana, start the observability stack once (`./dex obs
up`, then http://localhost:3300); every `./dex up` after that prints its dashboard links.
The stack is its own repository, [abutlabs/observability](https://github.com/abutlabs/observability)
([`monitor/README.md`](monitor/README.md) says how jamswap plugs in).

Both nets are GP 0.8.0 and run on amd64 and arm64. lasair6 pulls the published
`ghcr.io/abutlabs/lasair:2.1.3`; pj6 builds a PolkaJam image from the public release
(`nightly-2026-09-22`, sha256-pinned), fetched on your machine and never committed here.

### How the DEX reaches the chain

The off-chain builder uses one client-neutral interface,
[`offchain/chain.py`](offchain/chain.py), the **JIP-2** node RPC: spec-valid GP 0.8.0
work-packages and reads at the best or finalized block, on pj6 through a PolkaJam node
(with a runtime deploy through the chain's Bootstrap service) and on lasair6 through
lasair's `lasair-reader` (the service seeded into genesis). The service blob is the
same on both, and one scenario run on lasair and on PolkaJam
leaves byte-identical service state ([`docs/DIFFERENTIAL_TESTNET.md`](docs/DIFFERENTIAL_TESTNET.md)).

## What makes it special

- **A real order book on-chain.** Matching is heavy compute; JAM's Refine phase makes
  it affordable, audited, and slashable-if-wrong. No AMM price formula.
- **MEV-resistant by construction.** Orders clear in 6-second batch auctions at one
  fair price — no speed race to front-run.
- **Sealed orders.** Hide price and size until the moment of trade; a sealed order
  that doesn't cross rests hidden and keeps trying — with a zero-loss guarantee
  ([`docs/SEALED_ORDER_ROBUSTNESS.md`](docs/SEALED_ORDER_ROBUSTNESS.md)).
- **Durable settlement.** The DEX calls a fill final only once the chain has finalized
  it, and a finalized block cannot be re-orged. Both DEX nets finalize under GRANDPA;
  lasair6's chaos tests (2026-07: node restarts, two-node kills, quorum recovery) saw
  zero settlement reverts.

## Other ways to run it

| Mode | Command | What it is |
|---|---|---|
| Any JIP-2 node | `CHAIN_BACKEND=jip2 CHAIN_RPC=ws://… CHAIN_SPEC=… python3 offchain/server.py` | The DEX deploys itself on a chain with a Bootstrap service |
| Single-node quickstart | `docker compose up` | One lasair process, UI at `:8080`, no finality — the 60-second demo |
| Cross-client research nets | `./dex up NET=<net>` (`./dex nets` lists them) | lasair, PolkaJam, JavaJAM and pbnjam in one net — consensus research, not the DEX |
| Monitoring | `./dex obs up` | The observability stack (Grafana `:3300`, Prometheus, Loki), its own repo; every net and soak reports into it ([`monitor/README.md`](monitor/README.md)) |
| Soak tests | `soak/run <net> <secs>` | A long run under trading load with a pass/fail report: what it proves, how to reproduce, results ([`soak/README.md`](soak/README.md)) |

Every mode, with local source builds and platform notes:
[`docs/RUNNING.md`](docs/RUNNING.md). Every net, its clients, GP version, finality and
whether the DEX runs there: [`docs/NETS.md`](docs/NETS.md).

## Learn more

| Doc | What's in it |
|-----|--------------|
| [`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md) | The full explainer: why an on-chain order book is new, batch auctions, sealed orders, throughput & JAMKB |
| [`docs/RUNNING.md`](docs/RUNNING.md) | Every run mode: the DEX nets, any JIP-2 node, quickstart, research nets, dev builds, monitoring |
| [`docs/NETS.md`](docs/NETS.md) | Which net is which, and what the clients share on finality |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The full technical build: state machine, wire formats, round lifecycle |
| [`docs/SEALED_ORDERS.md`](docs/SEALED_ORDERS.md) | The three order-hiding rungs — what each protects |
| [`docs/SEALED_ORDER_ROBUSTNESS.md`](docs/SEALED_ORDER_ROBUSTNESS.md) | The zero-loss sealed-order guarantee: failure modes and the redesign |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Honest self-assessment — what's fixed, what carries an asterisk |
| [`docs/THROUGHPUT.md`](docs/THROUGHPUT.md) | Measured throughput & costs per 6-second batch |
| [`docs/JAMKB_IN_PRACTICE.md`](docs/JAMKB_IN_PRACTICE.md) | JAMKB with Jamswap as the live worked example |
| [`docs/DIFFERENTIAL_TESTNET.md`](docs/DIFFERENTIAL_TESTNET.md) | One service, two clients, byte-identical state |
| [`docs/DESIGN_QUESTIONS.md`](docs/DESIGN_QUESTIONS.md) | Open design choices we deliberately haven't locked in |
| [`docs/STATUS.md`](docs/STATUS.md) | Builder's checklist — everything built, everything next |
| [`docs/TESTING.md`](docs/TESTING.md) | The test layers, from matching engine to end-to-end |

## The abutlabs JAM suite

- **lasair** — an independent OCaml JAM client; runs multi-node testnets, finalizes
  under the draft GRANDPA spec, interoperates with PolkaJam on one chain. Its images are
  public (`ghcr.io/abutlabs/lasair`); its source is not yet, and its course,
  [Learning Lasair](https://abutlabs.github.io/jam-learning/lasair/), quotes the code it
  discusses.
- **[jamswap](https://github.com/abutlabs/jamswap)** — this: the order-book DEX.
- **[observability](https://github.com/abutlabs/observability)** — metrics, logs and
  dashboards for JAM networks with any mix of clients; its course,
  [Learning Observability](https://abutlabs.github.io/jam-learning/observability/),
  teaches it on a `lasair6` net you start yourself.

Both courses live on [jam-learning](https://abutlabs.github.io/jam-learning/).
