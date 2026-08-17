#!/usr/bin/env python3
"""Report the scale of a corpus before partitioning or downloading it.

Reads parquet *footers* only — `num_rows` from each file's metadata — so a
1.3-billion-row corpus is measured in a few thousand header reads rather than
hundreds of gigabytes of memory. It runs on a login node in under a minute
and needs no job.

The predecessor's partitioner had to load every row into pandas before it
could say how many tasks there would be, which is why that answer required a
1,920 GB node.

  plan_partition.py <upstream_metadata dir> [--urls-per-task 1000000]
                    [--json plan.json]

Writing the plan is optional; without it nothing is created.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pyarrow.parquet as pq  # noqa: E402

from opendinov3.core import metadata_partition as mp  # noqa: E402

#: successes/sec/node at 32 processes, retries 0.
#:
#: MEASURED 2026-08-15, experiment 0004 arm 10, on a full 100,000-URL slice:
#: 348.4 URLs/sec at 64.7% yield = 225.3 stored/sec. Four arms were compared
#: on equal slices and this one won on images stored per node-hour, not on
#: speed — see scripts/compare_arms.py.
#:
#: Superseded figures, kept so the gap stays legible:
#:   180.0  the predecessor's runs, retries 2. Never reproduced.
#:    14.6  2026-08-14, first production wave, retries 2, 8 nodes. Computed
#:          from completed shards only, so it undercounts work in flight.
#:
#: Measured at roughly four concurrent nodes. Eight nodes have run but their
#: wall time was not recorded — the tasks were rejected by a threshold since
#: corrected — so scaling beyond four is still extrapolation.
SUCC_PER_SEC = 225.3
SUCC_PER_SEC_IS_MEASURED_ON_A_CURRENT_WAVE = True
#: MEASURED 2026-08-15 across our own output, which varies by URL slice:
#: 55.0% over four tasks, 61.9% and 62.2% on two more, 64.7% on the
#: experiment arm. The low end is used, because a plan that assumes the
#: best slice under-counts the node-hours.
YIELD = 0.55
#: MEASURED 2026-08-15 on OUR OWN output: 692,851 images at 85.6 KB.
#:
#: The old figure, 25.1, came from the predecessor's tree (82,262,338 images
#: in 2.11 TB) whose pipeline settings were never verified. Ours stores at
#: original resolution (--resize_mode no); theirs may not have. It
#: under-predicted storage by 2.6x — 23.2 TB against 60.8 TB — which is the
#: cost of taking a number from someone else's output without checking how
#: it was produced.
KB_PER_IMAGE = 85.6


def read_sources(meta_dir: Path) -> list[mp.SourceFile]:
    """Row counts from parquet metadata, in sorted path order.

    Sorted because the partition must be reproducible from the inputs: the
    same files and the same task size have to give the same corpus, whatever
    order the filesystem happens to return them in.
    """
    sources: list[mp.SourceFile] = []
    for path in sorted(meta_dir.rglob("*.parquet")):
        try:
            rows = pq.ParquetFile(path).metadata.num_rows
        except Exception as exc:  # noqa: BLE001 — reported, not hidden
            print(f"  unreadable, skipped: {path.name}: {exc}", file=sys.stderr)
            continue
        sources.append(mp.SourceFile(path=str(path), rows=rows))
    return sources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("meta_dir", type=Path)
    parser.add_argument("--urls-per-task", type=int, default=1_000_000)
    parser.add_argument("--json", type=Path,
                        help="write the plan here (nothing is written without it)")
    args = parser.parse_args()

    if not args.meta_dir.is_dir():
        print(f"no such directory: {args.meta_dir}", file=sys.stderr)
        return 2

    sources = read_sources(args.meta_dir)
    if not sources:
        print(f"no parquet files under {args.meta_dir}", file=sys.stderr)
        return 2

    summary = mp.summarise(sources, args.urls_per_task)
    tasks = mp.plan_tasks(sources, args.urls_per_task)

    print(f"metadata      : {args.meta_dir}")
    print(f"parquet files : {len(sources):,}")
    print(f"rows          : {summary.total_rows:,}")
    print(f"urls per task : {summary.urls_per_task:,}")
    print(f"tasks         : {summary.tasks:,} "
          f"({summary.full_tasks:,} full, last has "
          f"{summary.final_task_rows:,})")
    print()

    images = summary.total_rows * YIELD
    # successes / (successes per second) / seconds per hour
    hours = images / SUCC_PER_SEC / 3600
    terabytes = images * KB_PER_IMAGE * 1024 / 1e12

    print(f"at {YIELD:.0%} yield and {SUCC_PER_SEC:.0f} successes/sec per node:")
    if not SUCC_PER_SEC_IS_MEASURED_ON_A_CURRENT_WAVE:
        print("  ⚠️  that rate is not measured on a current wave.")
    print(f"  images     : {images/1e6:,.0f} million")
    print(f"  storage    : {terabytes:.1f} TB (at {KB_PER_IMAGE} KB/image)")
    print(f"  node-hours : {hours:,.0f}")
    print()
    print(f"{'nodes':>7} {'days':>7}")
    print("-" * 16)
    for nodes in (4, 8, 16, 32):
        # Two nodes measured at 0.97 of one; beyond that this is extrapolation.
        efficiency = 1.0 if nodes == 1 else 0.97
        print(f"{nodes:>7} {hours / nodes / efficiency / 24:>7.1f}")
    print()
    print("Measured at about four concurrent nodes (experiment 0004). Eight "
          "have run but\ntheir wall time was not recorded, so anything above "
          "four is extrapolation.")

    if args.json:
        args.json.write_text(json.dumps({
            "meta_dir": str(args.meta_dir),
            "urls_per_task": summary.urls_per_task,
            "total_rows": summary.total_rows,
            "tasks": [
                {"task_id": t.task_id, "rows": t.rows,
                 # Relative to meta_dir, recorded above. An absolute path
                 # here would be a description of the machine that planned,
                 # not of the data: od.sh plans inside the container where
                 # the corpus is at /corpus, and the production job binds it
                 # at its host path instead.
                 "pieces": [{"path": os.path.relpath(p, args.meta_dir),
                             "start": s, "end": e}
                            for p, s, e in t.pieces]}
                for t in tasks
            ],
        }, indent=1))
        print(f"\nplan written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
