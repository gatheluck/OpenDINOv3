"""How large the images are, from metadata, before any are fetched.

WHY

DINOv3 takes 2 global crops at 256x256 and 8 local crops at 112x112. An image
whose short side is under 256 cannot supply a global crop without upscaling,
so what share of the corpus that is decides how much of a 902-million-image
download is actually useful for the intended training.

DataComp-1B records `original_width` and `original_height` — confirmed by
reading the files, not only the documentation — so the question is answerable
now rather than after 23 TB has arrived. Video trainers bucket by aspect
ratio, so its spread is reported for the same reason.

THE SHORT SIDE, NOT THE AREA

A 1000x100 image has plenty of pixels and still cannot yield a 256x256 crop.
Counting by area would call it large.

SAMPLING

Evenly spaced across the corpus, never the front of it. Reading the first
twelve tasks once gave 70-124 KB per image against a whole-corpus 25.1 KB,
because the front of the list was pilot data. Deterministic, so two runs
report the same figure and the number can be quoted.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Iterable, Sequence

Size = tuple[int | None, int | None]


def sample_indices(total: int, want: int) -> list[int]:
    """`want` positions spread evenly over `total`, including both ends.

    Deterministic: no randomness, so a reported figure is reproducible.
    """
    if want <= 0:
        raise ValueError(f"want must be positive, got {want}")
    if total <= 0:
        return []
    if want >= total:
        return list(range(total))
    if want == 1:
        return [0]
    step = (total - 1) / (want - 1)
    return sorted({round(i * step) for i in range(want)})


@dataclass(frozen=True)
class ResolutionStats:
    total: int
    unusable: int
    short_sides: tuple[int, ...]
    aspects: tuple[float, ...]

    def below(self, threshold: int) -> int:
        return sum(1 for side in self.short_sides if side < threshold)

    def fraction_below(self, threshold: int) -> float | None:
        if not self.total:
            return None
        return self.below(threshold) / self.total

    @property
    def median_short_side(self) -> float | None:
        return median(self.short_sides) if self.short_sides else None

    def percentile(self, p: float) -> float | None:
        """The short side at the p-th percentile.

        A single "share below 256" answers one consumer's question. This
        corpus is being built for several, and for consumers not yet known,
        so the spread is the useful output: a reader picks their own
        threshold and reads off their own answer.
        """
        if not self.short_sides:
            return None
        ordered = sorted(self.short_sides)
        position = (len(ordered) - 1) * p / 100
        low = int(position)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    @property
    def median_aspect(self) -> float | None:
        return median(self.aspects) if self.aspects else None

    def fraction_within(self, low: float, high: float) -> float | None:
        """Share whose aspect ratio falls in a band, for bucketing."""
        if not self.total:
            return None
        return sum(1 for a in self.aspects if low <= a <= high) / self.total


def summarise(sizes: Iterable[Size]) -> ResolutionStats:
    """Distribution over sizes, ignoring ones that cannot be believed.

    A zero or missing dimension is unknown, not small. Counting it as small
    would invent a quality problem; counting it as large would hide one.
    """
    short_sides: list[int] = []
    aspects: list[float] = []
    unusable = 0

    for width, height in sizes:
        if not width or not height or width <= 0 or height <= 0:
            unusable += 1
            continue
        short_sides.append(min(int(width), int(height)))
        aspects.append(width / height)

    return ResolutionStats(
        total=len(short_sides),
        unusable=unusable,
        short_sides=tuple(short_sides),
        aspects=tuple(aspects),
    )


def merge(parts: Sequence[ResolutionStats]) -> ResolutionStats:
    """Combine per-file results without holding every row at once."""
    short_sides: list[int] = []
    aspects: list[float] = []
    unusable = 0
    for part in parts:
        short_sides.extend(part.short_sides)
        aspects.extend(part.aspects)
        unusable += part.unusable
    return ResolutionStats(len(short_sides), unusable,
                           tuple(short_sides), tuple(aspects))
