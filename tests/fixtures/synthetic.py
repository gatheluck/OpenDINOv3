"""Generate shards with the same structure as production ones.

Real shards hold third-party images, so they cannot live in a public
repository. These fixtures reproduce the structure instead, which keeps CI
hermetic and sidesteps redistribution entirely.

The images are real JPEGs, not arbitrary bytes. A fixture that only looked
right structurally would pass every check here while hiding decode problems —
and decode behaviour is precisely what the training-side validation needs to
exercise.

Each generator takes explicit flags for producing malformed output. Tests that
assert a defect is caught need a way to create that defect, and doing it here
keeps the corruption in one reviewed place rather than scattered across tests.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time
from pathlib import Path

from PIL import Image

from opendinov3.core import shard


def make_jpeg(seed: int, size: tuple[int, int] = (32, 24)) -> bytes:
    """A small, genuinely decodable JPEG.

    Content varies with `seed` so that samples have distinct hashes; a fixture
    where every image is identical would not exercise deduplication logic.
    """
    colour = ((seed * 53) % 256, (seed * 97) % 256, (seed * 151) % 256)
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def make_stats(count: int, successes: int) -> dict:
    """A _stats.json body matching the shape img2dataset writes.

    The status_dict entries mirror the failure categories actually observed
    on the cluster, so tests exercise realistic keys rather than invented ones.
    """
    failed = count - successes
    status: dict[str, int] = {"success": successes}
    if failed:
        # Split across a permanent and a transient cause; classification of
        # these two groups is what retry logic depends on.
        permanent = failed // 2
        transient = failed - permanent
        if permanent:
            status["HTTP Error 404: Not Found"] = permanent
        if transient:
            status["<urlopen error timed out>"] = transient
    return {
        "count": count,
        "successes": successes,
        "failed_to_download": failed,
        "failed_to_resize": 0,
        "duration": 1.0,
        "start_time": time.time(),
        "end_time": time.time() + 1.0,
        "status_dict": status,
    }


def sample_key(shard_index: int, sample_index: int) -> str:
    """Sample keys are shard-local; reuse across shards is expected."""
    return f"{shard_index:05d}{sample_index:04d}"


def write_shard(
    directory: Path,
    shard_index: int = 0,
    n_samples: int = 5,
    *,
    drop_json_for: set[int] | None = None,
    corrupt_hash_for: set[int] | None = None,
    duplicate_key: bool = False,
) -> Path:
    """Write one shard triple into `directory` and return that directory.

    The keyword flags exist so tests can construct specific defects:
      drop_json_for     omit the metadata sidecar for those sample indices
      corrupt_hash_for  record a sha256 that does not match the bytes
      duplicate_key     emit two samples under the same key
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    drop_json_for = drop_json_for or set()
    corrupt_hash_for = corrupt_hash_for or set()

    names = shard.shard_filenames(shard_index)

    with tarfile.open(directory / names.tar, "w") as tf:

        def add(name: str, payload: bytes) -> None:
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))

        for i in range(n_samples):
            key = sample_key(shard_index, i)
            image = make_jpeg(i)
            digest = hashlib.sha256(image).hexdigest()
            if i in corrupt_hash_for:
                digest = "0" * 64

            add(f"{key}{shard.IMAGE_SUFFIX}", image)

            if i not in drop_json_for:
                meta = {
                    "key": key,
                    "url": f"https://example.invalid/{key}.jpg",
                    "caption": f"synthetic sample {i}",
                    "status": "success",
                    "width": 32,
                    "height": 24,
                    "original_width": 32,
                    "original_height": 24,
                    "sha256": digest,
                }
                add(f"{key}{shard.METADATA_SUFFIX}",
                    json.dumps(meta).encode("utf-8"))

        if duplicate_key and n_samples:
            # Same key emitted twice. tar permits it; the contract does not.
            key = sample_key(shard_index, 0)
            add(f"{key}{shard.IMAGE_SUFFIX}", make_jpeg(999))

    stats = make_stats(count=max(n_samples, 1), successes=n_samples)
    (directory / names.stats).write_text(json.dumps(stats, indent=2))

    return directory
