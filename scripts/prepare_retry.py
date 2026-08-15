#!/usr/bin/env python3
"""Keep what a killed task got right; set aside only what it got wrong.

  prepare_retry.py <task dir> --tag <attempt tag> [--min-shard-yield 0.05]

WHY

production_task.sh used to move the WHOLE task directory aside and start
from the first URL. A task killed at the walltime with 90 of 100 shards
finished re-downloaded all 100.

That was a blunt fix for a real problem. img2dataset decides a shard is
done by globbing `*.json`:

    done_shards = set(int(x.split("/")[-1].split("_")[0])
                      for x in fs.glob(output_path + "/*.json"))

so a shard that completed while storing nothing — the 2026-07-28 outage
wrote 100 such shards per task — would be inherited and skipped, and the
retry would reproduce the empty task.

The precise fix is to set aside only the shards that are bad. Two cases
matter and the third needs nothing:

  finished, healthy   keep; incremental mode skips it
  finished, empty     set aside, so it is redone
  killed mid-write    already has no `_stats.json`, so it is already
                      redone; nothing to do

Set aside rather than deleted: a failed attempt is evidence, and the
2026-07-28 loss was diagnosed weeks later from exactly these files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: A shard below this stored so little that inheriting it would bake the
#: loss in. The outage produced 0.1%; healthy shards run 58-65%.
MIN_SHARD_YIELD = 0.05

SHARD = re.compile(r"^(\d+)")


def prepare(task_dir: Path, tag: str,
            min_yield: float = MIN_SHARD_YIELD) -> tuple[int, int]:
    """Returns (kept, set_aside). Only touches finished, unhealthy shards."""
    shards = task_dir / "shards"
    if not shards.is_dir():
        return 0, 0

    kept = 0
    bad: list[str] = []
    for stats_path in sorted(shards.glob("*_stats.json")):
        match = SHARD.match(stats_path.name)
        if not match:
            continue
        shard_id = match.group(1)
        try:
            body = json.loads(stats_path.read_text())
            count = int(body.get("count", 0))
            successes = int(body.get("successes", 0))
        except (OSError, json.JSONDecodeError, ValueError):
            bad.append(shard_id)          # unreadable is not inheritable
            continue
        if count > 0 and successes / count >= min_yield:
            kept += 1
        else:
            bad.append(shard_id)

    if not bad:
        return kept, 0

    aside = task_dir / f"attempt-{tag}"
    aside.mkdir(parents=True, exist_ok=True)
    moved = 0
    for shard_id in bad:
        for path in shards.glob(f"{shard_id}*"):
            path.rename(aside / path.name)
            moved += 1
    return kept, len(bad)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dir", type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--min-shard-yield", type=float,
                        default=MIN_SHARD_YIELD)
    args = parser.parse_args()

    if not args.task_dir.is_dir():
        print(f"no previous attempt at {args.task_dir}")
        return 0

    kept, aside = prepare(args.task_dir, args.tag, args.min_shard_yield)
    if kept or aside:
        print(f"previous attempt: keeping {kept} finished shard(s), "
              f"setting aside {aside} that stored too little")
        if kept:
            print("   the kept shards are skipped rather than re-downloaded")
    else:
        print("previous attempt left no finished shards; starting over")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
