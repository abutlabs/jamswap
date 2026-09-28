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
| `Dockerfile.polkajam` | The PolkaJam image: fetches the black-box binary from the public release **at build time** (never committed/pushed; sha256-pinned per release and arch). Target `polkajam` is PolkaJam + the genesis minter; the default target `with-lasair` adds the lasair binary from the published `lasair` image (key cross-check + `--inject-service-spec`). |
| `pj-entrypoint.sh` | PolkaJam image entrypoint. `ROLE=init` → run `nets/genesis.py`; `ROLE=validator` → run PolkaJam as validator `INDEX` on the shared spec (`--peer-id <its genesis peer_id> --key-seed-file pj_<i>.seed --finality-mode $FINALITY_MODE --bootnode …`, plus `--external-ip $EXTERNAL_IP` when set). |
| `verify.sh` | Health check for a running `docker-compose.mixed.yml` (`make verify-mixed`). |

The shared-genesis generator moved to [`../nets/genesis.py`](../nets/genesis.py) (it was
`gen-spec.py`); every net, including this one, mints its genesis there. See
[`docs/NETS.md`](../docs/NETS.md) for the per-index client layouts it serves.

The lasair validators run the published multi-arch `ghcr.io/abutlabs/lasair` image
directly (its entrypoint reads `SPEC`/`OWN`/`IDENTITY`/`PEERS` from the compose env).

## Why static IPs

PolkaJam's `gen-spec` requires **numeric** validator addresses, so every node gets a
fixed IP on the `mixnet` compose network (index `i` → `172.28.0.(10+i)`); the same IPs
are baked into the shared genesis by `nets/genesis.py`, and lasair dials peers by them.

## Keys

Every validator is the standard JAM dev account of its index (JIP-5; `nets/devkeys.py`),
and each client holds only its own: PolkaJam loads dev seed `i`, lasair runs
`DEV_VALIDATOR=i` (`--dev-validator i`, lasair ≥ 2.1.0). `LASAIR_DEV_ALL_KEYS=1 ./dex up
NET=mixed` puts the lasair nodes in lasair's devnet mode instead (`--dev-all-keys`: every
dev secret, guarantees as any lasair index `GUARANTOR_OWN=3,4,5`); see `docs/NETS.md`,
"Keys per client".

## Compliance

PolkaJam is used **black-box**: fetched from the public
[`paritytech/polkajam-releases`](https://github.com/paritytech/polkajam-releases) at
image-build time on the user's machine, never committed or pushed to our registry.
See lasair `docs/DISCLOSURES.md` and `docs/MIXED_CLIENT_NETWORK.md`.
