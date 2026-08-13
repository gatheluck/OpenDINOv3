#!/usr/bin/env python3
"""Report the concurrency a configuration can actually reach, and refuse it
if that differs from what it asks for.

img2dataset gives each process one shard at a time, so a level asking for
more processes than there are shards runs at the shard count instead. Nothing
in the output says so. This is the check that keeps a run from recording
"8 → 64" while measuring "8 → 20".

  plan_experiment_0002.py --slice 200000 --samples-per-shard 1000 \
      --levels "8 32 64 8"

Exit 0 when the configuration measures what it claims, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import concurrency_plan as cp  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slice", type=int, required=True,
                        help="URLs per level")
    parser.add_argument("--samples-per-shard", type=int, required=True)
    parser.add_argument("--levels", required=True,
                        help='process counts, e.g. "8 32 64 8"')
    parser.add_argument("--min-waves", type=int, default=cp.DEFAULT_MIN_WAVES,
                        help="shards each process should get through")
    args = parser.parse_args()

    levels = [int(v) for v in args.levels.split()]
    planned = cp.plan(args.slice, args.samples_per_shard, levels)

    print(cp.describe(planned))

    problems = cp.validate(planned, min_waves=args.min_waves)
    if not problems:
        return 0

    print()
    for problem in problems:
        print(f"  ✗ {problem}", file=sys.stderr)

    try:
        suggested = cp.suggest_samples_per_shard(
            args.slice, levels, args.min_waves
        )
        print(
            f"\n  Set OD_SAMPLES_PER_SHARD to at most {suggested} "
            f"(currently {args.samples_per_shard}).",
            file=sys.stderr,
        )
    except ValueError as exc:
        print(f"\n  {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
