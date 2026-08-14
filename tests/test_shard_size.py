"""Contract for measuring bytes per stored image, per task.

`measurements.md` records per-image sizes differing threefold between tasks
— 19.7 KB, 23.6 KB, 58 KB, 116 KB — from single observations, with no way to
tell whether that is a property of the tasks or noise between shards. The
whole storage estimate for the remaining ~735 tasks rests on it.

Experiment 0003 removed one candidate explanation: two different tasks
downloaded at 685 s and 681 s, a 0.3% difference, so tasks do not differ in
any way that shows up as speed.

The number that settles it is the spread *within* a task against the spread
*between* tasks. If shards inside one task vary as much as tasks do, there is
nothing to explain.
"""

from __future__ import annotations

import pytest

from opendinov3.core import shard_size as ss


def shard(name: str, tar_bytes: int, successes: int) -> ss.ShardSize:
    return ss.ShardSize(name=name, tar_bytes=tar_bytes, successes=successes)


def test_bytes_per_image_counts_successes_not_attempts() -> None:
    """A failed download writes nothing to the tar, so it is not stored."""
    s = shard("00000", tar_bytes=1_000_000, successes=50)
    assert s.bytes_per_image == pytest.approx(20_000)


def test_a_shard_with_no_successes_has_no_size_rather_than_zero() -> None:
    """Zero would drag a task's mean down and look like small images."""
    assert shard("00000", tar_bytes=1024, successes=0).bytes_per_image is None


def test_a_task_aggregates_over_its_shards_by_total_not_by_mean() -> None:
    """Averaging per-shard means would weight a 10-image shard like a
    10,000-image one."""
    task = ss.summarise_task("task-000001", [
        shard("00000", tar_bytes=1_000_000, successes=100),   # 10 KB/img
        shard("00001", tar_bytes=9_000_000, successes=900),   # 10 KB/img
    ])
    assert task.successes == 1000
    assert task.bytes_per_image == pytest.approx(10_000)


def test_shards_without_successes_are_excluded_from_a_task() -> None:
    task = ss.summarise_task("task-000001", [
        shard("00000", tar_bytes=1_000_000, successes=100),
        shard("00001", tar_bytes=512, successes=0),
    ])
    assert task.usable_shards == 1
    assert task.bytes_per_image == pytest.approx(10_000)


def test_a_task_reports_its_own_shard_to_shard_spread() -> None:
    """The quantity the question turns on.

    A threefold difference between tasks means nothing if shards inside a
    task already differ threefold.
    """
    task = ss.summarise_task("task-000001", [
        shard("00000", tar_bytes=1_000_000, successes=100),   # 10 KB
        shard("00001", tar_bytes=3_000_000, successes=100),   # 30 KB
    ])
    assert task.min_bytes_per_image == pytest.approx(10_000)
    assert task.max_bytes_per_image == pytest.approx(30_000)
    assert task.spread == pytest.approx(3.0)


def test_a_task_with_no_usable_shard_is_reported_not_dropped() -> None:
    task = ss.summarise_task("task-000009", [shard("00000", 512, 0)])
    assert task.usable_shards == 0
    assert task.bytes_per_image is None
    assert task.spread is None


# --------------------------------------------------------------------------
# The comparison that answers the question
# --------------------------------------------------------------------------

def make_task(name: str, sizes: list[int]) -> ss.TaskSize:
    return ss.summarise_task(
        name, [shard(f"{i:05d}", kb * 100, 100) for i, kb in enumerate(sizes)]
    )


def test_between_task_variation_is_reported_against_within_task_variation(
) -> None:
    """Tasks differ 3×, but so do the shards inside each one.

    Nothing here needs a task-level explanation.
    """
    report = ss.compare_tasks([
        make_task("task-a", [100, 300]),
        make_task("task-b", [100, 300]),
    ])
    assert report.between_spread == pytest.approx(1.0)
    assert report.worst_within_spread == pytest.approx(3.0)
    assert report.between_exceeds_within is False


def test_a_genuine_task_level_difference_is_flagged() -> None:
    """Shards inside each task agree; the tasks do not. That needs a cause."""
    report = ss.compare_tasks([
        make_task("task-a", [100, 105]),
        make_task("task-b", [300, 310]),
    ])
    assert report.between_spread == pytest.approx(2.95, rel=0.02)
    assert report.worst_within_spread == pytest.approx(1.05, rel=0.02)
    assert report.between_exceeds_within is True


def test_tasks_with_no_usable_shard_do_not_enter_the_comparison() -> None:
    report = ss.compare_tasks([
        make_task("task-a", [100, 105]),
        ss.summarise_task("task-empty", [shard("00000", 512, 0)]),
    ])
    assert report.tasks_compared == 1
    assert report.tasks_skipped == 1


def test_comparing_nothing_is_refused() -> None:
    with pytest.raises(ValueError):
        ss.compare_tasks([])
