# DNS hostname admission before resolver deadlines

The transport budget still permits at most 50 write attempts per second per
shared gate. Increasing HTTP concurrency can nevertheless cause local DNS
failures: dnspython computes a query expiration before the socket send call,
so waiting for the transport lock consumes that timeout. A local immediate UDP
responder reproduced success without contention and timeout with a held gate.
This mechanism does not establish the cause of every observed DNS error.

The installed resolver now admits at most 16 active hostname resolutions across
processes sharing a gate. Admission wraps resolve_name before its A/AAAA
lifetime starts. Waiting callers poll shared nonblocking file-lock slots and
fail after a 60-second admission wait. Exceptions release the slot, and process
exit releases the OS lock. Slots live beside the budget file in its existing
node-local bind; never remove or replace them while workers are running.

The active resolver retains its three-second query timeout, 30-second lifetime,
cache, retries and TCP fallback. Every transport write still passes through the
original budget. This bounds contention rather than changing network deadlines
or granting additional sends. Numeric host handling remains dnspython's original
shortcut. Direct calls to resolver.resolve or dns.query do not use hostname
admission; the acquisition path uses getaddrinfo / resolve_name. Mixed old and
new workers do not share the new admission bound; use one pinned version per
whole acquisition node for comparison.

Sixteen slots are an initial conservative choice, not an observed optimum.
Admission adds bounded queueing latency to getaddrinfo and can itself time out
under sustained demand. It does not guarantee throughput or eliminate network
failures. No production speed improvement is claimed by this code change.

## Validation and next experiment

TDD RED: the installed-resolver concurrency test observed 32 active hostname
lookups against the expected maximum of 16. The first GREEN passed that test
and 14 existing DNS transport tests. Additional checks cover cross-process
sharing, exception release, bounded waiting, process death, and real local DNS
resolution whose admission wait exceeds the resolver lifetime. Removing the
admission wrapper in an isolated copy again failed the concurrency assertion
(32 > 16); no external resolver was contacted for these checks.

After merge, successful CI and approved rollout, repeat the comparison in
[experiment 0006](../experiments/0006-bounded-dns-throughput.md) on a fresh,
isolated set of outputs. Keep its candidate count, shard size, rate limit,
quality criteria and node cap unchanged; record the new source/image identity.
Stop the sequence on any failed arm. Only compare complete healthy arms and
check the repeated baseline before promoting concurrency. A failed experiment
is evidence to investigate, not permission to relax the health gate.

Focused validation: `python -m pytest -o addopts="" tests/test_dns_admission.py
 tests/test_dns_budget.py tests/test_dns_measurement.py -q` passed 29 tests in
4.65 seconds. Ruff, ty, relative-link and identifier checks passed. Full-suite
and CI outcomes are recorded in the PR; these checks do not prove cluster speed.
