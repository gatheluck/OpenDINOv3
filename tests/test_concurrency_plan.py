"""Contract for the relationship between shard count and process count.

img2dataset distributes work one shard at a time:
`Pool(processes_count).imap_unordered(downloader, reader)`, where the reader
yields `ceil(rows / number_sample_per_shard)` shards. Asking for more
processes than there are shards therefore does nothing — the extra processes
never receive work.

The originally registered configuration hit this exactly: 200,000 URLs at
10,000 per shard is 20 shards, so the 32- and 64-process levels would both
have run at 20 and looked identical. The experiment would have recorded
"8 → 32" while measuring "8 → 20".

Nothing about that is visible in the output, so it is checked here and
before submission rather than trusted.
"""

from __future__ import annotations

import pytest

from opendinov3.core import concurrency_plan as cp

LEVELS = [8, 32, 64, 8]


def test_shard_count_rounds_up() -> None:
    """Verified against img2dataset's reader: ceil(rows / per_shard)."""
    assert cp.shard_count(5000, 10000) == 1
    assert cp.shard_count(5000, 2500) == 2
    assert cp.shard_count(5000, 1000) == 5
    assert cp.shard_count(200_000, 10_000) == 20


def test_processes_beyond_the_shard_count_do_nothing() -> None:
    assert cp.effective_processes(64, shards=20) == 20
    assert cp.effective_processes(8, shards=20) == 8


def test_the_originally_registered_configuration_is_capped() -> None:
    """The regression this module exists to prevent."""
    levels = cp.plan(slice_size=200_000, samples_per_shard=10_000, levels=LEVELS)
    by_request = {level.requested: level for level in levels}

    assert by_request[8].effective == 8, "8 processes fit in 20 shards"
    assert by_request[32].effective == 20
    assert by_request[64].effective == 20
    assert by_request[32].effective == by_request[64].effective, (
        "the two upper levels would have been indistinguishable"
    )


def test_validate_names_the_capped_levels() -> None:
    levels = cp.plan(slice_size=200_000, samples_per_shard=10_000, levels=LEVELS)
    problems = cp.validate(levels)
    assert problems, "a capped configuration must not pass"
    assert any("32" in p for p in problems)
    assert any("64" in p for p in problems)


# --------------------------------------------------------------------------
# Waves: how many times each process cycles through a shard
# --------------------------------------------------------------------------

def test_waves_are_counted_against_effective_processes() -> None:
    """With one wave the run's wall time is the slowest shard, not the mean.

    Work is handed out dynamically, so the tail is bounded by a single shard;
    more waves shrink that tail relative to the run.
    """
    levels = cp.plan(slice_size=200_000, samples_per_shard=1_000, levels=[64])
    assert levels[0].shards == 200
    assert levels[0].waves == pytest.approx(200 / 64)


def test_a_single_wave_is_rejected_even_when_nothing_is_capped() -> None:
    """64 shards and 64 processes is uncapped but still one wave."""
    levels = cp.plan(slice_size=640_000, samples_per_shard=10_000, levels=[64])
    assert levels[0].effective == 64, "not capped"
    problems = cp.validate(levels, min_waves=3)
    assert problems, "one wave should still be refused"
    assert any("wave" in p for p in problems)


def test_the_chosen_configuration_passes() -> None:
    levels = cp.plan(slice_size=200_000, samples_per_shard=1_000, levels=LEVELS)
    assert cp.validate(levels, min_waves=3) == []


# --------------------------------------------------------------------------
# Suggesting a fix, so a failure does not cost another round trip
# --------------------------------------------------------------------------

def test_the_suggestion_actually_satisfies_the_check() -> None:
    suggested = cp.suggest_samples_per_shard(
        slice_size=200_000, levels=LEVELS, min_waves=3
    )
    levels = cp.plan(
        slice_size=200_000, samples_per_shard=suggested, levels=LEVELS
    )
    assert cp.validate(levels, min_waves=3) == [], (
        f"suggested {suggested} but it does not pass"
    )


def test_the_suggestion_is_driven_by_the_highest_level() -> None:
    """The lowest level is satisfied by anything the highest level allows."""
    assert cp.suggest_samples_per_shard(200_000, [8, 64], 3) == \
        cp.suggest_samples_per_shard(200_000, [64], 3)


def test_a_slice_too_small_for_the_top_level_is_refused() -> None:
    """No shard size rescues 100 URLs across 64 processes at 3 waves."""
    with pytest.raises(ValueError):
        cp.suggest_samples_per_shard(slice_size=100, levels=[64], min_waves=3)
