"""Contract for deciding whether a finished task is worth keeping.

On 2026-07-28 a roughly 24-hour loss of external connectivity destroyed 474
tasks. Every one of them ran to completion, wrote all 100 shards' statistics,
and stored almost nothing:

    70.6%  Name or service not known
    15.5%  [Errno 101] Network is unreachable
    13.8%  timed out
     0.1%  success

Nothing stopped, nothing warned, and the loss was found weeks later by
counting images rather than files. Several hundred node-hours were spent
writing zeros.

The guard exists so that the same outage costs one task instead of 474.
`Network is unreachable` is the decisive signal: remote hosts fail
individually all the time, but the local machine having no route is never
normal, so it is separated from the general failure bucket rather than
averaged into it.
"""

from __future__ import annotations

import pytest

from opendinov3.core import download_stats as ds
from opendinov3.core import task_health as th

DNS = "<urlopen error [Errno -2] Name or service not known>"
UNREACHABLE = "<urlopen error [Errno 101] Network is unreachable>"
TIMEOUT = "<urlopen error timed out>"


def stats(**status) -> dict:
    total = sum(status.values())
    return {"count": total, "successes": status.get("success", 0),
            "status_dict": dict(status)}


def make_run(*, candidates: int, successes: int, unreachable: int,
             dns: int) -> ds.RunSummary:
    """A run summarised straight from counts, for threshold tests."""
    other = candidates - successes - unreachable - dns
    return ds.RunSummary.from_stats(32, 3600.0, [stats(**{
        "success": successes, UNREACHABLE: unreachable, DNS: dns,
        TIMEOUT: max(0, other)})])


# --------------------------------------------------------------------------
# The signal that identifies an outage
# --------------------------------------------------------------------------

def test_no_route_from_this_machine_is_counted_separately() -> None:
    """A remote host refusing is routine; having no route is not."""
    counts = ds.classify({UNREACHABLE: 30, "success": 70})
    assert counts.unreachable == 30


def test_no_route_is_not_also_counted_as_something_else() -> None:
    """Left in the general bucket it would be averaged away: the outage
    tasks showed 15.5% unreachable against a normal 'other' of about 5%."""
    counts = ds.classify({UNREACHABLE: 30, "success": 70})
    assert counts.other == 0
    assert counts.transient == 0
    assert counts.dns == 0


def test_no_route_to_host_counts_the_same_way() -> None:
    counts = ds.classify({
        "<urlopen error [Errno 113] No route to host>": 5, "success": 95})
    assert counts.unreachable == 5


def test_ordinary_failures_are_unaffected() -> None:
    counts = ds.classify({
        DNS: 60, "HTTP Error 404: Not Found": 85, TIMEOUT: 11, "success": 644})
    assert (counts.dns, counts.permanent, counts.transient) == (60, 85, 11)
    assert counts.unreachable == 0


# --------------------------------------------------------------------------
# The verdict on one finished task
# --------------------------------------------------------------------------

def summary(**status) -> ds.RunSummary:
    return ds.RunSummary.from_stats(processes=32, wall_seconds=3600.0,
                                    stats=[stats(**status)])


def test_a_normal_task_is_healthy() -> None:
    """The measured production profile: 64.4% success, 6% DNS."""
    health = th.assess(summary(**{
        "success": 644, DNS: 60, "HTTP Error 403: Forbidden": 88,
        "HTTP Error 404: Not Found": 85, TIMEOUT: 11, "Image decoding error": 20,
        "HTTP Error 429: Too Many Requests": 8, "HTTP Error 400: Bad Request": 11,
    }))
    assert health.healthy is True
    assert health.reason is None


def test_the_outage_profile_is_rejected() -> None:
    """The exact breakdown of the 474 lost tasks."""
    health = th.assess(summary(**{
        DNS: 706, UNREACHABLE: 155, TIMEOUT: 138, "success": 1,
    }))
    assert health.healthy is False
    assert "unreachable" in health.reason.lower()


def test_a_small_amount_of_no_route_no_longer_fails_a_working_task() -> None:
    """This test used to assert the opposite, and the belief behind it was
    wrong.

    It read: "Held tight on purpose: this is never normal, and detecting it
    early is what turns a 474-task loss into a one-task loss." The premise —
    that no-route is never normal — came from one- and two-node runs. On
    2026-08-15 an eight-node wave stored 618,919 images from 1,000,000 URLs,
    a 61.9% yield, and was rejected for 2.05% unreachable. The step from
    four nodes to eight multiplies the figure by about 15, in both retry
    settings independently.

    3% unreachable alongside a 60% yield is a busy network, not an outage.
    The task below stored 600 of 1,000 and is worth keeping.
    """
    health = th.assess(summary(**{"success": 600, UNREACHABLE: 30, DNS: 60,
                                  "HTTP Error 404: Not Found": 310}))
    assert health.healthy is True, health.reason


def test_a_collapsed_yield_is_rejected_even_without_a_clear_cause() -> None:
    """A cause we have not seen before must not pass merely because it is
    unfamiliar."""
    health = th.assess(summary(**{"success": 100, "something new entirely": 900}))
    assert health.healthy is False
    assert "yield" in health.reason.lower()


def test_a_dns_spike_is_rejected() -> None:
    health = th.assess(summary(**{"success": 400, DNS: 600}))
    assert health.healthy is False
    assert "dns" in health.reason.lower()


def test_yield_a_little_below_normal_is_accepted() -> None:
    """URL lists vary. The guard catches collapse, not variation — a
    threshold near the normal 65% would stop the run on ordinary tasks."""
    health = th.assess(summary(**{"success": 450, DNS: 60,
                                  "HTTP Error 404: Not Found": 490}))
    assert health.healthy is True


def test_a_task_that_attempted_nothing_is_rejected() -> None:
    """Zero attempts is not a yield of zero; it is a task that did not run."""
    health = th.assess(ds.RunSummary.from_stats(32, 10.0, []))
    assert health.healthy is False
    assert "no attempt" in health.reason.lower()


def test_the_reason_names_the_measured_values() -> None:
    """A guard that says only 'failed' sends someone back to the logs."""
    health = th.assess(summary(**{DNS: 706, UNREACHABLE: 155, TIMEOUT: 138,
                                  "success": 1}))
    assert "15.5%" in health.reason or "15.50%" in health.reason


# ---------------------------------------------------------------------------
# The threshold's premise was falsified at scale
# ---------------------------------------------------------------------------

def test_a_healthy_yield_is_not_an_outage_however_unreachable_rises() -> None:
    """Measured on the cluster, 2026-08-15: an 8-node wave stored 618,919
    images from 1,000,000 URLs — 61.9%, squarely normal — and was rejected
    because unreachable was 2.05% against a 1% limit.

    The limit's comment read 'Normal is zero. Nothing routine produces
    this.' That was measured on one and two nodes. At eight it is 2%, and
    the same 15x step from four nodes to eight appears in both retry
    settings, so it is a property of concurrency rather than of the network
    being down.

    An outage cannot produce a 62% yield. The 2026-07-28 outage produced
    0.1%, which MIN_YIELD catches on its own.
    """
    run = make_run(candidates=1_000_000, successes=618_919,
                   unreachable=20_478, dns=60_000)
    verdict = th.assess(run)
    assert verdict.healthy, verdict.reason


def test_the_outage_is_still_caught() -> None:
    """The profile that cost 474 tasks: 70.6% DNS, 15.5% unreachable, 0.1%
    success. Relaxing the unreachable rule must not let this through."""
    run = make_run(candidates=1_000_000, successes=1_000,
                   unreachable=155_000, dns=706_000)
    verdict = th.assess(run)
    assert not verdict.healthy
    assert verdict.reason is not None


def test_unreachable_still_rejects_when_the_yield_is_also_degraded() -> None:
    """A partial outage: enough gets through to clear the 30% floor, but a
    fifth of the corpus is being lost to a transient condition and the task
    is worth retrying rather than accepting."""
    run = make_run(candidates=1_000_000, successes=400_000,
                   unreachable=200_000, dns=60_000)
    verdict = th.assess(run)
    assert not verdict.healthy
    assert "unreachable" in (verdict.reason or "")


def test_the_reason_no_longer_calls_a_busy_network_an_outage() -> None:
    """The old message asserted 'This is an outage, not a bad URL list' for
    a task that stored 618,919 images. Saying so sent the operator looking
    for a network failure that had not happened."""
    run = make_run(candidates=1_000_000, successes=400_000,
                   unreachable=200_000, dns=60_000)
    assert "outage" not in (th.assess(run).reason or "").lower()


def test_the_limit_itself_is_pinned_where_the_gate_cannot_hide_it() -> None:
    """With a degraded yield the gate does not apply, so the limit alone
    decides. 5% unreachable passed the old 1% limit's rejection and passes
    the new 10% one — this is the case that tells them apart."""
    run = make_run(candidates=1_000_000, successes=400_000,
                   unreachable=50_000, dns=60_000)
    assert th.assess(run).healthy, "5% unreachable is under the 10% limit"


def test_the_gate_itself_is_pinned_where_the_limit_cannot_hide_it() -> None:
    """Above the limit but plainly working: only the yield gate can save
    this, so removing the gate shows up here and nowhere else."""
    run = make_run(candidates=1_000_000, successes=600_000,
                   unreachable=150_000, dns=60_000)
    assert th.assess(run).healthy, "a 60% yield is not an outage"
