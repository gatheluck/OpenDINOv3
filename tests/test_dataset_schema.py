"""Contract for resolving upstream column names, per dataset.

The pipeline was written against DataComp-1B and matched its column names
exactly. Three of the four corpora in scope do not use those names:

    DataComp-1B   url   text   uid    face_bboxes
    COYO-700M     url   text   id     (no boxes; num_faces is a count)
    ReLAION-5B    URL   TEXT   hash   (no boxes)

An exact match on DataComp's names would have carried no caption at all for
ReLAION and no identifier for either of the others — silently, because a
missing optional column is not an error anywhere in the pipeline.

The schemas here are taken from each project's own documentation and
downloader, not guessed. LAION's own dataset card warns that naming is not
uniform across its repositories, so the resolver reports what it found
instead of assuming.
"""

from __future__ import annotations

import pytest

from opendinov3.core import dataset_schema as ds

# Documented schemas. Sources are named in dataset_schema.py.
DATACOMP = ["uid", "url", "text", "original_width", "original_height",
            "sha256", "face_bboxes", "clip_b32_similarity_score"]
COYO = ["id", "url", "text", "width", "height", "image_phash", "text_length",
        "num_faces", "clip_similarity_vitb32", "nsfw_score_opennsfw2",
        "watermark_score", "aesthetic_score_laion_v2"]
RELAION = ["URL", "TEXT", "WIDTH", "HEIGHT", "similarity", "hash",
           "punsafe", "pwatermark", "LANGUAGE"]


def test_datacomp_resolves_to_its_own_names() -> None:
    r = ds.resolve(DATACOMP)
    assert (r.url, r.caption, r.identifier) == ("url", "text", "uid")
    assert (r.width, r.height) == ("original_width", "original_height")
    assert r.face_boxes == "face_bboxes"


def test_coyo_uses_id_not_uid() -> None:
    """The predecessor's own config records this and the pipeline ignored it."""
    r = ds.resolve(COYO)
    assert (r.url, r.caption, r.identifier) == ("url", "text", "id")
    assert (r.width, r.height) == ("width", "height")


def test_relaion_is_uppercase() -> None:
    """An exact match on DataComp's names carries no caption here."""
    r = ds.resolve(RELAION)
    assert (r.url, r.caption) == ("URL", "TEXT")
    assert (r.width, r.height) == ("WIDTH", "HEIGHT")
    assert r.identifier == "hash"


def test_only_datacomp_can_have_faces_blurred() -> None:
    """COYO records a face count, not boxes; ReLAION records neither.

    Asking to blur is then a request that cannot be honoured, and must fail
    rather than quietly produce unblurred images.
    """
    assert ds.resolve(DATACOMP).face_boxes == "face_bboxes"
    assert ds.resolve(COYO).face_boxes is None
    assert ds.resolve(RELAION).face_boxes is None


def test_a_face_count_is_never_mistaken_for_boxes() -> None:
    """`num_faces` says how many, not where. Blurring needs where."""
    assert ds.resolve(["url", "num_faces"]).face_boxes is None


def test_a_missing_url_column_is_refused() -> None:
    """Everything else is optional; without a URL there is nothing to fetch."""
    with pytest.raises(ds.SchemaError):
        ds.resolve(["uid", "text", "width"])


def test_absent_optional_roles_are_reported_as_absent() -> None:
    r = ds.resolve(["url"])
    assert r.caption is None and r.identifier is None
    assert r.width is None and r.height is None and r.face_boxes is None


def test_the_columns_to_carry_are_the_resolved_ones(  ) -> None:
    """Whatever they are named upstream, these are what the manifest keeps."""
    assert ds.resolve(RELAION).columns_to_carry() == [
        "URL", "TEXT", "hash", "WIDTH", "HEIGHT"]
    assert ds.resolve(DATACOMP).columns_to_carry() == [
        "url", "text", "uid", "original_width", "original_height",
        "face_bboxes"]


def test_scores_and_other_upstream_columns_are_left_behind() -> None:
    """They inflate every manifest and are recoverable upstream by id."""
    carried = ds.resolve(COYO).columns_to_carry()
    assert "clip_similarity_vitb32" not in carried
    assert "aesthetic_score_laion_v2" not in carried


def test_the_resolution_is_described_for_a_human() -> None:
    """A run should record which upstream names it bound to which role."""
    described = ds.resolve(RELAION).describe()
    assert "caption" in described and "TEXT" in described
    assert "face" in described.lower()


def test_case_differences_alone_do_not_hide_a_column() -> None:
    """LAION's own card warns naming is not uniform across its repos."""
    assert ds.resolve(["Url", "Text"]).caption == "Text"


def test_an_exact_match_wins_over_a_case_insensitive_one() -> None:
    """If a schema somehow carries both, prefer the documented spelling."""
    r = ds.resolve(["url", "URL", "text"])
    assert r.url == "url"
