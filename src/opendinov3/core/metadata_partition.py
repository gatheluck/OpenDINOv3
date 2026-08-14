"""Cut upstream metadata into download tasks, from row counts alone.

WHY NOT REUSE THE PREDECESSOR'S PARTITIONER

Two blockers, both in its own source:

* `write_boundary_check()` raises unless the output path starts with their
  `OPEN_DINO_ROOT`. It cannot write into anyone else's tree.
* It loads every parquet file into pandas and concatenates before
  partitioning — 2,664 files, roughly 1.3 billion rows, each carrying the
  source file name as a per-row Python string. It runs only because the
  compute nodes have 1,920 GB of memory.

WHAT IS KEPT

Its determinism. Files sorted by path, concatenated in order, cut into
fixed-size chunks from the start. Same metadata, same task size, same
corpus — which is what makes the partition reproducible from the inputs
rather than from a saved artifact.

WHAT CHANGES

The plan is computed from parquet *metadata* — `num_rows` in the footer —
not from the rows themselves. 2,664 header reads instead of hundreds of
gigabytes, so the scale of a run can be known before anything is written and
without a large-memory node.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class SourceFile:
    """One upstream parquet file and how many rows it holds."""

    path: str
    rows: int


@dataclass(frozen=True)
class TaskSlice:
    """One download task, as row ranges over the source files.

    A task boundary lands inside a file more often than not, so `pieces` may
    name several sources. Each entry is (path, start row, end row), with the
    end exclusive.
    """

    task_id: int
    pieces: tuple[tuple[str, int, int], ...]
    rows: int


def plan_tasks(
    sources: Sequence[SourceFile], urls_per_task: int
) -> list[TaskSlice]:
    """Fixed-size tasks over the sources, in the order given.

    The order is not touched. Determinism depends on the caller sorting once
    and this function never reordering; otherwise the same metadata would
    produce a different corpus on a different filesystem.

    The final task is short rather than dropped — dropping it would silently
    discard URLs.
    """
    if urls_per_task <= 0:
        raise ValueError(f"urls_per_task must be positive, got {urls_per_task}")

    tasks: list[TaskSlice] = []
    pieces: list[tuple[str, int, int]] = []
    filled = 0

    for source in sources:
        # A file with no rows needs no special case: `while offset < rows` is
        # false immediately. A guard here would be an unreachable branch, and
        # mutation testing showed no test could tell whether it was present.
        offset = 0
        while offset < source.rows:
            take = min(urls_per_task - filled, source.rows - offset)
            if take <= 0:
                # Cannot happen with a positive urls_per_task, and the loop
                # must not be able to spin if it ever could. Removing the
                # argument check above previously turned this into an
                # infinite loop rather than a failing test, which a test
                # suite cannot catch.
                raise ValueError(
                    f"no progress possible: urls_per_task={urls_per_task}, "
                    f"filled={filled}, source={source.path}"
                )
            pieces.append((source.path, offset, offset + take))
            offset += take
            filled += take
            if filled == urls_per_task:
                tasks.append(TaskSlice(len(tasks), tuple(pieces), filled))
                pieces, filled = [], 0

    if filled:
        tasks.append(TaskSlice(len(tasks), tuple(pieces), filled))
    return tasks


@dataclass(frozen=True)
class PartitionSummary:
    total_rows: int
    tasks: int
    full_tasks: int
    final_task_rows: int
    urls_per_task: int

    @property
    def node_hours(self) -> float:
        """One task is about one node-hour at 32 processes.

        Measured twice on different tasks: 186.7 and 179.0 successes/sec for
        1,000,000 URLs at ~65% yield.
        """
        return self.total_rows / 1_000_000


def summarise(
    sources: Sequence[SourceFile], urls_per_task: int
) -> PartitionSummary:
    """The scale of a run, before anything is written."""
    tasks = plan_tasks(sources, urls_per_task)
    total = sum(s.rows for s in sources if s.rows > 0)
    final = tasks[-1].rows if tasks else 0
    return PartitionSummary(
        total_rows=total,
        tasks=len(tasks),
        full_tasks=sum(1 for t in tasks if t.rows == urls_per_task),
        final_task_rows=final,
        urls_per_task=urls_per_task,
    )
