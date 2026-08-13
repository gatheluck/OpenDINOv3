"""Contract for the shard throughput measurement.

WHY THIS EXISTS

The largest unvalidated assumption in the project is that the shards already
downloaded can actually be streamed by a training job. Nobody has checked.
Finishing the remaining download first and discovering a format problem
afterwards would waste weeks.

"A sample decodes" and "a training loop can consume this at speed" are
different claims. This measures the second one.

WHAT IS MEASURED, AND WHAT IS NOT

Measured: the rate at which samples can be read out of a tar and decoded,
which is the dominant cost in an image pipeline.

Not measured: the overhead a torch DataLoader adds on top — collation and
inter-process transfer. torch is deliberately absent from the image; adding
it costs several gigabytes for every pull. If the measured ceiling turns out
to sit close to what training needs, that overhead has to be measured too,
and torch gets added then.

The distinction is recorded because a throughput number without its scope is
easy to over-read.
"""

from __future__ import annotations

import pytest

from opendinov3.core import bench
from tests.fixtures import synthetic


# --------------------------------------------------------------------------
# Rate arithmetic
# --------------------------------------------------------------------------

def test_rate_is_samples_over_elapsed() -> None:
    result = bench.BenchResult(samples=1000, decode_failures=0, bytes_read=0,
                               elapsed_sec=2.0)
    assert result.samples_per_sec == 500.0


def test_zero_elapsed_does_not_divide_by_zero() -> None:
    """A shard small enough to finish inside timer resolution is plausible.

    Returning None rather than raising keeps a fast shard from aborting a
    whole benchmark run.
    """
    result = bench.BenchResult(samples=10, decode_failures=0, bytes_read=0,
                               elapsed_sec=0.0)
    assert result.samples_per_sec is None


def test_decode_failure_rate_excludes_nothing() -> None:
    """The denominator is every sample attempted, not just the successful ones.

    Dividing by successes would make the failure rate shrink as failures grow,
    which is the wrong direction.
    """
    result = bench.BenchResult(samples=90, decode_failures=10, bytes_read=0,
                               elapsed_sec=1.0)
    assert result.attempted == 100
    assert result.decode_failure_rate == pytest.approx(0.10)


def test_failure_rate_of_an_empty_run_is_none_not_zero() -> None:
    """Zero would claim a perfect run where nothing was attempted."""
    result = bench.BenchResult(samples=0, decode_failures=0, bytes_read=0,
                               elapsed_sec=1.0)
    assert result.decode_failure_rate is None


# --------------------------------------------------------------------------
# Aggregation across shards
# --------------------------------------------------------------------------

def test_aggregate_sums_counts_and_elapsed() -> None:
    total = bench.aggregate([
        bench.BenchResult(samples=100, decode_failures=1, bytes_read=10,
                          elapsed_sec=1.0),
        bench.BenchResult(samples=300, decode_failures=3, bytes_read=30,
                          elapsed_sec=3.0),
    ])
    assert total.samples == 400
    assert total.decode_failures == 4
    assert total.bytes_read == 40
    assert total.elapsed_sec == 4.0
    assert total.samples_per_sec == 100.0


def test_aggregate_of_nothing_is_empty_not_an_error() -> None:
    total = bench.aggregate([])
    assert total.samples == 0
    assert total.samples_per_sec is None


# --------------------------------------------------------------------------
# Reading real shard structure
# --------------------------------------------------------------------------

def test_streaming_a_shard_counts_every_sample(tmp_path) -> None:
    directory = synthetic.write_shard(tmp_path, shard_index=0, n_samples=7)
    result = bench.measure_shard(directory / "00000.tar")
    assert result.samples == 7
    assert result.decode_failures == 0


def test_bytes_read_is_recorded(tmp_path) -> None:
    """Throughput in samples/sec alone hides a change in image size.

    A pipeline resized to smaller images would look faster while moving the
    same bytes, so both are recorded.
    """
    directory = synthetic.write_shard(tmp_path, shard_index=0, n_samples=3)
    result = bench.measure_shard(directory / "00000.tar")
    assert result.bytes_read > 0


def test_a_corrupt_image_is_counted_and_iteration_continues(tmp_path) -> None:
    """One bad sample must not end the run.

    Real shards are built from arbitrary web content; a decoder failure is an
    expected event, not an exceptional one. Aborting on the first would make
    the measurement impossible on exactly the data it exists to measure.
    """
    directory = synthetic.write_shard(
        tmp_path, shard_index=0, n_samples=5, corrupt_image_for={2}
    )
    result = bench.measure_shard(directory / "00000.tar")
    assert result.decode_failures == 1
    assert result.samples == 4, "the other four samples must still be read"


def test_missing_shard_is_reported_clearly(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        bench.measure_shard(tmp_path / "does-not-exist.tar")
