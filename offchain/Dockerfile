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
COPY --from=build /committee /usr/local/bin/committee
COPY offchain/ /app/
WORKDIR /app
# COMMITTEE_BIN present => the server runs in encrypt-until-batch mode (option 2).
ENV COMMITTEE_BIN=/usr/local/bin/committee \
    PYTHONUNBUFFERED=1
CMD ["python3", "server.py"]
