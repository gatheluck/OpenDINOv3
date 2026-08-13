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
