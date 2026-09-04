"""Contract for caching name resolution inside a worker process.

WHY

Scaling from 4 nodes to 8 did not run out of connections — `unreachable`
stayed at 0.1% — it ran out of **resolver**. DNS failures went from 6.2% to
76.6% and yield collapsed from 64.0% to 17.4%, leaving 71 tasks that the
health guard correctly rejected.

At 575 URL/s per node, 8 nodes ask the shared resolver for ~4,600 names a
second. `od.sh hosts` measured 500,000 URLs across 118,834 hosts, so most of
those questions have already been asked.

WHAT MUST NOT BREAK, AND WHY EACH IS HERE

- **Failures are never cached.** Under a saturated resolver a failure is
  transient by definition, and caching one converts a blip into a certainty
  for the whole TTL — for the exact host that was unlucky, at the exact
  moment the resolver is struggling. It would make the problem it is meant
  to fix worse.
- **`gaierror` reaches the caller unchanged.** download_stats classifies DNS
  failures by matching `[Errno -2]` in the message. Wrap or re-raise it as
  something else and 6% of every wave silently moves into `other`, and the
  number the whole scaling decision rests on stops meaning anything.
- **The key is the whole argument tuple.** `getaddrinfo` answers differently
  per family and per port. Keying on the host alone would hand an IPv6-only
  answer to a caller that asked for IPv4.
- **Entries expire.** Without a TTL a wrong or moved record is wrong for the
  rest of the run.
- **It is bounded and thread-safe.** One node runs 32 processes of 32
  threads, and a task's URLs touch hundreds of thousands of hosts.
"""

from __future__ import annotations

import socket
import threading

import pytest

from opendinov3.net import dns_cache


ADDR = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


class FakeResolver:
    """Stands in for the real resolver and counts what it was asked."""

    def __init__(self, fail_hosts=(), answers=None):
        self.calls: list[tuple] = []
        self.fail_hosts = set(fail_hosts)
        self.answers = answers or {}
        self.lock = threading.Lock()

    def __call__(self, host, port, family=0, type=0, proto=0, flags=0):
        with self.lock:
            self.calls.append((host, port, family, type, proto, flags))
        if host in self.fail_hosts:
            raise socket.gaierror(-2, "Name or service not known")
        return self.answers.get(host, ADDR)

    def count(self, host=None) -> int:
        if host is None:
            return len(self.calls)
        return sum(1 for c in self.calls if c[0] == host)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def resolver():
    """Install the cache over a fake resolver, and put socket back after."""
    original = socket.getaddrinfo
    fake = FakeResolver()
    socket.getaddrinfo = fake
    try:
        yield fake
    finally:
        dns_cache.uninstall()
        socket.getaddrinfo = original


# --------------------------------------------------------------------------
# The point
# --------------------------------------------------------------------------

def test_a_repeated_name_is_asked_once(resolver) -> None:
    dns_cache.install()

    for _ in range(50):
        assert socket.getaddrinfo("a.example", 443) == ADDR

    assert resolver.count() == 1, resolver.calls


def test_a_different_name_is_asked(resolver) -> None:
    dns_cache.install()

    socket.getaddrinfo("a.example", 443)
    socket.getaddrinfo("b.example", 443)

    assert resolver.count() == 2


def test_nothing_is_cached_before_it_is_installed(resolver) -> None:
    """So a run with the flag off behaves exactly as it did."""
    socket.getaddrinfo("a.example", 443)
    socket.getaddrinfo("a.example", 443)

    assert resolver.count() == 2


# --------------------------------------------------------------------------
# The hazard: caching a failure
# --------------------------------------------------------------------------

def test_a_failure_is_not_cached(resolver) -> None:
    """A saturated resolver fails transiently. Caching that would turn one
    unlucky moment into a guaranteed failure for the whole TTL — for the
    host that was unlucky, while the resolver is already struggling."""
    resolver.fail_hosts.add("dead.example")
    dns_cache.install()

    for _ in range(3):
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("dead.example", 443)

    assert resolver.count("dead.example") == 3, "a failure was cached"


def test_a_name_that_starts_failing_and_recovers_is_seen_to_recover(resolver
                                                                    ) -> None:
    resolver.fail_hosts.add("flaky.example")
    dns_cache.install()

    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("flaky.example", 443)
    resolver.fail_hosts.clear()

    assert socket.getaddrinfo("flaky.example", 443) == ADDR


def test_the_resolver_error_reaches_the_caller_unchanged(resolver) -> None:
    """download_stats reads `[Errno -2]` out of this message to tell a DNS
    failure from anything else. Changing the type or the text moves 6% of
    every wave into `other`."""
    resolver.fail_hosts.add("dead.example")
    dns_cache.install()

    with pytest.raises(socket.gaierror) as caught:
        socket.getaddrinfo("dead.example", 443)

    assert caught.value.errno == -2
    assert "Name or service not known" in str(caught.value)


# --------------------------------------------------------------------------
# The key
# --------------------------------------------------------------------------

@pytest.mark.parametrize("second", [
    ("a.example", 80),
    ("a.example", 443, socket.AF_INET6),
    ("a.example", 443, socket.AF_INET, socket.SOCK_DGRAM),
    ("a.example", 443, socket.AF_INET, socket.SOCK_STREAM, 17),
    ("a.example", 443, socket.AF_INET, socket.SOCK_STREAM, 6, socket.AI_CANONNAME),
])
def test_every_argument_is_part_of_the_key(resolver, second) -> None:
    """getaddrinfo answers differently per family, port and flags. Keying on
    the host alone would hand an IPv6 answer to a caller asking for IPv4."""
    dns_cache.install()

    socket.getaddrinfo("a.example", 443, socket.AF_INET, socket.SOCK_STREAM, 6, 0)
    socket.getaddrinfo(*second)

    assert resolver.count() == 2, resolver.calls


# --------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------

def test_an_entry_expires(resolver) -> None:
    clock = Clock()
    dns_cache.install(ttl=600, clock=clock)

    socket.getaddrinfo("a.example", 443)
    clock.advance(599)
    socket.getaddrinfo("a.example", 443)
    assert resolver.count() == 1

    clock.advance(2)
    socket.getaddrinfo("a.example", 443)
    assert resolver.count() == 2


def test_the_default_ttl_spans_one_shard() -> None:
    """A worker process takes one shard at a time, so a host repeating inside
    a shard is the reuse there is to get. At the measured 575 URL/s per node
    over 32 processes, one 10,000-URL shard takes about 556 s.

    Pinned because a TTL under that quietly halves the hit rate, and nothing
    else in the system would show why.
    """
    assert dns_cache.DEFAULT_TTL >= 556
    assert dns_cache.DEFAULT_TTL <= 1800, "staleness beyond a task is not worth it"


# --------------------------------------------------------------------------
# Bounded and thread-safe
# --------------------------------------------------------------------------

def test_the_cache_does_not_grow_without_limit(resolver) -> None:
    """A task's URLs touch hundreds of thousands of hosts, across 32
    processes on a node also running img2dataset's own buffers."""
    dns_cache.install(maxsize=100)

    for i in range(500):
        socket.getaddrinfo(f"h{i}.example", 443)

    assert dns_cache.size() <= 100


def test_concurrent_lookups_are_safe_and_still_cache(resolver) -> None:
    """32 threads per process, and the same hosts come round again."""
    dns_cache.install()
    errors: list[BaseException] = []

    def work():
        try:
            for i in range(100):
                socket.getaddrinfo(f"h{i % 10}.example", 443)
        except BaseException as exc:   # noqa: BLE001 — recorded, then asserted
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # 10 distinct hosts, 1,600 lookups. A race can resolve one twice; a
    # broken cache resolves every time.
    assert resolver.count() < 100, resolver.count()


# --------------------------------------------------------------------------
# Installing and removing it
# --------------------------------------------------------------------------

def test_uninstall_restores_the_original(resolver) -> None:
    dns_cache.install()
    assert socket.getaddrinfo is not resolver

    dns_cache.uninstall()
    assert socket.getaddrinfo is resolver


def test_installing_twice_does_not_stack(resolver) -> None:
    """Two layers would double-count and make uninstall leave one behind."""
    dns_cache.install()
    dns_cache.install()
    dns_cache.uninstall()

    assert socket.getaddrinfo is resolver


def test_uninstalling_when_absent_is_harmless(resolver) -> None:
    dns_cache.uninstall()
    assert socket.getaddrinfo is resolver
