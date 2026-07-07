# Jamswap dev modes — public images by default, local lasair source builds on demand.
#
#   make up             # default DEX stack, published lasair image      (:8080)
#   make mixed          # mixed lasair+PolkaJam net, published image
#   make local          # build ../lasair from source -> lasair:local -> DEX stack
#   make mixed-local    # same source build -> mixed net
#   make verify         # e2e smoke test against the RUNNING DEX stack
#   make verify-mixed   # health check against the RUNNING mixed net
#   make down           # stop whichever stack is up (both compose files)
#
# Pre-push flow for a lasair change:  make local && make verify
#                                     make mixed-local && sleep 90 && make verify-mixed
# — only then tag client-vX.Y.Z and let CI publish (~80 min).
#
# LASAIR_SRC   path to the (private) lasair checkout      (default ../lasair)
# LASAIR_IMAGE any image ref, overrides both compose files (default: published)

LASAIR_SRC   ?= ../lasair
LOCAL_IMAGE  ?= lasair:local
MIXED        = -f docker-compose.mixed.yml

.PHONY: up down logs mixed mixed-down build-local local mixed-local verify verify-mixed

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

# E2E smoke test against the RUNNING default stack: register -> handle, duplicate
# work-packages survived, faucet deposit, signed withdraw. Fresh account per run.
verify:
	docker compose exec -T dex python3 /app/verify.py

# Health check against the RUNNING mixed net (give it ~90s of slots first).
verify-mixed:
	bash mixed/verify.sh
