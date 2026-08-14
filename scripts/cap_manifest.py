#!/usr/bin/env python3
"""Trim a task manifest to its first N URLs, in place.

  cap_manifest.py <urls.parquet> <n>

For experiment arms. An arm has to finish inside its walltime, or what it
measures is how long a kill takes rather than how fast the setting is.

Rewrites atomically: a truncated parquet left behind by an interrupted trim
would be read by the download as a complete manifest, and the task would
quietly fetch a fraction of its URLs while every count downstream agreed
with it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pyarrow.parquet as pq


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    try:
        cap = int(sys.argv[2])
    except ValueError:
        print(f"not a number: {sys.argv[2]}", file=sys.stderr)
        return 2
    if cap <= 0:
        print(f"cap must be positive, got {cap}", file=sys.stderr)
        return 2

    table = pq.read_table(path)
    if table.num_rows <= cap:
        print(f"manifest already holds {table.num_rows:,} URLs, "
              f"under the cap of {cap:,}")
        return 0

    temporary = path.with_suffix(path.suffix + ".partial")
    try:
        pq.write_table(table.slice(0, cap), temporary)
        os.replace(temporary, path)
    except Exception as exc:  # noqa: BLE001 — must not leave a partial file
        temporary.unlink(missing_ok=True)
        print(f"could not cap {path}: {exc}", file=sys.stderr)
        return 1
    print(f"manifest capped to {cap:,} of {table.num_rows:,} URLs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
