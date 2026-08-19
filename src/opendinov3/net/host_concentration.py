"""How many connections a URL list would actually need.

Pooling helps only where URLs share a host. This reads that off the metadata
before anything is fetched, so the decision to enable reuse rests on the
corpus rather than on a hope about it.

THE KEY IS (SCHEME, HOST, PORT)

That is what urllib3 pools on. `http://a/x` and `https://a/y` do not share a
connection and neither do two ports on one host, so counting bare hostnames
would overstate the reuse — optimistic in exactly the direction that wastes
a wave.

IT IS COUNTED IN WINDOWS

The pool lives in a worker process and a process works through a shard, so
reuse is bounded by repetition *within* a shard. A host appearing once in
each of a thousand shards costs a thousand connections however concentrated
the corpus looks in total. Measuring the whole list at once would report
that as heavy concentration and be wrong by the size of the corpus.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit

#: Ports that are implied by the scheme, so `https://a` and `https://a:443`
#: are one pool rather than two.
_DEFAULT_PORT = {"http": 80, "https": 443}


@dataclass(frozen=True)
class Concentration:
    """What a URL list would cost in connections."""

    urls: int
    connections: int
    unparseable: int
    hosts: int
    top_hosts: list[tuple[str, int]]
    window: int

    @property
    def reuse_factor(self) -> float:
        """URLs fetched per connection opened. 1.0 means no reuse at all."""
        fetchable = self.urls - self.unparseable
        if not fetchable or not self.connections:
            return 1.0
        return fetchable / self.connections

    @property
    def saved_fraction(self) -> float:
        """Share of connection setups that reuse removes."""
        fetchable = self.urls - self.unparseable
        if not fetchable:
            return 0.0
        return 1.0 - (self.connections / fetchable)


def pool_key(url: str) -> str | None:
    """The pool a URL would be served from, or None if it names no host.

    Hostnames are case-insensitive, so they are folded; the path, query and
    credentials are not part of the connection and are dropped.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if not scheme or not host:
        return None
    try:
        port = parts.port
    except ValueError:      # a port that is not a number
        return None
    if port is None or port == _DEFAULT_PORT.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def summarise(urls, window: int, top: int = 10) -> Concentration:
    """Connections needed to fetch `urls`, counted `window` at a time.

    `window` should be the shard size, because that is what one pool sees.
    """
    urls = list(urls)
    keys: list[str | None] = [pool_key(u) for u in urls]
    unparseable = sum(1 for k in keys if k is None)

    connections = 0
    for start in range(0, len(keys), max(window, 1)):
        chunk = {k for k in keys[start:start + window] if k is not None}
        connections += len(chunk)

    counts = Counter(k for k in keys if k is not None)
    return Concentration(
        urls=len(urls),
        connections=connections,
        unparseable=unparseable,
        hosts=len(counts),
        top_hosts=counts.most_common(top),
        window=window,
    )
