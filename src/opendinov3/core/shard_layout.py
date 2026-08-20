"""Where a task's shards are, in either tree.

This runner writes `task-NNNNNN/shards/00000.tar`. The predecessor's wrote
`task-NNNNNN/00000.tar`, with no `shards` directory. Reading tools globbed
the first shape only, so `od.sh slow` pointed at the predecessor's COYO tree
reported "no completed shards" for 747 task directories full of them —
indistinguishable from an empty corpus, and acting on it would have meant
re-downloading URLs already on disk.

Resolved once, here, rather than per script: `inspect_pilot` reads tars,
parquets and stats for one task, and if they disagreed about the layout it
would divide one tree's successes by another's attempts.

Only the read-only tools use this. Nothing writes into the predecessor's
tree — it is not ours, and od.sh binds it read-only.
"""

from __future__ import annotations

from pathlib import Path

#: Task directories, in both trees. Attempt directories set aside by a retry
#: (`task-NNNNNN.attempt-<jobid>`) match this too, which is the behaviour the
#: reading tools already had; changing it is a separate question about what
#: counts as corpus.
TASK_GLOB = "task-*"

#: Ours, when it is there.
SHARD_DIR = "shards"


def shard_dir(task_dir: Path) -> Path:
    """The directory holding this task's shard files.

    `shards` wins when it has something in it: a task with that directory
    was written by this pipeline, and anything beside it is a leftover. An
    *empty* `shards` does not win — a task set up but not yet written would
    otherwise hide shards that are really there.
    """
    nested = task_dir / SHARD_DIR
    if nested.is_dir() and any(nested.iterdir()):
        return nested
    return task_dir


def _files(task_dir: Path, pattern: str) -> list[Path]:
    return sorted(shard_dir(task_dir).glob(pattern))


def stats_files(task_dir: Path) -> list[Path]:
    """`NNNNN_stats.json`, one per completed shard."""
    return _files(task_dir, "*_stats.json")


def tar_files(task_dir: Path) -> list[Path]:
    return _files(task_dir, "*.tar")


def parquet_files(task_dir: Path) -> list[Path]:
    """The shard metadata parquet, not the task's `urls.parquet`.

    `urls.parquet` sits in the task directory, so in the predecessor's flat
    layout it would be swept up by a bare `*.parquet`. Excluded by name
    rather than by position, because position is exactly what differs.
    """
    return [p for p in _files(task_dir, "*.parquet") if p.name != "urls.parquet"]


def task_dirs(task_root: Path) -> list[Path]:
    return sorted(p for p in task_root.glob(TASK_GLOB) if p.is_dir())


def all_stats_files(task_root: Path) -> list[Path]:
    """Every completed shard's stats under a tree, whichever layout wrote it.

    A tree can hold both at once — our retries of the predecessor's tasks
    land beside them — so the layout is decided per task, not per tree.
    """
    found: list[Path] = []
    for task in task_dirs(task_root):
        found.extend(stats_files(task))
    return found
