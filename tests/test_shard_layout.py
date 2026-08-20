"""Contract for finding a task's shards, whichever tree wrote them.

Our runner writes `task-NNNNNN/shards/00000.tar`. The predecessor's wrote
`task-NNNNNN/00000.tar` — no `shards` directory. Every read-only tool here
globbed the first shape only, so pointing `od.sh slow` at the predecessor's
COYO tree answered "no completed shards under ..." for 747 task directories
that are full of them.

That answer is the dangerous kind: it is what an empty corpus looks like,
and acting on it would have meant re-downloading 747 million URLs that are
already on disk.

WHY ONE HELPER RATHER THAN A GLOB PER SCRIPT

`inspect_pilot` reads tars, parquets and stats for the same task. If they
disagreed about where a task's shards live it would count one layout's tars
against the other's stats and report a yield that describes neither.
"""

from __future__ import annotations

from opendinov3.core import shard_layout as sl


def write(path, text="{}"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_our_own_layout_is_found(tmp_path) -> None:
    task = tmp_path / "task-000000"
    write(task / "shards" / "00000_stats.json")
    write(task / "shards" / "00001_stats.json")

    assert [p.name for p in sl.stats_files(task)] == [
        "00000_stats.json", "00001_stats.json"]


def test_the_predecessors_layout_is_found(tmp_path) -> None:
    """747 COYO task directories are in this shape."""
    task = tmp_path / "task-000000"
    write(task / "00000_stats.json")
    write(task / "00001_stats.json")

    assert [p.name for p in sl.stats_files(task)] == [
        "00000_stats.json", "00001_stats.json"]


def test_a_task_with_both_is_read_once(tmp_path) -> None:
    """Counting both would double the URLs and halve the apparent yield.

    Ours wins: if a task has a `shards` directory it was written by this
    pipeline, and anything beside it is a leftover.
    """
    task = tmp_path / "task-000000"
    write(task / "shards" / "00000_stats.json")
    write(task / "00000_stats.json")

    found = sl.stats_files(task)
    assert len(found) == 1
    assert found[0].parent.name == "shards"


def test_an_empty_shards_directory_does_not_hide_the_shards(tmp_path) -> None:
    """A task set up but not yet written has an empty `shards`. Treating its
    presence as the answer would report zero for a tree that has data."""
    task = tmp_path / "task-000000"
    (task / "shards").mkdir(parents=True)
    write(task / "00000_stats.json")

    assert len(sl.stats_files(task)) == 1


def test_a_task_with_nothing_reports_nothing(tmp_path) -> None:
    task = tmp_path / "task-000000"
    task.mkdir()
    assert sl.stats_files(task) == []


def test_tars_and_parquets_come_from_the_same_place_as_the_stats(tmp_path
                                                                  ) -> None:
    """Or a yield would be computed from one layout's numerator and the
    other's denominator."""
    task = tmp_path / "task-000000"
    write(task / "00000_stats.json")
    write(task / "00000.tar", "x")
    write(task / "00000.parquet", "x")
    # A stray empty `shards` must not redirect only some of them.
    (task / "shards").mkdir()

    assert len(sl.stats_files(task)) == 1
    assert len(sl.tar_files(task)) == 1
    assert len(sl.parquet_files(task)) == 1


def test_every_task_in_a_tree_is_swept(tmp_path) -> None:
    for i in range(3):
        write(tmp_path / f"task-{i:06d}" / f"{i:05d}_stats.json")
    write(tmp_path / "not-a-task" / "00000_stats.json")

    found = sl.all_stats_files(tmp_path)
    assert len(found) == 3, found


def test_a_mixed_tree_is_swept_whole(tmp_path) -> None:
    """A tree can hold both: our retries of the predecessor's tasks."""
    write(tmp_path / "task-000000" / "00000_stats.json")
    write(tmp_path / "task-000001" / "shards" / "00000_stats.json")

    assert len(sl.all_stats_files(tmp_path)) == 2
