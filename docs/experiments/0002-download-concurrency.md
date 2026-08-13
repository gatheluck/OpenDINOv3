# Experiment 0002: Download concurrency within a single node

Status: pre-registered, not yet run
Date: 2026-08-13
Time budget: 2.5 hours (agreed upper bound)

## Question

Does raising per-node download concurrency above the current setting increase
throughput, and at what cost to yield?

## Why it matters

The node has **192 cores**, measured. The downloader runs **8 processes** —
about 4% of them. Downloading is latency-bound rather than CPU-bound: each
process sustains ~28 successful images/sec, far below what a core can do.

At the current setting the remaining ~735 tasks take **18–37 days**. If
concurrency scales even part of the way, that becomes days.

The 8-process setting is inherited, not derived. It is treated here as a
starting point for measurement, not as a constraint.

## Hypothesis

Throughput rises with process count over the range tested, because the
bottleneck is waiting on HTTP rather than CPU. Yield stays roughly flat,
because the dominant failure causes — 404, 403, dead domains — do not depend
on how fast requests are issued.

## Falsification criteria

Rejected if any of:

- Throughput fails to rise by at least 50% from 8 to 32 processes. Latency-
  bound work that does not scale at 4× the concurrency is bound by something
  else, and that something has to be identified before scaling further.
- Yield drops by more than 5 percentage points at higher concurrency. Losing
  images to go faster is not a trade worth making silently; the corpus is the
  product.
- The DNS failure fraction rises by more than 3 percentage points. A previous
  4-node run reported 89% DNS failure, and while that was node scaling rather
  than in-node concurrency, the same resolver sits behind both. 3 points is
  chosen because an earlier probe found the rate flat from 1 to 512
  concurrent lookups, spanning 7.2% to 9.4% — a rise past that observed
  spread is not something concurrency-independent behaviour has produced
  here before.
- The two runs at 8 processes disagree by more than 20%. That is the drift
  control; the threshold is under half the 50% effect the experiment is
  looking for, so a baseline wandering by more than this cannot support the
  claim.

### Thresholds

These are the values the analysis applies. They live in
`src/opendinov3/core/download_stats.py`, and `tests/test_experiment_0002_doc.py`
fails if this table and those constants disagree — so the numbers cannot be
adjusted after the result is known without the change being visible here.

| Criterion | Constant | Value |
|---|---|---|
| Throughput rise, 8 → 32 processes | `MIN_SPEEDUP` | `1.5` |
| Yield drop from baseline | `MAX_YIELD_DROP` | `0.05` |
| DNS fraction rise from baseline | `MAX_DNS_RISE` | `0.03` |
| Drift between the two 8-process runs | `MAX_BASELINE_DRIFT` | `0.2` |

## Design

Four runs on one node, in sequence, inside one job.

| Run | Processes | Threads | URL slice |
|---|---|---|---|
| 1 | 8 | 32 | URLs 1 – 200,000 |
| 2 | 32 | 32 | URLs 200,001 – 400,000 |
| 3 | 64 | 32 | URLs 400,001 – 600,000 |
| 4 | **8** | 32 | URLs 600,001 – 800,000 |

`threads` is held at 32 so that only one variable moves.

**Run 4 repeats run 1's setting on fresh URLs.** If the two disagree, the
cluster changed underneath the experiment and no difference between levels
can be attributed to concurrency.

### Why disjoint URL slices rather than the same URLs

Reusing one URL list would let the first run populate the resolver cache and
hand every later run an advantage that looks like a concurrency effect.

Disjoint slices avoid that, at the cost of letting composition vary between
levels. At 200,000 URLs per level and a success rate near 64%, the standard
error on yield is **0.11 percentage points** — far below the 5-point
falsification threshold. Composition variance is negligible at this size.

This is the mirror image of a mistake made earlier in this project: in a DNS
probe, distinct host sets at n=500 let the *kind* of failure vary between
levels, which made elapsed time incomparable. Large n is what makes the same
choice safe here.

### The slices are cut before any run starts

Slices are URL positions, and all four are cut in one pass over the source
before the first download begins.

The corpus stores its URL lists as parquet — `urls_clean.parquet`, one per
task. img2dataset reads parquet natively given `--url_col`, so the slices are
written back as parquet with every column preserved and the runs use the same
input path production uses. Converting to text would put the experiment on a
code path production never exercises.

The URL column is found by name and the list is refused if no known name is
present. A positional guess would work on whatever sample it was tried
against and fail on the corpus. This matters more than it sounds: img2dataset
accepts any string as a URL, so guessing wrong is not an error — it is a run
that fails every fetch and reports a yield near zero that looks like a
finding.

Text lists are still supported for hand-made inputs, with the same rule: one
URL per line, or tab-separated with a header naming the URL column, and
refused otherwise.

If the source is too short to fill every slice, the run does not start. A
level with a short slice is not comparable to the others, so a partial
experiment would spend the reservation slot to answer nothing. `OD_URLS` may
name a directory, in which case task lists are concatenated in sorted order
until there are enough rows.

### Which task list to draw from

A task under `raw_shards`, not one under `dns_recovery`.

`dns_recovery` holds URLs that already failed DNS once. Their failure profile
is not the corpus's, so throughput and yield measured on them would not
transfer to production. The submit script warns when pointed at one.

### Node count is not varied

One node throughout. A previous 4-node run degraded badly and is treated as
evidence about *node* scaling; that is a separate axis from in-node
concurrency, and mixing them would confound both.

## Measured quantities

Per run, from img2dataset's own `_stats.json` files plus wall time:

- successful images per second
- yield: successes / candidates
- failure breakdown, especially the DNS fraction
- wall time

## Confounders — what this cannot separate

- **Other tenants.** The reservation holds 302 nodes with ~284 in use by
  others. Their network traffic shares the same egress. Run 4 is the control
  for drift, but it cannot correct for it.
- **Time of day.** Remote hosts and their rate limits vary. The four runs are
  minutes apart, which limits this but does not remove it.
- **Resolver state.** Not observable from here.
- **One node, one sample per level.** No variance estimate within a level. A
  difference smaller than the run-1 versus run-4 gap cannot be claimed.
- **Remote rate limiting.** Higher concurrency may trigger 429s from hosts
  that appear disproportionately in one slice. Visible in the breakdown, but
  not separable from a general concurrency effect.

## What this cannot answer

- **Whether more nodes help.** Different axis, deliberately untouched.
- **The optimal setting.** Three points locate a direction, not an optimum.
- **Sustained behaviour.** Each run is roughly ten minutes. Effects that
  appear after hours — resolver exhaustion, accumulated throttling — are out
  of reach.
- **Effects on downstream data quality.** Yield and speed only.

## Cost and safety

- Runs inside the existing reservation, so no additional points are consumed.
- Writes only under the experiment's own output directory. The previous
  owner's tree is never written to; it is read only for the URL list.
- Time budget 2.5 hours. Estimated 40 minutes if concurrency helps, 76
  minutes in the worst case where it hurts. The job's walltime enforces the
  bound.

## Procedure

Configuration comes from the environment; no site identifier appears in any
script here.

```bash
source <your env file>

# Inspect the resolved settings and the generated job without submitting.
bash scripts/submit_experiment_0002.sh --dry-run

bash scripts/submit_experiment_0002.sh
```

`OD_URLS` points at a task's `urls_clean.parquet`, a text list, or a
directory of either. If it is unset, the submit script lists candidates under
`OD_ROOT` rather than guessing one.

Before submitting, the script reads the source's parquet metadata through the
container and reports the row count, the schema and the detected URL column.
A wrong schema or too few rows stops it there, rather than a minute into a
reserved node after the queue wait.

When the job finishes:

```bash
singularity exec --bind "${OD_PUBLIC_ROOT}:/work:ro" \
  --bind "${OD_OUT_ROOT}/experiments/0002:/out:ro" "${OD_SIF}" \
  python /work/scripts/analyse_experiment_0002.py /out
```

The analysis applies the criteria above from the constants in
`src/opendinov3/core/download_stats.py`, so it cannot quietly use different
thresholds than the ones registered here.

## Result

Not yet run.
