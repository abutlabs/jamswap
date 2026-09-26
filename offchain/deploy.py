#!/usr/bin/env python3
"""Deploy a service at runtime through the chain's Bootstrap service, over JIP-2 only.

    python3 offchain/deploy.py --rpc ws://localhost:19800 --chain-spec spec.json
        deploys service/jamswap-service.jam (or reuses it), then lists the default markets
        and funds the dev accounts (dex_setup.py); prints SERVICE_ID=<id> on stdout

A JAM dev chain starts with one service, the Bootstrap service (id 0, e.g. PolkaJam's
`--chain dev`), which turns work-item payloads into privileged actions in its accumulate.
Creating a service is one: accumulate calls the `new` host call (GP 0.8.0
pvm_invocations.tex, Omega_N), which

  - creates an account with code hash c, one preimage request {(c, |code|): []}, the
    minimum balance, created = this slot and parent = the Bootstrap service;
  - gives it the requested id if the caller is the registrar and the id is below
    S = 2^16 and free, else the next free public id.

The code is not part of the create. It is a preimage the new account has requested, so
anyone may provide it: the preimage extrinsic carries (service, blob) pairs for requests
not yet provided (GP 0.8.0 accumulation.tex, "Preimage Integration"), and JIP-2
submitPreimage hands the blob to a node to put in a block. Once provided, the account's
code is that preimage, E(var(metadata), code) (GP 0.8.0 accounts.tex), and refine looks
it up at a package's lookup anchor.

deploy():
  1. reuses a deployment: the service in the state file, or (without one) a service that
     already runs this code, if serviceData shows it with this code hash;
  2. else submits one work-item to the Bootstrap service, CreateService (below), with the
     lowest free id (as `jamt create-service` does) or the one asked for;
  3. waits for serviceData to show the service with this code hash, resubmitting a
     package that JIP-2 workPackageStatus reports Failed;
  4. submitPreimage(id, code) until servicePreimage shows the code provided;
  5. waits until the code is available at the lookup anchor the DEX's next package will
     name, so its first work-item refines; saves the id to the state file.

The Bootstrap instruction. The work-item payload is

    E(n) ++ instruction_1 ++ ... ++ instruction_n ++ salt (32 octets)

and an instruction is an enum: a variant octet, then its fields (integers little-endian
at their width, a byte string or list prefixed with its length as a GP general natural,
an optional value as 0, or 1 and the value). The type is public:
`jam_bootstrap_service_common::Instruction` (docs.rs, jam-bootstrap-service-common 0.1.28,
Apache-2.0), whose variant 0 is

    CreateService { code_hash: [u8; 32], code_len: u64, min_item_gas: u64,
                    min_memo_gas: u64, endowment: u64, memo: [u8; W_T],
                    registration: Option<Vec<u8>> }

Version 0.1.29, which PolkaJam 0.1.29 runs (`jamt list` names it), appends
`deposit_offset: u64` and `id: Option<u32>`, the fields behind `jamt create-service
--deposit-offset` and `--id`. That layout, the leading count and the trailing random
salt were confirmed by recording the JIP-2 calls `jamt create-service` made to a node we
ran (public protocol, our own socket) and decoding them with workpackage.py; nothing
else of PolkaJam was looked at. deploy() sends the instruction alone; the created
service is recognised by its account record, never by the Bootstrap service's own
bookkeeping.
"""
import argparse, json, os, sys, time
from typing import NamedTuple, Optional

import chain as chainmod
from workpackage import blake2b256, dec_nat, enc_nat, var

BOOTSTRAP_ID = 0
MIN_PUBLIC_ID = 1 << 16             # GP 0.8.0 S: ids below it are the registrar's to hand out
SALT_LEN = 32
MEMO_SIZE = 128                     # GP 0.8.0 W_T, when the node does not report it
DEFAULT_MIN_GAS = 10_000            # min_item_gas / min_memo_gas (jamt's defaults)
DEFAULT_ENDOWMENT = 10 ** 9         # covers the DEX's storage deposits on a dev chain
DEFAULT_CODE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "service",
                            "jamswap-service.jam")
DEFAULT_STATE = "/tmp/jamswap_deploy.json"
READY_GRACE_SLOTS = 6               # after Ready, slots to wait for the create to show
RESEND_PREIMAGE_SLOTS = 3           # resend submitPreimage if not provided by then


class DeployError(Exception):
    """The service could not be deployed (or reused)."""


# ---- the Bootstrap instruction --------------------------------------------------------
def _u(x, n):
    return int(x).to_bytes(n, "little")


def _option(value, encode):
    return b"\x00" if value is None else b"\x01" + encode(value)


class CreateService(NamedTuple):
    """jam-bootstrap-service 0.1.29 `Instruction::CreateService` (see the module doc)."""
    code_hash: bytes
    code_len: int
    min_item_gas: int = DEFAULT_MIN_GAS
    min_memo_gas: int = DEFAULT_MIN_GAS
    endowment: int = 0
    memo: bytes = b""
    registration: Optional[bytes] = None
    deposit_offset: int = 0         # the account's gratis storage; non-zero needs the manager
    service_id: Optional[int] = None

    VARIANT = 0

    def encode(self, memo_size=MEMO_SIZE):
        if len(self.code_hash) != 32:
            raise ValueError(f"code hash: expected 32 bytes, got {len(self.code_hash)}")
        if len(self.memo) > memo_size:
            raise ValueError(f"memo of {len(self.memo)} bytes exceeds W_T = {memo_size}")
        return (bytes([self.VARIANT]) + bytes(self.code_hash) + _u(self.code_len, 8)
                + _u(self.min_item_gas, 8) + _u(self.min_memo_gas, 8) + _u(self.endowment, 8)
                + bytes(self.memo).ljust(memo_size, b"\0")
                + _option(self.registration, var)
                + _u(self.deposit_offset, 8)
                + _option(self.service_id, lambda i: _u(i, 4)))


def bootstrap_payload(instructions, salt=None):
    """A Bootstrap work-item payload: the count, the encoded instructions, a 32-byte salt
    (random by default, so two identical instructions make different packages)."""
    salt = os.urandom(SALT_LEN) if salt is None else bytes(salt)
    if len(salt) != SALT_LEN:
        raise ValueError(f"salt: expected {SALT_LEN} bytes, got {len(salt)}")
    return enc_nat(len(instructions)) + b"".join(instructions) + salt


# ---- code metadata ----------------------------------------------------------------------
def split_code(blob):
    """(metadata, code) of a service code preimage E(var(m), c) (GP 0.8.0 accounts.tex)."""
    n, i = dec_nat(blob, 0)
    if i + n > len(blob):
        raise ValueError("code blob: metadata runs past the end")
    return bytes(blob[i:i + n]), bytes(blob[i + n:])


def describe_code(blob):
    """"name version" from the conventional metadata (a 0 octet, then the name and the
    version as length-prefixed strings), or "" when it is not in that form."""
    try:
        meta, _ = split_code(blob)
        if not meta or meta[0] != 0:
            return ""
        n, i = dec_nat(meta, 1)
        name = meta[i:i + n].decode()
        n, i = dec_nat(meta, i + n)
        return f"{name} {meta[i:i + n].decode()}"
    except (ValueError, UnicodeDecodeError):
        return ""


# ---- the state file ------------------------------------------------------------------------
def load_state(path):
    try:
        with open(path) as f:
            st = json.load(f)
        return st if isinstance(st, dict) and isinstance(st.get("service_id"), int) else None
    except (OSError, ValueError):
        return None


def save_state(path, state):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


# ---- the deploy ------------------------------------------------------------------------------
class Deployment(NamedTuple):
    service_id: int
    code_hash: bytes
    created: int                    # the slot the service was created in
    reused: bool                    # True: an existing service was reused, nothing created


class Deployer:
    """Deploys one code blob on the chain behind `chain` (a chain.Jip2Chain that can
    submit). Timing is injectable for tests: `sleep(seconds)` and `clock()`."""

    def __init__(self, chain, code, bootstrap_id=BOOTSTRAP_ID, state_file=None, log=None,
                 poll=2.0, sleep=time.sleep, clock=time.monotonic):
        self.chain, self.code = chain, bytes(code)
        self.code_hash = blake2b256(self.code)
        self.bootstrap_id, self.state_file = bootstrap_id, state_file
        self.log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self.poll, self.sleep, self.clock = poll, sleep, clock
        self._deadline = None

    # -- helpers
    def _record(self, sid, at="best"):
        return self.chain.service_record(sid, at)

    def _runs_our_code(self, info):
        return info is not None and info.code_hash == self.code_hash

    def _left(self):
        return self._deadline - self.clock()

    def _wait(self, what, check):
        """Poll `check()` until it returns something truthy (returned) or the deadline
        passes. A failed read is retried: the node may be busy."""
        last = None
        while True:
            try:
                v = check()
                if v:
                    return v
            except chainmod.ChainError as e:
                last = e
            if self._left() <= 0:
                raise DeployError(f"timed out waiting for {what}"
                                  + (f" (last error: {last})" if last else ""))
            self.sleep(self.poll)

    # -- 1. reuse
    def existing(self):
        """The id of a service to reuse, or None: the state file's, if the chain shows it
        with this code; else the lowest-numbered service already running this code."""
        st = load_state(self.state_file) if self.state_file else None
        if st and st.get("code_hash") == self.code_hash.hex():
            sid = st["service_id"]
            info = self._record(sid)
            if self._runs_our_code(info):
                return sid
            self.log(f"deploy: the state file names service {sid}, but the chain has "
                     f"{'no such service' if info is None else 'other code there'}")
        found = [s for s in self.chain.services() if self._runs_our_code(self._record(s))]
        if len(found) > 1:
            self.log(f"deploy: services {found} all run this code; reusing {found[0]}")
        return found[0] if found else None

    # -- 2, 3. create
    def free_id(self, start=1):
        """The lowest id at or above `start` with no service, below S."""
        listed = set(self.chain.services())
        for sid in range(max(start, 1), MIN_PUBLIC_ID):
            if sid not in listed and self._record(sid) is None:
                return sid
        raise DeployError(f"no free service id in [{start}, {MIN_PUBLIC_ID})")

    def _created(self, wanted, since_slot, scan):
        # the service our create made: the id asked for, if it runs our code; else, once
        # the create has been accumulated (`scan`), a service the Bootstrap service created
        # since we submitted that runs our code (it is not the registrar, so `new` chose
        # the id)
        info = self._record(wanted)
        if self._runs_our_code(info):
            return wanted
        if info is not None and info.created >= since_slot:
            raise DeployError(f"service id {wanted} was taken meanwhile by other code "
                              f"({info.code_hash.hex()[:16]}..); deploy again")
        if not scan:
            return None
        for sid in sorted(self.chain.services()):
            info = self._record(sid)
            if (self._runs_our_code(info) and info.parent == self.bootstrap_id
                    and info.created >= since_slot):
                return sid
        return None

    def create(self, service_id, endowment=DEFAULT_ENDOWMENT, min_item_gas=DEFAULT_MIN_GAS,
               min_memo_gas=DEFAULT_MIN_GAS):
        """Submit CreateService to the Bootstrap service and wait until the service is on
        chain; return its id. A package JIP-2 reports Failed, or whose status cannot be
        read once its anchor has left recent history, is resubmitted."""
        boot = self.chain.for_service(self.bootstrap_id)
        self._check_bootstrap()
        instr = CreateService(self.code_hash, len(self.code), min_item_gas, min_memo_gas,
                              endowment, service_id=service_id)
        memo_size = self.chain.parameter("transfer_memo_size", MEMO_SIZE)
        expiry = self.chain.parameter("recent_block_count", 8) + READY_GRACE_SLOTS
        since = self.chain.head().slot

        def send():
            try:
                r = boot.submit(bootstrap_payload([instr.encode(memo_size)]))
            except chainmod.ChainBusy as e:
                self.log(f"deploy: CreateService not sent ({e}); retrying")
                return None
            self.log(f"deploy: CreateService(id {service_id}, {len(self.code)} bytes, endowment "
                     f"{endowment}) to service {self.bootstrap_id}: package "
                     f"{r['package_hash'][:16]}.. on core {r['core']}, anchor #{r['anchor_slot']}")
            return r

        receipt, ready_slot = send(), None
        while True:
            try:
                sid = self._created(service_id, since, scan=ready_slot is not None)
                if sid is not None:
                    return sid
                if receipt is None:
                    receipt = send()
                else:
                    try:
                        status = boot.package_status(receipt)
                    except chainmod.ChainError as e:
                        status = {"Unknown": str(e)}
                    kind = next(iter(status)) if isinstance(status, dict) and status else None
                    expired = (kind in (None, "Unknown")
                               and self.chain.head().slot > receipt["anchor_slot"] + expiry)
                    if expired and self._created(service_id, since, scan=True) is not None:
                        continue                   # it landed after all
                    if kind == "Failed" or expired:
                        self.log(f"deploy: package {receipt['package_hash'][:16]}.. "
                                 + ("failed" if kind == "Failed" else "has no status and its anchor aged out")
                                 + f" ({status.get(kind) if kind else status}); resubmitting")
                        receipt, ready_slot = send(), None
                    elif kind == "Ready":
                        ready_slot = ready_slot or int(status["Ready"]["ready_in"]["slot"])
                        if self.chain.head().slot >= ready_slot + READY_GRACE_SLOTS:
                            raise DeployError(
                                f"the Bootstrap service accumulated CreateService (Ready at "
                                f"#{ready_slot}) but no service with code "
                                f"{self.code_hash.hex()[:16]}.. appeared: it refused the create "
                                f"(an id taken, its balance short of the endowment, or an "
                                f"instruction layout it does not know)")
            except chainmod.ChainError as e:
                self.log(f"deploy: waiting for the create: {e}")
            if self._left() <= 0:
                raise DeployError(f"timed out waiting for service {service_id} to be created")
            self.sleep(self.poll)

    def _check_bootstrap(self):
        # name the Bootstrap service from its code's metadata: the instruction layout is
        # the one confirmed for jam-bootstrap-service 0.1.29
        try:
            info = self._record(self.bootstrap_id)
            blob = info and self.chain.preimage(self.bootstrap_id, info.code_hash)
        except chainmod.ChainError as e:
            self.log(f"deploy: cannot read the Bootstrap service's code ({e})")
            return
        if info is None:
            raise DeployError(f"no service {self.bootstrap_id} (the Bootstrap service) on this chain")
        who = describe_code(blob) if blob else ""
        self.log(f"deploy: Bootstrap service {self.bootstrap_id}: {who or 'unnamed code'}")
        if who != "jam-bootstrap-service 0.1.29":
            self.log("deploy: warning: the CreateService layout is the one confirmed for "
                     "jam-bootstrap-service 0.1.29; a create this service does not understand "
                     "shows as no new service")

    # -- 4. provide the code
    def provide(self, sid):
        """submitPreimage(sid, code) until the code is provided (resent every few slots)."""
        h, n = self.code_hash, len(self.code)
        sent_at = [None]

        def provided():
            at = self.chain.head()                 # both reads at one block
            if self.chain.preimage(sid, h, at=at) is not None:
                return True
            req = self.chain.preimage_request(sid, h, n, at=at)
            if req is None:
                raise DeployError(f"service {sid} has not requested its code "
                                  f"({h.hex()[:16]}.., {n} bytes): nothing can provide it")
            if req:
                raise DeployError(f"service {sid}'s code request is {req}, not outstanding")
            slot = at.slot
            if sent_at[0] is None or slot >= sent_at[0] + RESEND_PREIMAGE_SLOTS:
                self.chain.provide(sid, self.code)
                self.log(f"deploy: submitPreimage({sid}, {n} bytes) at #{slot}")
                sent_at[0] = slot
            return False

        self._wait(f"service {sid}'s code to be provided", provided)

    # -- 5. usable
    def wait_usable(self, sid):
        """Until the code is at the lookup anchor the DEX's next package names."""
        def at_lookup_anchor():
            lookup = self.chain.default_lookup_anchor(self.chain.default_anchor())
            return self.chain.preimage(sid, self.code_hash, at=lookup.hash) is not None
        self._wait(f"service {sid}'s code at the lookup anchor", at_lookup_anchor)

    def run(self, service_id=None, fresh=False, endowment=DEFAULT_ENDOWMENT,
            min_item_gas=DEFAULT_MIN_GAS, min_memo_gas=DEFAULT_MIN_GAS, timeout=600):
        """Deploy (or reuse) the service; return a Deployment. `service_id` asks for an
        id (below 2^16, free); `fresh` never reuses."""
        self._deadline = self.clock() + timeout
        sid = None
        if service_id is not None:             # this id or nothing
            if not 0 < service_id < MIN_PUBLIC_ID:
                raise DeployError(f"service id {service_id}: must be in [1, {MIN_PUBLIC_ID})")
            info = self._record(service_id)
            if info is not None and (fresh or not self._runs_our_code(info)):
                raise DeployError(f"service id {service_id} is taken"
                                  + ("" if self._runs_our_code(info) else " by other code"))
            sid = service_id if info is not None else None
        elif not fresh:
            sid = self.existing()
        reused = sid is not None
        if reused:
            self.log(f"deploy: reusing service {sid} (code {self.code_hash.hex()[:16]}..)")
        else:
            self.log(f"deploy: {describe_code(self.code) or 'code'} "
                     f"({len(self.code)} bytes, hash {self.code_hash.hex()[:16]}..)")
            sid = self.create(service_id if service_id is not None else self.free_id(),
                              endowment, min_item_gas, min_memo_gas)
            self.log(f"deploy: service {sid} created")
        self.provide(sid)
        self.wait_usable(sid)
        info = self._record(sid)
        if not self._runs_our_code(info):
            raise DeployError(f"service {sid} vanished or changed code while deploying")
        if self.state_file:
            save_state(self.state_file, {
                "service_id": sid, "code_hash": self.code_hash.hex(),
                "bootstrap_id": self.bootstrap_id, "rpc": getattr(self.chain, "url", None),
                "created": info.created, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        self.chain.service_id = sid
        self.log(f"deploy: service {sid} ready (created #{info.created}, balance {info.balance})")
        return Deployment(sid, self.code_hash, info.created, reused)


def deploy(chain, code, service_id=None, fresh=False, bootstrap_id=BOOTSTRAP_ID,
           state_file=None, endowment=DEFAULT_ENDOWMENT, min_item_gas=DEFAULT_MIN_GAS,
           min_memo_gas=DEFAULT_MIN_GAS, timeout=600, log=None):
    """Deploy `code` (bytes) on `chain` (a chain.Jip2Chain), or reuse it; sets
    chain.service_id and returns a Deployment."""
    if not getattr(chain, "submits", False) or not hasattr(chain, "for_service"):
        raise DeployError(f"{chain.describe()} cannot deploy: runtime deploy needs the jip2 "
                          "backend with an authorizer (AUTHORIZER or CHAIN_SPEC)")
    return Deployer(chain, code, bootstrap_id, state_file, log).run(
        service_id, fresh, endowment, min_item_gas, min_memo_gas, timeout)


def _env_int(env, name, default=None):
    v = env.get(name)
    return int(v, 0) if v else default


def from_env(chain, env=None, log=None):
    """deploy() configured by the environment: SERVICE_CODE (the blob; default
    service/jamswap-service.jam), DEPLOY_STATE (default /tmp/jamswap_deploy.json),
    DEPLOY_SERVICE_ID, DEPLOY_ENDOWMENT, DEPLOY_FRESH=1, BOOTSTRAP_ID, DEPLOY_TIMEOUT."""
    env = os.environ if env is None else env
    with open(env.get("SERVICE_CODE") or DEFAULT_CODE, "rb") as f:
        code = f.read()
    return deploy(chain, code, service_id=_env_int(env, "DEPLOY_SERVICE_ID"),
                  fresh=env.get("DEPLOY_FRESH") == "1",
                  bootstrap_id=_env_int(env, "BOOTSTRAP_ID", BOOTSTRAP_ID),
                  state_file=env.get("DEPLOY_STATE") or DEFAULT_STATE,
                  endowment=_env_int(env, "DEPLOY_ENDOWMENT", DEFAULT_ENDOWMENT),
                  timeout=float(env.get("DEPLOY_TIMEOUT") or 600), log=log)


def main(argv=None):
    env = os.environ
    p = argparse.ArgumentParser(
        description="Deploy the jamswap service through the chain's Bootstrap service over "
                    "JIP-2 (no jamt), then list the default markets and fund the dev "
                    "accounts. Prints SERVICE_ID=<id> on stdout.")
    p.add_argument("--rpc", default=env.get("CHAIN_RPC") or "ws://localhost:19800",
                   help="JIP-2 node RPC (CHAIN_RPC)")
    p.add_argument("--chain-spec", default=env.get("CHAIN_SPEC"),
                   help="JIP-4 chain spec naming the authorizer (CHAIN_SPEC)")
    p.add_argument("--authorizer", default=env.get("AUTHORIZER"),
                   help="host:code_hash[:config[:token]] (AUTHORIZER)")
    p.add_argument("--code", default=env.get("SERVICE_CODE") or DEFAULT_CODE,
                   help="the service blob (SERVICE_CODE)")
    p.add_argument("--state", default=env.get("DEPLOY_STATE") or DEFAULT_STATE,
                   help="where the deployed id is kept (DEPLOY_STATE)")
    p.add_argument("--id", type=int, default=_env_int(env, "DEPLOY_SERVICE_ID"),
                   help="ask for this service id (below 65536); default: the lowest free")
    p.add_argument("--endowment", type=int, default=_env_int(env, "DEPLOY_ENDOWMENT", DEFAULT_ENDOWMENT))
    p.add_argument("--bootstrap-id", type=int, default=_env_int(env, "BOOTSTRAP_ID", BOOTSTRAP_ID))
    p.add_argument("--fresh", action="store_true", help="deploy a new service even if one runs this code")
    p.add_argument("--no-setup", action="store_true", help="skip listing markets and funding dev accounts")
    p.add_argument("--balance", type=int, default=None,
                   help="dev-account funding per asset, display units (GENESIS_BALANCE; default 1000000)")
    p.add_argument("--timeout", type=float, default=float(env.get("DEPLOY_TIMEOUT") or 600))
    a = p.parse_args(argv)
    if not (a.chain_spec or a.authorizer):
        p.error("an authorizer is needed to submit: --chain-spec or --authorizer")
    ch = chainmod.Jip2Chain(None, a.rpc, authorizer=a.authorizer, chain_spec=a.chain_spec)
    with open(a.code, "rb") as f:
        code = f.read()
    try:
        d = deploy(ch, code, service_id=a.id, fresh=a.fresh, bootstrap_id=a.bootstrap_id,
                   state_file=a.state, endowment=a.endowment, timeout=a.timeout)
        if not a.no_setup:
            import dex_setup
            dex_setup.setup(ch, balance=a.balance)
    except (DeployError, chainmod.ChainError) as e:
        print(f"deploy: {e}", file=sys.stderr)
        return 1
    print(f"SERVICE_ID={d.service_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
