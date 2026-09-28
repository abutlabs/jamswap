# Soak tests

A soak test runs a real JAM network for a long time under steady trading load, then checks
that nothing was lost and the chain stayed healthy. It is how we find the problems that
only appear after 20 minutes, not the ones a unit test catches.

This folder is the soak module: one command to run a soak, a report for every run, and
the results so far.

- [How a soak works](#how-a-soak-works)
- [What it checks](#what-it-checks)
- [The nets](#the-nets)
- [Run one](#run-one)
- [Watch it live](#watch-it-live)
- [Read the report](#read-the-report)
- [Results](#results)

## How a soak works

```
 load generator ──orders──▶ DEX (off-chain matching, rounds of up to 48 orders)
                                 │  a round = one work-package
                                 ▼
                          JAM network: 6 validators (lasair and/or PolkaJam)
                            guarantee → assure → audit → accumulate → finalize
                                 │
                                 ▼
                     the DEX service's on-chain state (orders settled)
```

1. **The net comes up** (`./dex up`): six validators on one genesis, with the DEX service
   in it, and the DEX's off-chain server.
2. **Load for SECS seconds.** The load generator places signed buy and sell orders from
   the six dev accounts: 12 crossing pairs a minute (1,440 orders an hour), 20% of them
   *sealed* (hidden until the round reveals them).
3. **The DEX batches orders into rounds.** Each round is a work-package the validators
   guarantee, make available, audit and accumulate. Settlement is final once the block
   is finalized.
4. **Drain.** 180 s with no new load, so orders in flight can finish.
5. **Verdict.** The soak checks every order's fate and the chain on every node.

## What it checks

| Check | Pass when | What it tells you |
|---|---|---|
| offered load | ≤ 0.01% of orders turned away | The DEX accepted the traffic. A refusal means it couldn't keep up (e.g. an account hit its open-order cap). |
| clearing SLO | ≥ 0.9999 | Of the orders that could trade, the share that did. The DEX's main promise. |
| SEALED zero-loss | 0 stuck | Every hidden order reached an end state; none lost between commit and reveal. |
| one head | every sample | All nodes agree on the chain head, sampled every few seconds. |
| liveness | blocks advance | Every node kept producing or importing blocks. |
| finality | 0 conflicts, stall < 1 epoch | Blocks were finalized, never two at one height, never going backwards. |
| authoring | every validator | Every validator produced blocks. |
| state parity | all digests agree | At one finalized block every node, whatever its client, holds byte-identical DEX state. |
| clear latency | (information) | Time from placing an order to its settlement, p50 and p99. |

A run passes only if **all** checks pass.

## The nets

| Net | Validators | What it tests |
|---|---|---|
| `lasair6` | 6 lasair | lasair alone runs the whole chain: authoring, finality, guaranteeing every DEX round. The `./dex up` default. |
| `lasair-pj` | 3 lasair + 3 PolkaJam 0.1.29 | lasair and PolkaJam on one chain, each validator holding only its own key: the mixed-client case. |
| `pj6` | 6 PolkaJam | The control: the DEX with no lasair node at all. |

`./dex nets` lists every net; `docs/NETS.md` has the details.

## Run one

Needs Docker with a few GB of disk free, and this repo. The first run pulls the images.

```
soak/run lasair-pj 600        # 10 minutes; use 3600 for the hour-long check
```

`soak/run` starts the obs stack if needed, brings the net up, records the configuration,
runs the soak, tears the net down and writes the report. Settings pass through as
environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `LASAIR_IMAGE` | the pin in `nets/profiles.py` | which lasair build runs |
| `LASAIR_DATA_DIR` | unset (memory only) | `/data`: durable storage, as a real node runs |
| `RATE` / `PROFILE` / `SEALED_RATIO` | 12 / trading / 0.2 | the load |
| `KEEP_UP` | 0 | 1 leaves the net running afterwards |

The step-by-step equivalent: `./dex up NET=<net>`, `./dex soak NET=<net> <secs>`,
`./dex down NET=<net>`.

## Watch it live

`./dex up` prints a Grafana link (the obs stack, `monitor/README.md`). Four dashboards,
switched by the buttons at the top:

- **Chain health:** one head, finality lag per node.
- **lasair validator duties:** blocks authored, co-signing, guarantees, assurances,
  audits, refine time, packages expired.
- **DEX:** offered, placed and refused orders, clearing SLO and latency, round sizes.
- **Memory:** RSS and OCaml heap per node.

Every pass/fail tile states its threshold, and the soak's start, drain and verdicts are
marked on the graphs.

## Read the report

Each run writes `REPORT.md` into its folder, `~/.cache/jamswap/soak/<net>-<time>/`:

1. **What was tested:** the net, validators, lasair image, storage, load and duration.
2. **Results:** every check with its threshold, the result and what it means.
3. **Why orders were refused**, if any, from the load generator's log.
4. **Dashboard links** for the run.
5. **Reproduce:** the exact command.

The same folder keeps the raw evidence: `verdict.txt`, `DONE`, `loadgen.log`, `dex.log`,
`chain.jsonl` and `parity.json`. To write a report for an older run:
`python3 soak/report.py ~/.cache/jamswap/soak/<run>`.

## Results

- [2026-09-28](reports/2026-09-28.md): the mixed lasair + PolkaJam net passes an hour on
  lasair 2.1.2; the all-lasair net fails after about 25 minutes, when big rounds take
  lasair too long to refine.
