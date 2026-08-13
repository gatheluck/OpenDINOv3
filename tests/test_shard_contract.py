"""Contract for a WebDataset shard produced by img2dataset.

WHY THIS EXISTS

A shard is the unit the whole pipeline agrees on. Downloading writes it,
validation reads it, and training streams it. If those three disagree about
its structure, the disagreement shows up only after a long download.

So the structure is written down here as an executable contract, and both
sides are checked against it: synthetic data in CI, and one real shard from
the cluster once (Phase 0b). If reality disagrees, the contract is wrong and
gets corrected — reality wins.

WHY SYNTHETIC FIXTURES

Real shards contain third-party images. This repository is public, so
committing samples would be redistribution. Generating data with the same
structure keeps CI hermetic and avoids the question entirely.

WHAT IS AND IS NOT VERIFIED YET

Verified against a real shard on the cluster:
  * the triple naming NNNNN.tar / NNNNN.parquet / NNNNN_stats.json
  * the keys present in _stats.json

Assumed, pending Phase 0b:
  * per-sample member suffixes inside the tar
  * the exact parquet column set
Anything in the second group is marked in the code it describes.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

from opendinov3.core import shard
from tests.fixtures import synthetic


# --------------------------------------------------------------------------
# Shard naming
# --------------------------------------------------------------------------

def test_shard_triple_names_derive_from_index() -> None:
    """One shard is three files sharing a zero-padded stem.

    Verified against the cluster: 00000.tar, 00000.parquet, 00000_stats.json.
    """
    names = shard.shard_filenames(0)
    assert names.tar == "00000.tar"
    assert names.parquet == "00000.parquet"
    assert names.stats == "00000_stats.json"


def test_shard_index_is_zero_padded_to_five_digits() -> None:
    assert shard.shard_filenames(42).tar == "00042.tar"
    assert shard.shard_filenames(12345).tar == "12345.tar"


def test_shard_index_must_be_non_negative() -> None:
    with pytest.raises(ValueError):
        shard.shard_filenames(-1)


# --------------------------------------------------------------------------
# stats.json
# --------------------------------------------------------------------------

def test_stats_requires_the_keys_the_pipeline_reads() -> None:
    """These keys drive accounting and failure analysis.

    status_dict in particular is what lets a failure be classified as
    permanent (404, 403) or transient (timeout, 5xx) without re-downloading.
    """
    stats = synthetic.make_stats(count=10, successes=7)
    shard.validate_stats(stats)  # must not raise


@pytest.mark.parametrize(
    "missing",
    ["count", "successes", "failed_to_download", "status_dict"],
)
def test_stats_missing_a_required_key_is_rejected(missing: str) -> None:
    stats = synthetic.make_stats(count=10, successes=7)
    del stats[missing]
    with pytest.raises(shard.ContractError, match=missing):
        shard.validate_stats(stats)


def test_stats_arithmetic_must_be_consistent() -> None:
    """successes + failures must not exceed the candidate count.

    A shard whose own statistics do not add up cannot be trusted for
    accounting, and accounting is what decides whether a task is done.
    """
    stats = synthetic.make_stats(count=10, successes=7)
    stats["successes"] = 8
    stats["failed_to_download"] = 5  # 8 + 5 + 0 > 10
    with pytest.raises(shard.ContractError, match="exceed"):
        shard.validate_stats(stats)


def test_status_dict_successes_must_match_the_successes_field() -> None:
    """status_dict is the breakdown; successes is its 'success' entry.

    They are written by the same tool, so a mismatch means the file is
    corrupt or was assembled by something else.
    """
    stats = synthetic.make_stats(count=10, successes=7)
    stats["status_dict"]["success"] = 6
    with pytest.raises(shard.ContractError, match="status_dict"):
        shard.validate_stats(stats)


# --------------------------------------------------------------------------
# Tar members
# --------------------------------------------------------------------------

def test_synthetic_shard_satisfies_the_contract(tmp_path) -> None:
    """The generator must produce something the validator accepts.

    If this fails, either the generator or the contract is wrong, and the
    rest of the suite proves nothing.
    """
    path = synthetic.write_shard(tmp_path, shard_index=0, n_samples=5)
    report = shard.validate_shard(path)
    assert report.sample_count == 5
    assert report.ok, report.problems


def test_every_sample_has_image_and_metadata(tmp_path) -> None:
    """A sample missing its .json cannot be attributed to a source URL.

    Provenance is the point of keeping the sidecar, so a sample without one
    is unusable even though the image decodes.
    """
    path = synthetic.write_shard(
        tmp_path, shard_index=0, n_samples=3, drop_json_for={1}
    )
    report = shard.validate_shard(path)
    assert not report.ok
    assert any("json" in p for p in report.problems)


def test_sample_keys_are_unique_within_a_shard(tmp_path) -> None:
    """Keys are shard-local, so duplicates inside one shard are a defect.

    Reuse across shards is expected and is not checked here.
    """
    path = synthetic.write_shard(
        tmp_path, shard_index=0, n_samples=3, duplicate_key=True
    )
    report = shard.validate_shard(path)
    assert not report.ok
    assert any("duplicate" in p for p in report.problems)


def test_declared_sha256_must_match_the_stored_bytes(tmp_path) -> None:
    """The hash is the basis for dedup and for integrity checks.

    A hash that does not match its own payload makes both meaningless.
    """
    path = synthetic.write_shard(
        tmp_path, shard_index=0, n_samples=3, corrupt_hash_for={2}
    )
    report = shard.validate_shard(path)
    assert not report.ok
    assert any("sha256" in p for p in report.problems)


def test_empty_shard_is_reported_rather_than_silently_accepted(tmp_path) -> None:
    """An empty shard is a plausible outcome of a killed job.

    Treating it as valid would let a truncated task be marked done.
    """
    path = synthetic.write_shard(tmp_path, shard_index=0, n_samples=0)
    report = shard.validate_shard(path)
    assert not report.ok
    assert any("empty" in p for p in report.problems)


# --------------------------------------------------------------------------
# The generator itself
# --------------------------------------------------------------------------

def test_generated_images_are_real_decodable_jpegs(tmp_path) -> None:
    """Fixtures must exercise the same decode path as production data.

    A fixture holding arbitrary bytes would pass structural checks while
    hiding decode problems, which is exactly what Phase 1 needs to detect.
    """
    from PIL import Image

    path = synthetic.write_shard(tmp_path, shard_index=0, n_samples=2)
    with tarfile.open(path / "00000.tar") as tf:
        jpegs = [m for m in tf.getmembers() if m.name.endswith(".jpg")]
        assert jpegs
        for member in jpegs:
            payload = tf.extractfile(member).read()
            image = Image.open(io.BytesIO(payload))
            image.load()
            assert image.format == "JPEG"


def test_generated_hash_matches_generated_bytes(tmp_path) -> None:
    path = synthetic.write_shard(tmp_path, shard_index=0, n_samples=2)
    with tarfile.open(path / "00000.tar") as tf:
        for member in tf.getmembers():
            if not member.name.endswith(".json"):
                continue
            meta = json.loads(tf.extractfile(member).read())
            key = member.name.rsplit(".", 1)[0]
            image_member = tf.getmember(f"{key}.jpg")
            payload = tf.extractfile(image_member).read()
            assert hashlib.sha256(payload).hexdigest() == meta["sha256"]
