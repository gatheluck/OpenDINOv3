"""Compare one node against several, holding total concurrency constant.

WHY THIS SHAPE

The open question is whether spreading work across nodes costs anything. A
previous 4-node run was reported to have failed 89% of its DNS lookups, and
that claim is what currently blocks scaling production.

Comparing "1 node" with "2 nodes" the obvious way moves two things at once:
the node count and the total number of processes. Holding the total fixed —
1 node × 32 processes against 2 nodes × 16 — leaves only the distribution.

That also makes the result independent of experiment 0002. Whatever the best
in-node process count turns out to be, "does spreading hurt?" is answered
separately.

THE GRANULARITY TRAP

img2dataset hands out work one shard at a time, so how many shards each
process gets through is part of the configuration, not an implementation
detail. Halving the URLs on a node halves its shards, and halving its
processes halves the demand, so the ratio survives the split. That is what
makes the two configurations comparable, and it is checked rather than
assumed — experiment 0002 nearly shipped with exactly this confound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .download_stats import (
    MAX_BASELINE_DRIFT,
    MAX_DNS_RISE,
    MAX_YIELD_DROP,
    RunSummary,
)

#: Throughput on several nodes, as a fraction of the single-node baseline,
#: below which spreading is judged to cost something. Adding nodes is only
#: worth doing if it is close to free; losing more than a fifth per doubling
#: compounds badly at the scale production needs.
MIN_DISTRIBUTION_RATIO = 0.8

#: Shards each process should get through. Same reasoning as experiment 0002:
#: with roughly one wave, wall time is the slowest shard rather than the mean.
DEFAULT_MIN_WAVES = 3


def parse_nodefile(text: str) -> list[str]:
    """Distinct hosts from $PBS_NODEFILE, in allocation order.

    PBS writes one line per chunk, so a host repeats when it holds more than
    one. Counting lines would report more nodes than were allocated.
    """
    seen: set[str] = set()
    hosts: list[str] = []
    for line in text.splitlines():
        host = line.strip()
        if not host or host in seen:
            continue
        seen.add(host)
        hosts.append(host)
    return hosts


@dataclass(frozen=True)
class NodeConfig:
    nodes: int
    processes_per_node: int
    urls_per_node: int
    shards_per_node: int

    @property
    def total_processes(self) -> int:
        return self.nodes * self.processes_per_node

    @property
    def capped(self) -> bool:
        """More processes on a node than shards for them to take."""
        return self.processes_per_node > self.shards_per_node

    @property
    def waves(self) -> float:
        return self.shards_per_node / self.processes_per_node

    @property
    def label(self) -> str:
        return f"{self.nodes}n×{self.processes_per_node}p"


def plan_distribution(
    total_processes: int,
    slice_size: int,
    samples_per_shard: int,
    node_counts: Sequence[int],
) -> list[NodeConfig]:
    """One configuration per node count, all at the same total concurrency.

    Refuses splits that do not divide evenly. An uneven split makes one node
    finish later than the others for reasons unrelated to the question, and
    the phase's wall time is set by the slowest node.
    """
    configs: list[NodeConfig] = []
    for nodes in node_counts:
        if nodes <= 0:
            raise ValueError(f"node count must be positive, got {nodes}")
        if total_processes % nodes:
            raise ValueError(
                f"{total_processes} processes do not divide evenly across "
                f"{nodes} nodes; an uneven split would make one node the "
                "bottleneck for reasons unrelated to distribution."
            )
        if slice_size % nodes:
            raise ValueError(
                f"{slice_size} URLs do not divide evenly across {nodes} nodes."
            )
        urls_per_node = slice_size // nodes
        configs.append(NodeConfig(
            nodes=nodes,
            processes_per_node=total_processes // nodes,
            urls_per_node=urls_per_node,
            shards_per_node=math.ceil(urls_per_node / samples_per_shard),
        ))
    return configs


def validate_distribution(
    configs: Sequence[NodeConfig], min_waves: int = DEFAULT_MIN_WAVES
) -> list[str]:
    """Problems that would make the comparison measure something else."""
    problems: list[str] = []

    for config in configs:
        if config.capped:
            problems.append(
                f"{config.label}: {config.processes_per_node} processes per "
                f"node but only {config.shards_per_node} shards; the extra "
                "processes would never receive work."
            )
        elif config.waves < min_waves:
            problems.append(
                f"{config.label}: {config.waves:.1f} shards per process "
                f"(want at least {min_waves}); wall time would be the "
                "slowest shard rather than the mean."
            )

    waves = {round(c.waves, 6) for c in configs}
    if len(waves) > 1:
        detail = ", ".join(f"{c.label}={c.waves:.2f}" for c in configs)
        problems.append(
            "shards per process differ between configurations "
            f"({detail}); work granularity would be a second variable, so a "
            "difference could not be attributed to the node count."
        )
    return problems


@dataclass(frozen=True)
class DistributionVerdict:
    """`distribution_neutral` is None when the multi-node phase did not run.

    That is an expected outcome, not a failure of the hypothesis: launching
    across nodes is the most fragile part of the job, which is why the
    single-node phases run first. An absent phase is reported as absent.
    """

    distribution_neutral: bool | None
    yield_preserved: bool
    dns_stable: bool
    baseline_stable: bool | None
    baseline_drift: float | None
    single_node_rate: float | None
    multi_node_rate: float | None
    rejected: bool


def judge_distribution(
    single: Sequence[RunSummary],
    multi: RunSummary | None,
    min_ratio: float = MIN_DISTRIBUTION_RATIO,
) -> DistributionVerdict:
    """Apply experiment 0003's criteria.

    The single-node baseline is the mean of its runs: the multi-node phase
    sits between them in time, so the mean is a fairer comparator than
    either endpoint.
    """
    if not single:
        raise ValueError("judge_distribution() needs a single-node baseline")

    rates = [r.successes_per_sec for r in single if r.successes_per_sec]
    single_rate = sum(rates) / len(rates) if rates else None
    multi_rate = multi.successes_per_sec if multi else None

    baseline_drift = _drift(single)
    baseline_stable = (
        None if baseline_drift is None else baseline_drift <= MAX_BASELINE_DRIFT
    )

    if multi is None or single_rate is None or multi_rate is None:
        neutral = None
    else:
        neutral = multi_rate >= min_ratio * single_rate

    yield_preserved = _yield_preserved(single, multi)
    dns_stable = _dns_stable(single, multi)

    rejected = (
        neutral is False
        or not yield_preserved
        or not dns_stable
        or baseline_stable is False
    )

    return DistributionVerdict(
        distribution_neutral=neutral,
        yield_preserved=yield_preserved,
        dns_stable=dns_stable,
        baseline_stable=baseline_stable,
        baseline_drift=baseline_drift,
        single_node_rate=single_rate,
        multi_node_rate=multi_rate,
        rejected=rejected,
    )


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _drift(single: Sequence[RunSummary]) -> float | None:
    """Gap between the repeated single-node runs, as a fraction of the first."""
    if len(single) < 2:
        return None
    first = single[0].successes_per_sec
    last = single[-1].successes_per_sec
    if not first or last is None:
        return None
    return abs(last - first) / first


def _yield_preserved(
    single: Sequence[RunSummary], multi: RunSummary | None
) -> bool:
    if multi is None or multi.yield_rate is None:
        return True
    base = _mean([r.yield_rate for r in single if r.yield_rate is not None])
    if base is None:
        return True
    return base - multi.yield_rate <= MAX_YIELD_DROP


def _dns_stable(single: Sequence[RunSummary], multi: RunSummary | None) -> bool:
    if multi is None or multi.dns_fraction is None:
        return True
    base = _mean([r.dns_fraction for r in single if r.dns_fraction is not None])
    if base is None:
        return True
    return multi.dns_fraction - base <= MAX_DNS_RISE
