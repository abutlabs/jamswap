# Watching runs live: the obs stack

One Prometheus and one Grafana that stay up across runs. Every jamswap net and every
lasair test script that opts in reports into them, so a soak can be watched in the
browser while it runs, and looked at again afterwards (30 days, capped at 5 GB).

```sh
monitor/obs/obs up        # or ./dex obs up
open http://localhost:3300
```

Grafana needs no login to look (anonymous Viewer). The CLI writes annotations as
`admin` (password `obs`, or `OBS_GRAFANA_PASSWORD` set before the first `up`). Both
ports listen on 127.0.0.1 only: Grafana `:3300`, Prometheus `:9390`.

## How a run gets in

- **jamswap nets.** `./dex up NET=<net>` registers the net's metrics endpoints under a
  fresh run id (`<net>-<UTC time>`), annotates the start and prints the dashboard link.
  `./dex soak` annotates the soak's start, load off, drain, every PASS/FAIL verdict line
  with its threshold, and the result as a region over the soak. `./dex load`, `noload`
  and `down` annotate too, and `./dex link NET=<net>` prints the links again. If obs is
  not running, `up` says so in one line and the net runs as before.
- **lasair scripts.** `OBS=<jamswap>/monitor/obs/obs scripts/keystore-net.sh` (also
  `memory-soak.sh`, `rehearsal-net.sh`) registers the native nodes' metrics ports as
  `host.docker.internal:<port>` and annotates the start and the verdict
  (lasair `scripts/obs-hook.sh`). Without `OBS` nothing changes.
- **Anything else.** `obs register <net> <run_id> <job> [node@]host:port... --label client=...`,
  then `obs annotate <run_id> "text" [--tags pass]` and `obs link <run_id>`. For a Docker
  net pass `--project <compose project>` (or `--network`): Prometheus joins that network
  and scrapes containers by name. `obs unregister <net>` ends the run. `obs -h` has the rest.

Every series carries `net`, `run_id`, `node`, `client` (lasair, polkajam, dex, loadgen,
netwatch) and `job`. What is scraped on a jamswap net: every lasair node (`:9615`), the
dex (`:8080`), the load generator (`:9111`), netwatch (`:9106`) and lasair's builder
(`:19980`). PolkaJam exports no Prometheus metrics (its only telemetry option is a JIP-3
push endpoint), so its nodes appear through netwatch, which reads every node's JIP-2
RPC and exports one series per node.

## The dashboards

Pick the net and the run at the top; the link a run prints sets both, and its time
range. Blue marks are run events, green and red marks are PASS and FAIL verdicts. Each
dashboard opens with what it answers; each stat's title says what passes.

| Dashboard | Answers |
|---|---|
| **Chain health** | Is the net one chain that keeps growing and finalizing on every node? Best and finalized block per node, finality lag, head agreement, peers. |
| **lasair validator duties** | Is every lasair node doing its whole job? Authoring, CE-134 co-signing and refusals, CE-133 guarantees, CE-135 inclusion, assurances, audits, the guarantor pipeline, refine time. |
| **DEX** | Is the DEX turning offered load into cleared trades? Offered and turned-away load by op, placed and terminal orders, the clearing SLO, latency, round sizes, refusals, the treasury reserve. |
| **Memory** | Does lasair's memory stay bounded as the chain grows? RSS, OCaml heap, live and top heap, tree entries, GRANDPA stores, compactions, and RSS per finalized block (the memory soak's verdict). |

The dashboards are generated: edit `monitor/obs/gen_dashboards.py`, run it, and Grafana
picks the JSON up within 10 s.

## Files

`monitor/obs/`: `obs` (the CLI), `docker-compose.yml` (compose project `obs`),
`prometheus.yml` (one file_sd job), `grafana/provisioning/`, `dashboards/`,
`gen_dashboards.py`. State (targets, registered nets, runs) is in `~/.cache/jamswap/obs`
(`OBS_STATE`). `obs down` keeps the data; `obs down --wipe` deletes it.

The older overlays (`docker-compose.monitor.yml` on the mixed net,
`docker-compose.lasair6-monitor.yml`; Grafana `:3010`) and the dashboards in
`monitor/grafana/` still work for those two nets. obs replaces them for everything new.
