"""Add up what a task's workers actually asked the resolver.

WHY

ABCI asked us to stop a wave because our DNS traffic was affecting other
users. Any answer we give them has to be measured, not predicted: `od.sh
hosts` says 76% of lookups repeat, but the cache is per process and a node
runs 32 of them, so the realised reduction is lower by an amount only a run
can tell us.

The quantity they asked about is **queries per second per node** — what
reaches their resolver — not our hit rate.

WHY IT COMES OUT OF THE LOG

The cache lives in each worker, and workers are spawned and recycled every
five shards, so there is no object left to read when the task ends. Each
process prints its counts as it exits and the totals are the sum. A task
whose log has no such lines ran without the cache, which is a different
statement from having made no queries — reported as unknown rather than
zero, because zero would flatter us to the people we owe an answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import dns_cache

_LINE = re.compile(rf"^{dns_cache.REPORT_PREFIX} hits=(\d+) misses=(\d+)\s*$",
                   re.MULTILINE)


def parse_line(line: str) -> tuple[int, int] | None:
    match = _LINE.match(line.strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


@dataclass(frozen=True)
class Summary:
    """What one task asked for."""

    workers: int
    hits: int
    misses: int
    wall_seconds: int | None

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def reduction(self) -> float | None:
        """Share of lookups the resolver never saw.

        None when the cache was not running: no reduction was measured, and
        saying 0% would be as wrong as saying 76%.
        """
        if not self.workers or not self.lookups:
            return None
        return self.hits / self.lookups

    @property
    def queries_per_second(self) -> float | None:
        """The number the site asked about, per node.

        None without a wall time — a rate cannot be invented from a count.
        """
        if not self.workers or not self.wall_seconds:
            return None
        return self.misses / self.wall_seconds


def summarise(task_dir: Path) -> Summary:
    log = task_dir / "img2dataset.log"
    text = log.read_text(errors="replace") if log.is_file() else ""
    found = _LINE.findall(text)

    wall = None
    marker = task_dir / "DONE.json"
    if marker.is_file():
        try:
            wall = int(json.loads(marker.read_text()).get("wall_seconds") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            wall = None

    return Summary(
        workers=len(found),
        hits=sum(int(h) for h, _ in found),
        misses=sum(int(m) for _, m in found),
        wall_seconds=wall or None,
    )
