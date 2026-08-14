#!/usr/bin/env python3
"""What resolution the corpus is, from metadata, before anything is fetched.

  measure_resolution.py <upstream_metadata dir> [--files 40] [--json out.json]

WHY

DINOv3 takes 2 global crops at 256x256 and 8 local crops at 112x112. An image
whose short side is under 256 cannot fill a global crop without upscaling.
Video models train a text-to-image stage at 256p and then 512p. So the share
of the corpus below those sizes decides how much of a 902-million-image,
23 TB download is usable for the training it is being collected for — and
DataComp records `original_width` and `original_height`, so it is answerable
here, in seconds on a login node, rather than afterwards.

Reads only the two size columns. Parquet is columnar, so the URLs and
captions are never touched; a file that is hundreds of megabytes on disk
costs a few megabytes to measure.

SAMPLING

Files evenly spaced across the corpus, both ends included, no randomness.
Reading the front is not sampling: the first twelve tasks of this corpus gave
70-124 KB per image against a whole-corpus 25.1 KB, because the front of the
list was pilot data. Within a chosen file every row is read, so there is no
second bias hiding inside it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import dataset_schema as ds  # noqa: E402
from opendinov3.core import resolution_stats as rs  # noqa: E402

#: Thresholds worth reporting, and what each one gates.
THRESHOLDS: dict[int, str] = {
    112: "DINOv3 local crop",
    256: "DINOv3 global crop; video T2I stage 1",
    512: "video T2I stage 2",
    1024: "high-resolution finetuning",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("meta_dir", type=Path)
    parser.add_argument("--files", type=int, default=40,
                        help="parquet files to read, spread across the "
                             "corpus; 0 reads every one")
    parser.add_argument("--json", type=Path, help="also write the result here")
    args = parser.parse_args()

    if not args.meta_dir.is_dir():
        print(f"no such directory: {args.meta_dir}", file=sys.stderr)
        return 2

    files = sorted(args.meta_dir.rglob("*.parquet"))
    if not files:
        print(f"no parquet files under {args.meta_dir}", file=sys.stderr)
        return 2

    columns = list(pq.ParquetFile(files[0]).schema_arrow.names)
    try:
        resolved = ds.resolve(columns)
    except ds.SchemaError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    if resolved.width is None or resolved.height is None:
        print("❌ no recorded width/height in this corpus "
              f"({columns}).", file=sys.stderr)
        print("   Resolution cannot be measured from metadata; it would have",
              file=sys.stderr)
        print("   to be measured after downloading, or the corpus judged on",
              file=sys.stderr)
        print("   other grounds. Reporting 0% below 256 here would be a",
              file=sys.stderr)
        print("   clean-looking answer to a question nobody measured.",
              file=sys.stderr)
        return 1

    chosen = (list(range(len(files))) if args.files <= 0
              else rs.sample_indices(len(files), args.files))

    print(f"directory   : {args.meta_dir}")
    print(f"files       : {len(chosen)} read of {len(files)}, "
          "spread evenly across the corpus")
    print(f"size columns: {resolved.width} / {resolved.height}")
    print()

    parts = []
    unreadable: list[str] = []
    for index in chosen:
        path = files[index]
        try:
            table = pq.read_table(path,
                                  columns=[resolved.width, resolved.height])
        except Exception as exc:  # a corrupt file is a finding, not a crash
            unreadable.append(f"{path.name}: {exc}")
            continue
        parts.append(rs.summarise(zip(table.column(resolved.width).to_pylist(),
                                      table.column(resolved.height).to_pylist())))

    stats = rs.merge(parts)
    if stats.total == 0:
        print("❌ every sampled row had an unusable size.", file=sys.stderr)
        return 1

    print(f"rows measured : {stats.total:,}")
    if stats.unusable:
        print(f"unusable size : {stats.unusable:,} "
              f"({stats.unusable / (stats.total + stats.unusable):.2%}) "
              "— zero or missing, excluded rather than counted as small")
    print(f"median short side : {stats.median_short_side:,.0f} px")
    print()
    print("share whose SHORT side is below:")
    for threshold, why in THRESHOLDS.items():
        fraction = stats.fraction_below(threshold)
        print(f"  {threshold:>5} px  {fraction:6.1%}   ({why})")
    print()
    print(f"median aspect ratio : {stats.median_aspect:.2f}")
    print(f"within 0.9-1.1 (square-ish) : "
          f"{stats.fraction_within(0.9, 1.1):.1%}")
    print(f"within 0.5-2.0              : "
          f"{stats.fraction_within(0.5, 2.0):.1%}")
    print()

    if unreadable:
        print(f"⚠️  {len(unreadable)} file(s) could not be read, "
              f"first: {unreadable[0]}")
        print()

    below_global = stats.fraction_below(256)
    if below_global > 0.5:
        print("→ More than half the corpus cannot fill a 256px global crop.")
        print("  Downloading it whole would spend most of the budget on")
        print("  images DINOv3 would have to upscale. Filtering on")
        print(f"  {resolved.width}/{resolved.height} before download is free —")
        print("  the columns are already in the manifest.")
    elif below_global > 0.15:
        print(f"→ {below_global:.1%} is below the 256px global crop. Worth a")
        print("  minimum-size filter at manifest time, which costs nothing.")
    else:
        print(f"→ {below_global:.1%} is below the 256px global crop. The")
        print("  corpus is suitable for DINOv3 as-is.")

    if args.json:
        payload = {
            "directory": str(args.meta_dir),
            "files_total": len(files),
            "files_read": len(chosen),
            "width_column": resolved.width,
            "height_column": resolved.height,
            "rows_measured": stats.total,
            "rows_unusable": stats.unusable,
            "median_short_side": stats.median_short_side,
            "median_aspect": stats.median_aspect,
            "fraction_below": {str(t): stats.fraction_below(t)
                               for t in THRESHOLDS},
            "unreadable_files": unreadable,
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
