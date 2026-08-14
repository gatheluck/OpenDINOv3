"""Read img2dataset statistics and compare download runs against each other.

img2dataset writes a `_stats.json` beside every shard. It carries a
`status_dict` mapping the exact text of each outcome to how often it
happened. The text is the only thing available, so this module turns it into
categories that a decision can be made from.

WHY CATEGORIES AND NOT RAW COUNTS

The counts on their own do not distinguish a URL that will never work from
one that failed this time. Roughly a fifth of every batch is 404 or 403, and
retrying those costs time and yields nothing. Separating permanent from
transient failures is what makes a retry policy decidable at all.

429 is transient, but it is also the one failure that says the downloader
itself caused the problem. It is counted inside `transient` and again on its
own, because a concurrency experiment that folded it into the transient
bucket would hide the very effect it exists to detect.

WHY THE THRESHOLDS LIVE HERE

`judge()` applies the criteria pre-registered in
docs/experiments/0002-download-concurrency.md. They are module constants so
that the document and the code can be checked against each other, rather
than the numbers being buried in a script that gets edited after the result
is known.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

SUCCESS_KEY = "success"

# getaddrinfo failures. -2 EAI_NONAME, -3 EAI_AGAIN, -4 EAI_FAIL, -5 EAI_NODATA.
# The message text around them is phrased differently across platforms and
# urllib versions; the errno is the part that stays put.
_GAI_ERRNO = re.compile(r"\[Errno -[2345]\]")

# Errors that say THIS machine has no path to the network, as distinct from a
# remote host refusing. 101 ENETUNREACH, 113 EHOSTUNREACH, 110 ETIMEDOUT at
# the socket layer.
#
# Kept apart from the general failure bucket because it is the signal that
# identifies an outage rather than bad URLs. On 2026-07-28 a day-long loss of
# connectivity destroyed 474 tasks; 15.5% of their attempts were Errno 101,
# which would have been invisible averaged into an "other" bucket that sits
# near 5% normally.
_NO_ROUTE_ERRNO = re.compile(r"\[Errno (101|113)\]")
_HTTP_STATUS = re.compile(r"HTTP Error (\d{3})")

# 408 and 429 are 4xx but worth another attempt; everything else in 4xx is
# the server saying no, and it will say no again.
_TRANSIENT_4XX = frozenset({408, 425, 429})
_RATE_LIMIT_STATUS = 429

# Pre-registered falsification criteria for experiment 0002.
MIN_SPEEDUP = 1.5
MAX_YIELD_DROP = 0.05
MAX_DNS_RISE = 0.03
MAX_BASELINE_DRIFT = 0.20


@dataclass(frozen=True)
class FailureCounts:
    """A partition of attempts, plus one overlapping count.

    `dns`, `unreachable`, `permanent`, `transient` and `other` are disjoint
    and cover every failure. `rate_limited` is a subset of `transient`, not a fifth bucket,
    so summing all five double-counts 429s. `total` is every attempt,
    successes included.
    """

    dns: int
    permanent: int
    transient: int
    rate_limited: int
    other: int
    total: int
    #: Local connectivity failures. Defaulted so existing callers keep
    #: working; never normal, so any material rate is an outage.
    unreachable: int = 0

    def __add__(self, other: "FailureCounts") -> "FailureCounts":
        return FailureCounts(
            dns=self.dns + other.dns,
            permanent=self.permanent + other.permanent,
            transient=self.transient + other.transient,
            rate_limited=self.rate_limited + other.rate_limited,
            other=self.other + other.other,
            total=self.total + other.total,
            unreachable=self.unreachable + other.unreachable,
        )


EMPTY_COUNTS = FailureCounts(
    dns=0, permanent=0, transient=0, rate_limited=0, other=0, total=0
)


def classify(status_dict: dict[str, int]) -> FailureCounts:
    """Sort img2dataset's outcome strings into categories.

    Unrecognised strings land in `other` rather than being dropped. A
    breakdown that silently discards what it does not understand reports a
    smaller failure count than actually occurred, which is worse than
    reporting an unexplained one.
    """
    dns = permanent = transient = rate_limited = other = 0
    unreachable = 0
    total = 0

    for message, count in status_dict.items():
        total += count

        if message == SUCCESS_KEY:
            continue

        if _GAI_ERRNO.search(message):
            dns += count
            continue

        if _NO_ROUTE_ERRNO.search(message):
            unreachable += count
            continue

        http = _HTTP_STATUS.search(message)
        if http:
            status = int(http.group(1))
            if status == _RATE_LIMIT_STATUS:
                rate_limited += count
            if status in _TRANSIENT_4XX or 500 <= status <= 599:
                transient += count
            elif 400 <= status <= 499:
                permanent += count
            else:
                other += count
            continue

        if "timed out" in message or "timeout" in message.lower():
            transient += count
            continue

        other += count

    return FailureCounts(
        dns=dns,
        permanent=permanent,
        transient=transient,
        rate_limited=rate_limited,
        other=other,
        total=total,
        unreachable=unreachable,
    )


@dataclass(frozen=True)
class RunSummary:
    """One download run at one concurrency setting."""

    processes: int
    wall_seconds: float
    candidates: int
    successes: int
    counts: FailureCounts

    @classmethod
    def from_stats(
        cls,
        processes: int,
        wall_seconds: float,
        stats: Iterable[dict],
    ) -> "RunSummary":
        """Aggregate the per-shard `_stats.json` bodies of a single run.

        `count` and `successes` are taken from the top level of each file
        rather than recomputed from `status_dict`, because those are the
        fields img2dataset itself reports against.
        """
        candidates = 0
        successes = 0
        counts = EMPTY_COUNTS

        for stat in stats:
            candidates += int(stat.get("count", 0))
            successes += int(stat.get("successes", 0))
            counts = counts + classify(stat.get("status_dict", {}))

        return cls(
            processes=processes,
            wall_seconds=float(wall_seconds),
            candidates=candidates,
            successes=successes,
            counts=counts,
        )

    @property
    def yield_rate(self) -> float | None:
        """Successes over attempts, or None when nothing was attempted."""
        if self.candidates == 0:
            return None
        return self.successes / self.candidates

    @property
    def successes_per_sec(self) -> float | None:
        """The quantity the experiment turns on: images actually obtained.

        Attempts per second would reward a setting that fails faster.
        """
        if self.wall_seconds <= 0:
            return None
        return self.successes / self.wall_seconds

    @property
    def dns_fraction(self) -> float | None:
        """DNS failures over every attempt, not over failures.

        Over failures the fraction would move whenever an unrelated failure
        category changed, which is not the question being asked.
        """
        if self.candidates == 0:
            return None
        return self.counts.dns / self.candidates


@dataclass(frozen=True)
class Verdict:
    """The pre-registered criteria, evaluated.

    `baseline_stable` is None when the repeated run is missing: the control
    was not run, so stability is unknown rather than fine. A None does not
    reject on its own — an absent control is an incomplete experiment, not a
    falsified hypothesis — but any claim drawn from it is unverified and
    should be reported that way.
    """

    scales: bool | None
    yield_preserved: bool
    dns_stable: bool
    baseline_stable: bool | None
    baseline_drift: float | None
    rejected: bool


def judge(runs: Sequence[RunSummary]) -> Verdict:
    """Apply experiment 0002's falsification criteria to its runs.

    The first run is the baseline. The first later run at a different
    process count is the scaling comparison. A later run back at the
    baseline's process count is the drift control.
    """
    if not runs:
        raise ValueError("judge() needs at least one run")

    baseline = runs[0]
    scaled = next((r for r in runs[1:] if r.processes != baseline.processes), None)
    repeat = next((r for r in runs[1:] if r.processes == baseline.processes), None)

    scales = _scales(baseline, scaled)
    yield_preserved = _yield_preserved(baseline, runs[1:])
    dns_stable = _dns_stable(baseline, runs[1:])
    baseline_drift = _drift(baseline, repeat)
    baseline_stable = None if baseline_drift is None else baseline_drift <= MAX_BASELINE_DRIFT

    rejected = (
        scales is False
        or not yield_preserved
        or not dns_stable
        or baseline_stable is False
    )

    return Verdict(
        scales=scales,
        yield_preserved=yield_preserved,
        dns_stable=dns_stable,
        baseline_stable=baseline_stable,
        baseline_drift=baseline_drift,
        rejected=rejected,
    )


def _scales(baseline: RunSummary, scaled: RunSummary | None) -> bool | None:
    """None when there is nothing to compare against, not False."""
    if scaled is None:
        return None
    base_rate = baseline.successes_per_sec
    scaled_rate = scaled.successes_per_sec
    if not base_rate or scaled_rate is None:
        return None
    return scaled_rate / base_rate >= MIN_SPEEDUP


def _yield_preserved(baseline: RunSummary, others: Sequence[RunSummary]) -> bool:
    base = baseline.yield_rate
    if base is None:
        return True
    return all(
        base - r.yield_rate <= MAX_YIELD_DROP
        for r in others
        if r.yield_rate is not None
    )


def _dns_stable(baseline: RunSummary, others: Sequence[RunSummary]) -> bool:
    base = baseline.dns_fraction
    if base is None:
        return True
    return all(
        r.dns_fraction - base <= MAX_DNS_RISE
        for r in others
        if r.dns_fraction is not None
    )


def _drift(baseline: RunSummary, repeat: RunSummary | None) -> float | None:
    """How far the repeated setting moved, as a fraction of the first run.

    This number bounds every claim the experiment makes: a difference
    between levels smaller than the gap between two runs at the *same*
    level is not attributable to the level.
    """
    if repeat is None:
        return None
    base_rate = baseline.successes_per_sec
    repeat_rate = repeat.successes_per_sec
    if not base_rate or repeat_rate is None:
        return None
    return abs(repeat_rate - base_rate) / base_rate
