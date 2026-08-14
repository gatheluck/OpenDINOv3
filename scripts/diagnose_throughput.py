#!/usr/bin/env python3
"""Why the wave is slow, and what settings would fix it.

  diagnose_throughput.py <task root> --node-hours 11.9 [--json out.json]

Reads the `_stats.json` img2dataset writes when a shard completes. Works on
a wave that is still running or was killed — it needs no DONE.json.

WHAT IT IS FOR

The pilot ran at 22.5 URLs/sec/node against a planning model of 277. Every
worker was busy (352 open tars minus 96 finished shards is exactly 32
processes x 8 nodes), so the time went into waiting, not queueing.

A dead URL occupies a thread for `timeout x (retries + 1)` — 30 seconds at
--timeout 10 --retries 2 — while a successful 25 KB fetch takes under a
second. So the cost per URL is set by the failure rate, and the failure
rate is recorded in status_dict. This reads it, checks that the arithmetic
actually explains the observed rate, and prices the alternatives.

It refuses to recommend a shorter timeout on faith: cutting below what
slow-but-live servers need turns successes into failures, so the report
says what is known and what is not.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import download_stats as ds  # noqa: E402
from opendinov3.core import throughput_model as tm  # noqa: E402

#: Settings the wave ran with, from scripts/production_task.sh.
TIMEOUT = 10.0
RETRIES = 2
PROCESSES = 32
THREADS = 32
#: A 25 KB image over a working connection. Deliberately generous: a larger
#: value makes the failure rate look smaller, so this errs against the
#: conclusion the report is reaching for.
SUCCESS_SECONDS = 1.0

CANDIDATES = [(3.0, 0), (5.0, 0), (5.0, 1), (10.0, 0)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_root", type=Path)
    parser.add_argument("--node-hours", type=float, required=True,
                        help="summed Elap Time of the subjobs, in hours")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    stats = sorted(args.task_root.glob("task-*/shards/*_stats.json"))
    if not stats:
        print(f"no completed shards under {args.task_root}", file=sys.stderr)
        return 2

    total = successes = 0
    merged: Counter[str] = Counter()
    for path in stats:
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        total += int(payload.get("count", 0))
        successes += int(payload.get("successes", 0))
        for message, count in (payload.get("status_dict") or {}).items():
            merged[message] += int(count)

    if total == 0:
        print("completed shards record no URLs at all", file=sys.stderr)
        return 1

    failures = ds.classify(merged)
    failed = total - successes
    failure_rate = failed / total
    workers = PROCESSES * THREADS
    node_seconds = args.node_hours * 3600
    observed_rate = total / node_seconds
    observed_cost = workers / observed_rate if observed_rate else float("inf")

    print(f"shards completed : {len(stats)}")
    print(f"URLs             : {total:,}")
    print(f"successes        : {successes:,}  ({successes / total:.1%})")
    print(f"failures         : {failed:,}  ({failure_rate:.1%})")
    print()
    print("failure mix:")
    for name in ("dns", "unreachable", "permanent", "transient",
                 "rate_limited", "other"):
        count = getattr(failures, name, 0)
        if count:
            print(f"  {name:<13} {count:>10,}  ({count / total:6.1%})")
    print()
    print("most common outcomes:")
    for message, count in merged.most_common(6):
        print(f"  {count:>9,}  {message[:88]}")
    print()

    print(f"observed         : {observed_rate:,.1f} URLs/sec/node "
          f"over {args.node_hours:.2f} node-hours")
    print(f"                   = {observed_cost:,.1f} s per URL per thread "
          f"({workers:,} in flight)")
    print()

    # Does the timeout arithmetic actually explain what we saw? If it does
    # not, changing the timeout will not fix it, and saying so is the point.
    predicted = tm.seconds_per_url(failure_rate, TIMEOUT, RETRIES,
                                   SUCCESS_SECONDS)
    print(f"model at {failure_rate:.1%} failures, timeout {TIMEOUT:.0f}s x "
          f"{RETRIES + 1} attempts:")
    print(f"                   = {predicted:,.1f} s per URL per thread")
    ratio = observed_cost / predicted if predicted else float("inf")
    explained = 0.7 <= ratio <= 1.4
    if explained:
        print(f"  → within {abs(1 - ratio):.0%} of what was observed: dead")
        print("    URLs holding threads for the full timeout is the cause.")
    else:
        print(f"  → off by {ratio:.1f}x. Timeouts do NOT explain the rate.")
        print("    Something else dominates — bandwidth, the shared")
        print("    filesystem, or per-host serialisation. Changing the")
        print("    timeout would not fix it; measure before tuning.")
    print()

    if explained:
        print("what other settings would cost, at the SAME failure rate:")
        for timeout, retries in CANDIDATES:
            gain = tm.speedup(failure_rate, SUCCESS_SECONDS,
                              (TIMEOUT, RETRIES), (timeout, retries))
            cost = tm.seconds_per_url(failure_rate, timeout, retries,
                                      SUCCESS_SECONDS)
            rate = tm.urls_per_second(cost, workers)
            hours = tm.hours_for_task(1_000_000, rate)
            print(f"  timeout {timeout:>4.0f}s retries {retries}  "
                  f"{gain:5.1f}x   {rate:7,.0f} URLs/s/node   "
                  f"1M-URL task {hours:5.1f} h")
        print()
        print("  ⚠️  These assume the failure rate does not move. A shorter")
        print("      timeout also cuts off slow-but-live servers, turning")
        print("      successes into failures. Nothing here measures the")
        print("      latency of the successes, so the yield cost is UNKNOWN.")
        print("      Verify on one task before applying to a wave.")
        print()

    print(f"at the observed rate, a 1,000,000-URL task takes "
          f"{tm.hours_for_task(1_000_000, observed_rate):,.1f} h")

    if args.json:
        payload = {
            "shards": len(stats), "urls": total, "successes": successes,
            "failure_rate": failure_rate,
            "observed_urls_per_second": observed_rate,
            "observed_seconds_per_url": observed_cost,
            "model_seconds_per_url": predicted,
            "model_explains_observation": explained,
            "failure_mix": {name: getattr(failures, name, 0)
                            for name in ("dns", "unreachable", "permanent",
                                         "transient", "rate_limited",
                                         "other")},
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
