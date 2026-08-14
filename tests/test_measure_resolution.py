"""Contract for measuring the corpus's resolution on a login node.

The arithmetic lives in core/resolution_stats.py and is tested there. What is
tested here is the seam: which files get opened, which columns get read, and
what happens when a corpus records no size at all.

Two of these are bugs this project has already shipped once. Sampling the
front of a sorted corpus produced a per-image size wrong by 3-5x. Hard-coding
DataComp's column spellings carried no caption for Re-LAION and no identifier
for COYO, silently, because a missing optional column raises nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_resolution.py"


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


def write(path: Path, widths, heights, *, wname="original_width",
          hname="original_height", **extra) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = {"url": [f"https://x/{i}.jpg" for i in range(len(widths))],
               wname: widths, hname: heights, **extra}
    pq.write_table(pa.table(columns), path)


def corpus(root: Path, per_file: list[tuple[int, int]], count: int) -> None:
    """`count` files, each holding one repeated size, so which files were
    read is visible in the answer."""
    for index in range(count):
        width, height = per_file[index % len(per_file)]
        write(root / f"part-{index:05d}.parquet", [width] * 4, [height] * 4)


def test_the_share_below_a_landmark_size_is_reported(tmp_path) -> None:
    write(tmp_path / "a.parquet", [100, 300, 640], [100, 300, 480])
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "256" in result.stdout
    assert "33.3%" in result.stdout


def test_the_spread_is_reported_not_just_one_models_threshold(tmp_path
                                                              ) -> None:
    """The corpus is for several consumers and for ones not yet named, so a
    single pass/fail against one model's input size is the wrong output."""
    write(tmp_path / "a.parquet", list(range(10, 1010, 10)),
          list(range(10, 1010, 10)))
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    for marker in ("p10", "p50", "p90"):
        assert marker in result.stdout, marker


def test_no_filtering_is_urged_for_a_merely_small_corpus(tmp_path) -> None:
    """A 200px photograph is small for some uses and fine for others.
    Dropping it at download time would decide for everyone, permanently."""
    write(tmp_path / "a.parquet", [200] * 10, [200] * 10)
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "nothing worth filtering out for everyone" in result.stdout


def test_degenerate_images_are_called_out_because_nobody_wants_them(tmp_path
                                                                    ) -> None:
    write(tmp_path / "a.parquet", [1] * 5 + [800] * 5, [1] * 5 + [600] * 5)
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "tracking" in result.stdout


def test_size_columns_are_resolved_not_hard_coded(tmp_path) -> None:
    """COYO spells them `width`/`height`; Re-LAION shouts them. Binding
    DataComp's spelling would report nothing measurable for either."""
    write(tmp_path / "a.parquet", [100], [100], wname="WIDTH", hname="HEIGHT")
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "WIDTH" in result.stdout and "HEIGHT" in result.stdout


def test_a_corpus_with_no_recorded_size_says_so_and_fails(tmp_path) -> None:
    """YFCC100M records no dimensions. Printing '0% below 256' would be a
    clean-looking answer to a question that was never measured."""
    pq.write_table(pa.table({"downloadurl": ["https://x/1.jpg"]}),
                   tmp_path / "a.parquet")
    result = run(tmp_path)
    assert result.returncode != 0
    assert "no recorded" in (result.stdout + result.stderr).lower()


def test_the_sample_spans_the_corpus_rather_than_its_first_files(tmp_path
                                                                ) -> None:
    """The failure this guards against is not hypothetical: reading the first
    twelve tasks of this corpus gave 70-124 KB per image against a true 25.1
    KB, because the front of the list was pilot data."""
    # Small only at the front, large everywhere after.
    for index in range(100):
        size = (64, 64) if index < 10 else (800, 600)
        write(tmp_path / f"part-{index:05d}.parquet", [size[0]] * 4,
              [size[1]] * 4)
    out = tmp_path / "r.json"
    result = run(tmp_path, "--files", "10", "--json", out)
    assert result.returncode == 0, result.stderr
    # Evenly spaced over 100 files takes 0, 11, 22 ... 99; exactly one of
    # those (index 0) is in the small front, so 1 file in 10.
    # A front-loaded sample would take 0..9 and report 1.0.
    assert json.loads(out.read_text())["fraction_below"]["256"] == 0.1


def test_the_number_of_files_read_is_stated(tmp_path) -> None:
    """A fraction from 10 of 2,664 files is a different claim from one over
    all of them, and the reader cannot tell them apart otherwise."""
    corpus(tmp_path, [(640, 480)], count=50)
    result = run(tmp_path, "--files", "5")
    assert "5" in result.stdout and "50" in result.stdout


def test_reading_every_file_is_possible(tmp_path) -> None:
    corpus(tmp_path, [(100, 100), (640, 480)], count=6)
    result = run(tmp_path, "--files", "0")
    assert result.returncode == 0, result.stderr
    assert "24" in result.stdout  # 6 files x 4 rows


def test_the_result_can_be_written_as_json(tmp_path) -> None:
    write(tmp_path / "a.parquet", [100, 640], [100, 480])
    out = tmp_path / "resolution.json"
    result = run(tmp_path, "--json", out)
    assert result.returncode == 0, result.stderr
    payload = json.loads(out.read_text())
    assert payload["rows_measured"] == 2
    assert payload["fraction_below"]["256"] == 0.5
    assert payload["files_read"] == 1 and payload["files_total"] == 1
    assert payload["percentile_short_side"]["50"] is not None


def test_an_empty_directory_is_an_error_not_an_empty_answer(tmp_path) -> None:
    result = run(tmp_path)
    assert result.returncode != 0


def test_size_columns_present_but_all_zero_is_reported_not_crashed_on(
        tmp_path) -> None:
    """The columns existing is not the same as them holding anything. This
    must say what it found, not raise on a median of nothing."""
    write(tmp_path / "a.parquet", [0, 0], [0, 0])
    result = run(tmp_path)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "unusable" in combined.lower()
    assert "Traceback" not in combined


def test_aspect_ratio_spread_is_reported_for_video_bucketing(tmp_path
                                                             ) -> None:
    write(tmp_path / "a.parquet", [100, 200], [100, 100])
    result = run(tmp_path)
    assert "aspect" in result.stdout.lower()
