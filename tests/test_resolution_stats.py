"""Contract for measuring the corpus's resolution before downloading it.

DataComp-1B records `original_width` and `original_height` — confirmed by
reading the files, not only the documentation — so the question that decides
whether the corpus can feed DINOv3's 2 global crops at 256x256 is answerable
from metadata rather than after 23 TB has arrived.

The sampling is the part worth testing. An earlier measurement in this
project took the first twelve tasks and reported 70-124 KB per image; the
whole-corpus figure is 25.1 KB. Those twelve were pilot tasks, nearly empty
and unrepresentative. Reading the front of a sorted corpus is not sampling
it.
"""

from __future__ import annotations

import pytest

from opendinov3.core import resolution_stats as rs


def test_the_share_below_a_threshold_is_counted() -> None:
    stats = rs.summarise([(100, 100), (255, 300), (256, 256), (640, 480)])
    assert stats.total == 4
    # Below 256 on the SHORT side: a 255x300 image cannot supply a 256 crop.
    assert stats.below(256) == 2
    assert stats.fraction_below(256) == pytest.approx(0.5)


def test_the_short_side_decides_not_the_area() -> None:
    """A 1000x100 image has plenty of pixels and still cannot yield a
    256x256 crop without upscaling."""
    assert rs.summarise([(1000, 100)]).below(256) == 1


def test_a_square_at_exactly_the_threshold_is_not_below_it() -> None:
    assert rs.summarise([(256, 256)]).below(256) == 0


def test_missing_or_impossible_sizes_are_excluded_rather_than_counted() -> None:
    """A zero or null size is unknown, not small. Counting it as small would
    invent a quality problem; counting it as large would hide one."""
    stats = rs.summarise([(0, 0), (None, 500), (500, None), (640, 480)])
    assert stats.total == 1
    assert stats.unusable == 3


def test_aspect_ratio_is_reported_for_bucketing() -> None:
    """Video trainers bucket by aspect ratio; how spread it is decides how
    much padding a batch wastes."""
    stats = rs.summarise([(100, 100), (200, 100), (100, 200)])
    assert stats.median_aspect == pytest.approx(1.0)
    assert stats.fraction_within(0.9, 1.1) == pytest.approx(1 / 3)


def test_an_empty_sample_reports_nothing_rather_than_dividing_by_zero() -> None:
    stats = rs.summarise([])
    assert stats.total == 0
    assert stats.fraction_below(256) is None
    assert stats.median_aspect is None


# --------------------------------------------------------------------------
# Sampling — the part that has already gone wrong once
# --------------------------------------------------------------------------

def test_a_sample_is_spread_across_the_corpus_not_taken_from_the_front(
) -> None:
    chosen = rs.sample_indices(total=1000, want=5)
    assert chosen == sorted(chosen)
    assert len(chosen) == len(set(chosen)) == 5
    assert chosen[0] == 0 and chosen[-1] == 999
    assert max(chosen) - min(chosen) == 999, "must span the corpus"


def test_asking_for_more_than_exists_takes_everything_once() -> None:
    assert rs.sample_indices(total=3, want=10) == [0, 1, 2]


def test_asking_for_one_takes_the_first() -> None:
    assert rs.sample_indices(total=100, want=1) == [0]


def test_a_sample_is_reproducible() -> None:
    """Two runs over the same corpus must report the same figure, or the
    number cannot be quoted."""
    assert rs.sample_indices(2664, 40) == rs.sample_indices(2664, 40)


def test_sampling_nothing_is_refused() -> None:
    with pytest.raises(ValueError):
        rs.sample_indices(total=100, want=0)
