"""Contract for checking the metadata's claims against what was downloaded.

measure_resolution reports what upstream SAYS the images are. Two things
could make that wrong for planning purposes, and both are checkable against
the 82.3 million images already on disk, without fetching anything:

  1. The claim itself. A CDN can serve a different size than was crawled.
  2. Independence. "53.7% of candidates are >=256px, so ~484M of the 902M
     downloads will be" only holds if success is uncorrelated with size.
     If small images rot faster, every derived count is wrong.

And one more that matters for treating the whole tree as one dataset: was
the existing corpus stored at original resolution, or resized? Our own
shards use --resize_mode no. A predecessor who used `border` would have
padded every image to a square, and the two halves would not be one corpus.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

SCRIPT = (Path(__file__).resolve().parent.parent / "scripts"
          / "verify_recorded_sizes.py")


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


def shard(path: Path, rows: list[dict]) -> None:
    """A shard parquet shaped like img2dataset's output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = {
        "status": [r.get("status", "success") for r in rows],
        "width": [r.get("width") for r in rows],
        "height": [r.get("height") for r in rows],
        "original_width": [r.get("ow") for r in rows],
        "original_height": [r.get("oh") for r in rows],
    }
    pq.write_table(pa.table(columns), path)


def ok(ow: int, oh: int, **kw) -> dict:
    return {"ow": ow, "oh": oh, "width": kw.get("w", ow),
            "height": kw.get("h", oh), "status": "success"}


def test_only_successful_samples_are_measured(tmp_path) -> None:
    """Asserted against the JSON, not the printed text.

    pytest's tmp_path embeds the test's own name and the tool prints the
    directory it read, so a substring check on stdout can be satisfied by
    the path rather than by the result. Three of these tests passed that
    way before a mutation exposed it.

    The third row is the one that matters: `failed_to_resize` means the
    image decoded, so it HAS a recorded size. Only the status distinguishes
    it, so a null-size check alone would count it as a success.
    """
    shard(tmp_path / "00000.parquet", [
        ok(640, 480), ok(800, 600),
        {"status": "failed_to_download", "ow": None, "oh": None,
         "width": None, "height": None},
        {"status": "failed_to_resize", "ow": 1024, "oh": 768,
         "width": None, "height": None},
    ])
    out = tmp_path / "v.json"
    assert run(tmp_path, "--json", out).returncode == 0
    payload = json.loads(out.read_text())
    assert payload["succeeded"] == 2
    assert payload["failed"] == 2


def test_a_shard_without_a_status_column_is_reported_not_assumed(tmp_path
                                                                 ) -> None:
    """Silently treating every row as a success would inflate the yield and
    quietly answer the question this script exists to ask."""
    pq.write_table(pa.table({"original_width": [640],
                             "original_height": [480]}),
                   tmp_path / "00000.parquet")
    out = tmp_path / "v.json"
    result = run(tmp_path, "--json", out)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert json.loads(out.read_text())["shards_without_status"] == 1


def test_resizing_in_the_existing_corpus_is_detected(tmp_path) -> None:
    """If width != original_width the images were resized on the way in, and
    they are not the same kind of data as ours."""
    shard(tmp_path / "00000.parquet", [
        ok(640, 480, w=256, h=256), ok(800, 600, w=256, h=256),
    ])
    result = run(tmp_path)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "resiz" in combined.lower()


def test_an_untouched_corpus_is_confirmed_as_original_resolution(tmp_path
                                                                 ) -> None:
    shard(tmp_path / "00000.parquet", [ok(640, 480), ok(800, 600)])
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "original resolution" in result.stdout


def test_the_claim_is_compared_against_what_arrived(tmp_path) -> None:
    """The whole point: is upstream's recorded size the size we got?"""
    shard(tmp_path / "00000.parquet", [ok(n, n) for n in range(100, 1100, 100)])
    baseline = tmp_path / "resolution.json"
    baseline.write_text(json.dumps({
        "percentile_short_side": {"50": 550.0},
        "rows_measured": 1000,
    }))
    result = run(tmp_path, "--baseline", baseline)
    assert result.returncode == 0, result.stderr
    assert "claimed      550" in result.stdout, result.stdout


def test_a_divergence_from_the_claim_is_called_out(tmp_path) -> None:
    """If what arrives is materially different from what was promised, every
    count derived from the metadata is wrong and must not be quoted."""
    shard(tmp_path / "00000.parquet", [ok(100, 100)] * 10)
    baseline = tmp_path / "resolution.json"
    baseline.write_text(json.dumps({"percentile_short_side": {"50": 800.0},
                                    "rows_measured": 10}))
    # The exit code, not the wording: "diverge" is a substring of this
    # test's own name, which reaches stdout through tmp_path.
    assert run(tmp_path, "--baseline", baseline).returncode == 1


def test_shards_are_sampled_across_the_tree_not_from_its_front(tmp_path
                                                               ) -> None:
    """The same bias as before: the front of this tree is pilot data."""
    for index in range(100):
        size = 64 if index < 10 else 800
        shard(tmp_path / f"task-{index:04d}" / "00000.parquet",
              [ok(size, size)] * 4)
    out = tmp_path / "v.json"
    result = run(tmp_path, "--files", "10", "--json", out)
    assert result.returncode == 0, result.stderr
    # Evenly spaced over 100 takes 0, 11, 22 ... 99: one small file in ten,
    # so the median is 800. Reading the front would take 0..9 and give 64.
    assert json.loads(out.read_text())["percentile_short_side"]["50"] == 800


def test_no_shards_is_an_error_not_an_empty_answer(tmp_path) -> None:
    assert run(tmp_path).returncode != 0
