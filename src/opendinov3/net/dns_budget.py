"""Pace dnspython transport writes across threads and spawned processes.

This controls this Python workload, not other software on a node. All workers
must use the same node-local file. UDP retries and TCP fallback use the same
socket factory. No token accumulation, OS resolver fallback, or privileged
network configuration is involved. A fresh interval is waited under the lock
before EVERY write, including after a crashed writer releases its lock.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from typing import cast

DEFAULT_QPS = 50.0
MAX_QPS = 80.0


class Budget:
    def __init__(self, path, qps=DEFAULT_QPS):
        if not math.isfinite(qps) or not 0 < qps <= MAX_QPS:
            raise ValueError(f"DNS QPS must be finite and within (0, {MAX_QPS}]")
        self.path = Path(path)
        self.qps = qps

    def send(self, operation):
        # A separate open-file description per call is essential: flock on a
        # reused descriptor does NOT serialize threads sharing that descriptor.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as state:
            fcntl.flock(state, fcntl.LOCK_EX)
            raw = state.read()
            data = json.loads(raw) if raw else {"qps": self.qps, "write_attempts": 0}
            if data["qps"] != self.qps:
                raise ValueError("Conflicting DNS budget for the shared state file")
            # Count attempts before sending; a crash may overcount, never
            # describe a possibly sent query as an observed success.
            data["write_attempts"] += 1
            state.seek(0)
            json.dump(data, state)
            state.truncate()
            state.flush()
            time.sleep(1 / self.qps)
            return operation()


class ResolutionAdmission:
    """Bound active hostname resolutions across this user's node workers.

    File locks release on process exit. Admission happens before resolve_name
    starts its A/AAAA lifetime; the transport gate still paces every write.
    """

    def __init__(self, path, limit=16, wait_seconds=60.0):
        if limit < 1 or not math.isfinite(wait_seconds) or wait_seconds <= 0:
            raise ValueError("Invalid DNS admission bounds")
        self.directory = Path(str(path) + ".admission")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.limit = limit
        self.wait_seconds = wait_seconds

    @contextmanager
    def enter(self):
        deadline = time.monotonic() + self.wait_seconds
        while True:
            for slot in range(self.limit):
                fd = os.open(self.directory / str(slot),
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    try:
                        yield
                    finally:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    return
                finally:
                    os.close(fd)
            if time.monotonic() >= deadline:
                raise TimeoutError("DNS admission wait exceeded its bound")
            time.sleep(0.05)


class BudgetSocket:
    def __init__(self, sock, budget):
        self.sock = sock
        self.budget = budget

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.sock.close()

    def send(self, data, *args):
        return self.budget.send(lambda: self.sock.send(data, *args))

    def sendto(self, data, *args):
        return self.budget.send(lambda: self.sock.sendto(data, *args))


def install(path, qps=DEFAULT_QPS):
    """Install before any HTTP client imports; never fall back on failure.

    dnspython 2.8.0 supports the public socket_factory and resolver override
    APIs. Numeric addresses use libc without DNS. Hosts/NSS-specific names and
    AI_ADDRCONFIG/AI_V4MAPPED are not supported by the override; this mode is
    for public HTTP acquisition, not a general replacement for system NSS.
    """
    import dns.query
    import dns.resolver

    budget = Budget(path, qps)
    # Validate permissions/state even in a process which never sends a query.
    budget.send(lambda: None)
    admission = ResolutionAdmission(path)

    class AdmittedResolver(dns.resolver.Resolver):
        def resolve_name(self, *args, **kwargs):
            with admission.enter():
                return super().resolve_name(*args, **kwargs)

    resolver = AdmittedResolver()
    resolver.cache = dns.resolver.LRUCache(max_size=100_000)
    resolver.timeout = 3
    resolver.lifetime = 30
    # The transport API uses socket operations structurally; this proxy delegates
    # all operations except writes. dnspython annotates the factory nominally.
    dns.query.socket_factory = lambda *args, **kwargs: cast(
        socket.socket, BudgetSocket(socket.socket(*args, **kwargs), budget)
    )
    dns.resolver.override_system_resolver(resolver)
