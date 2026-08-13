"""Contract for comparing one node against several, at equal total concurrency.

The question is whether spreading the same amount of concurrency across more
nodes costs anything. A previous 4-node run was reported to have failed 89% of
its DNS lookups, and that claim is what currently blocks scaling production.

Comparing "1 node" against "2 nodes" naively changes two things at once: the
node count and the total number of processes. This module holds the total
fixed, so only the distribution moves.

It also guards the confound that experiment 0002 nearly shipped with. Work is
handed out one shard at a time, so a configuration where processes get a
different number of shards each is not comparable to one where they do not —
even if every other setting matches.
"""

from __future__ import annotations

import pytest

from opendinov3.core import node_plan as np
from opendinov3.core.download_stats import FailureCounts, RunSummary

TOTAL = 32
SLICE = 200_000
SPS = 1_000


# --------------------------------------------------------------------------
# Reading the node list PBS gives us
# --------------------------------------------------------------------------

def test_the_nodefile_is_deduplicated() -> None:
    """PBS writes one line per chunk, so a node repeats when it holds several.

    Counting lines would report more nodes than were allocated.
    """
    text = "node07\nnode07\nnode11\nnode11\n"
    assert np.parse_nodefile(text) == ["node07", "node11"]


def test_the_nodefile_keeps_allocation_order() -> None:
    """Slices are assigned by position, so the order has to be stable."""
    assert np.parse_nodefile("b\na\nc\n") == ["b", "a", "c"]


def test_blank_lines_in_the_nodefile_are_ignored() -> None:
    assert np.parse_nodefile("node01\n\n  \nnode02\n") == ["node01", "node02"]


# --------------------------------------------------------------------------
# Building the configurations
# --------------------------------------------------------------------------

def test_total_concurrency_is_held_constant_across_configs() -> None:
    configs = np.plan_distribution(TOTAL, SLICE, SPS, node_counts=[1, 2])
    assert [c.total_processes for c in configs] == [TOTAL, TOTAL]
    assert [c.processes_per_node for c in configs] == [32, 16]


def test_each_node_gets_an_equal_share_of_the_urls() -> None:
    configs = np.plan_distribution(TOTAL, SLICE, SPS, node_counts=[1, 2])
    assert [c.urls_per_node for c in configs] == [200_000, 100_000]
    assert all(c.urls_per_node * c.nodes == SLICE for c in configs)


def test_shards_per_process_match_across_configs() -> None:
    """The property that makes the comparison mean anything.

    Halving the URLs per node halves its shards, and halving the processes
    per node halves the demand, so the ratio is unchanged. If it were not,
    work granularity would differ between the two configurations and any
    difference could be attributed to that instead of to the node count.
    """
    configs = np.plan_distribution(TOTAL, SLICE, SPS, node_counts=[1, 2])
    waves = [c.waves for c in configs]
    assert waves[0] == pytest.approx(waves[1])
    assert waves[0] == pytest.approx(6.25)


def test_a_node_count_that_does_not_divide_the_processes_is_refused() -> None:
    """3 nodes cannot share 32 processes evenly, and an uneven split would
    make one node the bottleneck for reasons unrelated to the question."""
    with pytest.raises(ValueError):
        np.plan_distribution(TOTAL, SLICE, SPS, node_counts=[1, 3])


def test_a_slice_that_does_not_divide_evenly_is_refused() -> None:
    with pytest.raises(ValueError):
        np.plan_distribution(TOTAL, 200_001, SPS, node_counts=[1, 2])


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def test_the_chosen_configuration_passes() -> None:
    configs = np.plan_distribution(TOTAL, SLICE, SPS, node_counts=[1, 2])
    assert np.validate_distribution(configs) == []


def test_a_capped_config_is_refused() -> None:
    """Same trap as experiment 0002: more processes than shards."""
    configs = np.plan_distribution(TOTAL, SLICE, 10_000, node_counts=[1, 2])
    problems = np.validate_distribution(configs)
    assert problems
    # Specific to capping, not merely to having too few shards: the two
    # failures have different fixes and a message about one is not evidence
    # about the other.
    assert any("never receive work" in p for p in problems), problems


def test_mismatched_granularity_is_refused_even_if_nothing_is_capped() -> None:
    """Guard against a future change breaking the equal-waves property.

    Constructed directly, because plan_distribution cannot currently produce
    it — which is the point: the check must fail loudly if that ever changes.
    """
    configs = [
        np.NodeConfig(nodes=1, processes_per_node=32,
                      urls_per_node=200_000, shards_per_node=200),
        np.NodeConfig(nodes=2, processes_per_node=16,
                      urls_per_node=100_000, shards_per_node=400),
    ]
    problems = np.validate_distribution(configs)
    assert any("granularit" in p or "shards per process" in p
               for p in problems), problems


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

def _run(rate: float, yield_rate: float = 0.64, dns: float = 0.06,
         processes: int = 32) -> RunSummary:
    candidates = 200_000
    successes = int(candidates * yield_rate)
    return RunSummary(
        processes=processes,
        wall_seconds=successes / rate if rate else 0.0,
        candidates=candidates,
        successes=successes,
        counts=FailureCounts(dns=int(candidates * dns), permanent=0,
                             transient=0, rate_limited=0, other=0,
                             total=candidates),
    )


def test_matching_throughput_means_distribution_is_free() -> None:
    verdict = np.judge_distribution(
        single=[_run(900), _run(880)], multi=_run(890)
    )
    assert verdict.distribution_neutral is True
    assert verdict.rejected is False


def test_the_single_node_baseline_is_averaged_over_both_runs() -> None:
    """The multi-node phase sits between them in time, so the mean is the
    fairer comparator than either endpoint."""
    verdict = np.judge_distribution(
        single=[_run(1000), _run(800)], multi=_run(810)
    )
    assert verdict.single_node_rate == pytest.approx(900, rel=1e-3)
    assert verdict.distribution_neutral is True  # 810 / 900 = 0.90


def test_a_large_throughput_loss_is_rejected() -> None:
    verdict = np.judge_distribution(
        single=[_run(900), _run(900)], multi=_run(400)
    )
    assert verdict.distribution_neutral is False
    assert verdict.rejected is True


def test_a_yield_collapse_is_rejected_even_if_throughput_holds() -> None:
    """The reported failure mode: many nodes, most lookups failing.

    Throughput measured in successes would drop too, but yield is the direct
    statement and is what the earlier 89% claim was about.
    """
    verdict = np.judge_distribution(
        single=[_run(900, yield_rate=0.64), _run(900, yield_rate=0.64)],
        multi=_run(900, yield_rate=0.40),
    )
    assert verdict.yield_preserved is False
    assert verdict.rejected is True


def test_a_dns_spike_across_nodes_is_rejected() -> None:
    verdict = np.judge_distribution(
        single=[_run(900, dns=0.06), _run(900, dns=0.06)],
        multi=_run(900, dns=0.50),
    )
    assert verdict.dns_stable is False
    assert verdict.rejected is True


def test_drift_between_the_two_single_node_runs_invalidates_it() -> None:
    verdict = np.judge_distribution(
        single=[_run(900), _run(400)], multi=_run(650)
    )
    assert verdict.baseline_stable is False
    assert verdict.rejected is True


def test_a_missing_second_single_node_run_leaves_stability_unknown() -> None:
    verdict = np.judge_distribution(single=[_run(900)], multi=_run(880))
    assert verdict.baseline_stable is None


def test_a_missing_multi_node_phase_is_reported_not_guessed() -> None:
    """The multi-node launch is the part most likely to fail on the cluster.

    The job runs the single-node phases first for that reason, so this case
    is expected to happen and must not be dressed up as a result.
    """
    verdict = np.judge_distribution(single=[_run(900), _run(890)], multi=None)
    assert verdict.distribution_neutral is None
    assert verdict.rejected is False
    assert verdict.baseline_stable is True
