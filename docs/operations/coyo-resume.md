# COYO one-hour restart preparation

## Decision and scope (2026-10-27 JST)

The user requested preparation for COYO after parking DataComp, with a hard
one-hour walltime. The user explicitly chose no face blurring. COYO's 128
upstream Parquet files have 746,972,269 rows, `url`, `text`, `id`, and `num_faces`,
but no face boxes; the existing schema inspector confirmed `coyo700m` matches.
No COYO production job has been submitted by this preparation.

## Existing acquisition evidence

Read-only inventory of canonical legacy tasks 1–746, excluding pilot task 0:

| Metric | Observed |
| --- | ---: |
| Present tasks / DONE markers | 746 / 746 |
| Shard statistics read | 74,598 |
| Candidate records | 745,972,269 |
| Successful records | 1,230,824 |
| Yield | 0.1650% |
| DNS failure fraction | 92.5540% |
| Unreachable fraction | 1.2119% |
| Tasks passing current health gates | 0 |
| Tasks with zero successes | 375 |

The scan had no unreadable statistics or arithmetic errors. These are historical
statistics, not a current DNS measurement or unique-image count. Pilot task 0
separately records 1,000,000 candidates and 348,290 successes; do not add it to
canonical coverage without resolving its input mapping. Legacy DONE fields
are unreliable: task 1's marker reports `attempted=successful=58777` and an
output path ending in task 0 while its shard statistics record one million
attempts. Do not use those markers as permission to skip or overwrite data.

The inventory command is read-only and returns exit 2 for unreadable/invalid
statistics. Explicit canonical bounds exclude pilot and archived attempts.
It always reports `reuse_verified=false`: no payload, URL correspondence,
checksums or image decoding are implied by this statistical scan.

```bash
python scripts/acquisition_inventory.py "$COYO_LEGACY_TASK_ROOT" \
  --first-task 1 --last-task 746 > "$COYO_INVENTORY"
```

## First-job candidate, not a bulk-submission plan

Keep the predecessor tree read-only. Use a separate COYO output namespace,
never DataComp's task tree and never reinterpret the old million-row shards
as ten-thousand-row shards. Retain all old successful records.

A draft plan generated with the existing planner uses 10,000 candidates/task:
74,698 tasks, total 746,972,269, final task 2,269 rows. Its site-local artifact
SHA-256 is `83495337d0c1e5e5b55a35288bab5c54905cb59223984288ce97b000f03c4798`.
This is a coverage reference for selecting one pilot, **not authorization to
submit the whole corpus again**. Do not use its printed DataComp throughput
model as a COYO prediction.

Candidate task 1500 covers global rows [15,000,000, 15,010,000). Legacy task 15
records zero successes across 100 shards; its task record declares input rows
[15,000,000, 16,000,000). The plan names upstream part 2 rows
[3,327,854, 3,337,854). This avoids a knowingly successful canonical range.
Before actual submission, verify URL/ID correspondence to the old manifest
and quarantine/pilot overlap. The interval correspondence alone does not prove
identity. No bulk recovery selection or verified-success exclusion index has
been built yet.

Use explicit paths for `OD_PLAN`, `OD_META_ROOT`, `OD_TASK_ROOT`, `OD_SIF`,
`OD_LOGDIR`; set `OD_BLUR_FACES=0`, then source `config/coyo-hourly.env.sh`.
It refuses missing paths or an unset/unsupported blur decision. Settings:
4 processes, 32 threads, 10,000/shard, timeout 10, retries 2, MAX_URLS=0,
HTTP pool 1, DNS cache 0, DNS budget 1 (50 send attempts/s/node). One hour
is the scheduling requirement, not a completion guarantee.

```bash
source config/coyo-hourly.env.sh || exit
bash scripts/submit_production.sh --from 1500 --to 1500 --dry-run
```

Preparation used only temporary paths on ABCI. Install reviewed/merged artifacts
and select permanent COYO output/log paths before execution. Use the reservation
wrapper and dedicated group TMPDIR; reconcile scheduler state, saved IDs,
output, current quota and shared-team usage before submitting. The DataComp
eight-node authorization is not a fresh COYO scaling decision. Start with one
job only after explicit merge/production approval.

After that job: check scheduler exit and actual walltime, all planned candidates,
TAR/Parquet keys/counts/statistics, captions and COYO ID retention, health gates,
DNS failures, success rate and measured speed. Keep gates unchanged. Do not
refill automatically or extend the hour on timeout. For bulk continuation,
reconcile successful legacy samples and input ranges first, preserve them in
place, and construct only the missing/retry ranges. Up to 100,000 candidates per
future task is provisional until COYO timing is measured; use MAX_URLS=0.

## Development evidence

- Inventory RED: five behavior failures against a zero-result implementation.
- Profile RED: nine behavior failures against an empty profile.
- GREEN: all 14 new tests passed; whole regression suite 714 passed.
- Existing COYO local-HTTP integration tests exercise actual downloads, identifier
  retention and refusal of unsupported blur; resume and partition tests passed.
- Three isolated-copy mutations detected: arithmetic guard removed, error exit
  suppressed, walltime changed to 12 hours.
- Runtime constraints: 6 passed. Ruff and ty checks on new Python files passed.
- No COYO runtime throughput is established by these tests.

ABCI temporary-directory dry-run returned `tasks 1500..1500`, `blur faces 0`,
`walltime 01:00:00`, and PBS indices `1501-1501`; it explicitly did not submit.
The preview had no submitter configured and used temporary outputs, so it
verifies command generation, not reservation-wrapper execution or deployment.
The local type-check initially lacked pytest; rerun with `uv tool run --with
pytest ty check` supplies the test dependency rather than suppressing the error.
