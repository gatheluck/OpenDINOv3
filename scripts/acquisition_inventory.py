#!/usr/bin/env python3
"""Read canonical task statistics without treating markers as proof of reuse.

Print JSON to stdout; never modify the inspected tree. Missing task IDs are
reported separately. Counts are records, not unique images. Payload integrity
and input-range correspondence require a separate validation before reuse.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opendinov3.core.download_stats import RunSummary
from opendinov3.core.shard_layout import stats_files
from opendinov3.core.task_health import assess


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--first-task", required=True, type=int)
    parser.add_argument("--last-task", required=True, type=int)
    args = parser.parse_args()
    if args.first_task < 0 or args.last_task < args.first_task:
        parser.error("invalid canonical task range")
    errors: list[str] = []
    tasks: list[dict] = []
    excluded: list[str] = []
    seen: set[int] = set()
    if not args.root.is_dir():
        errors.append("task root does not exist or is not a directory")
    for task in sorted(args.root.glob("task-*")):
        match = re.fullmatch(r"task-(\d{6})", task.name)
        if (
            not task.is_dir()
            or not match
            or not args.first_task <= int(match[1]) <= args.last_task
        ):
            excluded.append(task.name)
            continue
        tid = int(match[1])
        seen.add(tid)
        stats: list[dict] = []
        for path in stats_files(task):
            try:
                stat = json.loads(path.read_text())
                keys = ("count", "successes", "failed_to_download", "failed_to_resize")
                if any(type(stat[k]) is not int or stat[k] < 0 for k in keys):
                    raise ValueError("invalid counts")
                if sum(stat[k] for k in keys[1:]) != stat["count"]:
                    raise ValueError("statistics arithmetic mismatch")
                statuses = stat["status_dict"]
                if not isinstance(statuses, dict) or any(
                    type(v) is not int or v < 0 for v in statuses.values()
                ):
                    raise ValueError("invalid status counts")
                stats.append(stat)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"{task.name}/{path.name}: {exc}")
        run = RunSummary.from_stats(0, 0.0, stats)
        tasks.append(
            {
                "task_id": tid,
                "shards": len(stats),
                "candidates": run.candidates,
                "successes": run.successes,
                "done_marker_present": (task / "DONE.json").is_file(),
                **asdict(assess(run)),
            }
        )
    print(
        json.dumps(
            {
                "complete_scan": not errors,
                "errors": errors,
                "tasks": tasks,
                "missing_task_ids": sorted(
                    set(range(args.first_task, args.last_task + 1)) - seen
                ),
                "excluded_directories": excluded,
                "candidates": sum(t["candidates"] for t in tasks),
                "successes": sum(t["successes"] for t in tasks),
                "reuse_verified": False,
            },
            indent=2,
        )
    )
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
