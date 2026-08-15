#!/usr/bin/env python3
"""Decide, from the pilot, whether to spend the other 1,380 tasks.

  inspect_pilot.py <task root> [--samples 200] [--json report.json]

Runs on a login node in seconds. Reports only what changes the decision:

  yield            turns 1.39 billion rows into an image count and a
                   node-hour budget. The plan assumes 65%.
  bytes per image  turns that count into 23.2 TB. The plan assumes 25.1 KB.
  loadability      whether the shards can be read by webdataset, the
                   library a trainer would actually use. Nothing so far has
                   tested this on real output, and it is the point of the
                   corpus.
  decode failures  an image that arrived but cannot be opened is not an
                   image.
  caption mismatch a shifted caption still gives a full caption count and a
                   plausible corpus; only comparing against the parquet
                   finds it.
  EXIF share       EXIF carries GPS. Storing where a photo was taken while
                   blurring the faces in it is inconsistent, and this is the
                   only such decision reversible without re-downloading — so
                   its cost has to be known.
  size of arrivals the metadata's claim, checked against our own output.

A number that changes nothing is not here. See CLAUDE.md section 0.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import resolution_stats as rs  # noqa: E402

#: Yield below this and the 902-million-image plan is wrong.
MIN_YIELD = 0.30
#: A decode failure rate above this means the stored bytes are not images.
MAX_DECODE_FAILURE = 0.01


def read_shard_tar(path: Path, want: int):
    """Yield (key, jpeg bytes, caption) using webdataset if it is present.

    webdataset is what a trainer will use, so it is what this reads. tarfile
    is the fallback so the report still works in an image without it, and
    says which one it used.
    """
    try:
        import webdataset as wds
    except ImportError:
        wds = None

    if wds is not None:
        dataset = wds.WebDataset(str(path), shardshuffle=False,
                                 handler=wds.handlers.warn_and_continue)
        for count, sample in enumerate(dataset):
            if count >= want:
                return
            yield (sample.get("__key__"), sample.get("jpg"),
                   (sample.get("txt") or b"").decode("utf-8", "replace"))
        return

    with tarfile.open(path) as archive:
        pending: dict[str, dict] = {}
        for member in archive:
            key, _, suffix = member.name.rpartition(".")
            entry = pending.setdefault(key, {})
            entry[suffix] = archive.extractfile(member).read()
            if "jpg" in entry and "txt" in entry:
                yield key, entry["jpg"], entry["txt"].decode("utf-8", "replace")
                pending.pop(key)
                want -= 1
                if want <= 0:
                    return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_root", type=Path)
    parser.add_argument("--samples", type=int, default=200,
                        help="images to open per task")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    if not args.task_root.is_dir():
        print(f"no such directory: {args.task_root}", file=sys.stderr)
        return 2

    tasks = sorted(p for p in args.task_root.glob("task-*") if p.is_dir())
    if not tasks:
        print(f"no task directories under {args.task_root}", file=sys.stderr)
        return 2

    done, incomplete = [], []
    for task in tasks:
        (done if (task / "DONE.json").is_file() else incomplete).append(task)

    candidates = successes = 0
    shard_bytes = exif_bytes = parquet_bytes = 0
    sizes: list[tuple[int | None, int | None]] = []
    captions: dict[str, str] = {}
    empty_tasks: list[str] = []

    for task in done:
        marker = json.loads((task / "DONE.json").read_text())
        candidates += int(marker.get("candidates", 0))
        successes += int(marker.get("successes", 0))

        tars = sorted((task / "shards").glob("*.tar"))
        if not tars:
            empty_tasks.append(task.name)
            continue
        shard_bytes += sum(t.stat().st_size for t in tars)

        for path in sorted((task / "shards").glob("*.parquet")):
            parquet_bytes += path.stat().st_size
            table = pq.read_table(path)
            names = table.schema.names
            if "exif" in names:
                exif_bytes += sum(len(v or "")
                                  for v in table.column("exif").to_pylist())
            if "original_width" in names and "original_height" in names:
                sizes.extend(zip(table.column("original_width").to_pylist(),
                                 table.column("original_height").to_pylist()))
            if "caption" in names and "key" in names:
                # Keyed by (task, key). Every task starts at shard 00000, so
                # sample keys repeat across tasks; one flat dict let the last
                # task read win and reported 300 mismatches in 400 — three of
                # four tasks — on output that was fine.
                captions.update(
                    ((task.name, k), v)
                    for k, v in zip(table.column("key").to_pylist(),
                                    table.column("caption").to_pylist()))

    if empty_tasks:
        print(f"❌ {len(empty_tasks)} task(s) carry DONE.json but no shard "
              f"tar: {', '.join(empty_tasks[:5])}", file=sys.stderr)
        print("   A task marked complete with nothing in it is worse than a"
              " failed one.", file=sys.stderr)
        return 1

    if not done:
        print("no completed tasks yet", file=sys.stderr)
        return 1

    # --- open the images, the way a trainer would ----------------------------
    opened = decode_failures = caption_mismatches = 0
    per_task = max(1, args.samples // len(done))
    for task in done:
        for path in sorted((task / "shards").glob("*.tar"))[:2]:
            for key, jpg, text in read_shard_tar(path, per_task):
                opened += 1
                try:
                    from PIL import Image
                    import io as _io
                    Image.open(_io.BytesIO(jpg)).convert("RGB")
                except Exception:
                    decode_failures += 1
                    continue
                expected = captions.get((task.name, key))
                if expected is not None and expected != text:
                    caption_mismatches += 1

    stats = rs.summarise(sizes)
    measured_yield = successes / candidates if candidates else 0.0
    per_image = shard_bytes / successes if successes else 0.0

    print(f"task root       : {args.task_root}")
    print(f"tasks           : {len(done)} complete, "
          f"{len(incomplete)} incomplete")
    for task in incomplete[:8]:
        print(f"                  incomplete: {task.name}")
    print()
    print(f"candidates      : {candidates:,}")
    print(f"stored          : {successes:,}")
    print(f"yield           : {measured_yield:.1%}   (plan assumes 65%)")
    print(f"bytes per image : {per_image / 1024:,.1f} KB   "
          "(plan assumes 25.1 KB)")
    print()
    print(f"opened          : {opened:,} images via "
          f"{'webdataset' if 'webdataset' in sys.modules else 'tarfile'}")
    print(f"decode failures : {decode_failures:,}")
    print(f"caption mismatch: {caption_mismatches:,}")
    print()
    if stats.total:
        print(f"short side p10/p50/p90 : {stats.percentile(10):,.0f} / "
              f"{stats.percentile(50):,.0f} / {stats.percentile(90):,.0f} px")
    if parquet_bytes:
        print(f"EXIF            : {exif_bytes / 1024 ** 2:,.1f} MB of "
              f"{parquet_bytes / 1024 ** 2:,.1f} MB parquet "
              f"({exif_bytes / parquet_bytes:.1%})")
    print()

    # --- what the whole run would look like ----------------------------------
    TOTAL_ROWS = 1_387_173_656
    if measured_yield:
        print("extrapolated to the full corpus:")
        print(f"  images   : {TOTAL_ROWS * measured_yield / 1e6:,.0f} million")
        print(f"  storage  : "
              f"{TOTAL_ROWS * measured_yield * per_image / 1024 ** 4:,.1f} TB")
        print()

    verdict = 0
    if measured_yield < MIN_YIELD:
        print(f"❌ yield {measured_yield:.1%} is below {MIN_YIELD:.0%}. The")
        print("   budget and the image count are both wrong; do not widen")
        print("   the wave until this is understood.")
        verdict = 1
    if opened and decode_failures / opened > MAX_DECODE_FAILURE:
        print(f"❌ {decode_failures / opened:.1%} of sampled images do not")
        print("   decode. Stored bytes that are not images are not data.")
        verdict = 1
    if caption_mismatches:
        print(f"❌ {caption_mismatches} caption(s) do not match the parquet")
        print("   for their key. Text-conditioned training would learn")
        print("   wrong pairs, and the count of captions would look right.")
        verdict = 1
    if verdict == 0:
        print("→ The pilot holds. Widen the wave.")

    if args.json:
        payload = {
            "tasks_done": len(done),
            "tasks_incomplete": len(incomplete),
            "candidates": candidates,
            "successes": successes,
            "yield": measured_yield,
            "bytes_per_image": per_image,
            "webdataset_samples": opened,
            "decode_failures": decode_failures,
            "caption_mismatches": caption_mismatches,
            "exif_bytes_share": (exif_bytes / parquet_bytes
                                 if parquet_bytes else 0.0),
            "short_side_p50": stats.percentile(50),
            "short_side_p10": stats.percentile(10),
            "short_side_p90": stats.percentile(90),
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json}")
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
