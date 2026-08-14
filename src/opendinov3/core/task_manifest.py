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

from . import dataset_schema

#: The names the manifest writes, whatever upstream called them.
#:
#: Upstream spellings differ — DataComp uses url/text/uid/face_bboxes, COYO
#: uses id instead of uid, Re-LAION is uppercase throughout — so the roles are
#: resolved per corpus by dataset_schema and the columns renamed here. Every
#: downstream step then sees one schema, and img2dataset can be given the same
#: --url_col / --caption_col regardless of source.
#:
#: Scores, NSFW probabilities and dedup signals are left behind: they inflate
#: every manifest and are recoverable upstream by identifier.
URL_COLUMN = "url"
CANONICAL = {
    "url": "url",
    "caption": "text",
    "identifier": "uid",
    "width": "width",
    "height": "height",
    "face_boxes": "face_bboxes",
}

Piece = tuple[str, int, int]   # (path, start row, end row exclusive)


class ManifestError(ValueError):
    """The manifest cannot be built exactly as the plan describes it."""


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
        try:
            schema = dataset_schema.resolve(names)
        except dataset_schema.SchemaError as exc:
            raise ManifestError(f"{path}: {exc}") from exc

        # Optional roles are carried when upstream has them and skipped when
        # it does not: a corpus without captions or face boxes is a fact
        # about that corpus, not an error here.
        wanted = schema.columns_to_carry()
        renamed = [CANONICAL[role] for role, name in (
            ("url", schema.url), ("caption", schema.caption),
            ("identifier", schema.identifier), ("width", schema.width),
            ("height", schema.height), ("face_boxes", schema.face_boxes),
        ) if name]

        table = pq.read_table(path, columns=wanted).slice(start, end - start)
        if wanted != renamed:
            table = table.rename_columns(renamed)
        chunks.append(table)

    return chunks[0] if len(chunks) == 1 else pa.concat_tables(chunks)
