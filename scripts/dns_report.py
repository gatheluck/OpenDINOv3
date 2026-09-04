#!/usr/bin/env python3
"""How many names a wave actually asked the resolver for.

  dns_report.py <task root> [--tasks 20]

ABCI stopped a wave because our DNS traffic was affecting other users. The
answer we owe them is a measurement, not a prediction: `od.sh hosts` says 76%
of lookups repeat, but the cache is per worker process and a node runs 32 of
them, so the realised figure is lower by an amount only a run can give.

The quantity they asked about is **queries per second per node** — what
reaches their resolver — not our hit rate.

A task whose log carries no counts ran without the cache. That is reported as
unknown rather than zero: zero would be the flattering answer, given to the
people we owe an honest one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import shard_layout as sl  # noqa: E402
from opendinov3.net import dns_report as dr  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_root", type=Path)
    parser.add_argument("--tasks", type=int, default=20,
                        help="most recently finished tasks to read")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    finished = [task for task in sl.task_dirs(args.task_root)
                if (task / "DONE.json").is_file()]
    if not finished:
        print(f"no finished tasks under {args.task_root}", file=sys.stderr)
        return 2
    finished.sort(key=lambda t: (t / "DONE.json").stat().st_mtime)
    recent = finished[-args.tasks:]

    rows = [(task.name, dr.summarise(task)) for task in recent]
    measured = [(name, s) for name, s in rows if s.workers]

    print(f"tasks read      : {len(rows)}")
    print(f"with the cache  : {len(measured)}")
    print()
    if not measured:
        print("→ None of these tasks ran with OD_DNS_CACHE=1, so nothing was")
        print("  measured. This is not the same as making no queries.")
        return 0

    print(f"{'task':<16}{'workers':>9}{'lookups':>12}{'queries':>12}"
          f"{'saved':>8}{'q/s':>9}")
    for name, s in measured:
        rate = "—" if s.queries_per_second is None else f"{s.queries_per_second:.1f}"
        saved = "—" if s.reduction is None else f"{s.reduction:.0%}"
        print(f"{name:<16}{s.workers:>9,}{s.lookups:>12,}{s.misses:>12,}"
              f"{saved:>8}{rate:>9}")

    lookups = sum(s.lookups for _, s in measured)
    queries = sum(s.misses for _, s in measured)
    rated = [s for _, s in measured if s.queries_per_second is not None]
    print()
    print(f"lookups         : {lookups:,}")
    print(f"queries sent    : {queries:,}")
    print(f"saved by cache  : {1 - queries / lookups:.1%}" if lookups else "")
    if rated:
        per_node = sum(s.queries_per_second for s in rated) / len(rated)
        print(f"**queries/sec/node**: {per_node:.1f}")
        print()
        print(f"At N nodes the resolver sees about {per_node:.0f} x N per second.")
        print("Without the cache it would be "
              f"{per_node / (queries / lookups):.0f} x N." if queries else "")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "tasks": [
                {"task": name, "workers": s.workers, "lookups": s.lookups,
                 "queries": s.misses, "reduction": s.reduction,
                 "queries_per_second": s.queries_per_second}
                for name, s in measured],
            "lookups": lookups, "queries": queries,
        }, indent=1))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
