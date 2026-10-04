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
