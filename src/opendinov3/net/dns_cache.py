"""Answer a name the worker has already resolved, without asking again.

WHY

Going from 4 nodes to 8 did not exhaust connections — `unreachable` held at
0.1% — it exhausted the **resolver**. DNS failures rose from 6.2% to 76.6%,
yield fell from 64.0% to 17.4%, and the health guard rejected all 71 tasks
the wave produced.

The arithmetic is plain. At the measured 575 URL/s per node, eight nodes ask
the shared resolver for about 4,600 names a second, and `od.sh hosts`
measured 500,000 URLs spread over 118,834 hosts. Most of those questions
have already been answered.

WHY THIS AND NOT A RESOLVER DAEMON

The usual advice — run bind9 or knot-resolver on every node — needs a binary
in the image, a process to supervise, and a rebuild to change. This is a
dictionary in the process that already makes the calls. The repository is
bound into the container, so it ships with `git pull`.

The cost of that choice is honest and worth stating: the cache is
**per-process**. A node runs 32 of them, so a host can still be resolved up
to 32 times per node rather than once. The reduction is real but smaller
than a shared daemon would give.

FOUR THINGS IT MUST NOT DO

- **Cache a failure.** Under a saturated resolver a failure is transient by
  definition. Caching one turns a blip into a certainty for the whole TTL,
  for the host that was unlucky, at the moment the resolver is worst. It
  would deepen the problem it exists to fix.
- **Change the exception.** download_stats reads `[Errno -2]` out of
  `gaierror` to tell a DNS failure from anything else. Wrapping it moves 6%
  of every wave into `other` and the failure mix stops describing reality.
- **Key on the host alone.** `getaddrinfo` answers per family, port and
  flags. One key for all of them hands an IPv6 answer to an IPv4 caller.
- **Grow without limit.** A task's URLs touch hundreds of thousands of
  hosts, on a node already holding img2dataset's buffers.
"""

from __future__ import annotations

import socket
import threading
import time
from collections import OrderedDict

#: Seconds an answer is trusted.
#:
#: A worker process takes one shard at a time, so a host repeating inside a
#: shard is the reuse actually available. At the measured 575 URL/s per node
#: across 32 processes, a 10,000-URL shard takes about 556 s, and a TTL
#: below that would drop the tail of every shard for no reason. Above a task
#: (~37 min) the staleness stops buying anything.
#:
#: Pinned by test_the_default_ttl_spans_one_shard.
DEFAULT_TTL = 600.0

#: Entries kept. At 18 URL/s per process a TTL's worth of traffic is a few
#: thousand hosts, so this is headroom rather than a working limit — it is
#: here so a pathological corpus cannot exhaust the node.
DEFAULT_MAXSIZE = 100_000

#: How a worker reports its counts as it exits. One definition, because the
#: totals are parsed back out of the log and two spellings would make them
#: silently partial.
REPORT_PREFIX = "OD_DNS_CACHE_STATS"

_lock = threading.Lock()
_cache: OrderedDict[tuple, tuple] = OrderedDict()
_hits = 0
_misses = 0
_original = None
_ttl = DEFAULT_TTL
_maxsize = DEFAULT_MAXSIZE
_clock = time.monotonic


def size() -> int:
    """Entries currently held. For tests and for a bounds check."""
    with _lock:
        return len(_cache)


def stats() -> tuple[int, int]:
    """(hits, misses) since install.

    A miss is a question that reached the resolver, which is the quantity the
    site asked us to reduce. A failed lookup is a miss: it reached them.
    """
    with _lock:
        return _hits, _misses


def report_line(hits: int, misses: int) -> str:
    """The line a worker prints as it exits, and the only place its shape is
    written down."""
    return f"{REPORT_PREFIX} hits={hits} misses={misses}"


def report() -> str:
    return report_line(*stats())


def _lookup(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
    """`socket.getaddrinfo`, answered from the cache when it can be.

    Successes only. A raised `gaierror` is left to propagate exactly as the
    resolver raised it — same type, same errno, same message.
    """
    key = (host, port, family, type, proto, flags)
    now = _clock()

    with _lock:
        entry = _cache.get(key)
        if entry is not None:
            expires, value = entry
            if expires > now:
                _cache.move_to_end(key)
                global _hits          # noqa: PLW0603
                _hits += 1
                return value
            del _cache[key]
        global _misses                # noqa: PLW0603
        _misses += 1

    # Resolved outside the lock: a slow or hanging resolver must not stop
    # the other 31 threads from being served out of the cache.
    value = _original(host, port, family, type, proto, flags)

    with _lock:
        _cache[key] = (now + _ttl, value)
        _cache.move_to_end(key)
        while len(_cache) > _maxsize:
            _cache.popitem(last=False)
    return value


def install(ttl: float = DEFAULT_TTL, maxsize: int = DEFAULT_MAXSIZE,
            clock=time.monotonic) -> None:
    """Route `socket.getaddrinfo` through the cache.

    Patched at the socket layer rather than in one HTTP client, so it applies
    whether the run uses the pooled downloader or img2dataset's own urllib
    path. Installing twice is a no-op rather than a second layer: stacked
    wrappers would double-count and leave one behind on uninstall.
    """
    global _original, _ttl, _maxsize, _clock, _hits, _misses  # noqa: PLW0603
    _ttl, _maxsize, _clock = ttl, maxsize, clock
    with _lock:
        _cache.clear()
        _hits = _misses = 0
    if _original is not None:
        return
    _original = socket.getaddrinfo
    socket.getaddrinfo = _lookup


def uninstall() -> None:
    """Put the original back. Harmless when it was never installed."""
    global _original  # noqa: PLW0603
    if _original is None:
        return
    socket.getaddrinfo = _original
    _original = None
    with _lock:
        _cache.clear()
