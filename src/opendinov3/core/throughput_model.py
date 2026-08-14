"""What a URL costs, and what changing the fetch settings would change.

WHY THIS EXISTS

The pilot ran at 22.5 URLs/sec/node against a planning model of 277 — 12x
slow. Concurrency was not the cause: 352 open tars minus 96 completed
shards is exactly 256, which is 32 processes across 8 nodes, so every
worker was busy the whole time.

The time went into waiting. A successful 25 KB fetch takes well under a
second; a dead URL costs `timeout x (retries + 1)`, which is 30 seconds at
--timeout 10 --retries 2. The observed ~16 seconds per URL per thread is
therefore a weighted average of the two, and the weight is the failure
rate.

That makes the remedy arithmetic instead of guesswork. Given what was
observed, this recovers the failure rate, and given a proposed timeout and
retry count, it says what the throughput would become.

THE TRAP IT ALSO HAS TO MODEL

A shorter timeout is not free. Cutting it below what slow-but-live servers
need converts successes into failures — faster, and with less data. So
`yield_at_timeout` exists to price that, and any recommendation has to
quote both numbers.
"""

from __future__ import annotations

from typing import Mapping


def seconds_per_url(failure_rate: float, timeout: float, retries: int,
                    success_seconds: float) -> float:
    """Average wall time one URL occupies one thread.

    `retries=2` means three attempts, so a dead URL costs three timeouts.
    """
    if not 0.0 <= failure_rate <= 1.0:
        raise ValueError(f"failure_rate must be in [0, 1], got {failure_rate}")
    dead = timeout * (retries + 1)
    return failure_rate * dead + (1.0 - failure_rate) * success_seconds


def implied_failure_rate(observed_seconds: float, timeout: float,
                         retries: int, success_seconds: float) -> float:
    """Invert seconds_per_url: what failure rate explains what we measured.

    Refuses observations the model cannot explain rather than returning a
    rate outside [0, 1]. A negative rate would read as very good news, and
    a rate above 1 would hide the fact that something other than the
    timeout is dominating.
    """
    dead = timeout * (retries + 1)
    if dead <= success_seconds:
        raise ValueError("a failure must cost more than a success")
    rate = (observed_seconds - success_seconds) / (dead - success_seconds)
    if rate < 0.0:
        raise ValueError(
            f"{observed_seconds}s per URL is faster than a success "
            f"({success_seconds}s); the model does not apply")
    if rate > 1.0:
        raise ValueError(
            f"{observed_seconds}s per URL exceeds a total failure "
            f"({dead}s); something other than the timeout dominates")
    return rate


def speedup(failure_rate: float, success_seconds: float,
            old: tuple[float, int], new: tuple[float, int]) -> float:
    """How much faster `new` (timeout, retries) is than `old`.

    Assumes the failure rate is unchanged, which is only true while the new
    timeout stays above the latency of live servers — price that with
    yield_at_timeout before quoting this.
    """
    before = seconds_per_url(failure_rate, old[0], old[1], success_seconds)
    after = seconds_per_url(failure_rate, new[0], new[1], success_seconds)
    return before / after


def yield_at_timeout(timeout: float,
                     latency_percentiles: Mapping[float, float]) -> float:
    """Share of live servers still answered within `timeout`.

    `latency_percentiles` maps a number of seconds to the fraction of
    successful fetches that completed within it. A timeout beyond the
    measured range keeps everything that was measured — and says so by
    returning the largest known fraction rather than 1.0, because nothing
    observed a longer wait.
    """
    if not latency_percentiles:
        raise ValueError("no latency data")
    reachable = [seconds for seconds in latency_percentiles
                 if seconds <= timeout]
    if not reachable:
        return 0.0
    return latency_percentiles[max(reachable)]


def urls_per_second(seconds_per_url: float, workers: int) -> float:
    """Rate for a node running `workers` requests in flight."""
    if seconds_per_url <= 0:
        raise ValueError("seconds_per_url must be positive")
    return workers / seconds_per_url


def hours_for_task(urls: int, urls_per_second: float) -> float:
    if urls_per_second <= 0:
        raise ValueError("urls_per_second must be positive")
    return urls / urls_per_second / 3600.0
