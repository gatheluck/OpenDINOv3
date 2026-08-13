#!/usr/bin/env python3
"""Report what experiment 0003's configuration would actually measure.

Holds total concurrency fixed and varies only how it is spread across nodes.
Refuses a configuration where the comparison would not mean what it claims —
a level capped by shard count, too few shards per process, or shard
granularity that differs between the two configurations.

  plan_experiment_0003.py --total 32 --slice 200000 \
      --samples-per-shard 1000 --nodes "1 2"

Exit 0 when the configuration is comparable, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import node_plan as np  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--total", type=int, required=True,
                        help="total processes, held constant")
    parser.add_argument("--slice", type=int, required=True,
                        help="URLs per phase, held constant")
    parser.add_argument("--samples-per-shard", type=int, required=True)
    parser.add_argument("--nodes", required=True, help='node counts, e.g. "1 2"')
    parser.add_argument("--min-waves", type=int, default=np.DEFAULT_MIN_WAVES)
    args = parser.parse_args()

    node_counts = [int(v) for v in args.nodes.split()]
    try:
        configs = np.plan_distribution(
            args.total, args.slice, args.samples_per_shard, node_counts
        )
    except ValueError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 1

    print(f"{'config':>10} {'proc/node':>10} {'urls/node':>11} "
          f"{'shards':>8} {'shards/proc':>12}")
    print("-" * 56)
    for config in configs:
        print(f"{config.label:>10} {config.processes_per_node:>10} "
              f"{config.urls_per_node:>11,} {config.shards_per_node:>8} "
              f"{config.waves:>12.2f}")

    problems = np.validate_distribution(configs, min_waves=args.min_waves)
    if not problems:
        print(f"\n  total processes held at {args.total} in every "
              "configuration; shards per process match.")
        return 0

    print()
    for problem in problems:
        print(f"  ✗ {problem}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
