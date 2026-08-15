"""Contract for resuming a task instead of restarting it.

img2dataset already resumes. It decides a shard is done by globbing
`*.json`:

    done_shards = set(int(x.split("/")[-1].split("_")[0])
                      for x in fs.glob(output_path + "/*.json"))

and `NNNNN_stats.json` is written only when a shard finishes. So a shard
killed mid-write is already redone, and a finished one is already skipped.

The pipeline passed `--incremental_mode incremental` and then moved the
whole task directory aside before every retry, destroying exactly the state
that flag reads. A task killed at the walltime with 90 of 100 shards done
re-downloaded all 100.

The one real hazard is the 2026-07-28 outage, which wrote 100 finished
shards per task holding 0.1% successes. Those must not be inherited, or the
retry reproduces the empty task.

So: keep the finished healthy shards, set aside the finished empty ones,
and leave the mid-write ones alone because they are already not counted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opendinov3.core import task_health  # noqa: F401  (shared vocabulary)

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import prepare_retry as pr  # noqa: E402


def shard(task_dir: Path, index: int, *, count: int | None = 10_000,
          successes: int = 6_500, tar: bool = True,
          unreadable: bool = False) -> None:
    """One shard as img2dataset leaves it.

    `count=None` means no `_stats.json`: the shard was still being written
    when the job was killed.
    """
    shards = task_dir / "shards"
    shards.mkdir(parents=True, exist_ok=True)
    if tar:
        (shards / f"{index:05d}.tar").write_bytes(b"tar")
        (shards / f"{index:05d}.parquet").write_bytes(b"parquet")
    if count is None:
        return
    body = ("not json" if unreadable
            else json.dumps({"count": count, "successes": successes}))
    (shards / f"{index:05d}_stats.json").write_text(body)


def test_a_finished_healthy_shard_is_kept(tmp_path) -> None:
    """The whole point: 90 finished shards must not be re-downloaded."""
    shard(tmp_path, 0)
    kept, aside = pr.prepare(tmp_path, "retry")
    assert (kept, aside) == (1, 0)
    assert (tmp_path / "shards" / "00000_stats.json").is_file()


def test_a_finished_empty_shard_is_set_aside(tmp_path) -> None:
    """The outage profile. Inherited, incremental mode would skip it and
    the retry would reproduce the empty task."""
    shard(tmp_path, 0, count=10_000, successes=10)
    kept, aside = pr.prepare(tmp_path, "retry")
    assert (kept, aside) == (0, 1)
    assert not (tmp_path / "shards" / "00000_stats.json").exists()


def test_a_shard_killed_mid_write_is_left_alone(tmp_path) -> None:
    """It has no `_stats.json`, so img2dataset already does not count it.
    Touching it would be work for nothing."""
    shard(tmp_path, 0, count=None)
    kept, aside = pr.prepare(tmp_path, "retry")
    assert (kept, aside) == (0, 0)
    assert (tmp_path / "shards" / "00000.tar").is_file()


def test_a_mixed_task_keeps_only_the_good_shards(tmp_path) -> None:
    """The realistic case after a walltime kill."""
    for index in range(9):
        shard(tmp_path, index)                      # finished, healthy
    shard(tmp_path, 9, count=10_000, successes=3)   # finished, empty
    shard(tmp_path, 10, count=None)                 # mid-write
    kept, aside = pr.prepare(tmp_path, "retry")
    assert (kept, aside) == (9, 1)
    assert len(list((tmp_path / "shards").glob("*_stats.json"))) == 9


def test_every_file_of_a_bad_shard_moves_together(tmp_path) -> None:
    """Leaving the .tar behind would let the redone shard collide with it."""
    shard(tmp_path, 7, count=10_000, successes=1)
    pr.prepare(tmp_path, "retry")
    for suffix in (".tar", ".parquet", "_stats.json"):
        assert not (tmp_path / "shards" / f"00007{suffix}").exists()
        assert (tmp_path / "attempt-retry" / f"00007{suffix}").is_file()


def test_a_bad_shard_is_kept_as_evidence_not_deleted(tmp_path) -> None:
    """The 2026-07-28 loss was diagnosed weeks later from these files."""
    shard(tmp_path, 0, count=10_000, successes=5)
    pr.prepare(tmp_path, "retry")
    assert (tmp_path / "attempt-retry" / "00000_stats.json").is_file()


def test_an_unreadable_stats_file_is_not_inherited(tmp_path) -> None:
    """A truncated write cannot be judged, so it cannot be trusted."""
    shard(tmp_path, 0, unreadable=True)
    kept, aside = pr.prepare(tmp_path, "retry")
    assert (kept, aside) == (0, 1)


def test_a_zero_count_shard_is_not_inherited(tmp_path) -> None:
    """Dividing by it would raise; inheriting it would skip a shard that
    attempted nothing."""
    shard(tmp_path, 0, count=0, successes=0)
    assert pr.prepare(tmp_path, "retry") == (0, 1)


def test_no_previous_attempt_is_a_no_op(tmp_path) -> None:
    assert pr.prepare(tmp_path, "retry") == (0, 0)


def test_the_boundary_is_the_documented_one(tmp_path) -> None:
    """5%: the outage stored 0.1%, healthy shards store 58-65%. Nothing
    real sits near the line, which is why it can be a simple floor."""
    shard(tmp_path, 0, count=10_000, successes=500)     # exactly 5%
    shard(tmp_path, 1, count=10_000, successes=499)     # just under
    kept, aside = pr.prepare(tmp_path, "retry")
    assert (kept, aside) == (1, 1)
