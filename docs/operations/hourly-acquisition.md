# Migrating an in-flight acquisition to one-hour jobs

Decision (2026-10-26): use a hard walltime of `01:00:00` for new jobs,
with at most 100,000 candidates per new task. The hour is a team scheduling
requirement, not a throughput estimate. Completion within that hour has not
been measured for this configuration; validate a first job before refilling.
Existing submitted jobs finish with their original plan and limits.

## Prepare a new plan

Stop concurrent submissions while reconciling scheduler jobs, saved job IDs,
submission events and task outputs. Choose `FIRST_NEW_TASK` strictly after
all ever-submitted IDs. A queued job may have no directory yet: the CLI's
filesystem guard alone does **not** establish that the suffix is unsubmitted.
Keep unfinished older tasks on their original ranges and resume them separately;
never reinterpret their shards using smaller ranges.

After the change PR is reviewed and merged, create a separate plan:

```bash
python scripts/repartition_plan.py "$OLD_PLAN" "$NEW_PLAN" \
  --first-task "$FIRST_NEW_TASK" --urls-per-task 100000 \
  --task-root "$OD_TASK_ROOT"
```

The tool preserves prefix entries, ordered source rows and the full candidate
total. It records the original file's SHA-256, refuses any suffix output,
and never overwrites a destination. Retain both plans. Verify the new artifact
before submission; an interrupted write is not a valid plan. An empty suffix
is allowed. `urls_per_task` describes the **new suffix**, not preserved tasks;
read each entry's `rows` for completion checks. Task-count percentages are no
longer comparable across this migration: use candidate totals.

## Submit and validate

Use the previously verified downloader source and image. The planning utility
runs offline; changing the plan does not require replacing the downloader.
Set the production paths explicitly, including `OD_PLAN=$NEW_PLAN`, metadata,
image, output root and a fresh per-wave log directory. Never source experimental
output paths or `MAX_URLS` into production. Keep the reservation-only wrapper
`od_qsub.sh` and its dedicated group `TMPDIR`.

```bash
source config/datacomp-hourly.env.sh
bash scripts/submit_production.sh \
  --from "$FIRST_NEW_TASK" --to "$FIRST_NEW_TASK" --dry-run
```

Inspect the generated command and plan before submitting the same range without
`--dry-run`. Before every submission check live and pending jobs, saved IDs,
capacity and team usage. The total running plus queued acquisition jobs remains
at most eight, and shared-team limits still apply. Do not queue the entire new
suffix with an eight-running-job cap; that is not an eight-outstanding-job cap.

For the first job, record scheduler walltime and exit status, complete candidate
count, shard integrity, success rate, DNS failure rate and elapsed time. Keep
existing health gates. Refill only after successful validation. If it exceeds
one hour, inspect the failure and reuse validated shards via existing resume
logic; do not blindly resubmit or extend the agreed hour. Reassess future
unsubmitted task sizes from evidence. A new scheduler job gives the scheduler
another allocation decision; immediate resubmission does not guarantee other
teams an idle interval. No new inter-job delay was agreed.

## Development evidence and handoff

- Core RED: 11 behavior failures, 3 passes with an identity placeholder.
- CLI RED: 6 behavior failures with a no-output placeholder.
- GREEN: core plus original partition tests, 26 passed; CLI/core and production
  submission tests, 70 passed, including real Parquet extraction and an executed
  submitter that receives `--walltime 01:00:00` and the required settings.
- Five isolated mutations (source offset, prefix, remainder, existing output,
  overwrite) were all detected after a 21-pass baseline.
- Profile RED: one behavior failure with an empty profile; GREEN: one pass.
- Ruff and ty passed for the new Python modules and partition implementation.
  Container runtime constraints: six passed.
- Full regression suite: 699 passed (three upstream deprecation warnings);
  final focused checks after adding the production-size boundary: 84 passed.
  The old `opendinov3:test` image failed collection because it lacks dnspython;
  validation used `opendinov3:dns-budget` after `pip check` passed.
- The first production job and its duration remain unverified until rollout.
- Review/merge is required before operational installation. The continuation
  automation suspends old-format refills during this transition while retaining
  observation and validation of already submitted jobs.

Reproduce the focused checks inside the runtime container:

```bash
docker run --rm -v "$PWD:/w" -w /w opendinov3:dns-budget \
  python -m pytest -p no:cacheprovider \
  tests/test_repartition_tail.py tests/test_repartition_plan.py \
  tests/test_metadata_partition.py tests/test_production_cli.py \
  tests/test_hourly_profile.py
```

## Other corpora

For COYO inventory, explicit face policy and the first-job preparation, see
[coyo-resume.md](coyo-resume.md). Do not source the DataComp profile for COYO:
it enables face blurring, but COYO metadata has no face boxes.
