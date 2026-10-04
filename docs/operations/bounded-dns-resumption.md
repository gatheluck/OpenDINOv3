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
