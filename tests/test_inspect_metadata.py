"""Contract for reporting what an upstream metadata schema actually contains.

This exists because the question it answers decides whether 23 TB of download
is usable. DINOv3 is self-supervised and needs no captions; the text-to-image
stage of a video model cannot be trained without them. The current pipeline
carries only the URL column, so if captions are present upstream they are
being discarded.

It also exists because handing over an untested one-line command is not
test-driven development. Every other step in this project ships as a tested
script; investigation should not be the exception. Long pasted commands also
break on a line boundary, which is how the previous attempt failed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "inspect_metadata.py"


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


def write(path: Path, **columns) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(columns), path)


def test_every_column_is_listed(tmp_path) -> None:
    write(tmp_path / "a.parquet", url=["https://x/1.jpg"], uid=["a"],
          text=["a caption"], width=[640], height=[480])
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    for name in ("url", "uid", "text", "width", "height"):
        assert name in result.stdout


def test_a_caption_column_is_identified_when_present(tmp_path) -> None:
    write(tmp_path / "a.parquet", url=["https://x/1.jpg"], text=["a caption"])
    result = run(tmp_path)
    assert "caption column : text" in result.stdout


def test_the_absence_of_captions_is_stated_plainly(tmp_path) -> None:
    """Silence here would read as 'fine'. It is the finding that decides
    whether a text-to-image stage is possible at all."""
    write(tmp_path / "a.parquet", url=["https://x/1.jpg"], uid=["a"])
    result = run(tmp_path)
    assert "caption column : NONE" in result.stdout
    assert "text-to-image" in result.stdout


def test_alternative_caption_names_are_recognised(tmp_path) -> None:
    write(tmp_path / "a.parquet", url=["https://x/1.jpg"], caption=["c"])
    assert "caption column : caption" in run(tmp_path).stdout


def test_resolution_columns_are_identified(tmp_path) -> None:
    """Whether the corpus can feed DINOv3's 256px global crops is answerable
    before downloading if upstream records the size."""
    write(tmp_path / "a.parquet", url=["https://x/1.jpg"],
          width=[640], height=[480])
    out = run(tmp_path).stdout
    assert "width column   : width" in out
    assert "height column  : height" in out


def test_missing_resolution_columns_are_stated(tmp_path) -> None:
    write(tmp_path / "a.parquet", url=["https://x/1.jpg"])
    out = run(tmp_path).stdout
    assert "width column   : NONE" in out


def test_sample_values_are_shown_so_the_schema_can_be_believed(tmp_path) -> None:
    """A column named `text` that holds nulls is not a caption column."""
    write(tmp_path / "a.parquet", url=["https://x/1.jpg"], text=["hello world"])
    out = run(tmp_path).stdout
    assert "https://x/1.jpg" in out
    assert "hello world" in out


def test_the_row_count_across_all_files_is_reported(tmp_path) -> None:
    write(tmp_path / "a.parquet", url=["u"] * 10)
    write(tmp_path / "sub" / "b.parquet", url=["u"] * 5)
    out = run(tmp_path).stdout
    assert "files          : 2" in out
    assert "15" in out


def test_a_schema_mismatch_between_files_is_reported(tmp_path) -> None:
    """A plan built on the first file's schema would break on the others."""
    write(tmp_path / "a.parquet", url=["u"], text=["c"])
    write(tmp_path / "b.parquet", url=["u"])
    result = run(tmp_path)
    assert "differ" in result.stdout.lower() + result.stderr.lower()


def test_a_directory_without_parquet_is_refused(tmp_path) -> None:
    result = run(tmp_path)
    assert result.returncode != 0
    assert "no parquet" in result.stderr.lower()


def test_a_missing_directory_is_refused(tmp_path) -> None:
    result = run(tmp_path / "nope")
    assert result.returncode != 0
