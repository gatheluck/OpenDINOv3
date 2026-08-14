"""Contract for the one command that decides whether the wave continues.

The pilot is 8 of 1,388 tasks. What it has to settle, before the other
1,380 are spent:

  - yield, which is what turns 1.39 billion rows into an image count and a
    node-hour budget. The plan assumes 65%.
  - bytes per image, which is what turns that count into 23.2 TB.
  - whether the shards can actually be READ by the library a trainer would
    use. That is the whole point of the corpus and nothing so far has
    tested it on real output.
  - whether captions line up with the images they claim to describe.
  - how much of the storage is EXIF, which is still an open decision and
    the only one that is cheap to reverse.

Everything here is decision-relevant. A number that changes nothing is not
in this report; see CLAUDE.md section 0.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from PIL import Image

SCRIPT = (Path(__file__).resolve().parent.parent / "scripts"
          / "inspect_pilot.py")


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


def jpeg(width: int = 320, height: int = 240) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (12, 200, 40)).save(buffer, format="JPEG")
    return buffer.getvalue()


def make_task(root: Path, task_id: int = 0, *, samples: int = 4,
              candidates: int = 8, broken: int = 0, caption_shift: int = 0,
              exif: str | None = None, size=(320, 240)) -> Path:
    """One finished task, shaped like img2dataset's webdataset output."""
    task = root / f"task-{task_id:06d}"
    shards = task / "shards"
    shards.mkdir(parents=True, exist_ok=True)

    with tarfile.open(shards / "00000.tar", "w") as archive:
        for index in range(samples):
            key = f"{index:09d}"
            payload = b"not a jpeg" if index < broken else jpeg(*size)
            for suffix, data in ((".jpg", payload),
                                 (".txt", f"caption {index + caption_shift}"
                                          .encode()),
                                 (".json", json.dumps({"key": key}).encode())):
                info = tarfile.TarInfo(key + suffix)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))

    pq.write_table(pa.table({
        "key": [f"{i:09d}" for i in range(samples)],
        "status": ["success"] * samples,
        "caption": [f"caption {i}" for i in range(samples)],
        "width": [size[0]] * samples,
        "height": [size[1]] * samples,
        "original_width": [size[0]] * samples,
        "original_height": [size[1]] * samples,
        "exif": [exif] * samples,
        "sha256": [f"{i:064x}" for i in range(samples)],
    }), shards / "00000.parquet")

    (shards / "00000_stats.json").write_text(json.dumps({
        "count": candidates, "successes": samples,
        "status_dict": {"success": samples}}))
    (task / "DONE.json").write_text(json.dumps({
        "task_id": task_id, "candidates": candidates, "successes": samples}))
    return task


def test_the_yield_is_reported_because_the_whole_budget_rests_on_it(tmp_path
                                                                    ) -> None:
    make_task(tmp_path, 0, samples=4, candidates=8)
    out = tmp_path / "r.json"
    result = run(tmp_path, "--json", out)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(out.read_text())["yield"] == pytest.approx(0.5)


def test_bytes_per_image_is_reported_because_the_storage_estimate_rests_on_it(
        tmp_path) -> None:
    make_task(tmp_path)
    out = tmp_path / "r.json"
    assert run(tmp_path, "--json", out).returncode == 0
    assert json.loads(out.read_text())["bytes_per_image"] > 0


def test_the_shards_are_read_with_the_library_a_trainer_would_use(tmp_path
                                                                  ) -> None:
    """Not tarfile: webdataset. Whether OUR output loads in the consumer's
    tool is the question the corpus exists to answer."""
    make_task(tmp_path, samples=4)
    out = tmp_path / "r.json"
    assert run(tmp_path, "--json", out).returncode == 0
    payload = json.loads(out.read_text())
    assert payload["webdataset_samples"] == 4
    assert payload["decode_failures"] == 0


def test_an_undecodable_image_is_counted_not_skipped(tmp_path) -> None:
    """Silently dropping them would report a clean corpus that is not."""
    make_task(tmp_path, samples=4, broken=2)
    out = tmp_path / "r.json"
    # Half the images undecodable must fail the run, not merely be noted.
    result = run(tmp_path, "--json", out)
    assert result.returncode != 0
    assert json.loads(out.read_text())["decode_failures"] == 2


def test_a_caption_that_belongs_to_another_image_is_detected(tmp_path
                                                             ) -> None:
    """Shifted captions still give a full caption count and a plausible
    corpus. Only comparing them against the parquet finds it."""
    make_task(tmp_path, samples=4, caption_shift=1)
    out = tmp_path / "r.json"
    result = run(tmp_path, "--json", out)
    assert json.loads(out.read_text())["caption_mismatches"] == 4
    assert result.returncode != 0


def test_the_exif_share_is_measured_because_it_is_still_undecided(tmp_path
                                                                  ) -> None:
    """EXIF carries GPS. Blurring faces while storing where the photo was
    taken is inconsistent, and this is the only such decision that can be
    reversed without re-downloading — so its cost has to be known."""
    make_task(tmp_path, exif='{"GPS GPSLatitude": "[35, 41, 22]"}')
    out = tmp_path / "r.json"
    assert run(tmp_path, "--json", out).returncode == 0
    assert json.loads(out.read_text())["exif_bytes_share"] > 0


def test_a_task_marked_done_with_no_shards_is_not_reported_as_healthy(
        tmp_path) -> None:
    task = tmp_path / "task-000000"
    (task / "shards").mkdir(parents=True)
    (task / "DONE.json").write_text(json.dumps(
        {"task_id": 0, "candidates": 8, "successes": 4}))
    result = run(tmp_path)
    assert result.returncode != 0
    assert "no shard" in (result.stdout + result.stderr).lower()


def test_the_size_of_what_arrived_is_reported(tmp_path) -> None:
    """The metadata's claim, finally checked against our own output."""
    make_task(tmp_path, size=(640, 400))
    out = tmp_path / "r.json"
    assert run(tmp_path, "--json", out).returncode == 0
    assert json.loads(out.read_text())["short_side_p50"] == pytest.approx(400)


def test_incomplete_tasks_are_named_rather_than_averaged_away(tmp_path
                                                              ) -> None:
    make_task(tmp_path, 0)
    make_task(tmp_path, 1)
    (tmp_path / "task-000001" / "DONE.json").unlink()
    out = tmp_path / "r.json"
    result = run(tmp_path, "--json", out)
    payload = json.loads(out.read_text())
    assert payload["tasks_done"] == 1
    assert payload["tasks_incomplete"] == 1
    assert "task-000001" in result.stdout


def test_an_empty_root_is_an_error(tmp_path) -> None:
    assert run(tmp_path).returncode != 0


def test_a_low_yield_pilot_stops_the_wave(tmp_path) -> None:
    """Yield is what turns 1.39 billion rows into 902 million images and
    1,391 node-hours. If the pilot comes in far under the assumption, both
    numbers are wrong and widening the wave spends the budget on a plan
    that no longer describes reality — which is how 474 tasks were lost to
    the 2026-07-28 outage.
    """
    make_task(tmp_path, samples=4, candidates=100)
    result = run(tmp_path)
    assert result.returncode != 0
    assert "below" in result.stdout.lower()


def test_a_healthy_pilot_says_to_continue(tmp_path) -> None:
    """The guard must not have been bought by refusing everything."""
    make_task(tmp_path, samples=7, candidates=8)
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Widen the wave" in result.stdout
