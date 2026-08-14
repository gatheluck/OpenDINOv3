"""Why a thread spends 16 seconds on a URL, and what would change it.

The pilot ran at 22.5 URLs/sec/node against a model of 277. Concurrency was
not the problem: 352 tars minus 96 completed shards is exactly 256, which is
32 processes across 8 nodes, so every worker was busy. The time went
somewhere else.

A successful 25 KB fetch takes well under a second. A dead URL costs
`timeout x (retries + 1)` — 30 seconds at the current settings. So the
average cost per URL is a weighted mix of the two, and the weight is the
failure rate. That makes the fix arithmetic rather than guesswork: this
computes what the settings are costing and what other settings would cost.
"""

from __future__ import annotations

import pytest

from opendinov3.core import throughput_model as tm


def test_the_cost_of_a_url_is_the_mix_of_fast_success_and_slow_failure(
) -> None:
    """Half succeeding at 0.5s and half timing out at 30s averages ~15s,
    which is what the pilot showed."""
    cost = tm.seconds_per_url(failure_rate=0.5, timeout=10.0, retries=2,
                              success_seconds=0.5)
    assert cost == pytest.approx(15.25)


def test_no_failures_costs_only_the_successes() -> None:
    assert tm.seconds_per_url(0.0, 10.0, 2, 0.5) == pytest.approx(0.5)


def test_retries_multiply_the_cost_of_a_dead_url() -> None:
    """retries=2 means three attempts, not two."""
    assert tm.seconds_per_url(1.0, 10.0, 2, 0.5) == pytest.approx(30.0)
    assert tm.seconds_per_url(1.0, 10.0, 0, 0.5) == pytest.approx(10.0)


def test_the_failure_rate_can_be_recovered_from_what_was_observed() -> None:
    """The pilot's own numbers, inverted: given the measured seconds per
    URL, how much of the corpus must be failing slowly."""
    rate = tm.implied_failure_rate(observed_seconds=16.0, timeout=10.0,
                                   retries=2, success_seconds=0.5)
    assert 0.5 < rate < 0.6
    assert tm.seconds_per_url(rate, 10.0, 2, 0.5) == pytest.approx(16.0)


def test_an_observation_faster_than_a_success_is_refused() -> None:
    """Nonsense in, error out: it would otherwise report a negative
    failure rate and read as very good news."""
    with pytest.raises(ValueError):
        tm.implied_failure_rate(0.1, 10.0, 2, success_seconds=0.5)


def test_an_observation_slower_than_total_failure_is_refused() -> None:
    """Something other than the timeout is dominating; the model does not
    apply and must say so instead of returning a rate above 1."""
    with pytest.raises(ValueError):
        tm.implied_failure_rate(99.0, 10.0, 2, success_seconds=0.5)


def test_the_speedup_from_new_settings_is_reported() -> None:
    """The decision: what is a shorter timeout with no retries worth?"""
    gain = tm.speedup(failure_rate=0.55, success_seconds=0.5,
                      old=(10.0, 2), new=(3.0, 0))
    assert gain == pytest.approx(
        tm.seconds_per_url(0.55, 10.0, 2, 0.5)
        / tm.seconds_per_url(0.55, 3.0, 0, 0.5))
    assert gain > 5


def test_a_shorter_timeout_is_not_free() -> None:
    """Cutting the timeout below what slow-but-live servers need turns
    successes into failures. The model must expose that cost, or it will
    recommend a setting that quietly lowers the yield."""
    kept = tm.yield_at_timeout(timeout=3.0,
                               latency_percentiles={1.0: 0.2, 3.0: 0.8,
                                                    10.0: 0.95})
    assert kept == pytest.approx(0.8)


def test_a_timeout_beyond_the_measured_range_keeps_everything_measured(
) -> None:
    assert tm.yield_at_timeout(30.0, {1.0: 0.2, 10.0: 0.95}) == pytest.approx(0.95)


def test_throughput_scales_with_workers() -> None:
    """32 processes x 32 threads is 1,024 in-flight requests per node."""
    assert tm.urls_per_second(seconds_per_url=16.0, workers=1024
                              ) == pytest.approx(64.0)


def test_the_time_for_a_task_follows_from_the_rate() -> None:
    hours = tm.hours_for_task(urls=1_000_000, urls_per_second=22.5)
    assert hours == pytest.approx(12.35, abs=0.05)
