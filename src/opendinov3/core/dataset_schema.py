"""Resolve upstream column names to the roles this pipeline needs.

WHY

The pipeline was written against DataComp-1B and matched its column names
exactly. Three of the four corpora in scope do not use those names:

| Corpus | URL | caption | identifier | size | face boxes |
|---|---|---|---|---|---|
| DataComp-1B | `url` | `text` | `uid` | `original_width/height` | `face_bboxes` |
| COYO-700M | `url` | `text` | `id` | `width/height` | none |
| Re-LAION-5B | `URL` | `TEXT` | `hash` | `WIDTH/HEIGHT` | none |

Matching DataComp's spellings exactly would have carried no caption at all
for Re-LAION and no identifier for either of the others — silently, because
a missing optional column is not an error anywhere downstream.

SOURCES

- DataComp: `mlfoundations/datacomp` `download_upstream.py` passes
  `url_col="url"`, `caption_col="text"`, `save_additional_columns=["uid"]`,
  `bbox_col="face_bboxes"`.
- COYO-700M: the dataset card lists `id`, `url`, `text`, `width`, `height`,
  `num_faces` and score columns; its documented img2dataset invocation uses
  `--url_col "url" --caption_col "text"`.
- Re-LAION-5B: LAION's parquet carries `URL`, `TEXT`, `WIDTH`, `HEIGHT`,
  `similarity`, `hash`, `punsafe`, `pwatermark`, `LANGUAGE`, and the
  documented invocation uses `--url_col "URL" --caption_col "TEXT"`.

LAION's own dataset card warns that naming is not uniform across its
repositories, so this resolves against the schema in hand and reports what it
bound rather than assuming.

ONLY DATACOMP CAN BE BLURRED

COYO records `num_faces` — how many, not where — and Re-LAION records
neither. A request to blur faces on those corpora cannot be honoured, and
must fail rather than quietly produce unblurred images.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

#: Candidate spellings per role, in order of preference. Exact matches are
#: tried before case-insensitive ones so a documented spelling always wins.
ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    # `pageurl` is deliberately absent. YFCC100M carries both `downloadurl`
    # and `pageurl`; the latter is an HTML page. Choosing it would fetch a
    # hundred million web pages at full cost, and each would fail to decode
    # rather than announce itself as the wrong column.
    "url": ("url", "URL", "downloadurl", "image_url", "IMAGE_URL"),
    "caption": ("text", "TEXT", "caption", "CAPTION", "title", "txt",
                "alt_text"),
    # `photoid` before `uid`: YFCC100M carries both, and its `uid` is the
    # uploader's user id, not the sample's. Preferring `uid` there would give
    # an identifier shared by every photo one person posted — unique-looking
    # and not unique.
    "identifier": ("photoid", "uid", "id", "hash", "key", "sample_id"),
    "width": ("original_width", "width", "WIDTH"),
    "height": ("original_height", "height", "HEIGHT"),
    # Boxes only. `num_faces` is a count and cannot drive blurring.
    "face_boxes": ("face_bboxes", "face_boxes", "bboxes"),
}

#: What each corpus is documented to use, so that an observation which
#: disagrees can be reported instead of silently accepted.
#:
#: Published schemas go stale and are sometimes wrong. These are a
#: hypothesis; the schema read from the files is the fact. `inspect_metadata`
#: compares the two and says when they differ, which is worth knowing before
#: fetching tens of terabytes on the strength of a web page.
DOCUMENTED: dict[str, dict[str, str | None]] = {
    # download_upstream.py: url_col="url", caption_col="text",
    # save_additional_columns=["uid"], bbox_col="face_bboxes"
    "datacomp_1b": {
        "url": "url", "caption": "text", "identifier": "uid",
        "width": "original_width", "height": "original_height",
        "face_boxes": "face_bboxes",
    },
    # Same family; the filtered subset is drawn from this pool.
    # NOT independently verified — treated as DataComp-shaped until observed.
    "datacomp_commonpool_xlarge": {
        "url": "url", "caption": "text", "identifier": "uid",
        "width": "original_width", "height": "original_height",
        "face_boxes": "face_bboxes",
    },
    # Dataset card: id, url, text, width, height, num_faces, score columns.
    # num_faces is a count, so there are no boxes and no blurring.
    "coyo700m": {
        "url": "url", "caption": "text", "identifier": "id",
        "width": "width", "height": "height", "face_boxes": None,
    },
    # LAION parquet: URL, TEXT, WIDTH, HEIGHT, similarity, hash, punsafe,
    # pwatermark, LANGUAGE. The card warns naming varies across their repos.
    "relaion5b_research_safe": {
        "url": "URL", "caption": "TEXT", "identifier": "hash",
        "width": "WIDTH", "height": "HEIGHT", "face_boxes": None,
    },
    # photoid, downloadurl, pageurl, title, description, usertags, ...
    # No recorded size. `title` is the caption; `description` is longer prose.
    "yfcc100m": {
        "url": "downloadurl", "caption": "title", "identifier": "photoid",
        "width": None, "height": None, "face_boxes": None,
    },
}


class SchemaError(ValueError):
    """The schema cannot supply a role the pipeline requires."""


def _find(candidates: Sequence[str], columns: Sequence[str]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    lowered = {c.lower(): c for c in columns}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


@dataclass(frozen=True)
class ResolvedSchema:
    url: str
    caption: str | None
    identifier: str | None
    width: str | None
    height: str | None
    face_boxes: str | None

    def columns_to_carry(self) -> list[str]:
        """What the manifest keeps, in a stable order.

        Scores, NSFW probabilities and dedup signals are left behind: they
        inflate every manifest and are recoverable upstream by identifier.
        """
        wanted = [self.url, self.caption, self.identifier,
                  self.width, self.height, self.face_boxes]
        return [name for name in wanted if name]

    def describe(self) -> str:
        """What was bound to what, for the run's log.

        A run that silently dropped captions is indistinguishable from one
        whose corpus had none; recording the binding removes that ambiguity.
        """
        lines = [
            f"  url        -> {self.url}",
            f"  caption    -> {self.caption or 'NONE — no text-conditioned use'}",
            f"  identifier -> {self.identifier or 'NONE'}",
            f"  size       -> {self.width or 'NONE'} / {self.height or 'NONE'}",
            f"  face boxes -> {self.face_boxes or 'NONE — blurring impossible'}",
        ]
        return "\n".join(lines)


def resolve(columns: Sequence[str]) -> ResolvedSchema:
    """Bind roles to the column names actually present.

    Only the URL is required. Everything else is optional and its absence is
    a fact about the corpus, reported rather than raised — except that asking
    to blur without boxes is a request that cannot be met, which the caller
    checks.
    """
    url = _find(ROLE_ALIASES["url"], columns)
    if url is None:
        raise SchemaError(
            f"no URL column in {list(columns)}; tried "
            f"{list(ROLE_ALIASES['url'])}"
        )
    return ResolvedSchema(
        url=url,
        caption=_find(ROLE_ALIASES["caption"], columns),
        identifier=_find(ROLE_ALIASES["identifier"], columns),
        width=_find(ROLE_ALIASES["width"], columns),
        height=_find(ROLE_ALIASES["height"], columns),
        face_boxes=_find(ROLE_ALIASES["face_boxes"], columns),
    )


def compare_to_documented(corpus: str, resolved: ResolvedSchema) -> list[str]:
    """Where the observed schema differs from what the corpus documents.

    Returns the disagreements rather than raising: a difference is a finding
    to act on, not necessarily an error. Published schemas go stale, and a
    corpus may have been re-released.

    An unknown corpus raises, because silently approving a schema nobody has
    a documented expectation for is how an unverified assumption becomes a
    23 TB download.
    """
    if corpus not in DOCUMENTED:
        raise KeyError(
            f"no documented schema for {corpus!r}; known: "
            f"{sorted(DOCUMENTED)}"
        )
    expected = DOCUMENTED[corpus]
    problems: list[str] = []
    for role, want in expected.items():
        got = getattr(resolved, role)
        if got != want:
            problems.append(
                f"{role}: documented {want!r}, observed {got!r}"
            )
    return problems
