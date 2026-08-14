"""Contract for the runtime container image.

These assertions run INSIDE the image. They pin the properties the pipeline
depends on, so that a change to the base image or to dependency resolution
fails here rather than on the cluster.

The target cluster runs Python 3.12.11 and img2dataset 1.47.0. Matching the
minor version keeps behaviour comparable between local, CI and cluster runs;
matching img2dataset exactly matters because its CLI flags and output layout
define our shard format.
"""

from __future__ import annotations

import sys
from importlib import metadata

import pytest

EXPECTED_PYTHON = (3, 12)
EXPECTED_IMG2DATASET = "1.47.0"


def test_python_minor_version_matches_cluster() -> None:
    """Python minor version must match the cluster's interpreter.

    The cluster provides 3.12.11. A different minor version can change
    behaviour of the standard library and of compiled wheels.
    """
    assert sys.version_info[:2] == EXPECTED_PYTHON, (
        f"expected Python {EXPECTED_PYTHON[0]}.{EXPECTED_PYTHON[1]}, "
        f"got {sys.version_info[0]}.{sys.version_info[1]}"
    )


def test_img2dataset_is_pinned() -> None:
    """img2dataset must be present at the exact pinned version.

    Its CLI flags and output layout define the shard format we commit to,
    so an unintended upgrade is a breaking change.
    """
    assert metadata.version("img2dataset") == EXPECTED_IMG2DATASET


def test_img2dataset_is_importable() -> None:
    """Installed is not the same as importable.

    A broken native dependency (opencv, pyarrow) surfaces only on import.
    """
    import img2dataset  # noqa: F401


@pytest.mark.parametrize("module", ["pyarrow", "pandas", "PIL", "cv2", "fsspec"])
def test_shard_toolchain_importable(module: str) -> None:
    """Modules needed to read and validate shards must import.

    Shards are WebDataset tar files with a parquet sidecar, so reading them
    back for validation requires pyarrow and an image decoder.
    """
    __import__(module)


def test_skip_reencode_silently_discards_face_blurring() -> None:
    """Pinning an upstream behaviour we must not walk into.

    With `resize_mode=no` neither resize branch runs, so `encode_needed` is
    never set to True by the blur path. It is decided earlier, by
    `skip_reencode`. If that is on and the source is already JPEG, the
    resizer blurs the decoded array, then writes `img_buf` — the ORIGINAL
    bytes — and reports the blurred array's dimensions, so the output looks
    correct in every recorded field while being unblurred.

    No error, no warning. The documented img2dataset recipes for COYO-700M
    and LAION both pass `--skip_reencode=True`, so this is one plausible
    speed optimisation away from silently publishing unblurred faces.

    If this test starts failing, upstream has fixed it and the guard in
    production_task.sh can be revisited.
    """
    import io
    from PIL import Image
    from img2dataset.blurrer import BoundingBoxBlurrer
    from img2dataset.resizer import Resizer

    buffer = io.BytesIO()
    Image.new("RGB", (128, 128), (10, 200, 30)).save(buffer, format="JPEG")
    original = buffer.getvalue()
    bbox = [[0.1, 0.1, 0.6, 0.6]]

    def blurred_bytes(skip_reencode: bool) -> bytes:
        resizer = Resizer(image_size=256, resize_mode="no",
                          resize_only_if_bigger=False,
                          skip_reencode=skip_reencode,
                          blurrer=BoundingBoxBlurrer())
        out, _, _, _, _, err = resizer(io.BytesIO(original),
                                       blurring_bbox_list=bbox)
        assert err is None, err
        return out

    assert blurred_bytes(False) != original, (
        "blurring must change the bytes when re-encoding is on")
    assert blurred_bytes(True) == original, (
        "upstream behaviour changed: skip_reencode no longer discards the "
        "blur. Revisit the guard in production_task.sh.")
