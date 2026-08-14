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

#: What DataComp's own downloader carries, and why each is needed.
#:
#:   url          the image, obviously
#:   text         the caption. DataComp passes caption_col="text". DINOv3 is
#:                self-supervised and needs none, but the text-to-image stage
#:                that video models train first cannot be done without it, so
#:                dropping it would decide that question by accident.
#:   uid          DataComp's identifier, kept via save_additional_columns. It
#:                is how a sample is traced back to upstream.
#:   face_bboxes  DataComp blurs faces by default using this. Blurring is
#:                irreversible and is a decision for later — but only if the
#:                boxes were kept.
#:
#: Anything else upstream ships (CLIP scores, NSFW scores, dedup scores) is
#: left behind: it would inflate every manifest and is recoverable from
#: upstream by uid.
URL_COLUMN = "url"
CARRIED_COLUMNS = ("url", "text", "uid", "face_bboxes")

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

        names = list(handle.schema_arrow.names)
        url = _url_column(names, path)

        # Optional columns are carried when upstream has them and skipped
        # when it does not: derivative metadata sets differ, and a missing
        # caption is a fact about the source rather than an error here.
        wanted = [url] + [c for c in CARRIED_COLUMNS
                          if c != URL_COLUMN and c in names]
        table = pq.read_table(path, columns=wanted).slice(start, end - start)
        if url != URL_COLUMN:
            table = table.rename_columns([URL_COLUMN] + wanted[1:])
        chunks.append(table)

    return chunks[0] if len(chunks) == 1 else pa.concat_tables(chunks)
