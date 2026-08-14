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


def resolve_source(recorded: str) -> str:
    """Find the file the plan means, under this container's mount layout.

    A plan records the absolute path of every parquet it read. od.sh runs
    the planner inside the container with the corpus bound at /corpus, so
    plans come out holding /corpus/... paths. The production job binds the
    metadata at its HOST path instead (`--bind "${OD_META_ROOT}:${OD_META_ROOT}"`)
    and does not bind /corpus at all, so those paths do not exist there.

    A plan is a description of the data, not of one machine's mount table.
    When the recorded path is absent, the longest trailing run of components
    that is unique under OD_META_ROOT is used.

    Matching on the basename alone would be wrong: shards share basenames
    across subdirectories, so `b/part-00000.parquet` would silently pair
    with `a/part-00000.parquet` and the task would download another shard's
    URLs while every count still looked right.
    """
    root = os.environ.get("OD_META_ROOT")

    # Current plans record the path relative to the metadata root, so there
    # is exactly one place it can be and nothing to search for.
    if not os.path.isabs(recorded):
        if not root:
            raise FileNotFoundError(
                f"the plan records a relative source ({recorded}) and "
                "OD_META_ROOT is not set.\n"
                "   It is the directory the metadata lives under."
            )
        joined = os.path.join(root, recorded)
        if os.path.exists(joined):
            return joined
        raise FileNotFoundError(
            f"source is missing: {joined}\n"
            f"   (plan says {recorded!r}, OD_META_ROOT={root})"
        )

    # Older plans hold absolute paths from whatever mount planned them.
    if os.path.exists(recorded):
        return recorded
    if not root:
        raise FileNotFoundError(
            f"source is missing: {recorded}\n"
            "   The plan was written under a different mount layout. Set "
            "OD_META_ROOT to the directory\n"
            "   the metadata lives under so the path can be rebased."
        )

    parts = Path(recorded).parts
    # Longest suffix first: the most specific match wins.
    for depth in range(len(parts), 0, -1):
        candidate = Path(root, *parts[-depth:])
        if candidate.exists():
            return str(candidate)

    raise FileNotFoundError(
        f"source is missing: {recorded}\n"
        f"   Nothing matching it under OD_META_ROOT={root}\n"
        "   Either the plan is for a different corpus, or OD_META_ROOT "
        "points at the wrong tree."
    )


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
    try:
        pieces = [(resolve_source(p["path"]), int(p["start"]), int(p["end"]))
                  for p in entry["pieces"]]
    except FileNotFoundError as exc:
        print(f"task {args.task_id}: {exc}", file=sys.stderr)
        return 1
    for path, _, _ in pieces:
        print(f"source: {path}")

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
