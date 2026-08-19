#!/usr/bin/env python3
"""How much connection reuse this corpus's URLs would allow.

  measure_host_concentration.py <upstream_metadata> [--window 10000]

img2dataset opens a TCP connection and a TLS session per image. Pooling them
helps only where URLs share a host, so the size of that lever is a property
of the corpus and can be read here before a single image is fetched — on a
login node, in seconds, without taking a node.

The number that matters is the reuse factor: URLs fetched per connection
opened, counted a shard at a time because that is what one worker's pool
sees. 1.0 means reuse saves nothing. 10 means nine connections in ten are
never made, against a wave that failed with 35.3% `Network is unreachable`
and 38.5% `timed out` while the 400 Gbps link sat at 0.005%.

The URL column is resolved per corpus rather than assumed, so this reads
COYO and Re-LAION as well as DataComp.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import dataset_schema as ds  # noqa: E402
from opendinov3.net import host_concentration as hc  # noqa: E402

#: Read whole files, but stop once this many URLs are in hand. The estimate
#: converges long before the corpus does, and the point of this script is
#: that it costs a login-node minute rather than a node-hour.
DEFAULT_URLS = 500_000


def read_urls(meta_dir: Path, files: int, limit: int) -> tuple[list[str], str]:
    """URLs from the first `files` parquet files, and the column they came from.

    Sorted path order, the same order plan_partition.py partitions in, so a
    prefix here is a prefix of the corpus the wave will actually fetch.
    """
    urls: list[str] = []
    column = ""
    paths = sorted(meta_dir.rglob("*.parquet"))
    for path in paths[:files] if files else paths:
        handle = pq.ParquetFile(path)
        if not column:
            column = ds.resolve(list(handle.schema_arrow.names)).url
        table = pq.read_table(path, columns=[column])
        urls.extend(str(v) for v in table.column(column).to_pylist())
        if len(urls) >= limit:
            break
    return urls[:limit], column


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("meta_dir", type=Path)
    parser.add_argument("--files", type=int, default=4,
                        help="parquet files to read; 0 reads every one")
    parser.add_argument("--urls", type=int, default=DEFAULT_URLS,
                        help="stop after this many URLs")
    parser.add_argument("--window", type=int, default=10_000,
                        help="shard size — one pool serves one shard, so "
                             "reuse cannot cross this boundary")
    parser.add_argument("--json", type=Path, help="also write the result here")
    args = parser.parse_args()

    if not args.meta_dir.is_dir():
        print(f"no such directory: {args.meta_dir}", file=sys.stderr)
        return 2

    urls, column = read_urls(args.meta_dir, args.files, args.urls)
    if not urls:
        print(f"no parquet files under {args.meta_dir}", file=sys.stderr)
        return 2

    result = hc.summarise(urls, window=args.window)

    print(f"url column      : {column}")
    print(f"urls read       : {result.urls:,}")
    print(f"distinct hosts  : {result.hosts:,}")
    if result.unparseable:
        print(f"unparseable     : {result.unparseable:,}")
    print()
    print(f"shard window    : {result.window:,} urls")
    print(f"connections     : {result.connections:,}")
    print(f"**reuse factor**: {result.reuse_factor:.2f} urls per connection")
    print(f"setups avoided  : {result.saved_fraction:.1%}")
    print()
    print("busiest hosts:")
    for host, count in result.top_hosts:
        print(f"  {count:>9,}  {host[:70]}")
    print()

    # Stated as a verdict because the alternative is that a reader takes a
    # ratio of 1.1 for encouragement. The thresholds are round numbers, not
    # measurements, and are labelled as such.
    if result.reuse_factor >= 2.0:
        print(f"→ pooling would avoid {result.saved_fraction:.0%} of connection")
        print("  setups. Worth enabling: OD_HTTP_POOL=1")
    else:
        print("→ URLs barely share hosts, so pooling has little to reuse.")
        print("  It will not be the lever that fixes the rate; the limit on")
        print("  concurrent connections is the thing to raise.")
    print("  (2.0 is a round number chosen for this message, not a measured")
    print("   threshold. The reuse factor above is the finding.)")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "url_column": column,
            "urls": result.urls,
            "hosts": result.hosts,
            "unparseable": result.unparseable,
            "window": result.window,
            "connections": result.connections,
            "reuse_factor": result.reuse_factor,
            "saved_fraction": result.saved_fraction,
            "top_hosts": result.top_hosts,
        }, indent=1))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
