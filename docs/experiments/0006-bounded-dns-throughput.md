# Experiment 0006: throughput with a fixed shared DNS budget

Status: preregistered 2026-10-04; ABCI comparison not yet run.

The user authorized aggressive throughput testing within the existing rules.
The current 4-process/8-thread setting processes approximately 24–26 candidates
per second per node. That is not a measured system limit. Keep the acquisition
cap at four whole nodes including waiting jobs and experiments; reserve one
slot for this experiment when an existing acquisition finishes. Do not cancel
healthy acquisition to obtain a slot. Use the reservation wrapper and dedicated
group TMPDIR, and check capacity and other team use before submission.

## First comparison

Use the same first 32,000 URLs of task 434 in four sequential runs on one node,
each with its own fresh, isolated output directory. Keep 1,000 samples/shard
for all arms: 32 shards permit all requested processes to receive work.
Never point this experiment at the canonical corpus or reset the node DNS
counter. Use the reviewed HTTP deadline fix, pinned source and CI image.

| Arm | Processes | Threads per process | Purpose |
|---|---:|---:|---|
| A1 | 4 | 8 | Current concurrency baseline |
| B | 4 | 32 | Isolate HTTP concurrency gain |
| C | 8 | 32 | Test process scaling with more concurrent hosts |
| A2 | 4 | 8 | Repeat baseline to detect drift |

All arms retain `OD_DNS_BUDGET=1` (50 gate attempts/second), `OD_DNS_CACHE=0`,
`OD_HTTP_POOL=1`, `OD_BLUR_FACES=1`, timeout 10, retries 2 and MAX_URLS 32000.
The small shard size is a benchmark parameter; confirm the winning setting
on normal 10,000-row production shards before general rollout. No URL filtering,
retry reduction, quality-threshold relaxation or DNS-limit increase is part of
this first experiment. The sample is fixed for paired comparison, not claimed
representative of the entire billion-URL corpus. Successful pilot output is
kept isolated; do not count repeated arms as new unique production acquisition.

## Measurements and decisions

Wrap each existing production job command with `scripts/measure_dns_budget.py`.
It reads the existing node-local counter under its lock before/after the command
and writes only numeric data, not URLs. Existing files are never reset and an
attempt claim prevents accidental reuse of a report destination. A nonzero child
exit remains nonzero. Missing, invalid or decreasing counters invalidate the
measurement rather than becoming zero DNS activity.

The gate counter includes startup validation probes and failed sends. Its
average is a conservative activity proxy, **not whole-node wire QPS or a peak
rate**. It also counts other same-user workloads sharing that gate. Run one
acquisition on the assigned whole node and record that assumption. Preserve
snapshot and downloader durations separately; setup time is part of the outer
measurement, so compare both download speed and end-to-end throughput.

Record candidates/second, successful images/second, yield, DNS failure fraction,
unreachable fraction, runtime, exit, gate-attempt delta and elapsed time. Require
exactly 32,000 candidates, partial DONE with dns_budget=1, and the unchanged
production health gate in every arm. Do not advance after an arm fails.
Against A1, reject a candidate if yield falls by more than 5 percentage points
or DNS failures rise by more than 3 points. These match experiment 0002's
existing quality criteria. A2 throughput drifting more than 20% from A1 makes
the speed comparison inconclusive, rather than evidence to scale.

A gain of at least 50% with acceptable quality justifies a second comparison
at 16×32 and then 32×32, still under the same DNS budget and with enough shards.
Stop escalation if throughput plateaus or errors increase. An average gate
activity near 50 suggests the gate may bind; an average far below 50 does not
by itself exclude short saturated periods. If needed, refine temporal sampling
before claiming the bottleneck is established. Do not infer that 100-QPS support
guidance is an exclusive guaranteed allowance or that four nodes permit a
higher per-node allowance. Increasing the configured DNS budget requires a
separate documented, tested decision; this experiment cannot change it.

## Execution shape

For each arm, supply the usual production job environment, including task ID,
source, SIF, metadata and plan. Set a unique OD_TASK_ROOT for the arm, the settings
above and explicit OD_PROCESSES/OD_THREADS, then execute on the allocated node:

```bash
python3 "$OD_REPO/scripts/measure_dns_budget.py" \
  --state "/tmp/opendinov3-dns-${UID}/budget.json" \
  --output "$OD_LOGDIR/dns-measurement.json" \
  -- bash "$OD_REPO/scripts/production_job.sh"
```

Use `od_qsub.sh` for the containing batch job. Report outputs must have distinct
paths per arm. Create their parent directories before starting. The collector
needs only standard-library Python and runs outside Singularity; the actual
acquisition uses the existing tested container job. Preserve stdout/stderr in
private logs. Hard job termination can prevent the final measurement; do not
substitute a fabricated result or restart an already claimed arm blindly.

## Validation record

RED: seven measurement tests failed with a no-op implementation: missing deltas,
missing exit status, unsafe counter acceptance and absent rerun protection.
The first GREEN passed all seven tests. Further integration covers the CLI,
new/missing gate files and unfinished-attempt claims. Existing transport pacing
tests remain authoritative for the configured rate limit; counter sampling is
not a replacement for those tests or a packet capture.

Final focused GREEN: `python -m pytest -o addopts='' tests/test_dns_measurement.py
 tests/test_dns_budget.py -q` passed 24 tests in 2.87 seconds. Replacing measured
deltas with zero in an isolated source/test/script copy caused all three
selected measurement/CLI cases to fail for incorrect counts. The first mutation
copy omitted scripts, so its CLI failure was an environment error and was not
counted; the complete-copy rerun established the behavioral failure. Ruff and
ty passed for the new collector/CLI (Ruff also covers the new tests). Full CI
is required before deployment. This document is an experiment registration,
not a claim that ABCI throughput has already improved.

The initial full CI run passed 672 tests but failed the architectural invariant:
subprocess execution cannot live in core. The collector now lives in the
platform adapter package; the architectural check is unchanged.
Focused verification after relocation: 57 architecture, measurement and DNS
transport tests passed in 2.85 seconds; Ruff, ty and identifier checks passed.
