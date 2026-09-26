"""The DEX's starting state, made on chain with ordinary work-items.

A service deployed at runtime (deploy.py) starts with empty storage. setup() gives it what
mixed/gen-spec.py writes into genesis on lasair nets, with the work-items any client
submits, so it works on any chain and any backend:

  LIST      the default markets                    [6][market][base][quote]
  REGISTER  the six standard JAM dev accounts      [7][pubkey][signature over canon(register, pubkey)]
            (Alice..Fergie, docs.jamcha.in dev accounts), in that order
  DEPOSIT   `balance` of every asset to each       [1][account][asset][amount][nonce]

The service treats each one idempotently: listing a listed market and registering a
registered key change nothing, and a deposit is credited once per (account, nonce)
(crates/match-engine/src/deposit.rs, jamswap#7). So setup() submits whatever has not
landed, resubmits what has not landed after a while (or whose package failed), and a
re-run changes nothing. A deposit has the fixed nonce asset + 1, so no re-run funds an
account twice; it counts as landed once the service's deposit record for the account
(b"dn" ++ account: a floor and a window of recent nonces) holds it.

On the jip2 backend, every group goes in one work-package with one work-item per op,
accumulated in order: the six registrations in one package get handles 1..6 on a fresh
service, as gen-spec.py assigns them. A backend that sends one payload per package
(jamnp) registers them in whatever order they land.
"""
import os, struct, sys, time

import chain as chainmod

USDC, DOT, JAMKB = 0, 1, 2
ASSETS = (USDC, DOT, JAMKB)
DEFAULT_MARKETS = [(1, DOT, USDC), (2, JAMKB, USDC), (3, JAMKB, DOT)]   # (market, base, quote)
SCALE = 10_000                          # atomic units per display unit (SCALE in the service)
GENESIS_BALANCE = 1_000_000             # display units of each asset per dev account
TAG_DEPOSIT, TAG_LIST, TAG_REGISTER = 1, 6, 7

# the standard JAM dev accounts (docs.jamcha.in/basics/dev-accounts): public ed25519 seeds,
# the same ones the trading UI and loadgen hold
DEV_ACCOUNTS = [
    ("Alice", "996542becdf1e78278dc795679c825faca2e9ed2bf101bf3c4a236d3ed79cf59"),
    ("Bob", "b81e308145d97464d2bc92d35d227a9e62241a16451af6da5053e309be4f91d7"),
    ("Carol", "0093c8c10a88ebbc99b35b72897a26d259313ee9bad97436a437d2e43aaafa0f"),
    ("David", "69b3a7031787e12bfbdcac1b7a737b3e5a9f9450c37e215f6d3b57730e21001a"),
    ("Eve", "b4de9ebf8db5428930baa5a98d26679ab2a03eae7c791d582e6b75b7f018d0d4"),
    ("Fergie", "4a6482f8f479e3ba2b845f8cef284f4b3208ba3241ed82caa1b5ce9fc6281730"),
]


class SetupError(Exception):
    """setup() could not get every op onto the chain in time."""


# ---- payloads (layouts: service/src/lib.rs) ---------------------------------------------
def canon(action, *parts):
    return b"jamswap:v1:" + action + b"".join(parts)


def list_payload(market, base, quote):
    return bytes([TAG_LIST]) + struct.pack("<III", market, base, quote)


def register_payload(signing_key):
    """A self-signed REGISTER for a nacl.signing.SigningKey."""
    pk = bytes(signing_key.verify_key)
    return bytes([TAG_REGISTER]) + pk + signing_key.sign(canon(b"register", pk)).signature


def deposit_payload(account, asset, amount, nonce):
    """[tag][account][asset][amount][nonce]; crates/match-engine/src/deposit.rs is the layout."""
    if not 0 < nonce < 1 << 64:
        raise ValueError("a deposit nonce is in 1..2^64-1")
    return bytes([TAG_DEPOSIT]) + struct.pack("<IIQQ", account, asset, amount, nonce)


def dev_keys(accounts=None):
    """[(name, SigningKey)] for the dev accounts."""
    from nacl.signing import SigningKey
    return [(name, SigningKey(bytes.fromhex(seed))) for name, seed in (accounts or DEV_ACCOUNTS)]


# ---- what has landed (reads at the best block) -----------------------------------------------
def market_listed(chain, market):
    return bool(chain.read(b"mkt" + struct.pack("<I", market)))


def handle_of(chain, pubkey):
    v = chain.read(b"h" + bytes(pubkey))
    return int.from_bytes(v[:4], "little") if len(v) >= 4 else None


def deposit_landed(chain, account, nonce):
    """Has the service admitted `nonce` for `account`? Its record b"dn" ++ account is a
    floor and the nonces above it (match_engine::deposit::admit)."""
    v = chain.read(b"dn" + struct.pack("<I", account))
    if len(v) < 8:
        return False
    floor = int.from_bytes(v[:8], "little")
    window = {int.from_bytes(v[i:i + 8], "little") for i in range(8, len(v) - 7, 8)}
    return nonce <= floor or nonce in window


class Op:
    """One work-item and how to tell it has landed."""
    def __init__(self, what, payload, landed):
        self.what, self.payload, self.landed = what, payload, landed

    def __repr__(self):
        return f"Op({self.what})"


class Runner:
    """Gets ops onto a chain.Chain: up to `max_items` per submission, polled every `poll`
    seconds until landed, resent after `resend` seconds (sooner if JIP-2 reports the
    package Failed). Timing is injectable for tests."""

    def __init__(self, chain, log=None, poll=3.0, resend=60.0, sleep=time.sleep,
                 clock=time.monotonic):
        self.chain = chain
        self.log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self.poll, self.resend, self.sleep, self.clock = poll, resend, sleep, clock

    def _landed(self, op):
        try:
            return bool(op.landed())
        except chainmod.ChainError:
            return False                   # cannot tell yet: ask again next poll

    def _failed(self, receipts):
        status = getattr(self.chain, "package_status", None)
        for r in receipts:
            if status is None or "package_hash" not in r:
                continue
            try:
                s = status(r)
            except chainmod.ChainError:
                continue
            if isinstance(s, dict) and "Failed" in s:
                return True
        return False

    def run(self, what, ops, timeout=300):
        """Land every op; raise SetupError with what is left if `timeout` passes."""
        deadline = self.clock() + timeout
        pending = [op for op in ops if not self._landed(op)]
        rounds = 0
        while pending:
            if self.clock() >= deadline:
                raise SetupError(f"{what}: not landed after {timeout:g}s: {pending}")
            rounds += 1
            try:
                n = max(1, int(self.chain.max_items))
            except chainmod.ChainError:
                n = 1
            receipts = []
            for i in range(0, len(pending), n):
                group = pending[i:i + n]
                try:
                    receipts.append(self.chain.submit_items([op.payload for op in group]))
                except chainmod.ChainBusy as e:
                    self.log(f"{what}: {len(group)} op(s) not sent ({e}); retrying")
                except chainmod.ChainError as e:
                    self.log(f"{what}: {len(group)} op(s): outcome unknown ({e}); watching")
            self.log(f"{what}: sent {len(pending)} op(s) in {len(receipts)} package(s)"
                     + (f" (attempt {rounds})" if rounds > 1 else ""))
            sent, wait = self.clock(), self.resend if receipts else self.poll
            while pending and self.clock() - sent < wait and self.clock() < deadline:
                self.sleep(self.poll)
                pending = [op for op in pending if not self._landed(op)]
                if pending and self._failed(receipts):
                    self.log(f"{what}: a package failed; resending what has not landed")
                    break
        self.log(f"{what}: done")


def setup(chain, markets=None, accounts=None, balance=None, timeout=600, log=None, runner=None):
    """List `markets` and register + fund `accounts` (default: DEFAULT_MARKETS,
    DEV_ACCOUNTS, GENESIS_BALANCE or the GENESIS_BALANCE env). Returns {name: handle}."""
    markets = DEFAULT_MARKETS if markets is None else markets
    if balance is None:
        balance = int(os.environ.get("GENESIS_BALANCE") or GENESIS_BALANCE)
    runner = runner or Runner(chain, log)
    log = runner.log
    keys = dev_keys(accounts)
    t0 = runner.clock()

    def left():
        return max(0.0, timeout - (runner.clock() - t0))

    ops = [Op(f"list market {m}", list_payload(m, b, q), lambda m=m: market_listed(chain, m))
           for m, b, q in markets]
    ops += [Op(f"register {name}", register_payload(sk),
               lambda pk=bytes(sk.verify_key): handle_of(chain, pk) is not None)
            for name, sk in keys]
    runner.run("markets + dev accounts", ops, left())
    handles = {name: handle_of(chain, bytes(sk.verify_key)) for name, sk in keys}
    if balance > 0:
        amount = balance * SCALE
        ops = [Op(f"deposit {balance} of asset {a} to {name} ({handles[name]})",
                  deposit_payload(handles[name], a, amount, a + 1),
                  lambda h=handles[name], a=a: deposit_landed(chain, h, a + 1))
               for name, _ in keys for a in ASSETS]
        runner.run("dev account funding", ops, left())
    log("dex setup: markets " + ", ".join(str(m) for m, _, _ in markets) + " listed; accounts "
        + ", ".join(f"{name}={h}" for name, h in handles.items())
        + (f"; {balance} of each asset each" if balance > 0 else ""))
    return handles


def wanted(env=None, deployed=False):
    """Should the server run setup()? DEX_SETUP=1 always, 0 never; unset or "auto": only
    after it deployed the service itself (a genesis-seeded service is already set up)."""
    env = os.environ if env is None else env
    v = (env.get("DEX_SETUP") or "auto").strip().lower()
    return v == "1" or (v == "auto" and deployed)
