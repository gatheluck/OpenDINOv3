"""Contract for cutting upstream metadata into download tasks.

The predecessor's partitioner cannot be reused for two reasons, both in its
own source: it refuses to write outside their directory tree, and it loads
the whole corpus into pandas before partitioning — 2,664 parquet files,
roughly 1.3 billion rows, carrying a per-row Python string for the source
file name. It only runs at all because the compute nodes have 1,920 GB.

What it does get right is determinism: files sorted by path, concatenated in
order, cut into fixed-size chunks from the start. That property is kept here,
because it is what makes the partition reproducible from the metadata alone.

This module plans the partition from parquet *metadata* only — row counts,
not rows — so the plan costs 2,664 header reads instead of 300 GB of memory.
"""

from __future__ import annotations

import pytest

from opendinov3.core import metadata_partition as mp


def src(name: str, rows: int) -> mp.SourceFile:
    return mp.SourceFile(path=name, rows=rows)


def test_files_divide_evenly_into_whole_tasks() -> None:
    tasks = mp.plan_tasks([src("a", 300)], urls_per_task=100)
    assert [t.task_id for t in tasks] == [0, 1, 2]
    assert all(t.rows == 100 for t in tasks)


def test_a_task_spans_files_when_one_file_is_not_enough() -> None:
    """Files are whatever size upstream made them; tasks are a fixed size.

    A task boundary lands mid-file more often than not, so a task has to be
    able to name several sources.
    """
    tasks = mp.plan_tasks([src("a", 60), src("b", 60)], urls_per_task=100)
    assert tasks[0].pieces == (("a", 0, 60), ("b", 0, 40))
    assert tasks[0].rows == 100
    # 120 rows at 100 per task leaves 20, which becomes a short final task
    # rather than being dropped.
    assert tasks[1].pieces == (("b", 40, 60),)
    assert tasks[1].rows == 20


def test_the_remainder_becomes_a_short_final_task() -> None:
    """Dropping it would silently discard URLs from the corpus."""
    tasks = mp.plan_tasks([src("a", 250)], urls_per_task=100)
    assert len(tasks) == 3
    assert tasks[-1].rows == 50
    assert tasks[-1].pieces == (("a", 200, 250),)
    assert sum(t.rows for t in tasks) == 250


def test_file_order_is_preserved_exactly() -> None:
    """Determinism depends on it. The caller sorts; the planner must not
    reorder, or the same metadata would produce a different corpus."""
    tasks = mp.plan_tasks([src("b", 100), src("a", 100)], urls_per_task=100)
    assert [t.pieces[0][0] for t in tasks] == ["b", "a"]


def test_planning_twice_gives_the_identical_partition() -> None:
    sources = [src("a", 137), src("b", 291), src("c", 4)]
    assert mp.plan_tasks(sources, 100) == mp.plan_tasks(sources, 100)


def test_empty_files_are_skipped_without_shifting_anything() -> None:
    with_empty = [src("a", 100), src("empty", 0), src("b", 100)]
    without = [src("a", 100), src("b", 100)]
    assert mp.plan_tasks(with_empty, 100) == mp.plan_tasks(without, 100)


def test_no_row_is_used_twice_and_none_is_lost() -> None:
    """The property the whole corpus rests on."""
    sources = [src("a", 137), src("b", 291), src("c", 45)]
    tasks = mp.plan_tasks(sources, 100)

    seen: list[tuple[str, int]] = []
    for task in tasks:
        for name, start, end in task.pieces:
            seen += [(name, i) for i in range(start, end)]

    assert len(seen) == len(set(seen)), "a row is used by two tasks"
    assert len(seen) == sum(s.rows for s in sources), "a row was dropped"


def test_a_nonpositive_task_size_is_refused() -> None:
    with pytest.raises(ValueError):
        mp.plan_tasks([src("a", 10)], urls_per_task=0)


def test_the_loop_cannot_spin_even_without_the_argument_check() -> None:
    """A regression must fail, not hang.

    Removing the `urls_per_task` check used to make this an infinite loop.
    A test suite cannot catch a hang, so the loop itself now refuses to make
    no progress.
    """
    with pytest.raises(ValueError):
        mp.plan_tasks([src("a", 10)], urls_per_task=-1)


def test_no_sources_gives_no_tasks_rather_than_failing() -> None:
    assert mp.plan_tasks([], urls_per_task=100) == []


# --------------------------------------------------------------------------
# Reporting the scale before anything is written
# --------------------------------------------------------------------------

def test_a_summary_states_the_scale_the_run_will_have() -> None:
    summary = mp.summarise([src("a", 1_000_000), src("b", 500_000)], 400_000)
    assert summary.total_rows == 1_500_000
    assert summary.tasks == 4
    assert summary.full_tasks == 3
    assert summary.final_task_rows == 300_000


def test_a_summary_says_when_the_last_task_is_full() -> None:
    summary = mp.summarise([src("a", 800_000)], 400_000)
    assert summary.tasks == 2
    assert summary.full_tasks == 2
    assert summary.final_task_rows == 400_000
