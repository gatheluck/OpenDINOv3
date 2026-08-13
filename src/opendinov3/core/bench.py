"""Measure how fast shards can be read and decoded.

The question this answers is whether a training job can consume the shards
that have already been downloaded. "A sample decodes" and "a training loop
can keep up" are different claims, and only the second one decides whether
the corpus is usable.

SCOPE

What is measured: tar read plus image decode, which dominates the cost of an
image pipeline.

What is not measured: the overhead torch's DataLoader adds — collation and
inter-process transfer. torch is not in the image; it would add several
gigabytes to every pull. This gives the ceiling. If the ceiling turns out to
sit near what training needs, the remaining overhead has to be measured too.

Reporting a number without its scope invites over-reading it, so the scope
travels with the result.

DECODE FAILURES ARE EXPECTED

Shards are built from arbitrary web content. A decoder failure is a normal
event, not an exceptional one, so failures are counted and iteration
continues. This mirrors webdataset's own `warn_and_continue` handler, which
its documentation recommends applying at several pipeline stages precisely
because both a bad shard header and a bad sample are survivable.
"""

from __future__ import annotations

import io
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

IMAGE_SUFFIX = ".jpg"


@dataclass(frozen=True)
class BenchResult:
    """Outcome of reading some shards.

    Counts are kept separate from rates so that partial results stay
    aggregatable: summing rates would be wrong, summing counts is not.
    """

    samples: int
    decode_failures: int
    bytes_read: int
    elapsed_sec: float

    @property
    def attempted(self) -> int:
        """Every sample the reader tried, successful or not."""
        return self.samples + self.decode_failures

    @property
    def samples_per_sec(self) -> float | None:
        """None when no time elapsed, rather than raising.

        A shard small enough to finish within timer resolution should not
        abort a whole benchmark run.
        """
        if self.elapsed_sec <= 0:
            return None
        return self.samples / self.elapsed_sec

    @property
    def bytes_per_sec(self) -> float | None:
        if self.elapsed_sec <= 0:
            return None
        return self.bytes_read / self.elapsed_sec

    @property
    def decode_failure_rate(self) -> float | None:
        """Fraction of attempted samples that failed to decode.

        The denominator is everything attempted. Dividing by successes would
        make the rate fall as failures rise.

        None when nothing was attempted: zero would claim a clean run where
        no run happened.
        """
        if self.attempted == 0:
            return None
        return self.decode_failures / self.attempted


def aggregate(results: list[BenchResult]) -> BenchResult:
    """Combine per-shard results into one.

    Counts and elapsed time add; rates are recomputed from those sums rather
    than averaged, because averaging rates would weight a fast tiny shard the
    same as a slow large one.
    """
    return BenchResult(
        samples=sum(r.samples for r in results),
        decode_failures=sum(r.decode_failures for r in results),
        bytes_read=sum(r.bytes_read for r in results),
        elapsed_sec=sum(r.elapsed_sec for r in results),
    )


def measure_shard(tar_path: Path) -> BenchResult:
    """Read every image in one shard, decoding each, and time it.

    Decoding is forced with `Image.load()`. Pillow is lazy: `Image.open`
    only parses the header, so without the explicit load this would time
    header parsing and report a throughput that training could never reach.
    """
    tar_path = Path(tar_path)
    if not tar_path.exists():
        raise FileNotFoundError(f"shard not found: {tar_path}")

    samples = 0
    failures = 0
    total_bytes = 0

    start = time.perf_counter()
    with tarfile.open(tar_path) as tf:
        for member in tf:
            if not member.isfile() or not member.name.endswith(IMAGE_SUFFIX):
                continue
            payload = tf.extractfile(member).read()
            total_bytes += len(payload)
            try:
                image = Image.open(io.BytesIO(payload))
                image.load()
            except Exception:
                # Counted, not raised: bad samples are expected in web data,
                # and stopping here would make the measurement impossible on
                # exactly the data it exists to measure.
                failures += 1
                continue
            samples += 1
    elapsed = time.perf_counter() - start

    return BenchResult(
        samples=samples,
        decode_failures=failures,
        bytes_read=total_bytes,
        elapsed_sec=elapsed,
    )
