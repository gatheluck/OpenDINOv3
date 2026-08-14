# Production run: acquiring DataComp-1B

Status: implemented, not yet started
Date: 2026-08-14

## What is being built

1,388 tasks of 1,000,000 URLs each — 1,387,173,656 rows of upstream metadata,
measured from the parquet footers rather than assumed.

| | |
|---|---|
| Tasks | 1,388 (1,387 full, the last holds 173,656) |
| Expected images | ~902 million at 65% yield |
| Expected storage | **23.2 TB** at 25.1 KB/image |
| Total work | ~1,391 node-hours |

Every figure above is measured. 25.1 KB/image is the whole-corpus average
over the 82 million images already acquired; 65% is the yield measured twice,
on different tasks, in experiments 0002 and 0003.

## Why the job has this shape

**One node per subjob.** Experiment 0003 measured two nodes at 0.97 of one at
the same total concurrency, so spreading buys nothing. Meanwhile `pbsdsh`,
cross-node staging and shared node-local scratch produced all four bugs in
that experiment. Independent single-node subjobs give the same throughput,
schedule far better on a full cluster, and fail independently.

**32 processes per node.** Experiment 0002 measured 8 → 32 as a 2.96×
improvement and 64 as **31% worse** than 32, with transient failures rising
from 3.1% to 5.9% — at 2,048 concurrent connections the requests, not the
CPU, become the problem.

**10,000 samples per shard.** 163 MB at the measured image size, inside
webdataset's recommended 100 MB–1 GB. It also gives 100 shards against 32
processes, or 3.1 shards per process: img2dataset hands out work one shard at
a time, so fewer would leave processes idle and make wall time the slowest
shard rather than the mean.

**Waves, not one submission.** ABCI documents `-J start-stop[:step]` and a
75,000-subjob limit but no way to cap concurrent subjobs, so the range is the
only control. It doubles as the ramp: 0003 measured one and two nodes and
nothing above, so the first wave is small and its yield is checked before the
next.

## What each sample carries, and one irreversible choice

DataComp's own `download_upstream.py` passes these to img2dataset, and this
run passes the same:

| Argument | Value | Why |
|---|---|---|
| `--url_col` | `url` | the image |
| `--caption_col` | **`text`** | DINOv3 needs none, but the text-to-image stage video models train first cannot be done without it |
| `--save_additional_columns` | `uid`, `face_bboxes` | `uid` traces a sample back upstream; `face_bboxes` keeps blurring possible later |

Carrying only the URL — which an earlier draft of this pipeline did — would
have produced 23 TB with no captions, deciding by omission whether the corpus
can serve a text-conditioned model.

### Face blurring has no default

`OD_BLUR_FACES` must be set to `0` or `1`; the run refuses otherwise.

DataComp blurs by default, using `face_bboxes`. Blurring is **irreversible**
without re-downloading, it applies to roughly 902 million images, and it is a
legal question rather than a technical one. A default either way would settle
that by accident.

`face_bboxes` is stored with every sample regardless, so choosing `0` now
does not close the door on blurring later.

## The health guard

On 2026-07-28 a day-long loss of external connectivity destroyed 474 tasks.
Each ran to completion, wrote all 100 shards' statistics, stored almost
nothing, and **returned success**. The loss was found weeks later by counting
images rather than files, after several hundred node-hours of writing zeros.

A subjob now exits non-zero and writes no `DONE.json` when its output fails
any of these. Thresholds live in `src/opendinov3/core/task_health.py`, and
`tests/test_production_doc.py` fails if this table and those constants
disagree.

| Signal | Constant | Threshold | Normal (measured ×2) | Outage (measured) |
|---|---|---|---:|---:|
| Local connectivity failures | `MAX_UNREACHABLE_FRACTION` | `0.01` | 0% | 15.5% |
| DNS failures | `MAX_DNS_FRACTION` | `0.2` | 6.0–6.2% | 70.6% |
| Yield | `MIN_YIELD` | `0.3` | 64–65% | 0.1% |

The thresholds sit between the two measured profiles, closer to the outage:
stopping a healthy task costs one requeue, while continuing through an outage
costs the run.

`Errno 101 Network is unreachable` is classified separately from other
failures. A remote host refusing is routine; **this machine having no route
is never normal**, and in an "other" bucket that sits near 5% it would have
been invisible at 15.5%.

## Retry and idempotency

- A task with `DONE.json` is skipped. Waves get resubmitted and PBS requeues
  subjobs; re-downloading would waste a node-hour and replace good data with
  whatever the web returns today.
- A task directory **without** `DONE.json` is a failed attempt. It is moved
  to `task-NNNNNN.attempt-<jobid>` before the retry starts — not deleted,
  because a failed attempt is evidence. img2dataset's incremental mode skips
  any shard that already has output, and a failed attempt leaves statistics
  behind even when it stored nothing, so leaving it in place would reproduce
  the empty task exactly.

## Procedure

Once, on a login node:

```bash
python scripts/plan_partition.py <upstream_metadata> --json plan.json
```

Then per wave:

```bash
source <your env file>
export OD_PLAN=.../plan.json
export OD_META_ROOT=.../upstream_metadata

bash scripts/submit_production.sh --from 0 --to 7 --dry-run
bash scripts/submit_production.sh --from 0 --to 7
```

After a wave, before widening it:

```bash
python scripts/assess_task.py "${OD_TASK_ROOT}/task-000000"
```

## What is not settled

- **Node counts above two are extrapolation.** 0003 measured one and two.
  The ramp exists for that reason; widen a wave only after the previous one
  held its yield.
- **The reservation window bounds the run.** Its remaining time is not
  directly readable — `qrstat` returns nothing for this account — and is
  derived from the longest-running job's remaining walltime, which is a lower
  bound.
- **Absolute throughput is 3.5× below a historical figure** recorded from a
  single shard of one task. Tasks were shown not to differ in speed (0003:
  731 s and 725 s against 0002's 692 s on a different task), so cluster load
  and shard size remain as candidates. It does not affect the comparisons
  the settings were chosen from, only the schedule.
