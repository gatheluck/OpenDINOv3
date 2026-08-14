"""How many bytes each stored image costs, and whether tasks really differ.

WHY

`measurements.md` records per-image sizes of 19.7 KB, 23.6 KB, 58 KB and
116 KB — a threefold spread, each figure from a single observation. The
storage estimate for the remaining ~735 tasks rests on which of those is
typical, and nothing so far distinguishes "tasks genuinely differ" from
"shards vary and we looked at one each time".

Experiment 0003 removed one candidate: two different tasks downloaded at
685 s and 681 s, 0.3% apart, so tasks do not differ in any way that shows up
as speed.

THE COMPARISON THAT SETTLES IT

Spread *within* a task against spread *between* tasks. A threefold
difference between tasks means nothing if the shards inside a single task
already differ threefold. Only if shards agree and tasks disagree is there
something to explain.

Everything here is arithmetic over `_stats.json` and tar file sizes, so it
runs on a login node in seconds and needs no job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ShardSize:
    name: str
    tar_bytes: int
    successes: int

    @property
    def bytes_per_image(self) -> float | None:
        """None, not zero, when nothing was stored.

        Zero would pull a task's mean down and read as "small images".
        """
        if self.successes <= 0:
            return None
        return self.tar_bytes / self.successes


@dataclass(frozen=True)
class TaskSize:
    name: str
    usable_shards: int
    tar_bytes: int
    successes: int
    min_bytes_per_image: float | None
    max_bytes_per_image: float | None

    @property
    def bytes_per_image(self) -> float | None:
        """Total bytes over total images.

        Not the mean of the per-shard figures: that would weight a shard of
        ten images the same as one of ten thousand.
        """
        if self.successes <= 0:
            return None
        return self.tar_bytes / self.successes

    @property
    def spread(self) -> float | None:
        """Largest per-shard size over smallest, within this task."""
        if not self.min_bytes_per_image or not self.max_bytes_per_image:
            return None
        return self.max_bytes_per_image / self.min_bytes_per_image


def summarise_task(name: str, shards: Sequence[ShardSize]) -> TaskSize:
    usable = [s for s in shards if s.bytes_per_image is not None]
    sizes = [s.bytes_per_image for s in usable]
    return TaskSize(
        name=name,
        usable_shards=len(usable),
        tar_bytes=sum(s.tar_bytes for s in usable),
        successes=sum(s.successes for s in usable),
        min_bytes_per_image=min(sizes) if sizes else None,
        max_bytes_per_image=max(sizes) if sizes else None,
    )


@dataclass(frozen=True)
class SizeReport:
    tasks_compared: int
    tasks_skipped: int
    between_spread: float | None
    worst_within_spread: float | None
    smallest: TaskSize | None
    largest: TaskSize | None

    @property
    def between_exceeds_within(self) -> bool | None:
        """Whether tasks differ by more than shards inside a task do.

        False means the observed spread is ordinary shard-to-shard variation
        and needs no task-level explanation. True means it does.
        """
        if self.between_spread is None or self.worst_within_spread is None:
            return None
        return self.between_spread > self.worst_within_spread


def compare_tasks(tasks: Sequence[TaskSize]) -> SizeReport:
    if not tasks:
        raise ValueError("compare_tasks() needs at least one task")

    usable = [t for t in tasks if t.bytes_per_image is not None]
    skipped = len(tasks) - len(usable)
    if not usable:
        return SizeReport(0, skipped, None, None, None, None)

    ordered = sorted(usable, key=lambda t: t.bytes_per_image)
    smallest, largest = ordered[0], ordered[-1]
    within = [t.spread for t in usable if t.spread is not None]

    return SizeReport(
        tasks_compared=len(usable),
        tasks_skipped=skipped,
        between_spread=largest.bytes_per_image / smallest.bytes_per_image,
        worst_within_spread=max(within) if within else None,
        smallest=smallest,
        largest=largest,
    )
