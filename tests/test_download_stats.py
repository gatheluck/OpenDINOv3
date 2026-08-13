"""Contract for reading img2dataset statistics and comparing runs.

The concurrency experiment stands or falls on how its numbers are derived,
so the derivation is tested against known inputs rather than eyeballed once.
The status_dict keys used here are taken verbatim from shards produced on the
cluster; inventing plausible-looking keys would test the wrong strings.
"""

from __future__ import annotations

import pytest

from opendinov3.core import download_stats as ds

# Verbatim keys observed in _stats.json on the cluster.
DNS_KEY = "<urlopen error [Errno -2] Name or service not known>"
TIMEOUT_KEY = "<urlopen error timed out>"


def make_stats(**status) -> dict:
    successes = status.get("success", 0)
    total = sum(status.values())
    return {
        "count": total,
        "successes": successes,
        "failed_to_download": total - successes,
        "failed_to_resize": 0,
        "status_dict": dict(status),
    }


# --------------------------------------------------------------------------
# Failure classification
# --------------------------------------------------------------------------

def test_dns_failures_are_identified_by_their_errno() -> None:
    """Matching on the message text alone would be fragile.

    urllib phrases this differently across platforms; the errno is the stable
    part.
    """
    counts = ds.classify({DNS_KEY: 7, "success": 3})
    assert counts.dns == 7


def test_permanent_and_transient_failures_are_separated() -> None:
    """Retrying is only worth doing for one of these two groups.

    404 and 403 will not become successes on a second attempt; a timeout or a
    503 might.
    """
    counts = ds.classify({
        "HTTP Error 404: Not Found": 5,
        "HTTP Error 403: Forbidden": 3,
        "HTTP Error 410: Gone": 2,
        TIMEOUT_KEY: 4,
        "HTTP Error 503: Service Unavailable": 1,
        "HTTP Error 429: Too Many Requests": 6,
        "success": 79,
    })
    assert counts.permanent == 10
    assert counts.transient == 11


def test_rate_limiting_is_counted_separately_as_well() -> None:
    """429 is transient, but it is also the signal that we pushed too hard.

    Folding it into the transient bucket alone would hide exactly the effect
    the concurrency experiment is looking for.
    """
    counts = ds.classify({"HTTP Error 429: Too Many Requests": 6, "success": 94})
    assert counts.rate_limited == 6


def test_unrecognised_keys_are_kept_rather_than_dropped() -> None:
    """Silently discarding unknown failures would make the totals lie."""
    counts = ds.classify({"something entirely new": 4, "success": 96})
    assert counts.other == 4
    assert counts.total == 100


# --------------------------------------------------------------------------
# Per-run summary
# --------------------------------------------------------------------------

def test_run_summary_computes_yield_and_rate() -> None:
    run = ds.RunSummary.from_stats(
        processes=8,
        wall_seconds=100.0,
        stats=[make_stats(success=640, **{DNS_KEY: 360})],
    )
    assert run.candidates == 1000
    assert run.successes == 640
    assert run.yield_rate == pytest.approx(0.64)
    assert run.successes_per_sec == pytest.approx(6.4)


def test_run_summary_sums_across_shards() -> None:
    run = ds.RunSummary.from_stats(
        processes=8,
        wall_seconds=10.0,
        stats=[make_stats(success=6), make_stats(success=4)],
    )
    assert run.successes == 10
    assert run.successes_per_sec == pytest.approx(1.0)


def test_dns_fraction_is_over_candidates_not_over_failures() -> None:
    """Over failures, the fraction would move when unrelated failures change.

    The question is what share of all attempts DNS costs us, so the
    denominator is every attempt.
    """
    run = ds.RunSummary.from_stats(
        processes=8, wall_seconds=1.0,
        stats=[make_stats(success=500, **{DNS_KEY: 100,
                                          "HTTP Error 404: Not Found": 400})],
    )
    assert run.dns_fraction == pytest.approx(0.10)


def test_zero_wall_time_yields_none_not_an_exception() -> None:
    run = ds.RunSummary.from_stats(processes=8, wall_seconds=0.0,
                                   stats=[make_stats(success=10)])
    assert run.successes_per_sec is None


# --------------------------------------------------------------------------
# Verdict against the pre-registered criteria
# --------------------------------------------------------------------------

def _run(processes: int, rate: float, yield_rate: float,
         dns: float = 0.06) -> ds.RunSummary:
    """A summary with the three quantities the verdict depends on."""
    candidates = 100_000
    successes = int(candidates * yield_rate)
    dns_count = int(candidates * dns)
    return ds.RunSummary(
        processes=processes,
        wall_seconds=successes / rate if rate else 0.0,
        candidates=candidates,
        successes=successes,
        counts=ds.FailureCounts(dns=dns_count, permanent=0, transient=0,
                                rate_limited=0, other=0,
                                total=candidates),
    )


def test_throughput_that_scales_with_stable_yield_is_accepted() -> None:
    verdict = ds.judge([
        _run(8, 350, 0.64),
        _run(32, 1200, 0.63),
        _run(64, 2000, 0.63),
        _run(8, 355, 0.64),
    ])
    assert verdict.scales is True
    assert verdict.yield_preserved is True
    assert verdict.rejected is False


def test_insufficient_gain_from_8_to_32_is_rejected() -> None:
    """The criterion is a 50% rise; 20% is not enough to call it scaling."""
    verdict = ds.judge([
        _run(8, 350, 0.64),
        _run(32, 420, 0.64),
        _run(64, 430, 0.64),
        _run(8, 350, 0.64),
    ])
    assert verdict.scales is False
    assert verdict.rejected is True


def test_yield_loss_beyond_five_points_is_rejected() -> None:
    """Speed bought with images is not a trade to make silently."""
    verdict = ds.judge([
        _run(8, 350, 0.64),
        _run(32, 1400, 0.55),
        _run(64, 2000, 0.50),
        _run(8, 350, 0.64),
    ])
    assert verdict.yield_preserved is False
    assert verdict.rejected is True


def test_drift_between_the_repeated_baseline_invalidates_the_comparison() -> None:
    """Run 4 repeats run 1. If they disagree, the cluster moved.

    Reporting a concurrency effect on top of an unstable baseline would
    attribute someone else's traffic to our setting.
    """
    verdict = ds.judge([
        _run(8, 350, 0.64),
        _run(32, 1200, 0.64),
        _run(64, 2000, 0.64),
        _run(8, 180, 0.64),  # baseline halved between first and last run
    ])
    assert verdict.baseline_stable is False
    assert verdict.rejected is True, "an unstable baseline cannot support a claim"


def test_a_missing_repeat_run_leaves_stability_unknown() -> None:
    """Without the control, say so rather than assuming it was fine."""
    verdict = ds.judge([_run(8, 350, 0.64), _run(32, 1200, 0.64)])
    assert verdict.baseline_stable is None


def test_a_rising_dns_fraction_is_rejected() -> None:
    """The third pre-registered criterion, and the easiest one to forget.

    An earlier probe found the DNS failure rate flat from 1 to 512 concurrent
    lookups, spanning 7.2% to 9.4%. A rise past that observed spread is not
    something concurrency-independent behaviour has produced here before.
    """
    verdict = ds.judge([
        _run(8, 350, 0.64, dns=0.06),
        _run(32, 1200, 0.64, dns=0.30),
        _run(64, 2000, 0.64, dns=0.45),
        _run(8, 355, 0.64, dns=0.06),
    ])
    assert verdict.dns_stable is False
    assert verdict.rejected is True


def test_the_baseline_gap_is_reported_not_just_thresholded() -> None:
    """The protocol says no difference smaller than the run-1/run-4 gap can
    be claimed. A bare pass/fail hides the number that bound is made of.
    """
    verdict = ds.judge([
        _run(8, 350, 0.64),
        _run(32, 1200, 0.64),
        _run(64, 2000, 0.64),
        _run(8, 315, 0.64),  # 10% below the first run
    ])
    assert verdict.baseline_stable is True
    assert verdict.baseline_drift == pytest.approx(0.10, abs=1e-3)


def test_an_empty_run_reports_no_yield_rather_than_dividing_by_zero() -> None:
    """A slice can come back empty if the URL list is shorter than assumed."""
    run = ds.RunSummary.from_stats(processes=8, wall_seconds=5.0, stats=[])
    assert run.candidates == 0
    assert run.yield_rate is None
    assert run.dns_fraction is None
