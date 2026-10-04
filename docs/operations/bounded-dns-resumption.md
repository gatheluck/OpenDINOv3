# DataComp resumption: bounded DNS transport

2026-10-04. Acquisition is authorized for at most four nodes. The supplied
support guidance is 100 DNS queries/s/node when using 20 nodes, not a capacity
guarantee or permission to use 20 nodes. FRONTia team-wide reservations and
notification rules apply separately. Queue/account/path identifiers stay local.

## Implementation and limits

`OD_DNS_BUDGET=1` routes Python socket name resolution through dnspython 2.8.0.
Every UDP send and TCP write waits 20 ms while holding a process-shared file
lock. Retries, A/AAAA queries and TCP fallback share the same gate. This is
50 transport write attempts/s with no accumulated burst credits. TCP fragments
and failed writes consume extra intervals conservatively. This bounds the
Python downloader's DNS sends, not unrelated software or total node traffic.
Use one whole-node acquisition job per node; leave headroom for system traffic.

`production_job.sh` binds a user-specific node-local directory into all its
containers at `/dns-budget`; all spawned workers use `budget.json` there.
Never remove/replace it while workers are alive. Corrupt state, conflicting
budgets, missing dependencies or inability to initialize stop controlled work.
Counters describe attempts (including initialization probes), not responses or
packet capture. No setting changes the machine-wide DNS configuration.

The resolver uses system-configured nameservers and a TTL-aware cache. This
mode is intended for public HTTP URLs: it does not implement NSS/hosts-file
lookup or AI_ADDRCONFIG/AI_V4MAPPED. Numeric IPs require no DNS. Keep the old
fixed-TTL `OD_DNS_CACHE` disabled in this mode. Optional HTTP pooling is separate.
Native extensions or subprocesses performing their own DNS are outside this
Python socket override. Do not describe it as an OS-wide firewall or proof of
ABCI-wide compliance.

## Audit and restart order

Read-only audit found 1,388 planned tasks: 375 complete markers matching plan
rows, 59 started without DONE, 954 untouched. Complete markers report
239,024,316 successful records; remaining plan rows total 1,012,173,656.
These are marker/plan observations, not a fresh content hash verification.
Existing payloads and incomplete shards must be preserved and validated before
retry. Do not overwrite the predecessor corpus or reset the full campaign.

1. Run local transport, spawned-worker and real downloader tests.
2. Validate the candidate image/source on an isolated one-node canary; record
   effective source/image, DNS counters, yield, bytes and health results.
3. Resume missing tasks with at most four whole-node jobs and no competing
   controller, respecting current team-wide use and storage headroom.
4. Persist each task's DONE/health/logs plus periodic campaign accounting.

## Work log

- 2026-10-04: Inspected live plan/markers, group quota and current reservation
  through existing project records. No production task submitted yet.
- TDD RED: eight transport/budget assertions failed against a no-op skeleton:
  twelve DNS packets arrived in about 3 ms instead of at least 0.5 s, TCP/UDP
  writes were unpaced and invalid/conflicting budgets were accepted.
- GREEN: all eight passed after shared-lock transport pacing was implemented.
- TDD RED: two interpreter tests showed work continued without required control;
  GREEN: both passed after fail-closed sitecustomize initialization.
- TDD RED: two actual-shell tests detected missing submission/container settings;
  a real local-image acquisition incorrectly completed despite a missing budget
  directory. Forwarding and worker path activation are being verified.
- Initial sandbox-bind and missing-module failures were environment/scaffolding
  failures; they are explicitly excluded from the RED evidence above.

- Full Linux container regression: 649 passed, three pre-existing dependency
  deprecation warnings (159.09 s); subsequent TCP/retry/cache/error cases are
  covered by the focused suite. Runtime contract: six passed.
- Mutation checks on copied source: removing pacing, bypassing the TCP gate,
  and disabling required startup control each produced the expected failed
  behavioral assertion. Original source restored after each mutation.
- Required-budget settings now travel through submission and the actual job
  shell; successful controlled downloads record `settings.dns_budget` in DONE.
  The record is configuration evidence, not proof of total node wire traffic.

Implementation API references:
[resolver override](https://dnspython.readthedocs.io/en/stable/resolver-override.html)
and [transport socket factory](https://dnspython.readthedocs.io/en/stable/_modules/dns/query.html).

- Final related Linux tests: 109 passed (102.76 s), including real controlled
  image acquisition and all 14 DNS tests. New DNS module: Ruff and ty pass.
  Ruff on the two pre-existing production test files also reports existing
  findings outside this change; these are not suppressed or reported clean.
- Metadata preflight: 2,664 source parquet files and the existing SIF are
  present. The upstream source area remains read-only.

- Canary preflight found that `OD_MAX_URLS`, `OD_RETRIES`, and `OD_TIMEOUT`
  were not frozen into generated jobs. Three executed-shell RED cases each
  returned an empty setting. Serialize all three, preserving existing job
  defaults (unlimited, two retries, ten seconds) when not specified.

## First canary protocol (registered before execution)

Use one whole node, task 434 capped to the first 1,000 source URLs, an isolated
output directory, four processes/eight threads, 10,000 samples/shard, timeout
10 seconds, two retries, required DNS budget on, old DNS cache off, HTTP pool
on. Preserve face blurring (`OD_BLUR_FACES=1`): the last successful command
contains `--bbox_col face_bboxes`. One source shard means
this pilot primarily tests correctness; do not extrapolate full-task throughput.
Use the exact CI-published image digest and committed source. Require exit zero,
exactly 1,000 candidate records, the existing unmodified production health gate,
a partial DONE marker, and `settings.dns_budget=1`. Record source/image IDs,
success/failure counts and wall time. Failure means investigate, not scale up.
After success, validate/retry incomplete production tasks in waves of at most
four, with fresh scheduler and quota checks before each wave.

- Canary-setting GREEN: all 48 submission CLI tests pass (2.91 s).

- Final type check also covers the new DNS tests; explicit resolver installation
  assertions narrow optional library state without suppressing diagnostics.

## Live restart outcome: 2026-10-04

PR #54 was merged as `151d53fa8c3ff70d4da74aa02dafc005bfe802cc` and pulled.
The main-branch CI run passed **657 tests** (three existing deprecation warnings,
352.92 seconds), all six runtime checks, and published the tested image:

```
ghcr.io/gatheluck/opendinov3@sha256:4cab3c77d26b4205a2369badb471c9a3e54ecf68c943d10f566820045bb31b5b
```

The release was staged separately from the existing checkout and payloads.
Six runtime/dependency files matched the local committed source by SHA256.
The pulled SIF reported Python 3.12.15 and dnspython 2.8.0; its file hash was
saved alongside the source commit and OCI digest in the private run directory.

### Canary result

Task 434 ran on one whole node with the pre-registered 1,000-URL cap and settings.
PBS finished with exit **0** after 3 minutes 4 seconds; downloader wall time was
174 seconds. Output files totalled 53,617,292 bytes.

| Observation | Result |
|---|---:|
| Candidates | 1,000 |
| Successful records | 617 |
| Yield | 61.7% |
| DNS error fraction | 7.2% |
| Unreachable fraction | 0% |
| Existing health gate | healthy |
| DONE partial flag | true (not a completed million-URL task) |
| DONE settings.dns_budget | 1 |

The DNS error fraction is **not DNS QPS**. The first canary did not export the
node-local transport attempt counter or capture whole-node wire traffic. The
50-write/s bound is supported by the transport tests and the deployed source;
it does not establish a measured total-node DNS rate. Preserve that distinction
in later reporting. The canary's single shard is not a full-task throughput
estimate. Its output remains isolated from production.

The first qsub attempt failed before submission while copying its script to
temporary storage (`Disk quota exceeded`). Redirecting only this run's TMPDIR
to its dedicated group-area directory allowed submission. The failed attempt,
recovery and successful job identity are preserved in private logs; no existing
payloads or unrelated jobs were deleted, modified or cancelled.

### First production wave and continuation

After the canary passed, a fresh quota check showed 1,180 / 10,000 TiB and
167,487,835 / 600,000,000 files. Six other requested account nodes were present;
adding four acquisition jobs remained below the 16-node notification threshold
for the account snapshot. This account snapshot is not proof of all other team
members' usage; team-wide rules continue to apply independently.

Tasks **173, 346, 347 and 348** were submitted as four separate one-node jobs,
12 hours each, with four processes/eight threads, the DNS budget enabled, old
DNS cache disabled, HTTP pooling enabled, two retries and existing face blurring.
They target the original production task tree and use the tested shard-resume
logic. All four were observed in **R**, each holding its task lock; no completion
is claimed yet. Do not submit a second copy while these jobs remain active.

A 30-minute follow-up was enabled in the current chat to check jobs, save logs
and refill available acquisition slots. The cap includes queued and running
acquisition jobs. It must inspect actual scheduler state and task completion,
not infer completion from age or exit alone. Changed code still requires TDD
and the project's PR/approval process; routine acquisition does not wait for
another user instruction. The documentation PR does not block running work.

Private records live under
`${OD_OUT_ROOT}/production/resume-20261004-151d53f/`: `events.jsonl`,
`source-commit.txt`, `source-hashes.json`, `image-digest.txt`, `image-file.sha256`,
`canary.env.sh`, `canary-job.txt`, `logs/canary/`, and `logs/wave001/task-*/job.json`.
These retain real job identities and site configuration; do not copy them into
this public repository. Append operational events periodically and summarize
milestones here through a PR. Future waves must keep the four-node cap, inspect
quota and team use, and investigate failures before retrying.

## Task 173 investigation: 2026-10-04

At approximately 11:00 UTC, task 173 remained in PBS state R after more than
2 hours, but CPU time had advanced only seconds across successive observations.
Its task-lock heartbeat remained fresh. The other three acquisition jobs were
still updating output, so there is no evidence of a campaign-wide outage.

The task reused 99 finished shards and was processing only shard 7. The open
tar stopped growing at 536,084,480 bytes. A read-only tar header scan found
18,703 entries, with the last visible entry `000079925.jpg` (601,168 bytes),
near the end of the 10,000-row shard. The scan ended with `unexpected end of
data`; an unfinished, buffered tar is not proof of storage corruption. The
parquet sidecar remained empty and no DONE marker existed. The downloader log
contained startup messages only. These observations localize the symptom to
completion of the remaining shard; they do not identify a particular URL or
worker stack.

### Reproduced timeout limitation, not a confirmed production root cause

`pooled_download.download_image` passes `urllib3.Timeout(total=timeout)` and
then calls `response.read()` without an application-level transfer deadline.
A local HTTP server returned 20 bytes at 0.1-second intervals, with a declared
Content-Length of 20. Against the deployed source and dependency container,
`download_image(..., timeout=0.3, ...)` returned success after **2.709 seconds**
(including initial client import/setup). An assertion requiring completion
within 0.9 seconds failed for the intended reason. The first invocation lacked
the source mount and failed at import; that is an environment error, not RED.
No external dataset host was contacted by this reproduction.

This agrees with [urllib3's documented timeout semantics](https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html):
read/total timeouts do not bound the complete response when bytes keep arriving.
The pool uses `block=False`, ruling out waiting for a pool-capacity slot in
this path. A slow ongoing body is therefore a plausible explanation, but the
reproduction does **not** establish what the production worker is doing.
Other waits, image processing, or a lost worker result remain unexcluded.

The running job was submitted without compute-node SSH access enabled.
[ABCI documents this as disabled by default](https://docs.abci.ai/v3/en/job-execution/).
Existing logs do not contain thread stacks, and sending a diagnostic signal
without a registered handler could terminate the process. No such signal,
blind retry, cancellation, or change to running code was attempted.

Next corrective work should first provide observable worker stack/progress
information and a tested whole-transfer deadline, preserving DNS pacing,
face blurring, retry accounting and completed-shard reuse. Such code changes
need TDD, regression checks and a separate implementation PR; this investigation
alone is not evidence that a fix has been deployed or the task recovered.

## HTTP body deadline correction: 2026-10-04

A regression test now requires a response trickling bytes every 0.1 seconds
with timeout 0.4 seconds to fail promptly, and verifies that a subsequent normal
request succeeds. RED: the old downloader returned a successful stream instead.
A second RED exposed a compressed-response edge case: a slowly delivered gzip
filename kept urllib3's decoding read loop inside one call for 2.916 seconds.

The correction reads raw body chunks and checks a monotonic budget before and
after each read, then decodes the completed in-memory response. Incomplete
responses close their connection; empty error responses retain reuse. Ordinary
compressed bodies still decode correctly. This budget includes elapsed time
since the request began but is enforced at body-read boundaries. It is not a
hard deadline for DNS, TLS, response headers, CPU decoding, or a blocked system
call; existing socket timeouts still govern individual blocking reads. Do not
claim it guarantees that every request finishes in exactly `OD_TIMEOUT` seconds.
It fixes the reproduced body-trickle failure mode, not a proven diagnosis of
task 173's worker state. Decoding after collection adds memory overhead for
compressed bodies; no new image-size filtering is introduced.

Production rollout must use the reviewed merged commit and its published image.
Preserve the running jobs that are making progress. For task 173, retain the
99 completed shards and the failed-attempt logs, confirm its old job has exited
before one replacement is submitted, and enable account-only compute SSH for
diagnostics on the replacement. Keep the reserved queue, four-node cap,
`OD_DNS_BUDGET=1`, face blurring and group-area TMPDIR. Inspect actual worker
state if the replacement stalls; do not cycle retries based on age alone.

Validation: HTTP/DNS/production task/submission regression tests passed
**117 tests** (96.23 seconds, one existing dependency deprecation warning).
The command was `python -m pytest -o addopts='' tests/test_pooled_download.py
 tests/test_dns_budget.py tests/test_production_task.py
 tests/test_production_cli.py -q` inside the dependency container. Do not set a
global PYTHONPATH for this suite: doing so activates sitecustomize in helper
processes and caused two environment-induced failures in the first run.
Disabling the deadline in an isolated source/test copy made both new deadline
tests fail (2 failures, 7.73 seconds). An earlier mutation attempt accidentally
selected the original source via conftest and is not counted as validation.
Ruff checks cover the changed module and tests; identifier and diff checks pass.
`ty` reports an existing dynamic monkeypatch assignment diagnostic, reproduced
unchanged on the main-branch module; it is not suppressed or claimed green.

### CI follow-up and first completed attempt

The initial PR CI run passed 659 tests but failed the worker-exit DNS reporting
test: concurrent `print` calls interleaved a report and its newline. A new
controlled competing-writer test reproduced the malformed record (RED). Sending
the short report plus newline in one `os.write` fixes the interleaving; all 13
worker-patch tests pass. This fixes the observed race instead of weakening the
parser or rerunning CI until lucky. This reporting path is for the legacy DNS
cache, which remains disabled in the current bounded-DNS production jobs.

Task 346 finished downloading with exit 1 at the unchanged health gate:
1,000,000 candidates, 496,623 successes, DNS fraction 28.2766%. Separating by
this attempt's start time shows 77 reused shards (770,000 candidates, 352,269
successes, 267,945 DNS failures) and 23 newly written shards (230,000 candidates,
144,354 successes, 14,821 DNS failures). The new portion's DNS fraction is
6.444%, versus 34.798% in the reused portion. Aggregate rejection is therefore
dominated by old DNS-degraded shards. Keep this task incomplete; do not lower
the gate or blindly retry the identical reuse plan. Preserve good shards while
investigating targeted recovery of the degraded portion.

## First verified full completion: 2026-10-04 12:20 UTC

Task 348 completed with PBS exit 0 after 3:20:41. Its DONE marker matches task
348 and the full 1,000,000 planned candidates, has `partial=false`, records
551,287 successes and `settings.dns_budget=1`. The unchanged health gate passed:
yield 55.1287%, DNS fraction 19.2802%, unreachable fraction 0.1581%, 100 shards.
These totals include reused shards; they are not all newly downloaded records.

Tasks 346 and 347 completed their download passes but exited 1 at the health
gate, without DONE markers. Separating stats by the new attempt's start time:

| Task | Portion | Candidates | Successes | DNS failures |
|---|---|---:|---:|---:|
| 346 | reused | 770,000 | 352,269 | 267,945 |
| 346 | newly written | 230,000 | 144,354 | 14,821 |
| 347 | reused | 690,000 | 309,795 | 248,474 |
| 347 | newly written | 310,000 | 194,967 | 19,875 |

The approximately 6.4% DNS fractions in the new portions contrast with the
much worse reused portions. Do not lower the health gate or retry the same
reuse plan indefinitely. Keep good shards and investigate targeted recovery
of DNS-degraded history. Neither failed task is counted as completed.

Task 173 remained running with unchanged output; the HTTP body correction is
in PR #57, whose updated CI passed. At this observation, merge approval was
still pending; a scheduled heartbeat was not treated as approval. Task 379 was submitted after
346 ended. Fresh quota and scheduler checks before filling the next two slots
showed 1,181 / 10,000 TiB, 168,386,103 / 600,000,000 files and two active account
jobs. Tasks 380 and 381 were selected as unfinished and unlocked, keeping the
acquisition cap at four including queued jobs. Private job identities and
submission events remain in the run directory.

A diagnostic SSH option attempted for task 379 was rejected before job creation:
a second qsub `-v` replaced the wrapper's required RTYPE. Scheduler inspection
confirmed no new job. The subsequent submission used the existing working
arguments without SSH. Diagnostic SSH for task 173's eventual recovery requires
combining environment variables correctly through a tested wrapper change;
adding another `-v` is not a valid recovery procedure.

### PR integration update: 2026-10-04

PR #57 was subsequently merged as `d1c97e283fec084599c34f934873b6ec7b452866`.
PR #58 retains both the HTTP correction/validation record and the full-task
completion record. This documentation merge does not establish deployment of
the correction or recovery of task 173.

### PR #58 CI correction: graceful reporting probe shutdown

After resolving the documentation merge, CI failed with 660 passing tests and
one worker-statistics failure: reported hits and misses both summed to zero.
This was distinct from the previously fixed line-interleaving race. The probe
used a Pool context manager, whose exit terminates workers rather than waiting
for their Python exit callbacks. Atomic output cannot preserve a callback that
never executes.

RED: registering a 0.2-second exit cleanup in the workers reproduced the same
`(0, 0)` failure locally. GREEN: explicitly close and join the test pool before
cleanup, retaining all count/cache assertions and the delayed exit. This tests
graceful reporting rather than relying on scheduling luck. It does not change
production shutdown or promise that DNS statistics survive forced termination;
current production continues to use `OD_DNS_CACHE=0` and `OD_DNS_BUDGET=1`.
Conflict resolution alone was not proof of CI success. The latest head must
pass the complete CI workflow before this PR is reported ready.
