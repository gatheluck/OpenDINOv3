"""What concurrency a configuration can actually reach.

img2dataset hands out work one shard at a time — its distributor is
`Pool(processes_count).imap_unordered(downloader, reader)`, and the reader
yields `ceil(rows / number_sample_per_shard)` shards. Two consequences follow,
and neither is visible in the output:

* Asking for more processes than there are shards does nothing. The extra
  workers never receive a task, and a run at 64 processes over 20 shards is a
  run at 20.

* With roughly one shard per process, the run's wall time is the slowest
  shard rather than the mean. Work is handed out dynamically so the tail is
  bounded by one shard, but that tail is only small relative to the run when
  each process cycles through several shards.

Both would corrupt a concurrency measurement silently, so a configuration is
checked before it is submitted rather than after it has produced numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

#: Each process should cycle through at least this many shards, so that one
#: slow shard is a fraction of the run rather than its duration. A judgement
#: call, not a measurement; raise it for a tighter tail at the cost of
#: smaller shards.
DEFAULT_MIN_WAVES = 3


def shard_count(slice_size: int, samples_per_shard: int) -> int:
    """Shards img2dataset will produce. Matches its reader exactly."""
    return math.ceil(slice_size / samples_per_shard)


def effective_processes(requested: int, shards: int) -> int:
    """Processes that will actually receive work."""
    return min(requested, shards)


@dataclass(frozen=True)
class LevelPlan:
    requested: int
    effective: int
    shards: int

    @property
    def capped(self) -> bool:
        return self.effective < self.requested

    @property
    def waves(self) -> float:
        """Shards each working process gets through, on average."""
        return self.shards / self.effective if self.effective else 0.0


def plan(
    slice_size: int, samples_per_shard: int, levels: Sequence[int]
) -> list[LevelPlan]:
    shards = shard_count(slice_size, samples_per_shard)
    return [
        LevelPlan(
            requested=level,
            effective=effective_processes(level, shards),
            shards=shards,
        )
        for level in levels
    ]


def validate(
    levels: Sequence[LevelPlan], min_waves: int = DEFAULT_MIN_WAVES
) -> list[str]:
    """Problems that would make the measurement mean something else.

    Returns messages rather than raising, so a caller can show all of them at
    once instead of one per attempt.
    """
    problems: list[str] = []
    seen: set[int] = set()

    for level in levels:
        if level.requested in seen:
            continue
        seen.add(level.requested)

        if level.capped:
            problems.append(
                f"{level.requested} processes would run at {level.effective}: "
                f"only {level.shards} shards exist, and img2dataset gives each "
                "process one shard at a time."
            )
        elif level.waves < min_waves:
            problems.append(
                f"{level.requested} processes get {level.waves:.1f} shards "
                f"each (want at least {min_waves}): at roughly one wave the "
                "run's wall time is its slowest shard rather than its mean."
            )
    return problems


def suggest_samples_per_shard(
    slice_size: int,
    levels: Sequence[int],
    min_waves: int = DEFAULT_MIN_WAVES,
) -> int:
    """The largest shard size that lets every level reach its process count.

    Driven by the highest level: whatever satisfies it satisfies the rest.
    Larger shards are preferred because they stay closer to what production
    writes and pay less per-shard overhead.
    """
    top = max(levels)
    value = slice_size // (top * min_waves)
    if value < 1:
        raise ValueError(
            f"{slice_size} URLs cannot give {top} processes {min_waves} shards "
            f"each; at least {top * min_waves} URLs are needed."
        )
    return value


def describe(levels: Sequence[LevelPlan]) -> str:
    """A table for the submit script, so the cap is seen before submitting."""
    lines = [
        f"{'processes':>10} {'effective':>10} {'shards':>8} {'shards/proc':>12}",
        "-" * 44,
    ]
    for level in levels:
        flag = "  ← capped" if level.capped else ""
        lines.append(
            f"{level.requested:>10} {level.effective:>10} "
            f"{level.shards:>8} {level.waves:>12.1f}{flag}"
        )
    return "\n".join(lines)
