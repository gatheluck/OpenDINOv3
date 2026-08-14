"""Build one task's URL list from row ranges over the upstream metadata.

Each array subjob builds its own manifest from the shared plan and then
downloads it. Nothing is coordinated between subjobs and nothing is written
in advance, so extraction has to be deterministic and self-contained: the
same plan entry must produce the same rows on any node, in any order the
scheduler runs them, including after a requeue.

A task boundary usually lands inside a source file, so a manifest is
assembled from ranges over several files. Every failure mode here is silent
if unchecked — a short read, a missing column, a stale plan — and produces a
corpus that is simply wrong with nothing in the logs to say so. So each is
refused rather than worked around.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from .urllist import URL_COLUMN_NAMES

#: Only what img2dataset needs. Carrying the upstream schema would inflate
#: every manifest and tie the corpus to whatever columns upstream happens to
#: ship.
OUTPUT_COLUMN = "url"

Piece = tuple[str, int, int]   # (path, start row, end row exclusive)


class ManifestError(ValueError):
    """The manifest cannot be built exactly as the plan describes it."""


def _url_column(names: Sequence[str], path: str) -> str:
    for candidate in URL_COLUMN_NAMES:
        if candidate in names:
            return candidate
    raise ManifestError(
        f"{path} has no URL column; columns are {list(names)}, "
        f"expected one of {list(URL_COLUMN_NAMES)}"
    )


def build_manifest(pieces: Sequence[Piece]) -> pa.Table:
    """The rows named by `pieces`, concatenated in the order given.

    Order is preserved because the plan's order is what makes the partition
    reproducible.
    """
    if not pieces:
        raise ManifestError("no pieces given; a task must name its source rows")

    chunks: list[pa.Table] = []
    for path, start, end in pieces:
        if not Path(path).is_file():
            raise ManifestError(f"source is missing: {path}")

        handle = pq.ParquetFile(path)
        available = handle.metadata.num_rows
        if start < 0 or end > available or start >= end:
            raise ManifestError(
                f"{path} holds {available} rows; the plan asks for "
                f"[{start}, {end}). The plan and the metadata disagree — "
                "one of them is stale."
            )

        column = _url_column(handle.schema_arrow.names, path)
        table = pq.read_table(path, columns=[column]).slice(start, end - start)
        if column != OUTPUT_COLUMN:
            table = table.rename_columns([OUTPUT_COLUMN])
        chunks.append(table)

    return chunks[0] if len(chunks) == 1 else pa.concat_tables(chunks)
