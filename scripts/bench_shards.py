#!/usr/bin/env python3
"""Measure shard read-and-decode throughput.

This is the runner for a measurement experiment, not a test. It is
non-deterministic and its result depends on the machine, the filesystem and
what else is running, so it never gates CI. Record the conditions alongside
the numbers.

Usage:
    python scripts/bench_shards.py <dir-or-tar> [--limit N] [--json OUT]

Examples:
    python scripts/bench_shards.py /data/task-000688 --limit 3
    python scripts/bench_shards.py /data/task-000688/00000.tar

Scope, repeated here because a throughput figure detached from its scope is
easy to misread: this measures tar read plus image decode. It does not
include the collation and inter-process overhead a torch DataLoader adds.
The figure is a ceiling, not a prediction of training throughput.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opendinov3.core import bench  # noqa: E402


def find_shards(target: Path, limit: int | None) -> list[Path]:
    """Shards in deterministic order so repeated runs are comparable."""
    if target.is_file():
        return [target]
    shards = sorted(target.glob("*.tar"))
    return shards[:limit] if limit else shards


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path,
                        help="directory containing shards, or one .tar")
    parser.add_argument("--limit", type=int, default=None,
                        help="measure at most this many shards")
    parser.add_argument("--json", type=Path, default=None,
                        help="also write the result as JSON")
    args = parser.parse_args()

    if not args.target.exists():
        print(f"not found: {args.target}", file=sys.stderr)
        return 2

    shards = find_shards(args.target, args.limit)
    if not shards:
        print(f"no .tar shards under {args.target}", file=sys.stderr)
        return 2

    print(f"machine       : {platform.node()} / {platform.machine()}")
    print(f"python        : {platform.python_version()}")
    print(f"shards        : {len(shards)}")
    print()
    print(f"{'shard':<24}{'samples':>9}{'fail':>7}{'samples/s':>12}{'MB/s':>10}")
    print("-" * 62)

    results = []
    for path in shards:
        r = bench.measure_shard(path)
        results.append(r)
        sps = f"{r.samples_per_sec:,.0f}" if r.samples_per_sec else "-"
        mbs = f"{r.bytes_per_sec / 1e6:,.1f}" if r.bytes_per_sec else "-"
        print(f"{path.name:<24}{r.samples:>9,}{r.decode_failures:>7,}"
              f"{sps:>12}{mbs:>10}")

    total = bench.aggregate(results)
    print("-" * 62)
    sps = f"{total.samples_per_sec:,.0f}" if total.samples_per_sec else "-"
    mbs = f"{total.bytes_per_sec / 1e6:,.1f}" if total.bytes_per_sec else "-"
    print(f"{'TOTAL':<24}{total.samples:>9,}{total.decode_failures:>7,}"
          f"{sps:>12}{mbs:>10}")

    rate = total.decode_failure_rate
    print()
    print(f"elapsed            : {total.elapsed_sec:,.1f} s")
    print(f"decode failure rate: "
          f"{f'{rate:.3%}' if rate is not None else 'n/a'}")
    print()
    print("Scope: tar read + image decode only. Excludes DataLoader collation")
    print("and inter-process transfer. Treat as a ceiling.")

    if args.json:
        args.json.write_text(json.dumps({
            "machine": platform.node(),
            "python": platform.python_version(),
            "shard_count": len(shards),
            "samples": total.samples,
            "decode_failures": total.decode_failures,
            "bytes_read": total.bytes_read,
            "elapsed_sec": total.elapsed_sec,
            "samples_per_sec": total.samples_per_sec,
            "bytes_per_sec": total.bytes_per_sec,
            "decode_failure_rate": rate,
            "scope": "tar read + PIL decode; excludes DataLoader overhead",
            "per_shard": [
                {"shard": p.name, "samples": r.samples,
                 "decode_failures": r.decode_failures,
                 "elapsed_sec": r.elapsed_sec}
                for p, r in zip(shards, results)
            ],
        }, indent=2))
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
