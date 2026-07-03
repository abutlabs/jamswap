# Off-chain server image WITH the encrypt-until-batch committee sidecar built in.
# Build context is the repo root (so crates/ is available):
#   docker build -f offchain/Dockerfile -t jamswap-dex .
# Stage 1 compiles the committee binary (Rust); stage 2 runs the stdlib-only Python server.
FROM rust:1-slim AS build
WORKDIR /src
ENV CARGO_TARGET_DIR=/target
# committee path-deps vdec + match-engine, so copy all three crates
COPY crates/ ./crates/
RUN cargo build --release --manifest-path crates/committee/Cargo.toml \
 && cp /target/release/committee /committee

FROM python:3-slim
# PyNaCl: server-side governance signing for the (opt-in) beneficiary profit sweep, and
# the order-signature PREFLIGHT. Signatures are enforced ON-CHAIN regardless (public
# orders verified per-order in refine; sealed commits owner-signed) — the preflight just
# turns a would-be-rejected round into an instant API error, so it defaults ON.
RUN pip install --no-cache-dir pynacl
COPY --from=build /committee /usr/local/bin/committee
COPY offchain/ /app/
WORKDIR /app
# The committee binary ships in the image but the DEFAULT sealing mode is commit–reveal
# (rung 3, the permissionless base state — no committee, no extra operators). Set
# ENC_MODE=1 to opt in to encrypt-until-batch (rung 2, simulated committee — see
# docs/COMMITTEE_DEPLOYMENT.md for what a real deployment needs).
# BENEFICIARY_SWEEP=1 lets the OWNER sweep profit over the API (prototype: server holds
# the demo gov key) — leave off unless operator == owner. See docs/REVENUE.md.
ENV COMMITTEE_BIN=/usr/local/bin/committee \
    REQUIRE_ORDER_SIG=1 \
    PYTHONUNBUFFERED=1
CMD ["python3", "server.py"]
