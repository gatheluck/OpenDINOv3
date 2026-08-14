#!/usr/bin/env python3
"""What resolution the corpus is, from metadata, before anything is fetched.

  measure_resolution.py <upstream_metadata dir> [--files 40] [--json out.json]

WHY

This corpus is being built as a general asset on ABCI: usable for large image
recognition models and for video generation models, and for consumers not yet
named. So this does not grade the corpus against any one model's input size.
It characterises it — the spread of image sizes and aspect ratios — so that
whoever uses it can read off their own answer at their own threshold.

The one thing worth knowing up front is how much of it is degenerate:
tracking pixels, spacer GIFs and icons are waste for every consumer, unlike a
200-pixel photograph, which is merely small for some of them.

DataComp records `original_width` and `original_height`, so this is
answerable in seconds on a login node rather than after 23 TB has arrived.

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

#: Reference points, so a reader can locate their own threshold. These are
#: landmarks, not requirements: nothing here is a pass mark, and the corpus
#: is not being filtered to any of them.
THRESHOLDS: dict[int, str] = {
    32: "degenerate — tracking pixels, spacers, icons",
    112: "small-crop scale",
    224: "the common classification input",
    256: "the common SSL global-crop scale",
    512: "the common text-to-image scale",
    1024: "high-resolution work",
}

#: Where the mass sits, which is the actual output of this script.
PERCENTILES = (1, 5, 10, 25, 50, 75, 90, 99)


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
    print()
    print("short side, by percentile:")
    for p in PERCENTILES:
        print(f"  p{p:<3} {stats.percentile(p):>8,.0f} px")
    print()
    print("share whose SHORT side is below (landmarks, not requirements):")
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

    # The download keeps every image at its original resolution
    # (--resize_mode no), and img2dataset records each sample's real decoded
    # width and height in the shard's parquet. So no consumer is committed to
    # anyone else's threshold: filtering happens at training time, per use,
    # without re-downloading. Nothing below is a reason to filter now.
    degenerate = stats.fraction_below(32)
    if degenerate > 0.01:
        print(f"→ {degenerate:.1%} has a short side under 32 px: tracking")
        print("  pixels, spacers and icons. That is waste for every")
        print("  consumer, and the only filter that does not cost someone")
        print("  else their use case. It is also the only one that saves")
        print("  bandwidth, since img2dataset's --min_image_size decodes")
        print("  the image before rejecting it.")
    else:
        print(f"→ Only {degenerate:.2%} is degenerate (short side < 32 px),")
        print("  so there is nothing worth filtering out for everyone.")
    print()
    print("  Everything else stays: images are stored at original")
    print("  resolution and each sample's real size is recorded per shard,")
    print("  so any consumer can filter to their own threshold later.")

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
            "percentile_short_side": {str(p): stats.percentile(p)
                                      for p in PERCENTILES},
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
