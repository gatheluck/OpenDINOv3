#!/usr/bin/env python3
"""Create a separate plan for smaller, not-yet-submitted tasks.

Stop concurrent submitters and reconcile saved/live jobs before selecting the
boundary. The directory guard cannot detect queued jobs with no output yet.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opendinov3.core.metadata_partition import TaskSlice, repartition_tail


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--first-task", type=int, required=True)
    parser.add_argument("--urls-per-task", type=int, default=100_000)
    parser.add_argument("--task-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if not args.task_root.is_dir():
            raise ValueError("task root must be an existing directory")
        for path in args.task_root.glob("task-*"):
            suffix = path.name.removeprefix("task-")
            if not suffix.isdigit() or int(suffix) >= args.first_task:
                raise ValueError(f"existing or unrecognized task output: {path.name}")
        raw = args.plan.read_bytes()
        plan = json.loads(raw)
        tasks = [
            TaskSlice(
                t["task_id"],
                tuple((p["path"], p["start"], p["end"]) for p in t["pieces"]),
                t["rows"],
            )
            for t in plan["tasks"]
        ]
        if sum(t.rows for t in tasks) != plan["total_rows"]:
            raise ValueError("plan total differs from sum of task rows")
        result = repartition_tail(
            tasks, first_task=args.first_task, urls_per_task=args.urls_per_task
        )
        updated = dict(plan)
        updated["urls_per_task"] = args.urls_per_task
        # Preserve even extra prefix fields verbatim; they belong to old jobs.
        updated["tasks"] = plan["tasks"][: args.first_task] + [
            {
                "task_id": t.task_id,
                "rows": t.rows,
                "pieces": [{"path": p, "start": s, "end": e} for p, s, e in t.pieces],
            }
            for t in result[args.first_task :]
        ]
        updated["repartition"] = {
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "first_task": args.first_task,
            "urls_per_task": args.urls_per_task,
        }
        serialized = json.dumps(updated, indent=2) + "\n"
        with args.output.open("x") as output:
            output.write(serialized)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"repartition refused: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
