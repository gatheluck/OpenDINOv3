"""Contract for building one task's URL list from the upstream metadata.

Each array subjob builds its own manifest from the shared plan and then
downloads it. Nothing is coordinated between subjobs and nothing is written
in advance, so the extraction has to be deterministic and self-contained:
given the same plan entry, it must produce the same rows every time, on any
node, in any order the scheduler happens to run them.

A task boundary usually lands inside a source file, so a manifest is
assembled from row ranges over several files. Getting that arithmetic wrong
would duplicate or skip URLs silently — the corpus would simply be wrong,
with nothing in any log to say so.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from opendinov3.core import task_manifest as tm


def write_source(path, first: int, rows: int) -> None:
    """Shaped like DataComp-1B: url, text, uid, face_bboxes."""
    pq.write_table(pa.table({
        "url": [f"https://h.example/{first + i}.jpg" for i in range(rows)],
        "text": [f"caption {first + i}" for i in range(rows)],
        "uid": [f"{first + i:012d}" for i in range(rows)],
        "face_bboxes": [[] for _ in range(rows)],
        "clip_score": [0.3] * rows,
    }), path)


@pytest.fixture
def sources(tmp_path):
    write_source(tmp_path / "a.parquet", first=0, rows=100)
    write_source(tmp_path / "b.parquet", first=100, rows=100)
    return tmp_path


def test_a_manifest_from_one_file_takes_exactly_its_range(sources) -> None:
    table = tm.build_manifest([(str(sources / "a.parquet"), 10, 20)])
    assert table.num_rows == 10
    assert table.column("url")[0].as_py() == "https://h.example/10.jpg"
    assert table.column("url")[-1].as_py() == "https://h.example/19.jpg"


def test_a_manifest_spanning_two_files_is_contiguous(sources) -> None:
    """The common case: a task boundary lands inside a file."""
    table = tm.build_manifest([
        (str(sources / "a.parquet"), 90, 100),
        (str(sources / "b.parquet"), 0, 10),
    ])
    urls = table.column("url").to_pylist()
    assert len(urls) == 20
    assert urls[9] == "https://h.example/99.jpg"
    assert urls[10] == "https://h.example/100.jpg"


def test_the_url_column_is_found_by_name_not_position(sources, tmp_path) -> None:
    """Upstream schemas differ; position is not a contract."""
    odd = tmp_path / "odd.parquet"
    pq.write_table(pa.table({
        "uid": ["a", "b"], "width": [1, 2],
        "url": ["https://x/1.jpg", "https://x/2.jpg"],
    }), odd)
    table = tm.build_manifest([(str(odd), 0, 2)])
    assert table.column("url").to_pylist() == ["https://x/1.jpg",
                                               "https://x/2.jpg"]


def test_the_columns_datacomp_itself_carries_are_carried(sources) -> None:
    """Not just the URL.

    DataComp's own downloader passes caption_col="text",
    save_additional_columns=["uid"] and bbox_col="face_bboxes". Carrying only
    the URL would produce a corpus with no captions — usable for DINOv3,
    which is self-supervised, and useless for the text-to-image stage that
    video models train first. It would also discard the face boxes that make
    blurring possible at all.
    """
    table = tm.build_manifest([(str(sources / "a.parquet"), 0, 5)])
    assert table.column_names == ["url", "text", "uid", "face_bboxes"]


def test_columns_absent_upstream_are_simply_not_carried(tmp_path) -> None:
    """Older or derivative metadata may lack some of them. Missing optional
    columns are not an error; a missing URL is."""
    path = tmp_path / "minimal.parquet"
    pq.write_table(pa.table({"url": ["https://x/1.jpg"], "uid": ["a"]}), path)
    table = tm.build_manifest([(str(path), 0, 1)])
    assert table.column_names == ["url", "uid"]


def test_the_caption_column_keeps_the_name_img2dataset_is_told(sources) -> None:
    """production_task.sh passes --caption_col text; a rename here would
    silently drop every caption."""
    table = tm.build_manifest([(str(sources / "a.parquet"), 0, 2)])
    assert "text" in table.column_names
    assert table.column("text")[0].as_py() == "caption 0"


def test_a_source_without_a_url_column_is_refused(tmp_path) -> None:
    """Silently producing an empty manifest would look like a dead task."""
    bad = tmp_path / "bad.parquet"
    pq.write_table(pa.table({"uid": ["a"], "width": [1]}), bad)
    with pytest.raises(tm.ManifestError):
        tm.build_manifest([(str(bad), 0, 1)])


def test_a_range_beyond_the_file_is_refused(sources) -> None:
    """A plan built from stale metadata must fail loudly, not short."""
    with pytest.raises(tm.ManifestError):
        tm.build_manifest([(str(sources / "a.parquet"), 90, 150)])


def test_a_missing_source_is_refused(tmp_path) -> None:
    with pytest.raises(tm.ManifestError):
        tm.build_manifest([(str(tmp_path / "nope.parquet"), 0, 1)])


def test_an_empty_piece_list_is_refused(sources) -> None:
    with pytest.raises(tm.ManifestError):
        tm.build_manifest([])


def test_building_twice_gives_identical_rows(sources) -> None:
    """Determinism: a requeued subjob must download the same URLs."""
    pieces = [(str(sources / "a.parquet"), 90, 100),
              (str(sources / "b.parquet"), 0, 10)]
    assert tm.build_manifest(pieces).equals(tm.build_manifest(pieces))


def test_an_uppercase_corpus_is_normalised(tmp_path) -> None:
    """Re-LAION is uppercase throughout. Matching DataComp's spellings
    exactly would carry no caption at all from it."""
    path = tmp_path / "relaion.parquet"
    pq.write_table(pa.table({
        "URL": ["https://x/1.jpg"], "TEXT": ["a caption"],
        "hash": ["abc"], "WIDTH": [640], "HEIGHT": [480],
        "punsafe": [0.01],
    }), path)
    table = tm.build_manifest([(str(path), 0, 1)])
    assert table.column_names == ["url", "text", "uid", "width", "height"]
    assert table.column("text")[0].as_py() == "a caption"
    assert table.column("uid")[0].as_py() == "abc"
    assert "punsafe" not in table.column_names


def test_a_coyo_shaped_corpus_keeps_its_identifier(tmp_path) -> None:
    """COYO uses `id`; the predecessor's own config recorded that and the
    pipeline ignored it."""
    path = tmp_path / "coyo.parquet"
    pq.write_table(pa.table({
        "id": [123], "url": ["https://x/1.jpg"], "text": ["c"],
        "width": [640], "height": [480], "num_faces": [2],
    }), path)
    table = tm.build_manifest([(str(path), 0, 1)])
    assert table.column_names == ["url", "text", "uid", "width", "height"]
    assert table.column("uid")[0].as_py() == 123
    # num_faces is a count, not boxes: it cannot drive blurring and is not
    # carried as if it could.
    assert "face_bboxes" not in table.column_names
