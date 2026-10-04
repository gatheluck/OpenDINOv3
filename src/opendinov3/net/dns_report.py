"""Summarise observed name-resolution API calls, not DNS packets.

Exit reports cannot prove that every process reported. Even a successful
DONE marker does not prove counter coverage. Counts and rates therefore
remain partial observations; a missing report means unknown, not cache off.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import dns_cache

_LINE = re.compile(
    rf"^{dns_cache.REPORT_PREFIX} hits=(\d+) misses=(\d+)\s*$", re.MULTILINE
)


def parse_line(line: str) -> tuple[int, int] | None:
    match = _LINE.match(line.strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


@dataclass(frozen=True)
class Summary:
    """Counters from available exit reports; process coverage is unverified."""

    reports: int
    hits: int
    misses: int
    wall_seconds: int | None

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def measurement_status(self) -> str:
        return "partial" if self.reports else "unknown"

    @property
    def cache_hit_fraction(self) -> float | None:
        """Fraction served by the application cache in the observed reports."""
        if not self.reports or not self.lookups:
            return None
        return self.hits / self.lookups

    @property
    def observed_resolver_calls_per_second(self) -> float | None:
        """Observed getaddrinfo calls divided by task wall time, not peak QPS."""
        if not self.reports or not self.wall_seconds:
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
            metadata = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            metadata = None
        value = metadata.get("wall_seconds") if isinstance(metadata, dict) else None
        # production_task.sh writes positive integer seconds. Reject malformed
        # values, including bool (an int subclass), instead of inventing a rate.
        if type(value) is int and value > 0:
            wall = value

    return Summary(
        reports=len(found),
        hits=sum(int(h) for h, _ in found),
        misses=sum(int(m) for _, m in found),
        wall_seconds=wall,
    )
