"""Read and slice parquet URL lists.

The corpus keeps its URL lists as `urls_clean.parquet`, one per task.
img2dataset reads parquet directly given `--url_col`, so slices are written
back as parquet with every column intact. Converting to text would put the
experiment on an input path production never uses, which is a difference the
measurement would then carry silently.

Row counts per task are not assumed. A directory is read in sorted order and
only until enough rows exist, because the corpus holds hundreds of these
files on a shared filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .urllist import URL_COLUMN_NAMES, UrlListFormatError

PARQUET_GLOB = "*.parquet"


def choose_url_column(columns: list[str]) -> str:
    """The column holding the image URL.

    Refuses rather than falling back to a position: a positional guess works
    on whatever sample it was tried against and fails on the corpus.
    """
    for name in URL_COLUMN_NAMES:
        if name in columns:
            return name
    raise UrlListFormatError(
        f"no URL column in schema {columns!r}; "
        f"expected one of {list(URL_COLUMN_NAMES)}"
    )


def parquet_files(source: Path) -> list[Path]:
    """Every parquet under `source`, sorted, or the file itself."""
    if source.is_file():
        return [source]
    if source.is_dir():
        return sorted(source.rglob(PARQUET_GLOB))
    raise UrlListFormatError(f"no such file or directory: {source}")


def read_urls(source: Path, at_least: int | None = None) -> tuple[pa.Table, str]:
    """Concatenate parquet files until `at_least` rows are available.

    Returns the table and the name of its URL column.
    """
    files = parquet_files(source)
    if not files:
        raise UrlListFormatError(f"no {PARQUET_GLOB} found under {source}")

    tables: list[pa.Table] = []
    rows = 0
    for path in files:
        tables.append(pq.read_table(path))
        rows += tables[-1].num_rows
        if at_least is not None and rows >= at_least:
            break

    table = tables[0] if len(tables) == 1 else pa.concat_tables(tables)
    return table, choose_url_column(table.column_names)


def slice_table(
    table: pa.Table, count: int, n_slices: int, offset: int = 0
) -> list[pa.Table]:
    """Disjoint consecutive slices of `count` rows each, starting at `offset`.

    Refuses a short final slice. A level with fewer URLs than the others is
    not comparable to them, so a partial experiment would spend a reserved
    node to produce numbers that answer nothing.

    `offset` lets a later phase start where an earlier one stopped, so that
    phases of the same experiment never share URLs — otherwise one phase
    warms remote caches for the next and the difference between them
    includes that.
    """
    needed = offset + count * n_slices
    if table.num_rows < needed:
        workable = max(0, (table.num_rows - offset)) // n_slices
        raise UrlListFormatError(
            f"{table.num_rows} rows is not enough for {n_slices} slices of "
            f"{count} starting at row {offset} ({needed} needed). "
            f"Largest workable count is {workable}."
        )
    return [table.slice(offset + i * count, count) for i in range(n_slices)]


@dataclass(frozen=True)
class SourceInfo:
    """What a dry run can report before anything is submitted."""

    files: int
    num_rows: int
    columns: list[str]
    url_column: str | None
    error: str | None = None


def describe(source: Path) -> SourceInfo:
    """Row count and schema, read from parquet metadata only.

    Metadata carries the row count, so this does not read the data itself —
    it stays cheap on a shared filesystem even for a large corpus directory.
    """
    files = parquet_files(source)
    if not files:
        raise UrlListFormatError(f"no {PARQUET_GLOB} found under {source}")

    total = 0
    columns: list[str] = []
    for path in files:
        meta = pq.ParquetFile(path)
        total += meta.metadata.num_rows
        if not columns:
            columns = list(meta.schema_arrow.names)

    try:
        url_column = choose_url_column(columns)
        error = None
    except UrlListFormatError as exc:
        url_column = None
        error = str(exc)

    return SourceInfo(
        files=len(files),
        num_rows=total,
        columns=columns,
        url_column=url_column,
        error=error,
    )
