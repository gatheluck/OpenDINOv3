#!/usr/bin/env python3
"""Report observed getaddrinfo calls and application-cache effectiveness.

  dns_report.py <task root> [--tasks 20]

Includes unfinished tasks. Exit reports may be missing even when the cache
was enabled, so coverage is unverified and absent counters are unknown.
This diagnostic neither measures wire DNS QPS nor enforces a rate limit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import shard_layout as sl
from opendinov3.net import dns_report as dr


def activity_time(task: Path) -> float:
    """Use available evidence, including logs from failed/unfinished tasks."""
    return max(
        path.stat().st_mtime
        for path in (task, task / "DONE.json", task / "img2dataset.log")
        if path.exists()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_root", type=Path)
    parser.add_argument(
        "--tasks",
        type=int,
        default=20,
        help="most recently modified tasks to read, including unfinished tasks",
    )
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.tasks <= 0:
        parser.error("--tasks must be positive")

    tasks = sorted(
        sl.task_dirs(args.task_root), key=lambda t: (activity_time(t), t.name)
    )
    if not tasks:
        print(f"no tasks under {args.task_root}", file=sys.stderr)
        return 2

    rows = []
    for task in tasks[-args.tasks :]:
        summary = dr.summarise(task)
        rows.append(
            {
                "task": task.name,
                "measurement_status": summary.measurement_status,
                "reports": summary.reports,
                "observed_lookups": summary.lookups if summary.reports else None,
                "observed_resolver_calls": summary.misses if summary.reports else None,
                "observed_cache_hit_fraction": summary.cache_hit_fraction,
                "wall_seconds": summary.wall_seconds,
                "observed_resolver_calls_per_second": summary.observed_resolver_calls_per_second,
            }
        )

    payload = {
        "metric": "getaddrinfo_calls_after_cache_miss",
        "rate_scope": "task_wall_time_average_of_observed_counts",
        "coverage": "unverified",
        "tasks": rows,
    }
    print(f"tasks read: {len(rows)}")
    print("Metric: observed getaddrinfo calls after application-cache misses.")
    print(
        "Coverage unverified: exit reports can be lost, including on forced termination."
    )
    print("Unknown does not mean cache disabled or zero calls.")
    print(
        "Rates are task-wall-time averages of observed counts, not wire DNS QPS or peaks."
    )
    print("This report cannot establish compliance with a DNS QPS limit.")
    print()
    for row in rows:
        calls = row["observed_resolver_calls"]
        rate = row["observed_resolver_calls_per_second"]
        hit = row["observed_cache_hit_fraction"]
        rate_text = "unknown" if rate is None else f"{rate:.1f}"
        hit_text = "unknown" if hit is None else f"{hit:.1%}"
        print(
            f"{row['task']}: {row['measurement_status']}; reports={row['reports']}; "
            f"observed calls={calls if calls is not None else 'unknown'}; "
            f"observed calls/s={rate_text}; observed cache hit fraction={hit_text}"
        )

    # Write even if every measurement is unknown, replacing any older report.
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
