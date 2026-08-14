#!/usr/bin/env python3
"""Measure bytes per stored image across tasks, and say whether tasks differ.

Reads `_stats.json` and the size of the tar beside it. No downloads, no
decoding, no job — it runs on a login node in seconds.

  measure_image_size.py <raw_shards dir> [--tasks 12] [--shards 5]

Answers G2: the recorded per-image sizes span 19.7–116 KB, each from a single
observation, and the storage estimate for ~735 remaining tasks depends on
which is typical. The test is spread within a task against spread between
tasks — a threefold difference between tasks means nothing if shards inside
one task already differ threefold.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import shard_size as ss  # noqa: E402


def read_task(task_dir: Path, limit: int) -> ss.TaskSize:
    shards: list[ss.ShardSize] = []
    for stats_path in sorted(task_dir.glob("*_stats.json"))[:limit]:
        tar_path = stats_path.with_name(
            stats_path.name.replace("_stats.json", ".tar"))
        if not tar_path.is_file():
            continue
        try:
            stats = json.loads(stats_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  skipped {stats_path.name}: {exc}", file=sys.stderr)
            continue
        shards.append(ss.ShardSize(
            name=stats_path.name.split("_")[0],
            tar_bytes=tar_path.stat().st_size,
            successes=int(stats.get("successes", 0)),
        ))
    return ss.summarise_task(task_dir.name, shards)


def kb(value: float | None) -> str:
    return "—" if value is None else f"{value / 1024:.1f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="directory holding task-* dirs")
    parser.add_argument("--tasks", type=int, default=12,
                        help="how many tasks to sample")
    parser.add_argument("--shards", type=int, default=5,
                        help="how many shards per task (they are large)")
    args = parser.parse_args()

    task_dirs = sorted(d for d in args.root.glob("task-*") if d.is_dir())
    if not task_dirs:
        print(f"no task-* directories under {args.root}", file=sys.stderr)
        return 2

    sampled = task_dirs[:args.tasks]
    print(f"root   : {args.root}")
    print(f"tasks  : {len(sampled)} of {len(task_dirs)} "
          f"(first {args.shards} shards each)")
    print()

    tasks = [read_task(d, args.shards) for d in sampled]

    print(f"{'task':<16} {'shards':>7} {'images':>10} {'KB/img':>8} "
          f"{'min':>8} {'max':>8} {'spread':>7}")
    print("-" * 70)
    for task in tasks:
        print(f"{task.name:<16} {task.usable_shards:>7} {task.successes:>10} "
              f"{kb(task.bytes_per_image):>8} "
              f"{kb(task.min_bytes_per_image):>8} "
              f"{kb(task.max_bytes_per_image):>8} "
              f"{('—' if task.spread is None else f'{task.spread:.2f}×'):>7}")

    report = ss.compare_tasks(tasks)
    if report.tasks_compared == 0:
        print("\nno task had a usable shard", file=sys.stderr)
        return 1

    print()
    print(f"tasks compared : {report.tasks_compared}"
          + (f" ({report.tasks_skipped} skipped, no usable shard)"
             if report.tasks_skipped else ""))
    print(f"smallest task  : {report.smallest.name} "
          f"{kb(report.smallest.bytes_per_image)} KB/img")
    print(f"largest task   : {report.largest.name} "
          f"{kb(report.largest.bytes_per_image)} KB/img")
    print(f"between tasks  : {report.between_spread:.2f}×")
    print(f"within a task  : up to {report.worst_within_spread:.2f}× "
          "(worst shard-to-shard spread inside one task)")
    print()

    if report.between_exceeds_within:
        print("→ Tasks differ by MORE than shards inside a task do. That needs "
              "a task-level explanation, and the storage estimate has to use a "
              "distribution rather than one number.")
    else:
        print("→ Tasks differ by LESS than shards inside a task already do. "
              "The recorded 3× spread is ordinary shard-to-shard variation; "
              "no task-level explanation is required.")

    total = report.largest.bytes_per_image
    print()
    print("Storage for 735 remaining tasks at 1,000,000 URLs and 64.5% yield:")
    for label, value in (("smallest", report.smallest.bytes_per_image),
                         ("largest", report.largest.bytes_per_image)):
        tb = 735 * 1_000_000 * 0.645 * value / 1e12
        print(f"  at the {label} observed size ({kb(value)} KB): {tb:.0f} TB")
    del total
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
