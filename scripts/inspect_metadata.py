#!/usr/bin/env python3
"""Report what an upstream metadata schema holds, before anything is downloaded.

  inspect_metadata.py <upstream_metadata dir> [--sample 3]

WHY IT MATTERS

DINOv3 is self-supervised and needs no captions: 2 global crops at 256x256
and 8 local crops at 112x112, ten views per image. The text-to-image stage
that video models such as HunyuanVideo train first cannot be done without
captions at all.

The download pipeline currently carries only the URL column. If upstream
records captions, they are being thrown away — and 23 TB would arrive unable
to train the text-conditioned half of the work.

Recorded width and height matter for the same reason: DINOv3's global crop is
256 pixels, so how much of the corpus is smaller than that is a quality
question answerable here rather than after the download.

Reads parquet footers and one row group. Runs on a login node in seconds.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pyarrow.parquet as pq

#: In upstream order of preference. DataComp uses `text`; others differ.
CAPTION_NAMES = ("text", "caption", "txt", "alt_text", "title")
URL_NAMES = ("url", "image_url", "URL")
WIDTH_NAMES = ("width", "original_width", "w")
HEIGHT_NAMES = ("height", "original_height", "h")


def first_present(names: tuple[str, ...], columns: list[str]) -> str | None:
    for name in names:
        if name in columns:
            return name
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("meta_dir", type=Path)
    parser.add_argument("--sample", type=int, default=3,
                        help="rows to print, so the schema can be believed")
    args = parser.parse_args()

    if not args.meta_dir.is_dir():
        print(f"no such directory: {args.meta_dir}", file=sys.stderr)
        return 2

    files = sorted(args.meta_dir.rglob("*.parquet"))
    if not files:
        print(f"no parquet files under {args.meta_dir}", file=sys.stderr)
        return 2

    first = pq.ParquetFile(files[0])
    columns = list(first.schema_arrow.names)

    total_rows = 0
    mismatched: list[str] = []
    for path in files:
        handle = pq.ParquetFile(path)
        total_rows += handle.metadata.num_rows
        if list(handle.schema_arrow.names) != columns:
            mismatched.append(path.name)

    print(f"directory      : {args.meta_dir}")
    print(f"files          : {len(files)}")
    print(f"rows           : {total_rows:,}")
    print()
    print(f"columns        : {columns}")
    print()

    url = first_present(URL_NAMES, columns)
    caption = first_present(CAPTION_NAMES, columns)
    width = first_present(WIDTH_NAMES, columns)
    height = first_present(HEIGHT_NAMES, columns)

    print(f"url column     : {url or 'NONE'}")
    print(f"caption column : {caption or 'NONE'}")
    print(f"width column   : {width or 'NONE'}")
    print(f"height column  : {height or 'NONE'}")
    print()

    if caption is None:
        print("→ No caption column. DINOv3 does not need one, but the")
        print("  text-to-image stage that video models train first cannot be")
        print("  done from this metadata. Captions would have to come from")
        print("  somewhere else.")
    else:
        print(f"→ Captions are available in `{caption}`. The download must")
        print("  pass --caption_col to keep them; it currently does not.")
    print()

    if width is None or height is None:
        print("→ No recorded resolution. Whether the corpus can feed DINOv3's")
        print("  256px global crops can only be measured after downloading.")
    else:
        print(f"→ Resolution is recorded in `{width}`/`{height}`, so the share")
        print("  below DINOv3's 256px global crop is measurable before"
              " downloading.")
    print()

    if mismatched:
        print(f"⚠️  {len(mismatched)} file(s) differ in schema from "
              f"{files[0].name}, first: {mismatched[0]}")
        print("   A plan built on one schema would break on the others.")
        print()

    if args.sample > 0:
        table = first.read_row_group(0).slice(0, args.sample).to_pydict()
        print(f"first {min(args.sample, len(next(iter(table.values()), [])))} "
              "row(s):")
        for index in range(min(args.sample,
                               len(next(iter(table.values()), [])))):
            print(f"  --- row {index} ---")
            for name, values in table.items():
                print(f"    {name:22} = {str(values[index])[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
