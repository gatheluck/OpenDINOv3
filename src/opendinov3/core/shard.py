"""The shard contract: what a valid WebDataset shard looks like.

A shard is the unit the pipeline agrees on. Downloading writes it, validation
reads it, training streams it. This module is the single place that says what
its structure is, so those three cannot drift apart.

Pure by design: no I/O beyond reading the files it is asked to check, and no
knowledge of any scheduler, filesystem layout, or site. That keeps it testable
without a cluster, and portable to one that is not PBS.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

# img2dataset zero-pads shard indices to five digits. Verified against shards
# produced on the cluster: 00000.tar / 00000.parquet / 00000_stats.json.
SHARD_INDEX_WIDTH = 5

# Keys the pipeline reads out of _stats.json. Verified against a real file.
# status_dict is the failure breakdown; it is what lets a failure be classified
# as permanent (404, 403) or transient (timeout, 5xx) without re-downloading.
REQUIRED_STATS_KEYS = (
    "count",
    "successes",
    "failed_to_download",
    "failed_to_resize",
    "status_dict",
)

# Per-sample members inside the tar.
IMAGE_SUFFIX = ".jpg"
METADATA_SUFFIX = ".json"


class ContractError(ValueError):
    """A shard or its statistics violate the agreed structure."""


@dataclass(frozen=True)
class ShardFilenames:
    tar: str
    parquet: str
    stats: str


@dataclass
class ValidationReport:
    """Outcome of checking one shard.

    Collects every problem rather than raising on the first. A partially
    written shard usually fails several checks at once, and seeing all of
    them is what makes the cause obvious.
    """

    path: Path
    sample_count: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def shard_filenames(index: int) -> ShardFilenames:
    """The three filenames that make up shard `index`."""
    if index < 0:
        raise ValueError(f"shard index must be non-negative, got {index}")
    stem = f"{index:0{SHARD_INDEX_WIDTH}d}"
    return ShardFilenames(
        tar=f"{stem}.tar",
        parquet=f"{stem}.parquet",
        stats=f"{stem}_stats.json",
    )


def validate_stats(stats: dict) -> None:
    """Check a parsed _stats.json against the contract.

    Raises on the first violation: unlike a shard, statistics are small and a
    single inconsistency already makes the file unusable for accounting.
    """
    for key in REQUIRED_STATS_KEYS:
        if key not in stats:
            raise ContractError(f"stats is missing required key: {key}")

    count = stats["count"]
    successes = stats["successes"]
    failed_download = stats["failed_to_download"]
    failed_resize = stats["failed_to_resize"]

    total = successes + failed_download + failed_resize
    if total > count:
        raise ContractError(
            f"outcomes exceed the candidate count: "
            f"{successes} + {failed_download} + {failed_resize} = {total} > {count}"
        )

    status = stats["status_dict"]
    if not isinstance(status, dict):
        raise ContractError("status_dict must be a mapping")

    # status_dict is the breakdown of the same run that produced `successes`,
    # so its "success" entry has to agree. A mismatch means the file was
    # assembled by something other than the downloader, or is truncated.
    reported = status.get("success", 0)
    if reported != successes:
        raise ContractError(
            f"status_dict disagrees with successes: "
            f"status_dict['success']={reported} but successes={successes}"
        )


def validate_shard(directory: Path, index: int = 0) -> ValidationReport:
    """Check the tar of shard `index` in `directory`.

    Structural checks only. Decoding every image is deliberately out of scope
    here: it is orders of magnitude slower, and belongs in the throughput
    measurement rather than in a per-shard gate.
    """
    directory = Path(directory)
    names = shard_filenames(index)
    report = ValidationReport(path=directory / names.tar)

    if not report.path.exists():
        report.problems.append(f"missing tar: {names.tar}")
        return report

    images: dict[str, bytes] = {}
    metadata: dict[str, dict] = {}
    duplicates: set[str] = set()

    with tarfile.open(report.path) as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            name = member.name
            if name.endswith(IMAGE_SUFFIX):
                key = name[: -len(IMAGE_SUFFIX)]
                if key in images:
                    duplicates.add(key)
                images[key] = tf.extractfile(member).read()
            elif name.endswith(METADATA_SUFFIX):
                key = name[: -len(METADATA_SUFFIX)]
                if key in metadata:
                    duplicates.add(key)
                payload = tf.extractfile(member).read()
                try:
                    metadata[key] = json.loads(payload)
                except json.JSONDecodeError as exc:
                    report.problems.append(f"{name}: invalid json ({exc})")

    report.sample_count = len(images)

    if not images:
        # A killed job can leave a well-formed but empty tar. Accepting it
        # would let a truncated task be marked done.
        report.problems.append("shard is empty: no image members found")
        return report

    for key in sorted(duplicates):
        report.problems.append(f"duplicate sample key within shard: {key}")

    for key in sorted(images):
        if key not in metadata:
            report.problems.append(f"{key}: missing {METADATA_SUFFIX} sidecar")
            continue

        declared = metadata[key].get("sha256")
        if declared is None:
            continue  # hashing is optional at download time
        actual = hashlib.sha256(images[key]).hexdigest()
        if declared != actual:
            report.problems.append(
                f"{key}: sha256 mismatch (declared {declared[:12]}..., "
                f"actual {actual[:12]}...)"
            )

    for key in sorted(set(metadata) - set(images)):
        report.problems.append(f"{key}: metadata without a corresponding image")

    return report
