#!/usr/bin/env python3
"""Cut a URL list into disjoint slices, one URL per line.

Disjoint because the concurrency experiment must not let an earlier run warm
a resolver cache for a later one. Done in a single pass so that the header,
if there is one, is interpreted exactly once — a per-slice guess would either
drop a URL or fetch a column name.

Stops reading as soon as enough URLs have been collected, so pointing this at
a very large list costs only what it uses.

Refuses input it cannot read rather than emitting something plausible. See
src/opendinov3/core/urllist.py for why that matters here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import urllist  # noqa: E402

PARQUET_MAGIC = b"PAR1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="URL list to read")
    parser.add_argument("outdir", type=Path, help="where to write the slices")
    parser.add_argument("--count", type=int, required=True,
                        help="URLs per slice")
    parser.add_argument("--slices", type=int, required=True,
                        help="how many slices to cut")
    args = parser.parse_args()

    if not args.source.is_file():
        print(f"error: no such file: {args.source}", file=sys.stderr)
        return 2

    with args.source.open("rb") as handle:
        if handle.read(4) == PARQUET_MAGIC:
            print(
                f"error: {args.source} is a parquet file, not a text URL list.\n"
                "       Point at a text list, or convert it first.",
                file=sys.stderr,
            )
            return 2

    wanted = args.count * args.slices
    args.outdir.mkdir(parents=True, exist_ok=True)

    urls: list[str] = []
    column: int | None = None
    header_seen = False

    with args.source.open("r", errors="replace") as handle:
        for lineno, raw in enumerate(handle):
            line = raw.rstrip("\n").rstrip("\r")

            if lineno == 0:
                if urllist.looks_like_header(line):
                    column = urllist.find_url_column(line)
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

    print(f"source     : {args.source}")
    print(f"header     : {'yes' if header_seen else 'no'}"
          + (f" (url is column {column})" if column is not None else ""))
    print(f"collected  : {len(urls)} URLs (wanted {wanted})")

    written = 0
    for index in range(args.slices):
        chunk = urls[index * args.count:(index + 1) * args.count]
        if len(chunk) < args.count:
            break
        path = args.outdir / f"slice_{index + 1}.txt"
        path.write_text("\n".join(chunk) + "\n")
        print(f"  wrote {path} ({len(chunk)} URLs)")
        written += 1

    if written < args.slices:
        # Not a warning. A level whose slice is short or missing cannot be
        # compared with the others, so the experiment would burn its slot to
        # produce numbers that answer nothing. Better to stop and be told the
        # list is smaller than assumed.
        print(
            f"error: only {written} of {args.slices} slices could be filled "
            f"from {len(urls)} URLs at {args.count} each.\n"
            f"       Lower --count to at most {len(urls) // args.slices}, "
            "or use a longer list.",
            file=sys.stderr,
        )
        return 1
    return 0


def _field(line: str, column: int) -> str:
    fields = line.split(urllist.DELIMITER)
    return fields[column].strip() if column < len(fields) else ""


if __name__ == "__main__":
    raise SystemExit(main())
