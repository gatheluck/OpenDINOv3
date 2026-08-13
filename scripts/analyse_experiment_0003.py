#!/usr/bin/env python3
"""Report experiment 0003 against the criteria it was registered with.

Reads the three phase directories the job produced, sums each phase across
whatever nodes it used, and applies the pre-registered thresholds from
src/opendinov3/core/node_plan.py.

Prints the numbers whether or not they support the hypothesis, and says
plainly when the multi-node phase did not run — that is an expected outcome,
not something to paper over.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import node_plan as np  # noqa: E402
from opendinov3.core.download_stats import RunSummary  # noqa: E402

SINGLE = ("phase1_single", "phase3_single")
MULTI = "phase2_multi"


def load_phase(directory: Path) -> tuple[RunSummary, int] | None:
    """Sum every node's shards for one phase. Wall time is the phase's own."""
    if not directory.is_dir():
        return None

    wall_file = directory / "wall_seconds"
    wall = float(wall_file.read_text().strip()) if wall_file.is_file() else 0.0

    stats = []
    nodes = 0
    for node_dir in sorted(directory.glob("node*")):
        found = sorted(node_dir.glob("run*/shards/*_stats.json"))
        if found:
            nodes += 1
        for path in found:
            try:
                stats.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"  skipped {path}: {exc}", file=sys.stderr)

    if not stats:
        return None
    return RunSummary.from_stats(processes=0, wall_seconds=wall, stats=stats), nodes


def fmt(value: float | None, spec: str = ".1f") -> str:
    return "—" if value is None else format(value, spec)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outdir", type=Path)
    args = parser.parse_args()

    loaded = {name: load_phase(args.outdir / name)
              for name in (*SINGLE, MULTI)}

    single = [loaded[n][0] for n in SINGLE if loaded[n]]
    if not single:
        print(f"no single-node phase found under {args.outdir}", file=sys.stderr)
        return 2
    multi = loaded[MULTI][0] if loaded[MULTI] else None

    print("experiment 0003 — does spreading across nodes cost anything?")
    print(f"source: {args.outdir}\n")
    print(f"{'phase':<14} {'nodes':>6} {'wall s':>8} {'cand':>9} {'succ':>9} "
          f"{'yield':>7} {'succ/s':>9} {'DNS':>7}")
    print("-" * 74)
    for name in (SINGLE[0], MULTI, SINGLE[1]):
        entry = loaded[name]
        if not entry:
            print(f"{name:<14} {'—':>6}   NOT RUN")
            continue
        run, nodes = entry
        print(f"{name:<14} {nodes:>6} {run.wall_seconds:>8.0f} "
              f"{run.candidates:>9} {run.successes:>9} "
              f"{fmt(run.yield_rate, '.1%'):>7} "
              f"{fmt(run.successes_per_sec, '.1f'):>9} "
              f"{fmt(run.dns_fraction, '.1%'):>7}")

    verdict = np.judge_distribution(single=single, multi=multi)

    print("\npre-registered criteria")
    print("-" * 46)
    if verdict.distribution_neutral is None:
        skipped = args.outdir / f"{MULTI}_SKIPPED"
        why = skipped.read_text().strip() if skipped.is_file() else "phase absent"
        print(f"  multi-node phase : NOT RUN ({why})")
        print("  → the question this experiment exists for is unanswered.")
        print("    The single-node phases below are a drift measurement only.")
    else:
        print(f"  single-node mean : {fmt(verdict.single_node_rate)} succ/s")
        print(f"  multi-node       : {fmt(verdict.multi_node_rate)} succ/s")
        ratio = (verdict.multi_node_rate / verdict.single_node_rate
                 if verdict.single_node_rate else None)
        print(f"  ratio            : {fmt(ratio, '.2f')}× "
              f"(needs ≥ {np.MIN_DISTRIBUTION_RATIO})")
        print(f"  distribution free: {verdict.distribution_neutral}")

    print(f"  yield preserved  : {verdict.yield_preserved} "
          f"(max drop {np.MAX_YIELD_DROP:.0%})")
    print(f"  DNS stable       : {verdict.dns_stable} "
          f"(max rise {np.MAX_DNS_RISE:.0%})")
    if verdict.baseline_drift is None:
        print("  drift control    : NOT RUN — stability unknown.")
    else:
        print(f"  baseline drift   : {verdict.baseline_drift:.1%} "
              f"(stable: {verdict.baseline_stable}, "
              f"allowed {np.MAX_BASELINE_DRIFT:.0%})")
        print(f"  → no difference smaller than {verdict.baseline_drift:.1%} "
              "can be attributed to node count.")

    print()
    print("REJECTED" if verdict.rejected else "NOT REJECTED")
    print()
    print("Scope: two nodes, one sample per phase, inside a shared reservation. "
          "Says nothing about more than two nodes, nor about interaction with "
          "per-node concurrency. See docs/experiments/0003-node-distribution.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
