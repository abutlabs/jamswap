"""The jamswap test nets: one entry per `./dex up NET=<name>` profile.

A profile is either
  - a hand-written compose file kept as is (`compose`): lasair6, the DEX net, and
    mixed, the original 3:3 lasair + PolkaJam net; or
  - a per-index client layout (`clients`), from which nets/netgen.py generates
    nets/compose/<name>.yml (every node, the genesis minter, ports, keys).

Layout fields:
  clients   one client per validator index (tiny = 6): lasair, pj, pbnjam, javajam
  net       1..99, unique: subnet 10.231.<net>.0/24, JAMNP-S UDP port of validator i
            41000 + 100*net + i, RPC port 42000 + 100*net + i (both on the host too)
  finality  PolkaJam/JavaJAM --finality-mode (grandpa | dummy); lasair nodes run
            LASAIR_FINALITY (grandpa, the jam-np PR #6 draft wire, by default)
  dex       add the DEX (on :8200+net; nets/netgen.py dex_backend). With a lasair node
            in the layout: lasair's builder and reader bridges, the service in genesis.
            Without: the dex on a node's JIP-2 RPC, deploying the service at startup,
            plus a load generator and netwatch over every node (`./dex soak`).
  issue     the jamswap issue the net is for
"""

PROFILES = {
    # ---- hand-written ------------------------------------------------------------
    "lasair6": dict(
        compose="docker-compose.lasair6.yml", project="lasair6",
        clients="lasair,lasair,lasair,lasair,lasair,lasair", issue="#12",
        about="6x lasair, GRANDPA: THE DEX net (sealed orders settle durably)"),
    "mixed": dict(
        compose="docker-compose.mixed.yml", project="jamswap",
        clients="pj,pj,pj,lasair,lasair,lasair", issue="#2",
        about="3 PolkaJam : 3 lasair, dummy finality; consensus research"),
    # ---- generated (nets/compose/<name>.yml) -------------------------------------
    "pj6": dict(
        clients="pj,pj,pj,pj,pj,pj", net=1, finality="grandpa", dex=True, issue="#17",
        about="6x PolkaJam, GRANDPA + the DEX on JIP-2: no lasair node at all"),
    "pj-pbnjam": dict(
        clients="pj,pj,pj,pj,pj,pbnjam", net=2, finality="grandpa", issue="#19",
        about="5 PolkaJam : 1 pbnjam; PolkaJam's 5-of-6 GRANDPA quorum alone"),
    "pj-pbnjam-42": dict(
        clients="pj,pj,pj,pj,pbnjam,pbnjam", net=3, finality="grandpa", issue="#19",
        about="4 PolkaJam : 2 pbnjam"),
    "pj-javajam": dict(
        clients="pj,pj,pj,javajam,javajam,javajam", net=4, finality="grandpa", issue="#18",
        about="3 PolkaJam : 3 JavaJAM, both GRANDPA (the PR #6 draft between them)"),
    "pj-javajam-42": dict(
        clients="pj,pj,pj,pj,javajam,javajam", net=5, finality="grandpa", issue="#18",
        about="4 PolkaJam : 2 JavaJAM, both GRANDPA"),
    "lasair-pj-javajam": dict(
        clients="lasair,lasair,pj,pj,javajam,javajam", net=6, finality="grandpa",
        dex=True, issue="#20",
        about="2 lasair : 2 PolkaJam : 2 JavaJAM + the DEX"),
    "nolasair": dict(
        clients="pj,pj,javajam,javajam,pbnjam,pbnjam", net=7, finality="grandpa",
        issue="#21", about="2 PolkaJam : 2 JavaJAM : 2 pbnjam, the control with no lasair"),
}

DEFAULT = "lasair6"

# ---- per-client images (pinned) -------------------------------------------------
PJ_RELEASE = "nightly-2026-09-22"            # mixed/Dockerfile.polkajam pins its sha256
LASAIR_IMAGE = "ghcr.io/abutlabs/lasair:2.0.0"
# docker.io/shimonchick/pbnjam-node:main-54226be (2026-09-25; linux/amd64 + arm64)
PBNJAM_IMAGE = ("docker.io/shimonchick/pbnjam-node:main-54226be"
                "@sha256:ceb5f651164d754280c4aa88f0cf3203f0f6115cb30eb1a3dd72788224aba1aa")
# JavaJAM publishes one single-arch image per tag; ./dex picks the host's arch
JAVAJAM_IMAGES = {
    "amd64": ("ghcr.io/methodfive/javajam:0.4.3"
              "@sha256:573c030b793208af25c0212f9f44e71e99322b70cc1ecc26281b9441cf55293f"),
    "arm64": ("ghcr.io/methodfive/javajam:0.4.3-arm64"
              "@sha256:ab8d65b58fdaab8688da9f58295e92e33753b84fc95c9234805fa1af4b5180cc"),
}
