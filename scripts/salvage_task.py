#!/usr/bin/env python3
"""Mark an already-downloaded task complete, without downloading it again.

  salvage_task.py <task dir> [<task dir> ...]

WHY

A wave of eight tasks finished, stored about 620,000 images each at a 62%
yield, and was rejected by an unreachable threshold measured on one and two
nodes. The images are on disk. Re-downloading a million URLs per task to
recover a marker file would cost a node-hour each and would replace good
images with whatever the web returns today.

WHAT IT WILL NOT DO

It re-runs the SAME health check the task already ran and writes DONE.json
only if that check now passes. There is no force flag. Salvaging a task the
guard still rejects would make the guard optional, and the guard exists
because 474 tasks were once lost to output that looked finished.

The marker records `salvaged: true` and leaves `wall_seconds` null. The
elapsed time is not recoverable after the fact, and an estimate presented as
a measurement would quietly corrupt the throughput record that the wave
sizing depends on.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import download_stats as ds  # noqa: E402
from opendinov3.core import task_health as th  # noqa: E402


def salvage(task_dir: Path) -> tuple[bool, str]:
    """Returns (ok, message). Writes DONE.json only when healthy."""
    if not task_dir.is_dir():
        return False, f"{task_dir}: no such directory"

    marker = task_dir / "DONE.json"
    if marker.is_file():
        return True, f"{task_dir.name}: already complete, left alone"

    stats = sorted((task_dir / "shards").glob("*_stats.json"))
    if not stats:
        return False, (f"{task_dir.name}: no shard statistics — the task "
                       "did not get far enough to be judged")

    bodies = []
    for path in stats:
        try:
            bodies.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"{task_dir.name}: cannot read {path.name}: {exc}"

    run = ds.RunSummary.from_stats(0, 0.0, bodies)
    verdict = th.assess(run)
    if not verdict.healthy:
        return False, f"{task_dir.name}: still rejected — {verdict.reason}"

    try:
        task_id = int(task_dir.name.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return False, f"{task_dir.name}: cannot read a task id from the name"

    marker.write_text(json.dumps({
        "task_id": task_id,
        "completed_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        # Not recoverable after the fact. Null rather than an estimate, so
        # nothing reads it as a measurement.
        "wall_seconds": None,
        "salvaged": True,
        "candidates": run.candidates,
        "successes": run.successes,
        "yield": verdict.yield_rate,
        "partial": False,
        "planned_candidates": run.candidates,
    }, indent=1))
    return True, (f"{task_dir.name}: marked done — {run.successes:,} of "
                  f"{run.candidates:,} ({verdict.yield_rate:.1%})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dirs", type=Path, nargs="+")
    args = parser.parse_args()

    failures = 0
    for task_dir in args.task_dirs:
        ok, message = salvage(task_dir)
        print(("  " if ok else "❌ ") + message)
        if not ok:
            failures += 1

    if failures:
        print(f"\n{failures} of {len(args.task_dirs)} could not be salvaged. "
              "They stay unmarked, so a later wave retries them.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
