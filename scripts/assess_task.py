#!/usr/bin/env python3
"""Decide whether a finished task is worth keeping, and say so in the exit code.

  assess_task.py <task dir> [--min-yield 0.30] [--max-dns 0.20]
                            [--max-unreachable 0.01] [--json health.json]

Exit 0 when the task should be marked done; non-zero otherwise.

WHY THE EXIT CODE MATTERS

On 2026-07-28 a day-long loss of connectivity produced 474 tasks that ran to
completion, wrote every shard's statistics, stored almost nothing, and
returned success. Nothing stopped. The exit code is what turns that into one
failed subjob instead of a lost run.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import shard_layout as sl  # noqa: E402
from opendinov3.core import task_health as th  # noqa: E402
from opendinov3.core.download_stats import RunSummary  # noqa: E402


def load_stats(task_dir: Path) -> list[dict]:
    found = []
    # `shards/` only, not rglob. A retry sets aside the previous attempt's
    # empty shards into `attempt-<tag>/` INSIDE the task directory, as
    # evidence; counting them would drag the yield down with data the task
    # deliberately discarded, and reject a healthy retry.
    # Either tree's layout: ours nests them under `shards/`, the
    # predecessor's leaves them in the task directory. Neither descends into
    # `attempt-<tag>/`, which is what the note above is about.
    for path in sl.stats_files(task_dir):
        try:
            found.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  unreadable, skipped: {path.name}: {exc}", file=sys.stderr)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dir", type=Path)
    parser.add_argument("--min-yield", type=float, default=th.MIN_YIELD)
    parser.add_argument("--max-dns", type=float, default=th.MAX_DNS_FRACTION)
    parser.add_argument("--max-unreachable", type=float,
                        default=th.MAX_UNREACHABLE_FRACTION)
    parser.add_argument("--json", type=Path, help="also write the verdict here")
    args = parser.parse_args()

    stats = load_stats(args.task_dir)
    if not stats:
        print(f"no shard statistics under {args.task_dir}: img2dataset wrote "
              "nothing, which is a failure rather than a task of yield zero",
              file=sys.stderr)
        return 2

    run = RunSummary.from_stats(processes=0, wall_seconds=0.0, stats=stats)
    health = th.assess(run, min_yield=args.min_yield, max_dns=args.max_dns,
                       max_unreachable=args.max_unreachable)

    print(f"shards      : {len(stats)}")
    print(f"candidates  : {run.candidates:,}")
    print(f"successes   : {run.successes:,}")
    print(f"yield       : {health.yield_rate:.1%}")
    print(f"DNS         : {health.dns_fraction:.1%}")
    print(f"unreachable : {health.unreachable_fraction:.1%}")
    print(f"thresholds  : yield >= {args.min_yield:.0%}, "
          f"DNS <= {args.max_dns:.0%}, "
          f"unreachable <= {args.max_unreachable:.0%}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            **asdict(health),
            "shards": len(stats),
            "candidates": run.candidates,
            "successes": run.successes,
            "thresholds": {"min_yield": args.min_yield,
                           "max_dns": args.max_dns,
                           "max_unreachable": args.max_unreachable},
        }, indent=1))

    if health.healthy:
        print("\nHEALTHY")
        return 0
    print(f"\nREJECTED: {health.reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
