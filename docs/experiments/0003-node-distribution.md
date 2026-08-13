# Experiment 0003: Does spreading work across nodes cost anything?

Status: **two attempts failed, both on staging rather than on the question. Third not yet run.**
Date: 2026-08-13

## Question

At the same total concurrency, does running on two nodes deliver the same
throughput and yield as running on one?

## Why it matters

Production needs more than one node. The handoff records a 4-node run that
reported **89% DNS failure**, and that single observation is what currently
stops any scaling decision. It has never been reproduced or explained.

If distribution is free, node count becomes the lever for finishing the
remaining ~735 tasks in days rather than weeks. If it is not, the cause has
to be found before adding nodes, and the 89% figure gets an explanation.

## Why this is independent of experiment 0002

Experiment 0002 asks how many processes to run **on** a node. This asks
whether it matters **which** node they run on. Holding the total number of
processes fixed separates them: whatever 0002 concludes about the best
per-node process count, "does spreading hurt?" is answered here.

Comparing "1 node" with "2 nodes" the obvious way would move both variables
at once and answer neither.

## Hypothesis

Throughput and yield are unchanged by distribution, because the bottleneck is
the remote hosts rather than anything shared on our side. The 89% figure was
a property of that particular run — most likely a URL list already filtered
to dead domains, as the `dns_recovery` lists are — rather than of node count.

## Design

One job holding **2 nodes**, three phases in sequence. Total processes are
held at **32** throughout; only their distribution changes.

| Phase | Nodes | Processes/node | URLs | URLs/node |
|---|---|---|---|---|
| A1 | 1 | 32 | 200,000 | 200,000 |
| B | 2 | 16 | 200,000 | 100,000 |
| A2 | 1 | 32 | 200,000 | 200,000 |

Each phase uses its own disjoint slice, so no phase warms a resolver cache
for the next. **A2 repeats A1** as the drift control: the reservation is
shared with ~238 other jobs, so the baseline can move underneath the
experiment.

### Shard granularity is held constant, not merely assumed

img2dataset hands out work one shard at a time, so shards-per-process is part
of the configuration. At 1,000 samples per shard:

| Phase | Shards/node | Processes/node | Shards per process |
|---|---|---|---|
| A | 200 | 32 | 6.25 |
| B | 100 | 16 | 6.25 |

Halving the URLs on a node halves its shards, and halving its processes
halves the demand, so the ratio survives the split. `validate_distribution`
checks this and refuses a configuration where it does not hold — experiment
0002 nearly shipped with exactly this confound.

### The single-node phases run first, deliberately

Launching across nodes is the most fragile part of the job. ABCI's
documentation states that multi-node work requires `rt_HF` and lists the
allocated hosts in `$PBS_NODEFILE`, but **does not recommend a launch
mechanism**; SSH to the allocated nodes needs `-v USE_SSH`, which the
reservation wrapper does not currently pass.

So the job detects what works at runtime — `pbsdsh` first, then `ssh` — and
if neither does, phases A1 and A2 have already run. That leaves a usable
drift measurement and a clear diagnostic instead of a wasted queue slot,
which at several hours of queue wait is the difference that matters.

## Falsification criteria

Rejected if any of:

- Multi-node throughput falls below **80%** of the single-node mean. Adding
  nodes is only worth doing if it is close to free; losing more than a fifth
  per doubling compounds badly at production scale.
- Yield drops by more than **5 percentage points**. This is the direct form
  of the 89% claim.
- The DNS failure fraction rises by more than **3 percentage points**. Same
  threshold and reasoning as 0002: an earlier probe found the rate flat from
  1 to 512 concurrent lookups, spanning 7.2% to 9.4%, so a rise past that
  observed spread is new behaviour.
- The two single-node phases disagree by more than **20%**. That is the drift
  control; a baseline wandering by more than this cannot support the claim.

### Thresholds

Applied from the constants in `src/opendinov3/core/node_plan.py`.
`tests/test_experiment_0003_doc.py` fails if this table and those constants
disagree, so the numbers cannot be adjusted once the result is known.

| Criterion | Constant | Value |
|---|---|---|
| Multi-node throughput vs single-node mean | `MIN_DISTRIBUTION_RATIO` | `0.8` |
| Yield drop from the single-node baseline | `MAX_YIELD_DROP` | `0.05` |
| DNS fraction rise | `MAX_DNS_RISE` | `0.03` |
| Drift between the two single-node phases | `MAX_BASELINE_DRIFT` | `0.2` |

## Measured quantities

Per phase, from img2dataset's `_stats.json` files across all its nodes, plus
the phase's wall time:

- successful images per second
- yield: successes / candidates
- failure breakdown, especially the DNS fraction
- wall time (for phase B, the wall time of the phase, so a slow node counts)

## Confounders — what this cannot separate

- **Other tenants.** The reservation is shared and was ~100% occupied when
  this was written. Their traffic uses the same egress. A2 controls for
  drift but cannot correct for it.
- **Two nodes is not many nodes.** A shared resolver or egress link may be
  fine at 2 and collapse at 8. This bounds nothing above 2.
- **Which nodes we got.** PBS chooses. Two nodes on the same rack or switch
  may behave differently from two that are far apart, and we neither observe
  nor control that.
- **One sample per phase.** No variance estimate within a phase; a difference
  smaller than the A1/A2 gap cannot be claimed.
- **The idle node during A1 and A2.** Half the allocation sits unused in two
  of three phases. That is the price of doing the comparison inside one job,
  where the time window is shared.

## What this cannot answer

- **Why the earlier run reported 89%.** If distribution is neutral here, that
  run differed in some other way — most likely its URL list. This does not
  identify it.
- **The optimal node count.** Two points, and the upper one is 2.
- **Interaction with per-node concurrency.** Total is fixed at 32. Whether
  spreading behaves the same at a total of 256 is not tested.
- **Sustained behaviour.** Each phase is minutes.

## Cost and safety

- Runs inside the existing reservation, so no additional points are consumed.
- Holds 2 nodes rather than 1, which is a larger claim on a fully occupied
  reservation. Walltime is sized from measured rates rather than padded.
- Writes only under the experiment's own output directory. The previous
  owner's tree is read only for the URL list.
- **Use a different task list from experiment 0002.** Re-downloading the same
  URLs within hours would let remote caching and rate limiting carry over
  between the two experiments.

## Procedure

```bash
source <your env file>
bash scripts/submit_experiment_0003.sh --dry-run
bash scripts/submit_experiment_0003.sh
```

Analysis, after the job finishes:

```bash
singularity exec --bind "${OD_PUBLIC_ROOT}:/work:ro" \
  --bind "${OD_OUT_ROOT}/experiments/0003:/out:ro" "${OD_SIF}" \
  python /work/scripts/analyse_experiment_0003.py /out
```

## Result

### Attempt 1, 2026-08-13: failed, measured nothing

Job wall time **17 seconds**, exit status **0**. No shard was written by any
phase.

**The part expected to be fragile worked.** Two nodes were allocated
(hnode031, hnode034), `pbsdsh` was selected as the launcher, and scripts ran
on both nodes — the log shows `phase2_multi: 2 nodes × 16 processes
(launcher: pbsdsh)` with node0 and node1 both starting. Multi-node launch on
ABCI via pbsdsh is therefore confirmed to work, which is worth keeping even
though the run produced nothing.

**Every worker then exited immediately:**

```
❌ no slice_1.parquet or slice_1.txt in /out/phase1_single/node0/slices
```

The staging step wrote the slice as a symlink carrying an **absolute host
path**:

```bash
ln -sf "${OD_EXP_OUT}/slices_p1/slice_1.parquet" "${dir}/slices/slice_1.parquet"
```

The worker reads that directory through a bind mount at `/out`, where the
host path does not exist, so the file was simply absent. The `url_column`
written beside it two lines earlier was **copied**, and survived. The two
differed in nothing else.

Fixed by `scripts/stage_node_slices.sh`, which copies. A relative symlink
would also work, but only while every consumer resolves it the same way; a
copy carries no such condition and the slices are a few megabytes.

### Two other faults the same run exposed

**The job reported success.** `Exit_status = 0` for a job that wrote nothing,
so `qstat` said it had worked. The job now counts the shards it produced and
exits non-zero when that count is zero.

**`--env HOME=...` is refused by SingularityCE:**

```
WARNING: Overriding HOME environment variable with SINGULARITYENV_HOME is not permitted
```

It was doing nothing but emitting that warning three times per job — and had
been doing so in experiment 0002 as well, which nonetheless ran for 1h38m
without trouble. The flag is removed; `XDG_CACHE_HOME` is accepted and is
what the caches follow. Home usage is now printed before and after each run
so this stays a measured fact rather than an assumption. ABCI's container
documentation does not cover HOME or automatic bind mounts, deferring to the
SingularityCE guide.

### What was changed to keep this from recurring

The pieces were all individually tested; the job script that joins them had
never been executed. That is the third bug of this shape in this project
— after an assumed input format and a shard count that capped concurrency.

`tests/stubs/singularity` now stands in for the container, so
`tests/test_experiment_0003_job.py` runs the real job script end to end
against a local HTTP server. Reverting the staging to `ln -sf` fails three
tests.

Honest limitation: the stub translates bind paths but does not isolate the
filesystem, so it would **not** by itself have caught this bug — under the
stub the absolute symlink still resolves. What catches it is the explicit
pair of assertions that no staged path is an absolute symlink, and that the
staged tree still works after being moved.

### Attempt 2, 2026-08-13: failed again, on the leftovers of attempt 1

Job wall **12 seconds**, no shard written. This time the job said so — the
shard count added after attempt 1 made it exit non-zero — but the run was
still lost.

```
cp: '.../slices_p1/slice_1.parquet' and
    '.../phase1_single/node0/slices/slice_1.parquet' are the same file
❌ cannot copy .../slices_p1/slice_1.parquet
bash: : No such file or directory
```

Attempt 1's symlinks were still sitting in the output directory. `cp -f`
does not remove a symlink destination; it follows it, sees source and
destination are the same file, and refuses. Reproduced exactly, locally, in
one command.

Then `bash ""` ran, because `run_phase_single` used the empty output of the
failed staging as a script path. That is why the phases printed `wall 0 s`
and looked like they had happened, and why `pbsdsh` reported `exit status
127` on the other node.

**Three faults, all fixed:**

1. **Staging was not idempotent.** It now removes the destination before
   copying. A re-run always finds the last run's files; that is the normal
   case, not an edge case.
2. **A failed staging did not stop the phase.** It now returns an error
   instead of running an empty command.
3. **A re-run would have mixed with the previous one.** Had attempt 1
   produced shards, the analysis would have summed both runs and reported
   the total as a single measurement. The job now moves any previous attempt
   to `previous_<jobid>/` — set aside, not deleted — and counts shards only
   under the current phase directories.

### Why the fix for attempt 1 did not prevent attempt 2

The end-to-end test added after attempt 1 ran the job **once, into an empty
directory**. Every real re-run starts from a directory that is not empty.
The test now runs the job twice into the same tree and checks both that it
succeeds and that the two runs stay separate. Removing either fix fails a
test.

Four bugs in this experiment have now come from the same place: the seams
between components, not the components. Each was invisible in unit tests and
each cost a queue slot.

### Also confirmed

Home is unaffected by dropping `--env HOME`: `df` on the home filesystem
reported 11% used both before and after the run, unchanged.

### Next

Third attempt. The question the experiment exists for is still unanswered:
no measurement of node distribution has been taken.

No manual cleanup is needed — the job sets the previous attempts aside
itself.
