# Jamswap dev modes — public images by default, local lasair source builds on demand.
#
#   make up             # default DEX stack, published lasair image      (:8080)
#   make mixed          # mixed net, EQUAL 3 PolkaJam / 3 lasair (consensus comparison)
#   make mixed-dex      # mixed net, lasair-dominant — the DEX SETTLES TRADES on-chain
#   make local          # build ../lasair from source -> lasair:local -> DEX stack
#   make mixed-local    # source build -> equal-split mixed net
#   make mixed-dex-local# source build -> functional-DEX mixed net
#   make verify         # e2e smoke test against the RUNNING DEX stack (:8080)
#   make verify-mixed   # health check against the RUNNING mixed net
#   make down           # stop whichever stack is up (all compose files)
#
# TWO MIXED MODES (see docker-compose.mixed-dex.yml + docs/mixed_chain_dex_settlement.md):
#   mixed      EQUAL split: both clients author/seal/import apples-to-apples; the
#              Grafana dashboards compare them. With lasair >= 1.7.0 (the default
#              image) trades ALSO SETTLE here — the tier-1 settlement fix:
#              fork-choice-aware guarantor + assure-any-pending + builder fan-out
#              complete availability on the contested chain.
#   mixed-dex  lasair authors a near-linear canonical chain (pj0 co-validates).
#              Historical: was the only settling mode before the tier-1 fix.
#
# Pre-push flow for a lasair change:  make local && make verify
#                                     make mixed-dex-local && make verify   (after ~1 epoch)
# — only then tag client-vX.Y.Z and let CI publish (~80 min).
#
# LASAIR_SRC   path to the (private) lasair checkout      (default ../lasair)
# LASAIR_IMAGE any image ref, overrides both compose files (default: published)

LASAIR_SRC   ?= ../lasair
LOCAL_IMAGE  ?= lasair:local
MIXED        = -f docker-compose.mixed.yml
MIXED_DEX    = -f docker-compose.mixed.yml -f docker-compose.mixed-dex.yml

MONITOR      = -f docker-compose.mixed.yml -f docker-compose.monitor.yml

.PHONY: up down logs mixed mixed-down mixed-dex mixed-dex-down build-local local mixed-local mixed-dex-local verify verify-mixed monitor monitor-down

up:
	docker compose up -d

down:
	docker compose down --remove-orphans
	docker compose $(MIXED) down -v --remove-orphans 2>/dev/null || true

logs:
	docker compose logs -f --tail 50

mixed:
	docker compose $(MIXED) up -d --build

mixed-down:
	docker compose $(MIXED) down -v --remove-orphans

# Functional-DEX mixed net (lasair-dominant): trades actually settle on-chain.
mixed-dex:
	docker compose $(MIXED_DEX) up -d --build

mixed-dex-down:
	docker compose $(MIXED_DEX) down -v --remove-orphans

# Build the lasair image from source (Dockerfile.mesh = the exact image CI publishes).
# Requires the private lasair checkout next to this repo (or LASAIR_SRC=...).
build-local:
	@test -f $(LASAIR_SRC)/Dockerfile.mesh || \
	  { echo "no lasair source at $(LASAIR_SRC) (set LASAIR_SRC=/path/to/lasair)"; exit 1; }
	docker build -f $(LASAIR_SRC)/Dockerfile.mesh -t $(LOCAL_IMAGE) $(LASAIR_SRC)

local: build-local
	LASAIR_IMAGE=$(LOCAL_IMAGE) docker compose up -d --force-recreate

mixed-local: build-local
	LASAIR_IMAGE=$(LOCAL_IMAGE) docker compose $(MIXED) up -d --build --force-recreate

mixed-dex-local: build-local
	LASAIR_IMAGE=$(LOCAL_IMAGE) docker compose $(MIXED_DEX) up -d --build --force-recreate

# Prometheus + Grafana + exporter on TOP of an already-running mixed net.
# Grafana: http://localhost:3000 (no login) — dashboard "JAM mixed network".
# Only the three monitoring services are touched — the net itself (and whatever
# image it runs, published or lasair:local) is left exactly as it is.
monitor:
	docker compose $(MONITOR) up -d --build exporter prometheus grafana canary

monitor-down:
	docker compose $(MONITOR) rm -sf exporter prometheus grafana canary

# E2E smoke test against the RUNNING default stack: register -> handle, duplicate
# work-packages survived, faucet deposit, signed withdraw. Fresh account per run.
verify:
	docker compose exec -T dex python3 /app/verify.py

# Health check against the RUNNING mixed net (give it ~90s of slots first).
verify-mixed:
	bash mixed/verify.sh
