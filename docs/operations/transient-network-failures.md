# Temporary DNS failures and timeouts

## Evidence and limits (2026-10-27)

A completed COYO verification shard contained 9,875 candidates and 5,158
successes. Its final row messages include 256 temporary DNS failures, 767
name-not-known failures, 156 connect timeouts and 67 read timeouts. These are
row outcomes after retries, not individual attempts or DNS packet counts.
The sample is a contiguous input interval, not a representative random sample
of the entire dataset. Run identities and original statistics remain in the
private acquisition worklog.

`dns_report.failure_phases(status_dict)` reports aggregate final outcomes
without printing image URLs. Its message-based categories describe observed
failure phases, not a proven root cause. Connect failures can originate at the
destination or in the network; read failures can reflect slow headers or body
reads. EAI_AGAIN can reflect resolver/network delays or workload-local waiting.
EAI_NONAME alone does not prove a permanently dead domain.

The pinned img2dataset implementation repeats a failed request immediately,
up to `retries + 1` attempts. The HTTP pool disables its own retries, so it does
not multiply this budget. DNS admission precedes the resolver lifetime: the
current bounds are 60 seconds for admission, 30 seconds for resolution, and
three seconds per DNS query. The HTTP transfer deadline is separate. A ten
second HTTP setting is therefore **not** a ten second bound on the complete
DNS-plus-HTTP operation. See [DNS admission](dns-resolution-admission.md).

The logs do not identify which DNS server or wait phase caused each EAI_AGAIN,
nor contain every retry outcome. No claim that ABCI's resolver is broken, or
that all transient failures are recoverable, follows from this investigation.

## Bounded change

With HTTP pooling installed, the existing retry entry point now waits 0.5,
1.0, then at most 2.0 seconds between temporary DNS/timeout attempts. With the
current two retries this adds at most 1.5 seconds per affected row. Successful
requests and exhausted retries never sleep. Attempt counts, timeout settings,
DNS transport budget, admission limits, and health thresholds are unchanged.
No retries are added inside urllib3. Other failure categories retain upstream
behavior; this change does not fix missing images or publisher refusals.

Runtime classification uses actual exception types and EAI_AGAIN, including
urllib3's wrapped causes. A string subclass preserves the exact final error
text while carrying the transient marker within the worker. URL text cannot
trigger retries or delays. This differs from the offline message classifier,
which is for reporting only. Installation is repeated safely in spawned
workers; pooling disabled leaves upstream behavior untouched.

## Verification and rollout gate

TDD first reproduced four failures: immediate attempts instead of the required
spacing. A separate boundary test caught URL-text misclassification before
the exception-based implementation. Tests exercise the installed img2dataset
entry point, successful recovery via a real local HTTP server after injected
EAI_AGAIN, retry exhaustion, zero retries, unchanged other failures, and a
spawned worker performing actual urllib3 calls with a failing resolver.

Local tests prove the retry policy, not improved production yield. Do not
replace a pinned running source tree. Merge and production deployment require
individual approval. Existing data and completed jobs remain intact.

Before rollout, preregister a paired A/B/B/A comparison on a frozen, separately
recorded sample of at most 1,000 failed candidates plus 200 success controls.
Stratify the failed sample into EAI_AGAIN, connect timeout and read timeout;
record sample hashes, original outcomes and selection seed before any requests.
Use baseline and candidate commits with the same pinned image, one reserved
node at a time, one-hour walltime, two retries, timeout ten, and the existing
50-send-attempt/s DNS budget. Start each arm with fresh process caches and
separate output directories; never merge experimental copies into canonical
data or overwrite prior results. Check team capacity before each arm.

Compare paired recovery counts by category, DNS sends, successes per wall
second and walltime. The deliberately failure-enriched sample cannot be judged
by production yield thresholds or used to estimate whole-dataset yield. Keep
the production health gate unchanged; any later production canary must pass it.
Stop on job failure or one-hour timeout. Promote only if both candidate arms
recover more failed rows than their adjacent baselines and do not reduce
successes per wall second by more than 5%; otherwise retain the baseline and
investigate. Report negative and inconclusive results, including time-order
effects. No production performance claim has yet been made.

Validation commands and results for this change:

- RED: `python -m pytest -p no:cacheprovider tests/test_transient_retry.py
  --tb=short` initially failed four spacing assertions (one zero-retry case
  passed). URL-text and connection-refusal boundary tests each subsequently
  reproduced an intended failure before their fixes.
- GREEN/regression: `python -m pytest -p no:cacheprovider --tb=short` passed
  736 tests. The local runtime image initially lacked dnspython; collection
  failure was an environment problem, not RED. Installing the pinned 2.8.0
  in an isolated container resolved it. Every other locked package matched.
- Isolated-copy mutations removing the delay, omitting retry installation, and
  treating EAI_NONAME as transient were all detected. The first permanent-DNS
  mutation survived because the mocked wrapper lacked its cause; adding the
  real cause fixed this coverage gap and the repeated mutation failed.
- Ruff on changed Python files, ty on changed runtime modules, identifier guard,
  relative documentation links and `git diff --check` passed.

No cluster rollout or recovery-rate comparison has been executed for this
candidate. Existing pinned acquisition jobs do not use this change.
