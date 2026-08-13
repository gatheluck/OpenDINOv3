#!/usr/bin/env python3
"""Report experiment 0002 against the criteria it was registered with.

Reads the run directories the worker produced and evaluates the three
pre-registered falsification criteria plus the drift control. The criteria
live in src/opendinov3/core/download_stats.py as constants so that this
script cannot quietly apply different ones than the protocol states.

Prints the numbers whether or not they support the hypothesis.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import download_stats as ds  # noqa: E402

RUN_DIR = re.compile(r"^run(\d+)_p(\d+)$")


def load_run(directory: Path) -> ds.RunSummary | None:
    match = RUN_DIR.match(directory.name)
    if not match:
        return None

    processes = int(match.group(2))
    wall_file = directory / "wall_seconds"
    wall_seconds = float(wall_file.read_text().strip()) if wall_file.is_file() else 0.0

    stats = []
    for path in sorted(directory.glob("shards/*_stats.json")):
        try:
            stats.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  skipped {path.name}: {exc}", file=sys.stderr)

    return ds.RunSummary.from_stats(
        processes=processes, wall_seconds=wall_seconds, stats=stats
    )


def fmt(value: float | None, spec: str = ".1f") -> str:
    return "—" if value is None else format(value, spec)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outdir", type=Path, help="the experiment output directory")
    args = parser.parse_args()

    directories = sorted(
        (d for d in args.outdir.iterdir() if d.is_dir() and RUN_DIR.match(d.name)),
        key=lambda d: int(RUN_DIR.match(d.name).group(1)),
    )
    if not directories:
        print(f"no run directories under {args.outdir}", file=sys.stderr)
        return 2

    runs = [r for r in (load_run(d) for d in directories) if r is not None]

    print("experiment 0002 — download concurrency")
    print(f"source: {args.outdir}")
    print()
    print(f"{'run':<12} {'proc':>5} {'wall s':>8} {'cand':>9} {'succ':>9} "
          f"{'yield':>7} {'succ/s':>9} {'DNS':>7} {'429':>7}")
    print("-" * 82)

    for directory, run in zip(directories, runs):
        rate_limited = (
            run.counts.rate_limited / run.candidates if run.candidates else None
        )
        print(
            f"{directory.name:<12} {run.processes:>5} "
            f"{run.wall_seconds:>8.0f} {run.candidates:>9} {run.successes:>9} "
            f"{fmt(run.yield_rate, '.1%'):>7} "
            f"{fmt(run.successes_per_sec, '.1f'):>9} "
            f"{fmt(run.dns_fraction, '.1%'):>7} "
            f"{fmt(rate_limited, '.1%'):>7}"
        )

    print()
    print("failure breakdown (share of attempts)")
    print(f"{'run':<12} {'permanent':>10} {'transient':>10} {'other':>10}")
    print("-" * 44)
    for directory, run in zip(directories, runs):
        n = run.candidates or 1
        print(
            f"{directory.name:<12} "
            f"{run.counts.permanent / n:>10.1%} "
            f"{run.counts.transient / n:>10.1%} "
            f"{run.counts.other / n:>10.1%}"
        )

    verdict = ds.judge(runs)
    baseline = runs[0]
    scaled = next((r for r in runs[1:] if r.processes != baseline.processes), None)

    print()
    print("pre-registered criteria")
    print("-" * 44)

    if scaled and baseline.successes_per_sec and scaled.successes_per_sec:
        speedup = scaled.successes_per_sec / baseline.successes_per_sec
        print(f"  speedup {baseline.processes}→{scaled.processes} processes: "
              f"{speedup:.2f}×  (needs ≥ {ds.MIN_SPEEDUP}×)")
    print(f"  scales           : {verdict.scales}")
    print(f"  yield preserved  : {verdict.yield_preserved} "
          f"(max drop allowed {ds.MAX_YIELD_DROP:.0%})")
    print(f"  DNS stable       : {verdict.dns_stable} "
          f"(max rise allowed {ds.MAX_DNS_RISE:.0%})")

    if verdict.baseline_drift is None:
        print("  baseline control : NOT RUN — stability unknown. Any "
              "difference below is unverified.")
    else:
        print(f"  baseline drift   : {verdict.baseline_drift:.1%} "
              f"(stable: {verdict.baseline_stable}, "
              f"allowed {ds.MAX_BASELINE_DRIFT:.0%})")
        print(f"  → no difference smaller than {verdict.baseline_drift:.1%} "
              "can be attributed to concurrency.")

    print()
    print("REJECTED" if verdict.rejected else "NOT REJECTED")
    print()
    print("Scope: one node, one sample per level, inside a shared reservation. "
          "Says nothing about scaling across nodes, nor about the optimum. "
          "See docs/experiments/0002-download-concurrency.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
