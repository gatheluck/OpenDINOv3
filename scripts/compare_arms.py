#!/usr/bin/env python3
"""Which of the four fetch settings to spend the corpus on.

  compare_arms.py <task root> [--tasks 8-11] [--json out.json]

Reads the DONE.json each arm wrote and ranks them.

THE METRIC IS NOT SPEED

`retries 0` is faster per URL almost by definition: a dead URL costs one
timeout instead of three. It can also store FEWER images, because a URL
that would have succeeded on the second attempt is now a failure. An arm
that finishes sooner with less data is not better — the corpus is the
images, not the wall clock.

So arms are ranked by images stored per node-hour, and each arm's yield is
printed beside it so a drop cannot hide inside a speedup.

WHAT THE ANSWER MEANS

  arm 2 (more threads) wins    per-request latency is the cost, and there
                               is 250x headroom in bandwidth to spend
  arm 3 (no retries) wins      the retries were the cost
  nothing wins                 something shared is saturated — DNS is the
                               suspect — and adding threads is a wave spent
                               on nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TOTAL_URLS = 1_387_173_656
#: Below this, two arms are the same arm and the difference is noise.
MEANINGFUL_SPEEDUP = 1.25
#: A winner storing this much less of what it attempted is buying speed
#: with data, which is the wrong trade for a corpus.
MAX_YIELD_DROP = 0.05


def load(task_root: Path, first: int, last: int):
    arms, missing = [], []
    for task_id in range(first, last + 1):
        task_dir = task_root / f"task-{task_id:06d}"
        marker = task_dir / "DONE.json"
        if not marker.is_file():
            if task_dir.exists():
                missing.append(task_dir.name)
            continue
        done = json.loads(marker.read_text())
        wall = int(done.get("wall_seconds", 0))
        candidates = int(done.get("candidates", 0))
        successes = int(done.get("successes", 0))
        if wall <= 0 or candidates <= 0:
            missing.append(task_dir.name)
            continue
        settings = done.get("settings", {})
        arms.append({
            "task": task_id,
            "threads": int(settings.get("threads", 0)),
            "retries": int(settings.get("retries", -1)),
            "wall_seconds": wall,
            "candidates": candidates,
            "successes": successes,
            "yield": successes / candidates,
            "urls_per_second": candidates / wall,
            "successes_per_second": successes / wall,
        })
    return arms, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_root", type=Path)
    parser.add_argument("--tasks", default="8-11",
                        help="inclusive task range holding the arms")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    try:
        first, last = (int(part) for part in args.tasks.split("-"))
    except ValueError:
        print(f"--tasks wants FIRST-LAST, got {args.tasks}", file=sys.stderr)
        return 2

    arms, missing = load(args.task_root, first, last)

    for name in missing:
        print(f"⚠️  {name} has no usable DONE.json — started and not "
              "finished, or killed")
    if missing:
        print()

    if len(arms) < 2:
        print(f"❌ {len(arms)} arm(s) finished; a comparison needs at least "
              "two.", file=sys.stderr)
        print("   One result is a measurement, not a choice.",
              file=sys.stderr)
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(
                {"arms": arms, "arms_missing": len(missing)}, indent=2))
        return 1

    ranked = sorted(arms, key=lambda a: a["successes_per_second"],
                    reverse=True)
    control = min(arms, key=lambda a: (a["threads"], -a["retries"]))
    best = ranked[0]
    speedup = (best["successes_per_second"]
               / control["successes_per_second"]
               if control["successes_per_second"] else float("inf"))

    print(f"{'arm':>4}  {'threads':>7} {'retries':>7}  {'wall':>7}  "
          f"{'yield':>6}  {'URLs/s':>8}  {'stored/s':>9}")
    for a in ranked:
        mark = " <- control" if a is control else ""
        print(f"{a['task']:>4}  {a['threads']:>7} {a['retries']:>7}  "
              f"{a['wall_seconds'] / 60:>6.1f}m  {a['yield']:>6.1%}  "
              f"{a['urls_per_second']:>8.1f}  "
              f"{a['successes_per_second']:>9.1f}{mark}")
    print()

    yield_drop = control["yield"] - best["yield"]
    if speedup < MEANINGFUL_SPEEDUP:
        print(f"→ No setting helps: the best arm is only {speedup:.2f}x the")
        print("  control. Every worker was already busy and the bandwidth")
        print("  is 0.4% used, so the limit is shared rather than per-node.")
        print("  DNS is the suspect: --timeout does not bound name")
        print("  resolution. Adding threads would be a wave spent on")
        print("  nothing; measure resolution latency next.")
    elif yield_drop > MAX_YIELD_DROP:
        print(f"→ The fastest arm is {speedup:.2f}x, but it loses "
              f"{yield_drop:.1%} of the yield")
        print(f"  ({control['yield']:.1%} -> {best['yield']:.1%}). It is "
              "buying speed with images,")
        print("  which is the wrong trade for a corpus. Prefer the fastest")
        print("  arm whose yield holds:")
        held = [a for a in ranked if control["yield"] - a["yield"]
                <= MAX_YIELD_DROP]
        if held:
            keep = held[0]
            print(f"    arm {keep['task']}: threads {keep['threads']}, "
                  f"retries {keep['retries']}, "
                  f"{keep['successes_per_second'] / control['successes_per_second']:.2f}x")
            best = keep
        else:
            print("    none — every arm loses yield. Keep the control.")
            best = control
    else:
        print(f"→ arm {best['task']} wins: threads {best['threads']}, "
              f"retries {best['retries']}, {speedup:.2f}x the control,")
        print(f"  with the yield holding at {best['yield']:.1%}.")
    print()

    hours = TOTAL_URLS / best["urls_per_second"] / 3600
    print(f"full corpus at the chosen setting: {hours:,.0f} node-hours")
    for nodes in (16, 32, 64):
        print(f"  {nodes:>3} nodes -> {hours / nodes / 24:5.1f} days")

    if args.json:
        payload = {
            "arms": ranked,
            "arms_missing": len(missing),
            "control": control,
            "best": {**best, "speedup": speedup},
            "projected_node_hours": hours,
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
