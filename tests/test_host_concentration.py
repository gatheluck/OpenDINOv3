"""Contract for predicting how much connection reuse a URL list allows.

Pooling only helps when URLs share a host. If every URL in a shard is on a
different host, a pool holds one connection per URL and reuse saves nothing;
if a shard's ten thousand URLs sit on a thousand hosts, nine in ten
connections never have to be made.

That ratio can be read off the metadata before a single image is fetched,
which is cheaper than inferring it from a wave's throughput afterwards.

WHY THE KEY IS (SCHEME, HOST, PORT)

That is what urllib3 pools on. `http://a/x` and `https://a/y` do not share a
connection, and neither do two ports on one host. Counting bare hostnames
would overstate the reuse and the estimate would be optimistic in exactly
the direction that wastes a wave.

WHY IT IS MEASURED IN WINDOWS

The pool lives in a worker process, and a process works through a shard.
Reuse is therefore bounded by repetition *within* a shard, not across the
corpus: a host appearing once in each of a thousand shards is a thousand
connections however concentrated the corpus looks in total.
"""

from __future__ import annotations

from opendinov3.net import host_concentration as hc


def test_every_url_on_its_own_host_allows_no_reuse() -> None:
    urls = [f"https://h{i}.example/img.jpg" for i in range(100)]
    result = hc.summarise(urls, window=100)

    assert result.connections == 100
    assert result.reuse_factor == 1.0
    assert result.saved_fraction == 0.0


def test_one_host_serving_everything_is_one_connection() -> None:
    urls = [f"https://one.example/{i}.jpg" for i in range(100)]
    result = hc.summarise(urls, window=100)

    assert result.connections == 1
    assert result.reuse_factor == 100.0
    assert result.saved_fraction == 0.99


def test_the_path_and_query_do_not_make_a_new_connection() -> None:
    urls = ["https://a.example/one.jpg?v=1", "https://a.example/two.jpg"]
    assert hc.summarise(urls, window=2).connections == 1


def test_the_scheme_does_make_a_new_connection() -> None:
    """urllib3 pools per scheme; http and https to one host are two pools.

    Counting bare hostnames would call this one connection and overstate the
    reuse — optimistic in the direction that costs a wave.
    """
    urls = ["http://a.example/x.jpg", "https://a.example/y.jpg"]
    assert hc.summarise(urls, window=2).connections == 2


def test_an_explicit_port_makes_a_new_connection() -> None:
    urls = ["https://a.example/x.jpg", "https://a.example:8443/y.jpg"]
    assert hc.summarise(urls, window=2).connections == 2


def test_the_default_port_is_not_a_different_host() -> None:
    """`https://a` and `https://a:443` are the same pool."""
    urls = ["https://a.example/x.jpg", "https://a.example:443/y.jpg"]
    assert hc.summarise(urls, window=2).connections == 1


def test_host_case_does_not_split_a_pool() -> None:
    """Hostnames are case-insensitive; treating them otherwise would
    undercount reuse."""
    urls = ["https://A.Example/x.jpg", "https://a.example/y.jpg"]
    assert hc.summarise(urls, window=2).connections == 1


# --------------------------------------------------------------------------
# Windows, because the pool does not outlive the shard
# --------------------------------------------------------------------------

def test_repetition_across_windows_does_not_count_as_reuse() -> None:
    """A host appearing once per shard is one connection per shard.

    Measured over the whole list this looks like heavy concentration — two
    hosts, a hundred URLs. Measured as the pool actually works, it is no
    reuse at all, and a wave planned on the first number would disappoint.
    """
    urls = []
    for _ in range(50):
        urls += ["https://a.example/x.jpg", "https://b.example/y.jpg"]

    assert hc.summarise(urls, window=2).connections == 100
    assert hc.summarise(urls, window=2).reuse_factor == 1.0
    # The whole list at once would have said otherwise.
    assert hc.summarise(urls, window=100).connections == 2


def test_a_trailing_part_window_is_still_counted() -> None:
    urls = [f"https://one.example/{i}.jpg" for i in range(5)]
    result = hc.summarise(urls, window=2)

    assert result.urls == 5
    # Three windows: two of two, one of one. One connection each.
    assert result.connections == 3


def test_the_busiest_hosts_are_reported_for_a_sanity_check() -> None:
    """A single CDN carrying the corpus is a different situation from broad
    concentration, and the numbers alone do not distinguish them."""
    urls = ["https://big.example/x.jpg"] * 10 + ["https://s.example/y.jpg"]
    result = hc.summarise(urls, window=100)

    assert result.top_hosts[0] == ("https://big.example", 10)


def test_an_unparseable_url_is_counted_not_dropped() -> None:
    """Silently skipping them would flatter the estimate."""
    result = hc.summarise(["not a url at all", "https://a.example/x.jpg"],
                          window=100)

    assert result.urls == 2
    assert result.unparseable == 1


def test_no_urls_is_not_a_division_by_zero() -> None:
    result = hc.summarise([], window=100)
    assert result.urls == 0
    assert result.connections == 0
    assert result.reuse_factor == 1.0
