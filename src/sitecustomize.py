"""Apply this project's network patches in every worker process.

WHY IT HAS TO BE HERE AND NOT IN THE WRAPPER

img2dataset's distributor calls `get_context("spawn")`. A spawned child does
not inherit the parent's memory — it starts a fresh interpreter and imports
everything again — so a monkeypatch applied in the wrapper reaches the
parent, which downloads nothing, and never reaches the 32 workers, which
download everything.

Connection pooling shipped exactly that way and did nothing in production.
Measured:

    parent : opendinov3.net.dns_cache._lookup
    child  : socket.getaddrinfo

Python imports `sitecustomize` at interpreter startup from anything on
`sys.path`, so every spawned child runs this, including the replacements
`maxtasksperchild=5` creates part-way through a shard. The wrapper puts this
directory on `PYTHONPATH` for img2dataset and its children only, so no other
process in the job pays for it.

WHY IT CANNOT RAISE

This runs before the interpreter is fully up. An exception here does not
degrade one download, it kills every worker in the wave. So each step is
guarded and a failure falls back to upstream behaviour — but says so on
stderr, because a silent no-op is precisely how the pooling defect survived
review.
"""

from __future__ import annotations

import os
import sys


def _enabled(name: str) -> bool:
    """Only an explicit `1`.

    Unlike OD_BLUR_FACES, which refuses anything but 0 or 1 because blurring
    cannot be undone, these are reversible: the next wave without them is
    back to upstream. So an unset or unrecognised value is off rather than a
    hard error, which also keeps every unrelated Python process that happens
    to see this directory cheap.
    """
    return os.environ.get(name, "0") == "1"


def _warn(message: str) -> None:
    print(f"⚠️  sitecustomize: {message}", file=sys.stderr)


def _install_dns_cache() -> None:
    from opendinov3.net import dns_cache

    ttl = dns_cache.DEFAULT_TTL
    raw = os.environ.get("OD_DNS_CACHE_TTL")
    if raw:
        try:
            ttl = float(raw)
        except ValueError:
            _warn(f"OD_DNS_CACHE_TTL={raw!r} is not a number; "
                  f"using the default {dns_cache.DEFAULT_TTL:.0f}s")
    dns_cache.install(ttl=ttl)


def _install_connection_pool() -> None:
    from opendinov3.net import pooled_download

    pooled_download.install()


def _main() -> None:
    for variable, install in (("OD_DNS_CACHE", _install_dns_cache),
                              ("OD_HTTP_POOL", _install_connection_pool)):
        if not _enabled(variable):
            continue
        try:
            install()
        except Exception as err:   # noqa: BLE001 — must not kill the worker
            _warn(f"{variable}=1 but the patch did not install ({err!r}); "
                  "continuing without it")


_main()
