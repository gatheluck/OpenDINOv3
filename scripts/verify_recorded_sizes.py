#!/usr/bin/env python3
"""Check the metadata's claims against the images that actually arrived.

  verify_recorded_sizes.py <shards dir> [--files 40] [--baseline r.json]

WHY

measure_resolution.py reports what upstream SAYS the images are. Planning off
that rests on two things nobody has checked:

  1. The claim is true. A CDN can serve a different size than was crawled.
  2. Success is uncorrelated with size. "53.7% of candidates have a short
     side >= 256, so about 484M of the 902M downloads will" only holds if
     small images do not rot faster than large ones. If they do, every
     derived count is wrong.

Both are answerable from the 82.3 million images already on disk, with no
download. This reads the shard parquet that img2dataset writes beside each
tar, which records each sample's real decoded size and its status.

AND ONE MORE THING

Our shards use --resize_mode no, so they hold original-resolution images. If
the existing tree was fetched with `border` instead, every image in it was
scaled and padded to a square, and the two halves are not one dataset. That
shows up as width != original_width, so it is checked here rather than
discovered by whoever tries to train on the union.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.core import resolution_stats as rs  # noqa: E402

PERCENTILES = (1, 5, 10, 25, 50, 75, 90, 99)

#: How far the arrived median may sit from the claimed one before the
#: metadata stops being a safe basis for planning. Chosen so that ordinary
#: sampling noise between a 1.5% metadata sample and a shard sample does not
#: trip it, while a systematic difference does.
MAX_MEDIAN_DRIFT = 0.20


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shards_dir", type=Path)
    parser.add_argument("--files", type=int, default=40,
                        help="shard parquet files to read, spread across the "
                             "tree; 0 reads every one")
    parser.add_argument("--baseline", type=Path,
                        help="resolution.json from measure_resolution.py")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    if not args.shards_dir.is_dir():
        print(f"no such directory: {args.shards_dir}", file=sys.stderr)
        return 2

    files = sorted(args.shards_dir.rglob("*.parquet"))
    if not files:
        print(f"no shard parquet under {args.shards_dir}", file=sys.stderr)
        return 2

    chosen = (list(range(len(files))) if args.files <= 0
              else rs.sample_indices(len(files), args.files))

    print(f"directory   : {args.shards_dir}")
    print(f"files       : {len(chosen)} read of {len(files)}, "
          "spread evenly across the tree")
    print()

    sizes: list[tuple[int | None, int | None]] = []
    succeeded = failed = 0
    resized = 0
    no_status: list[str] = []

    for index in chosen:
        path = files[index]
        try:
            table = pq.read_table(path)
        except Exception as exc:
            print(f"⚠️  unreadable: {path.name}: {exc}", file=sys.stderr)
            continue
        names = table.schema.names
        if "original_width" not in names or "original_height" not in names:
            continue
        # A shard with no status column cannot be split into successes and
        # failures. Assuming every row succeeded would inflate the yield and
        # quietly answer the question this script exists to ask.
        if "status" not in names:
            no_status.append(path.name)
            statuses = [None] * table.num_rows
        else:
            statuses = table.column("status").to_pylist()

        widths = table.column("original_width").to_pylist()
        heights = table.column("original_height").to_pylist()
        stored_w = (table.column("width").to_pylist()
                    if "width" in names else widths)
        stored_h = (table.column("height").to_pylist()
                    if "height" in names else heights)

        for status, ow, oh, sw, sh in zip(statuses, widths, heights,
                                          stored_w, stored_h):
            if status is not None and status != "success":
                failed += 1
                continue
            if not ow or not oh:
                failed += 1
                continue
            succeeded += 1
            sizes.append((ow, oh))
            if (sw, sh) != (ow, oh):
                resized += 1

    if no_status:
        print(f"⚠️  {len(no_status)} shard(s) carry no `status` column, "
              f"first: {no_status[0]}")
        print("    Their rows are counted only when a size is present, so")
        print("    the success rate below is a lower bound for them.")
        print()

    stats = rs.summarise(sizes)
    if stats.total == 0:
        print("❌ no successfully downloaded rows with a recorded size.",
              file=sys.stderr)
        return 1

    total_rows = succeeded + failed
    print(f"rows        : {total_rows:,} "
          f"({succeeded:,} success, {failed:,} failed or sizeless)")
    print(f"success rate: {succeeded / total_rows:.1%}")
    print()

    # --- is this the same kind of data as ours? ------------------------------
    verdict = 0
    if resized:
        verdict = 1
        print(f"❌ {resized:,} of {succeeded:,} samples "
              f"({resized / succeeded:.1%}) were RESIZED on the way in:")
        print("   the stored width/height differ from the decoded original.")
        print("   Our own shards use --resize_mode no and keep the original,")
        print("   so the two halves are not one dataset. Decide how to")
        print("   reconcile them before treating the union as a corpus.")
    else:
        print("→ Stored at original resolution: stored size equals decoded")
        print("  size for every sample read. Same as ours.")
    print()

    print("short side of what ARRIVED, by percentile:")
    for p in PERCENTILES:
        print(f"  p{p:<3} {stats.percentile(p):>8,.0f} px")
    print()
    print(f"median aspect ratio : {stats.median_aspect:.2f}")
    print()

    # --- does it match what upstream claimed? --------------------------------
    if args.baseline:
        try:
            claimed = json.loads(args.baseline.read_text())
        except Exception as exc:
            print(f"cannot read the baseline: {exc}", file=sys.stderr)
            return 2
        claimed_p = claimed.get("percentile_short_side", {})
        print("claimed (metadata) vs arrived (shards), short side:")
        for p in PERCENTILES:
            want = claimed_p.get(str(p))
            got = stats.percentile(p)
            if want is None:
                continue
            delta = (got - want) / want if want else 0.0
            print(f"  p{p:<3} claimed {want:>8,.0f}   arrived {got:>8,.0f}"
                  f"   {delta:+7.1%}")
        print()

        want_median = claimed_p.get("50")
        if want_median:
            drift = abs(stats.percentile(50) - want_median) / want_median
            if drift > MAX_MEDIAN_DRIFT:
                print(f"❌ the arrived median differs from the claim by "
                      f"{drift:.1%}.")
                print("   Counts derived from the metadata distribution are")
                print("   not safe to quote. Either the recorded sizes are")
                print("   wrong, or download success depends on size — both")
                print("   change the plan.")
                verdict = 1
            else:
                print(f"→ the arrived median is within {drift:.1%} of the "
                      "claim.")
                print("  The metadata distribution is a sound basis for")
                print("  planning, and success is not strongly size-dependent.")
        print()

    if args.json:
        payload = {
            "directory": str(args.shards_dir),
            "files_total": len(files),
            "files_read": len(chosen),
            "rows": total_rows,
            "succeeded": succeeded,
            "failed": failed,
            "resized": resized,
            "shards_without_status": len(no_status),
            "percentile_short_side": {str(p): stats.percentile(p)
                                      for p in PERCENTILES},
            "median_aspect": stats.median_aspect,
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"wrote {args.json}")
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
