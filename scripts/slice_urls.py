#!/usr/bin/env python3
"""Cut a URL list into disjoint slices.

Disjoint because the concurrency experiment must not let an earlier run warm
a resolver cache for a later one.

Two input formats, because the corpus uses one and hand-made lists use the
other:

  parquet  — what the corpus stores (`urls_clean.parquet`, one per task).
             Slices are written back as parquet with every column intact, so
             img2dataset reads them through `--url_col`, the same path
             production uses.
  text     — one URL per line, or tab-separated with a header naming the URL
             column. Cut in a single pass so the header is read exactly once.

Input whose layout cannot be determined is refused rather than guessed at:
img2dataset accepts any string as a URL, so a wrong guess is not an error, it
is a run that fails every fetch and reports a yield near zero that looks like
a finding.

  slice_urls.py SRC OUTDIR --count N --slices K
  slice_urls.py SRC --inspect
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pyarrow.parquet as pq  # noqa: E402

from opendinov3.core import parquet_urls as pu  # noqa: E402
from opendinov3.core import urllist  # noqa: E402

PARQUET_MAGIC = b"PAR1"


def is_parquet(source: Path) -> bool:
    if source.is_dir():
        return any(source.rglob(pu.PARQUET_GLOB))
    with source.open("rb") as handle:
        return handle.read(4) == PARQUET_MAGIC


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="URL list, or a directory of them")
    parser.add_argument("outdir", type=Path, nargs="?",
                        help="where to write the slices")
    parser.add_argument("--count", type=int, help="URLs per slice")
    parser.add_argument("--slices", type=int, help="how many slices to cut")
    parser.add_argument("--inspect", action="store_true",
                        help="report the schema and row count, write nothing")
    args = parser.parse_args()

    if not args.source.exists():
        print(f"error: no such file or directory: {args.source}", file=sys.stderr)
        return 2

    parquet = is_parquet(args.source)
    print(f"source     : {args.source}")
    print(f"format     : {'parquet' if parquet else 'text'}")

    if args.inspect:
        return inspect(args.source, parquet)

    if args.outdir is None or args.count is None or args.slices is None:
        print("error: OUTDIR, --count and --slices are required unless --inspect",
              file=sys.stderr)
        return 2

    args.outdir.mkdir(parents=True, exist_ok=True)
    if parquet:
        return cut_parquet(args.source, args.outdir, args.count, args.slices)
    return cut_text(args.source, args.outdir, args.count, args.slices)


def inspect(source: Path, parquet: bool) -> int:
    if not parquet:
        print("           (text input: run without --inspect to cut slices)")
        return 0
    try:
        info = pu.describe(source)
    except Exception as exc:  # noqa: BLE001 — reported, not swallowed
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"files      : {info.files}")
    print(f"rows       : {info.num_rows:,}")
    print(f"columns    : {info.columns}")
    if info.url_column:
        print(f"url column : {info.url_column}")
    else:
        print(f"url column : NONE — {info.error}", file=sys.stderr)
        return 2
    return 0


def cut_parquet(source: Path, outdir: Path, count: int, n_slices: int) -> int:
    wanted = count * n_slices
    try:
        table, column = pu.read_urls(source, at_least=wanted)
        slices = pu.slice_table(table, count=count, n_slices=n_slices)
    except Exception as exc:  # noqa: BLE001 — the message is the point
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"url column : {column}")
    print(f"rows read  : {table.num_rows:,} (wanted {wanted:,})")

    # The worker needs the column name to pass to img2dataset. Writing it
    # beside the slices keeps the two from disagreeing.
    (outdir / "url_column").write_text(column + "\n")

    for index, chunk in enumerate(slices, start=1):
        path = outdir / f"slice_{index}.parquet"
        pq.write_table(chunk, path)
        print(f"  wrote {path} ({chunk.num_rows:,} rows)")
    return 0


def cut_text(source: Path, outdir: Path, count: int, n_slices: int) -> int:
    wanted = count * n_slices
    urls: list[str] = []
    column: int | None = None
    header_seen = False

    with source.open("r", errors="replace") as handle:
        for lineno, raw in enumerate(handle):
            line = raw.rstrip("\n").rstrip("\r")

            if lineno == 0:
                if urllist.looks_like_header(line):
                    try:
                        column = urllist.find_url_column(line)
                    except urllist.UrlListFormatError as exc:
                        print(f"error: {exc}", file=sys.stderr)
                        return 2
                    header_seen = True
                    continue
                if urllist.DELIMITER in line:
                    print(
                        "error: tab-separated input with no header; cannot "
                        "tell which column holds the URL.",
                        file=sys.stderr,
                    )
                    return 2

            value = line.strip() if column is None else _field(line, column)
            if value:
                urls.append(value)
            if len(urls) >= wanted:
                break

    print(f"header     : {'yes' if header_seen else 'no'}"
          + (f" (url is column {column})" if column is not None else ""))
    print(f"collected  : {len(urls):,} URLs (wanted {wanted:,})")

    written = 0
    for index in range(n_slices):
        chunk = urls[index * count:(index + 1) * count]
        if len(chunk) < count:
            break
        path = outdir / f"slice_{index + 1}.txt"
        path.write_text("\n".join(chunk) + "\n")
        print(f"  wrote {path} ({len(chunk):,} URLs)")
        written += 1

    if written < n_slices:
        print(
            f"error: only {written} of {n_slices} slices could be filled from "
            f"{len(urls)} URLs at {count} each.\n"
            f"       Lower --count to at most {len(urls) // n_slices}, or use "
            "a longer list.",
            file=sys.stderr,
        )
        return 1
    return 0


def _field(line: str, column: int) -> str:
    fields = line.split(urllist.DELIMITER)
    return fields[column].strip() if column < len(fields) else ""


if __name__ == "__main__":
    raise SystemExit(main())
