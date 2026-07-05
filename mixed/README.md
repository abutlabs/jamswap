# Mixed-client JAM network (lasair + PolkaJam)

One shared chain, two independent JAM client implementations, leadership rotating
**across clients**. Driven by [`../docker-compose.mixed.yml`](../docker-compose.mixed.yml):

```sh
docker compose -f docker-compose.mixed.yml up      # one line; multi-arch
```

See the [README section](../README.md#run-it-on-a-mixed-client-chain--lasair-and-polkajam-one-command)
for the walkthrough. This directory holds the plumbing.

## Files

| File | Role |
|---|---|
| `gen-spec.py` | The **shared-genesis generator** (runs as `spec-init`). Decides which client owns each validator index, derives each validator's genesis entry with the **owning client's real keys** (PolkaJam `gen-keys` for its indices; lasair `mixed_keys` for lasair's — bandersnatch from the sealing seed, `peer_id = N(k)` from the QUIC identity), assigns each node a static IP, and runs `polkajam gen-spec`. Writes `spec.json` + `nodes.json` + `pj_<i>.seed` + `ready` to the shared volume. |
| `pj-entrypoint.sh` | PolkaJam image entrypoint. `ROLE=init` → run `gen-spec.py`; `ROLE=validator` → run PolkaJam as validator `INDEX` on the shared spec (`--peer-id <its genesis peer_id> --key-seed-file --finality-mode dummy --bootnode …`). |
| `Dockerfile.polkajam` | The PolkaJam image: fetches the black-box binary from the public release **at build time** (never committed/pushed) and copies lasair's `mixed_keys` from the published `lasair` image so the init step can derive lasair keys. |

The lasair validators run the published multi-arch `ghcr.io/abutlabs/lasair` image
directly (its entrypoint reads `SPEC`/`OWN`/`IDENTITY`/`PEERS` from the compose env).

## Why static IPs

PolkaJam's `gen-spec` requires **numeric** validator addresses, so every node gets a
fixed IP on the `mixnet` compose network (index `i` → `172.28.0.(10+i)`); the same IPs
are baked into the shared genesis by `gen-spec.py`, and lasair dials peers by them.

## Key ownership stays split

No client ever holds another's validator secret. Each validator's genesis entry
carries only the **owning** client's public keys; PolkaJam signs its slots, lasair
signs its own. Both re-execute the whole chain and agree on state.

## Compliance

PolkaJam is used **black-box**: fetched from the public
[`paritytech/polkajam-releases`](https://github.com/paritytech/polkajam-releases) at
image-build time on the user's machine, never committed or pushed to our registry.
See lasair `docs/DISCLOSURES.md` and `docs/MIXED_CLIENT_NETWORK.md`.
