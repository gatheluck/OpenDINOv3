"""Contract for slicing parquet URL lists.

The corpus stores its URL lists as `urls_clean.parquet`, one per task. An
earlier version of this code assumed a text list and refused parquet, which
was an assumption about the data rather than a fact about it.

img2dataset reads parquet natively via `--url_col`, so the slices stay in
parquet and every column is preserved. That keeps the experiment on the same
input path production uses instead of introducing a conversion that only the
experiment would exercise.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from opendinov3.core import parquet_urls as pu
from opendinov3.core.urllist import UrlListFormatError


def make_table(n: int, *, first: int = 0, url_name: str = "url") -> pa.Table:
    return pa.table({
        "key": [f"{first + i:09d}" for i in range(n)],
        url_name: [f"https://h{i % 7}.example/{first + i}.jpg" for i in range(n)],
        "width": [640] * n,
    })


def write(path, table: pa.Table) -> None:
    pq.write_table(table, path)


# --------------------------------------------------------------------------
# Choosing the URL column
# --------------------------------------------------------------------------

def test_the_url_column_is_found_by_name() -> None:
    assert pu.choose_url_column(["key", "url", "width"]) == "url"


def test_an_exact_url_column_wins_over_a_similar_one() -> None:
    """Both names appear in real schemas. Guessing between them is not needed
    when one of them is exactly the canonical name."""
    assert pu.choose_url_column(["image_url", "url", "key"]) == "url"


def test_a_schema_with_no_url_column_is_refused() -> None:
    """Positional fallback would work on a sample and fail on the corpus."""
    with pytest.raises(UrlListFormatError):
        pu.choose_url_column(["key", "width", "height"])


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def test_a_single_file_is_read_whole(tmp_path) -> None:
    write(tmp_path / "urls_clean.parquet", make_table(50))
    table, column = pu.read_urls(tmp_path / "urls_clean.parquet")
    assert table.num_rows == 50
    assert column == "url"


def test_a_directory_is_concatenated_in_sorted_order(tmp_path) -> None:
    """One task's list may be shorter than the experiment needs.

    Sorted order makes the composition of each slice reproducible; directory
    iteration order is not stable across filesystems.
    """
    for name, first in (("task-000002", 100), ("task-000001", 0)):
        d = tmp_path / name
        d.mkdir()
        write(d / "urls_clean.parquet", make_table(100, first=first))

    table, column = pu.read_urls(tmp_path)
    assert table.num_rows == 200
    assert column == "url"
    # task-000001 sorts first, so its keys lead.
    assert table.column("key")[0].as_py() == "000000000"
    assert table.column("key")[100].as_py() == "000000100"


def test_reading_stops_once_enough_rows_are_available(tmp_path) -> None:
    """A corpus directory can hold hundreds of files; reading all of them to
    use the first few would be wasted I/O on a shared filesystem."""
    for i in range(5):
        d = tmp_path / f"task-{i:06d}"
        d.mkdir()
        write(d / "urls_clean.parquet", make_table(100, first=i * 100))

    table, _ = pu.read_urls(tmp_path, at_least=150)
    assert table.num_rows == 200  # two files, not five
    assert table.num_rows < 500


def test_an_empty_directory_is_refused(tmp_path) -> None:
    with pytest.raises(UrlListFormatError):
        pu.read_urls(tmp_path)


# --------------------------------------------------------------------------
# Slicing
# --------------------------------------------------------------------------

def test_slices_are_disjoint_and_exact(tmp_path) -> None:
    table = make_table(100)
    slices = pu.slice_table(table, count=25, n_slices=4)

    assert [s.num_rows for s in slices] == [25, 25, 25, 25]
    seen = [k.as_py() for s in slices for k in s.column("key")]
    assert len(set(seen)) == 100, "slices overlap"


def test_slicing_preserves_every_column(tmp_path) -> None:
    """img2dataset may be told to read a caption column too. Dropping columns
    here would make the experiment's input differ from production's."""
    table = make_table(40)
    first = pu.slice_table(table, count=10, n_slices=2)[0]
    assert first.column_names == table.column_names


def test_too_few_rows_is_refused_with_the_number_that_would_fit() -> None:
    """A level with a short slice is not comparable, so this stops the run.

    The message carries the largest workable --count so the next attempt does
    not need another round trip to the cluster to find it.
    """
    table = make_table(90)
    with pytest.raises(UrlListFormatError) as excinfo:
        pu.slice_table(table, count=25, n_slices=4)
    assert "22" in str(excinfo.value), "should suggest 90 // 4 = 22"


def test_exactly_enough_rows_is_accepted() -> None:
    table = make_table(100)
    assert len(pu.slice_table(table, count=25, n_slices=4)) == 4


# --------------------------------------------------------------------------
# Describing, so a dry run can report before anything is submitted
# --------------------------------------------------------------------------

def test_describe_reports_rows_and_columns_without_loading_everything(
    tmp_path,
) -> None:
    write(tmp_path / "urls_clean.parquet", make_table(1234))
    info = pu.describe(tmp_path / "urls_clean.parquet")
    assert info.num_rows == 1234
    assert "url" in info.columns
    assert info.url_column == "url"
    assert info.files == 1


def test_describe_totals_a_directory(tmp_path) -> None:
    for i in range(3):
        d = tmp_path / f"task-{i:06d}"
        d.mkdir()
        write(d / "urls_clean.parquet", make_table(1000))
    info = pu.describe(tmp_path)
    assert info.num_rows == 3000
    assert info.files == 3
