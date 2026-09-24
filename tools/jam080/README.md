# jam080 — build JAM services for Graypaper 0.8.0

No public service SDK targets GP 0.8.0 yet. The published `jam-pvm-build` /
`jam-pvm-common` 0.1.28 target GP 0.7.2: they link with polkavm-linker 0.30,
whose `JamV1` ISA still has `sbrk`, and they use the 0.7.2 host-call ids. A
0.7.2-built `.jam` blob does not run on a 0.8.0 node. This directory is the
smallest change to that toolchain that makes a GP 0.8.0 blob. Retire it when
Parity publishes a 0.8.0 SDK.

| Directory | What it is |
|---|---|
| `builder/` | `jam080-build`: the `jam-pvm-builder` 0.1.28 pipeline (same nightly toolchain, rustflags, `-Z build-std`) linked with polkavm-linker **0.36.0** (crates.io, pinned), whose `JamV1` is the 0.8.0 ISA (no `sbrk`, `unlikely` = opcode 2). Writes a `ProgramBlob` `.jam` and prints its blake2b-256 code hash. |
| `jam-pvm-common/` | crates.io `jam-pvm-common` 0.1.28, patched (below). |
| `jam-types/` | crates.io `jam-types` 0.1.28, patched (below). |

## Changes to the upstream crates

Both crates are Apache-2.0 (Parity Technologies, `paritytech/polkajam`
repository), taken from crates.io as published source. The license text is in
`LICENSE-APACHE`. Each modified source file starts with a `MODIFIED` notice.

- `jam-pvm-common/src/imports.rs`: GP 0.8.0 host-call table. `grow_heap` takes
  index 1, and every later host call moves up by one (0.8.0 appendix B).
- `jam-pvm-common/src/mem.rs`: a bump allocator over `grow_heap` replaces
  picoalloc. picoalloc emits `sbrk`, which the 0.8.0 ISA does not have.
- `jam-pvm-common/Cargo.toml`: `polkavm-derive` 0.36; `jam-types` as a path
  dependency.
- `jam-types/src/simple.rs`: `ProtocolParameters` decodes the 0.8.0 `fetch(0)`
  layout. N, V, W_E and W_P are no longer protocol parameters, so those fields
  are `#[codec(skip)]`, and `validate` / `apply` ignore them when they are zero.
- `jam-types/Cargo.toml`: version `0.1.28+gp080`.

## Use

`./dex rebuild` builds the jamswap service with it. For any other service crate:

```sh
rustup toolchain install nightly-2025-05-10 --component rust-src   # once
cargo build --release --manifest-path tools/jam080/builder/Cargo.toml
# in the service crate's Cargo.toml:
#   jam-pvm-common = { path = "<jamswap>/tools/jam080/jam-pvm-common", default-features = false, features = ["service"] }
#   polkavm-derive = "0.36"
tools/jam080/builder/target/release/jam080-build <service-crate-dir> --out <dir>
```

## Reproducibility

The same sources in the same place build the same bytes, and the blob embeds no
file paths. But cargo hashes a path dependency into symbol names by its **absolute**
path when it sits outside the workspace being built, and that reaches the code
layout: the same service built from two checkout paths gave two code hashes. So
put the service, its path dependencies and this SDK in one cargo workspace, and
commit its `Cargo.lock`. jamswap's root `Cargo.toml` does exactly that. `./dex
rebuild` then reproduces `service/jamswap-service.jam` byte for byte from any
clone. This was checked from three checkout paths.

## Verification

- `service/jamswap-service.jam` (`./dex rebuild`) runs the DEX on lasair 2.x
  (GP 0.8.0).
- lasair's `test/fixtures/lasair-demo-service.jam` and `zk-jam-service.jam` are
  built with this toolchain. lasair's service suites run them on its 0.8.0 PVM and
  host calls: deploy lifecycle, refine, tally, service consensus, live guarantee,
  duplicate package, seed service, JAMNP-S.
- polkavm-linker 0.36.0 from crates.io and polkavm's `v0.36.0` source tree link
  byte-identical blobs.
- `measure-gas.sh <lasair-checkout>` rebuilds zk-jam-service's gas spikes (ed25519,
  blake2s, vdec, Groth16, zk-FBA) with this toolchain and measures them on lasair's
  0.8.0 PVM. lasair's `scripts/pvm-refine-differential.sh` shows the polkavm
  interpreter reaching the same gas on each spike.
