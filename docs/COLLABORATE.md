# Mixed testnets: bring your client, bring your service

jamswap is an order-book DEX that runs as a JAM service, and a harness that runs it on test
nets of any mix of JAM clients. It is meant to be shared: the more clients it runs on and
the more services run beside it, the more every client team learns about interop before
mainnet does. This page is how to join in; the step-by-step version, with every command
and what it prints, is on jam-learning:
[Build a mixed-testnet JAM service](https://abutlabs.github.io/jam-learning/mixed-testnet/).

## Where it runs today

| Net | Validators | What passes |
|---|---|---|
| `lasair6` | 6 × lasair | A1–A4, 10-minute soak; JIP-3 telemetry from every node |
| `pj6` | 6 × PolkaJam | A1–A4, 10-minute and 1-hour soaks; the service deployed at runtime |
| `lasair-pj` | 3 × lasair + 3 × PolkaJam, one GRANDPA | A1–A4, 10-minute and 1-hour soaks (the hour with `LASAIR_DATA_DIR=/data`); the service state byte-identical on both clients |

A1–A4, the acceptance every net must pass ([`docs/NETS.md`](NETS.md)): **A1** one head;
**A2** the finalized head advances on every node with the same hash; **A3** a public and a
sealed round settle and the service state is byte-identical on every client; **A4**
`soak_verdict.py` exits 0. `./dex up NET=<name>` starts a net, `./dex soak NET=<name> 600`
runs A1–A4 on it, and the [observability stack](https://github.com/abutlabs/observability)
shows it live (`./obs up` first; the course
[Learning Observability](https://abutlabs.github.io/jam-learning/observability/) teaches
reading it).

## Bring your client

The DEX never talks to a client's internals. It reaches the chain only through the
[JIP-2](https://github.com/polkadot-fellows/JIPs) node RPC (`offchain/chain.py`,
`Jip2Chain`), so a node that joins a jamswap net needs:

1. **JAMNP-S**, to be a validator beside the other clients on the net.
2. **A JIP-4 chain spec and a JIP-5 dev key by index**, for example `--chain spec.json
   --dev-validator 3`: every net mints one genesis and hands each validator its own key.
3. **JIP-2 on a WebSocket** (conventionally port 19800) with the methods the DEX and its
   deploy call: `parameters`, `bestBlock`, `finalizedBlock`, `parent`, `stateRoot`,
   `listServices`, `serviceData`, `serviceValue`, `servicePreimage`, `serviceRequest`,
   `workPackageStatus`, `submitWorkPackage`, `submitPreimage`. Optional: `syncState` and
   `statistics` for the dashboards.
4. **A Bootstrap service (id 0)** if the service is to be deployed at runtime (as on
   `pj6`); nets without one seed the service into genesis instead.
5. Optional: **JIP-3 telemetry** (`--telemetry HOST:PORT`), for the Block life and
   work-package dashboards.

Check your node before anything else:

```sh
python3 offchain/jip2_check.py ws://<your-node>:19800
```

It calls every method above: the reads for real, the two submissions with no arguments so
nothing reaches the chain. It prints one line per method and exits 0 when jamswap can run.

Then add your client to a net:

1. An adapter in [`nets/netgen.py`](../nets/netgen.py): a function returning your node's
   compose service (image, command line, ports). `svc_pbnjam` is ten lines. Register the
   client in `CLIENT_ALIASES` ([`nets/genesis.py`](../nets/genesis.py)) and, in
   `nets/netgen.py`, in `SERVICE_PREFIX`, `ADAPTERS` and `OBS_KINDS`.
2. A profile in [`nets/profiles.py`](../nets/profiles.py), for example
   `clients="pj,pj,pj,yours,yours,yours"`, `dex=True`.
3. `./dex gen`, `./dex up NET=<profile>`, `./dex soak NET=<profile> 600`.
4. Open an issue here with what you saw: the soak verdict, the dashboards, the logs.

Other clients are treated as black boxes: we run the published image and read its public
I/O, and a divergence is judged against the Graypaper, JAMNP-S and the JIPs, never against
another client's behaviour. Adapters and profiles already exist for JavaJAM and
pbnjam-node (`pj-javajam`, `pj-pbnjam`), not run since we moved those nets down the list;
teams on those clients are the easiest to welcome back.

## Bring your service

- **Build it for GP 0.8.0.** No public service SDK targets 0.8.0 yet;
  [`tools/jam080`](../tools/jam080/README.md) is the smallest change to the 0.1.28 toolchain
  that does (Apache-2.0), and it builds any service crate, not only this one.
- **Deploy it at runtime** on a net with a Bootstrap service, over JIP-2 only:

  ```sh
  python3 offchain/deploy.py --rpc ws://<node>:19800 --chain-spec <spec.json> \
      --code your-service.jam --no-setup
  ```

  It creates the service through the Bootstrap service, provides its code as a preimage,
  waits until the code is available where refine will look it up, and prints
  `SERVICE_ID=<id>`.
- **Send it work.** `offchain/workpackage.py` builds GP 0.8.0 work-packages and
  `Jip2Chain.submit` sends them with `submitWorkPackage`; jamswap's own round builder
  (`offchain/round.py`) is a worked example.
- **Watch it.** The observability stack shows the chain your service runs on: heads,
  finality and, from nodes that send JIP-3, each block's life. jamswap's own panels come
  from [`observability/gen_dashboards.py`](../observability/gen_dashboards.py), a worked
  example of a service adding dashboards for itself.

## Talk to us

Open an issue in this repository, or ask in the JAM implementers' public channel. jamswap
is a JAM *service*: running it, reading its code or joining a net with it is not reading
another team's implementation, so it sits outside the JAM Prize's clean-room rules (6 and
7), which cover implementation code.

jamswap and this page are checked by running
them: the soaks above, the JIP-2 check, and the unit tests (`python3 -m pytest offchain`).
