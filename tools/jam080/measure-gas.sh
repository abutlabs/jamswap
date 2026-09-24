#!/usr/bin/env bash
# Re-measure the refine gas of the crypto that jamswap's throughput numbers rest on,
# under the GP 0.8.0 gas model.
#
#   tools/jam080/measure-gas.sh <lasair-checkout>
#
# Sources: zk-jam-service's gas spikes (github.com/abutlabs/zk-jam-service @ ZK_REF:
# spikes/crypto-gas, vdec-gas, groth16-gas, fba-zk), rebuilt with this toolchain. Only
# each service's two SDK dependency lines change. Rig: lasair's bin/demo_deploy.exe,
# which deploys, refines and accumulates in-process on lasair's 0.8.0 PVM, gas model
# and host calls. The spikes' own bench.py drove the HTTP lasair-node, which is retired
# and runs GP 0.7.2. Refine budget: 1e9 (tiny G_R).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
LASAIR=$(cd "${1:?usage: measure-gas.sh <lasair-checkout>}" && pwd)
ZK_REPO=${ZK_REPO:-https://github.com/abutlabs/zk-jam-service}
ZK_REF=${ZK_REF:-2141ed1}
# the clone + six release builds are ~1.3 GB: a temp work dir is removed on exit;
# WORK=<dir> keeps it (and reuses it on the next run)
if [ -z "${WORK:-}" ]; then WORK=$(mktemp -d /tmp/jam080-gas.XXXXXX); trap 'rm -rf "$WORK"' EXIT; fi
mkdir -p "$WORK/jam"

cargo build --release -q --manifest-path "$HERE/builder/Cargo.toml"
BUILD=$HERE/builder/target/release/jam080-build
( cd "$LASAIR" && { eval "$(opam env 2>/dev/null)" || true; } && dune build bin/demo_deploy.exe )
DEMO=$LASAIR/_build/default/bin/demo_deploy.exe

ZK=$WORK/zk-jam-service
[ -d "$ZK/.git" ] || git clone -q "$ZK_REPO" "$ZK"
git -C "$ZK" cat-file -e "$ZK_REF^{commit}" 2>/dev/null || git -C "$ZK" fetch -q origin
git -C "$ZK" checkout -q --force "$ZK_REF"
S=$ZK/spikes

# standalone <crate-dir>: its own cargo workspace (the clone sits inside jamswap's)
standalone() { grep -q '^\[workspace\]' "$1/Cargo.toml" || printf '\n[workspace]\n' >> "$1/Cargo.toml"; }

# port <service-dir>: build it against the 0.8.0 SDK; prints the .jam path
port() {
  sed -i.orig \
    -e "s|^jam-pvm-common = .*|jam-pvm-common = { path = \"$HERE/jam-pvm-common\", default-features = false, features = [\"service\"] }|" \
    -e 's|^polkavm-derive = .*|polkavm-derive = "0.36"|' "$1/Cargo.toml"
  standalone "$1"
  "$BUILD" "$1" --out "$WORK/jam" | awk '{print $1}'
}

# refine <jam> <payload-hex> -> "<refine gas> <output hex>"
refine() {
  ( cd "$LASAIR" && LASAIR_REFINE_GAS=1000000000 "$DEMO" "$1" "$2" ) | awk '
    /^\[3\] REFINE/ { r = 1 }
    r && /output/   { out = $3 }
    r && /gas used/ { print $4, out; exit }'
}

le32() { printf '%02x%02x%02x%02x' $(($1 & 255)) $(($1 >> 8 & 255)) $(($1 >> 16 & 255)) $(($1 >> 24 & 255)); }
fail() { echo "measure-gas: $*" >&2; exit 1; }
G_R=5000000000
row() { printf '| %s | %s | %s |\n' "$1" "$(printf "%'d" "$2")" "$(printf "%'d" $((G_R / $2)))"; }

echo "| Operation | gas | per full refine (G_R 5e9) |"
echo "|---|---|---|"

# ed25519-compact verify and blake2s-256 (crypto-gas): per_op = (g(n2) - g(n1)) / (n2 - n1)
CRYPTO=$(port "$S/crypto-gas/crypto-service")
read -r g1 o1 < <(refine "$CRYPTO" "00$(le32 10)")
read -r g2 o2 < <(refine "$CRYPTO" "00$(le32 110)")
[ "$o1" = 0a ] && [ "$o2" = 6e ] || fail "ed25519: not every verify succeeded ($o1, $o2)"
row "ed25519-compact verify (one signed order)" $(( (g2 - g1) / 100 ))
read -r g1 _ < <(refine "$CRYPTO" "01$(le32 100)")
read -r g2 _ < <(refine "$CRYPTO" "01$(le32 1100)")
row "blake2s-256, 64-byte message" $(( (g2 - g1) / 1000 ))

# vdec (vdec-gas): one sealed order decrypted by an n-member committee, n = 1..5
VDEC=$(port "$S/vdec-gas/vdec-service")
standalone "$S/vdec-gas/committee"
( cd "$S/vdec-gas/committee" && cargo build --release -q )
GEN=$S/vdec-gas/committee/target/release/gen-round
for n in 1 2 3 5; do
  honest=$("$GEN" $n | awk '$1 == "honest" {print $2}')
  tampered=$("$GEN" $n | awk '$1 == "tampered" {print $2}')
  read -r g out < <(refine "$VDEC" "$honest")
  read -r _ outt < <(refine "$VDEC" "$tampered")
  [ "${out:0:2}" = 01 ] && [ ${#out} -eq 36 ] || fail "vdec n=$n: honest round did not decrypt ($out)"
  [ "$outt" = 00 ] || fail "vdec n=$n: tampered round was not rejected ($outt)"
  eval "vdec_$n=$g"
  row "vdec: decrypt one sealed order, committee n=$n" "$g"
done
row "vdec: each extra committee member (marginal, n=1..5)" $(( (vdec_5 - vdec_1) / 4 ))

# Groth16/BN254 verify of an untrusted proof, whole refine (groth16-gas)
G16=$(port "$S/groth16-gas/groth16-service")
read -r g out < <(refine "$G16" 00)
[ "$out" = 01 ] || fail "groth16: proof did not verify ($out)"
row "Groth16/BN254 verify, 1 public input (whole refine)" "$g"

# zk FBA clearing proof: one Groth16 verify settles a whole batch (fba-zk)
FBA=$(port "$S/fba-zk/fba-service")
standalone "$S/fba-zk/circuit"
( cd "$S/fba-zk/circuit" && cargo run --release -q >/dev/null )
ART=$S/fba-zk/circuit/artifacts
cmp -s "$ART/vk.bin" "$S/fba-zk/fba-service/vk.bin" || fail "fba-zk: regenerated vk differs from the committed one"
read -r g out < <(refine "$FBA" "$(xxd -p "$ART/submission.bin" | tr -d '\n')")
read -r _ outb < <(refine "$FBA" "$(xxd -p "$ART/submission_bad.bin" | tr -d '\n')")
[ "${out:0:2}" = 01 ] || fail "fba-zk: honest clearing proof did not verify ($out)"
[ "${outb:0:2}" != 01 ] || fail "fba-zk: a lied settlement was accepted ($outb)"
row "zk FBA clearing proof, whole batch (whole refine)" "$g"
