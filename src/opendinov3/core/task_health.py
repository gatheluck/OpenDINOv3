"""Decide whether a finished download task is worth keeping.

WHY THIS EXISTS

On 2026-07-28 a roughly 24-hour loss of external connectivity destroyed 474
tasks. Every one of them ran to completion and wrote all 100 shards'
statistics:

    70.6%  Name or service not known
    15.5%  [Errno 101] Network is unreachable
    13.8%  timed out
     0.1%  success

Nothing stopped and nothing warned. The loss was found weeks later by
counting images rather than counting files, after several hundred node-hours
had been spent writing zeros.

With this guard the same outage costs one task.

CHOOSING THE THRESHOLDS

The normal profile is measured, twice, on different tasks: 64–65% yield, 6%
DNS, no unreachable errors. The outage profile is measured too. The
thresholds sit between them, and closer to the outage, because the cost of
stopping a healthy task is one requeue while the cost of continuing through
an outage is the whole run.

`unreachable` is held tight on purpose. A remote host refusing is routine;
this machine having no route is never normal, and it is the signal that
distinguishes an outage from a poor URL list.
"""

from __future__ import annotations

from dataclasses import dataclass

from .download_stats import RunSummary

#: Normal is 64–65%. The outage produced 0.1%. Set well below normal so
#: ordinary variation between URL lists does not stop a run.
MIN_YIELD = 0.30

#: Normal is 6.0–6.2%, flat across concurrency and node count. The outage
#: produced 70.6%.
MAX_DNS_FRACTION = 0.20

#: FALSIFIED AND REVISED, 2026-08-15.
#:
#: The old value was 0.01 with the note "Normal is zero. Nothing routine
#: produces this." That was measured on one and two nodes. On eight, a wave
#: that stored 618,919 images from 1,000,000 URLs — 61.9% yield, squarely
#: normal — recorded 2.05%, and was rejected for it. The step from four
#: nodes to eight multiplies this figure by about 15, in both retry
#: settings independently, so it is a property of concurrency rather than
#: of the network being down.
#:
#: Held at 10%: above the 2.1% that eight nodes produce while perfectly
#: healthy, below the 15.5% of the 2026-07-28 outage.
MAX_UNREACHABLE_FRACTION = 0.10

#: Above this yield, a task is working, whatever the unreachable rate says.
#: An outage cannot produce it: 2026-07-28 yielded 0.1%. Normal is 58-65%.
#: This exists so the unreachable rule cannot throw away a task that plainly
#: succeeded, which is what it did before being revised.
HEALTHY_YIELD = 0.50


@dataclass(frozen=True)
class Health:
    healthy: bool
    reason: str | None
    yield_rate: float | None
    dns_fraction: float | None
    unreachable_fraction: float | None


def assess(
    run: RunSummary,
    min_yield: float = MIN_YIELD,
    max_dns: float = MAX_DNS_FRACTION,
    max_unreachable: float = MAX_UNREACHABLE_FRACTION,
) -> Health:
    """Whether this task's output should be kept and marked done.

    Checks the decisive signal first: an outage is worth naming as an outage,
    because the response to it is to wait rather than to investigate URLs.
    """
    if run.candidates <= 0:
        return Health(False, "no attempt was made: the task recorded 0 "
                             "candidate URLs", None, None, None)

    unreachable = run.counts.unreachable / run.candidates
    dns = run.dns_fraction
    yield_rate = run.yield_rate

    def result(reason: str | None) -> Health:
        return Health(reason is None, reason, yield_rate, dns, unreachable)

    # Only a rejection when the yield is ALSO degraded. A task that stored
    # 62% of what it attempted is not suffering an outage, whatever its
    # unreachable rate; calling it one sent the operator looking for a
    # network failure that had not happened, and discarded good data.
    working = yield_rate is not None and yield_rate >= HEALTHY_YIELD
    if unreachable > max_unreachable and not working:
        return result(
            f"network unreachable from this node for {unreachable:.1%} of "
            f"attempts "
            f"(limit {max_unreachable:.1%}) AND the yield is only "
            f"{yield_rate:.1%} (healthy is {HEALTHY_YIELD:.0%}+). "
            "Connectivity is degraded rather than the URL list being poor; "
            "retry later rather than accepting this."
        )

    if dns is not None and dns > max_dns:
        return result(
            f"DNS failed for {dns:.1%} of attempts (limit {max_dns:.1%}, "
            f"normal 6%). Resolver or connectivity problem; yield was "
            f"{yield_rate:.1%}."
        )

    if yield_rate is not None and yield_rate < min_yield:
        return result(
            f"yield {yield_rate:.1%} is below {min_yield:.0%} (normal 65%) "
            f"with no single cause identified: DNS {dns:.1%}, unreachable "
            f"{unreachable:.1%}. Inspect the failure breakdown before "
            "continuing."
        )

    return result(None)
