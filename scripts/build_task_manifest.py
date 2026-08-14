#!/usr/bin/env python3
"""Build one task's URL list from the shared plan.

Run by every array subjob, on a compute node, with no operator watching. The
plan says which rows of which upstream files belong to this task; this turns
that into the parquet img2dataset reads.

  build_task_manifest.py --plan plan.json --task-id 42 --output urls.parquet

Writes atomically: a truncated parquet left by a failed attempt would be read
by the next one as a real manifest, and the task would quietly download a
fraction of its URLs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pyarrow.parquet as pq  # noqa: E402

from opendinov3.core import task_manifest as tm  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        plan = json.loads(args.plan.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read plan {args.plan}: {exc}", file=sys.stderr)
        return 2

    entries = {int(t["task_id"]): t for t in plan.get("tasks", [])}
    if args.task_id not in entries:
        print(
            f"task {args.task_id} is not in the plan, which holds "
            f"{len(entries)} tasks. The array range and the plan disagree; "
            "one of them is wrong and guessing would shorten the corpus.",
            file=sys.stderr,
        )
        return 2

    entry = entries[args.task_id]
    pieces = [(p["path"], int(p["start"]), int(p["end"])) for p in entry["pieces"]]

    try:
        table = tm.build_manifest(pieces)
    except tm.ManifestError as exc:
        print(f"task {args.task_id}: {exc}", file=sys.stderr)
        return 1

    expected = int(entry.get("rows", table.num_rows))
    if table.num_rows != expected:
        print(f"task {args.task_id}: plan says {expected} rows, extraction "
              f"produced {table.num_rows}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename: rename is atomic within a
    # filesystem, so no reader ever sees a partial file.
    temporary = args.output.with_suffix(args.output.suffix + ".partial")
    try:
        pq.write_table(table, temporary)
        os.replace(temporary, args.output)
    except Exception as exc:  # noqa: BLE001 — must not leave a partial file
        temporary.unlink(missing_ok=True)
        print(f"task {args.task_id}: cannot write {args.output}: {exc}",
              file=sys.stderr)
        return 1

    print(f"task {args.task_id}: {table.num_rows:,} URLs -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
