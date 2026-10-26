"""A smaller task size must preserve the exact ordered source rows."""

from copy import deepcopy

import pytest

from opendinov3.core.metadata_partition import TaskSlice, repartition_tail


def rows(tasks):
    return [
        (path, row)
        for task in tasks
        for path, start, end in task.pieces
        for row in range(start, end)
    ]


@pytest.fixture
def original():
    return [
        TaskSlice(0, (("a", 0, 7),), 7),
        TaskSlice(1, (("a", 7, 10), ("b", 0, 4)), 7),
        TaskSlice(2, (("b", 4, 9),), 5),
    ]


@pytest.mark.parametrize("size", [1, 3, 7, 100_000])
def test_exact_coverage_and_unchanged_prefix(original, size):
    before = deepcopy(original)
    result = repartition_tail(original, first_task=1, urls_per_task=size)
    assert result[:1] == original[:1]
    assert rows(result) == rows(original)
    assert all(0 < task.rows <= size for task in result[1:])
    assert [t.task_id for t in result] == list(range(len(result)))
    assert sum(t.rows for t in result) == 19
    assert original == before
    assert repartition_tail(result, first_task=1, urls_per_task=size) == result


def test_last_partial_task_is_kept(original):
    result = repartition_tail(original, first_task=1, urls_per_task=5)
    assert [t.rows for t in result] == [7, 5, 5, 2]
    assert result[-1].pieces == (("b", 7, 9),)


@pytest.mark.parametrize("first,size", [(-1, 3), (4, 3), (1, 0), (1, -1)])
def test_invalid_cut_or_size_is_rejected(original, first, size):
    with pytest.raises(ValueError):
        repartition_tail(original, first_task=first, urls_per_task=size)


@pytest.mark.parametrize(
    "bad",
    [
        TaskSlice(9, (("b", 4, 9),), 5),
        TaskSlice(2, (("b", 4, 9),), 6),
        TaskSlice(2, (("b", -1, 4),), 5),
        TaskSlice(2, (("b", 9, 4),), 5),
    ],
)
def test_malformed_plan_is_rejected(original, bad):
    original[-1] = bad
    with pytest.raises(ValueError):
        repartition_tail(original, first_task=1, urls_per_task=3)


def test_empty_tail_preserves_plan(original):
    assert repartition_tail(original, first_task=3, urls_per_task=3) == original
    assert repartition_tail([], first_task=0, urls_per_task=3) == []


def test_million_row_suffix_becomes_ten_hundred_thousand_row_tasks():
    original = [
        TaskSlice(0, (("a", 0, 1_000_000),), 1_000_000),
        TaskSlice(1, (("a", 1_000_000, 2_000_000),), 1_000_000),
    ]
    result = repartition_tail(original, first_task=1, urls_per_task=100_000)
    assert result[0] == original[0]
    assert len(result) == 11
    assert [task.pieces for task in result[1:]] == [
        (("a", start, start + 100_000),)
        for start in range(1_000_000, 2_000_000, 100_000)
    ]
    assert all(task.rows == 100_000 for task in result[1:])
