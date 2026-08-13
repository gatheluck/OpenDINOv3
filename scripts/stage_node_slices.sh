#!/usr/bin/env bash
# Put one node's slice where the worker expects it.
#
#   stage_node_slices.sh <exp_out> <phase> <node index> <slice path, relative>
#
# The worker takes a directory containing `slice_1.parquet` and `url_column`.
# Each node of a phase needs a different slice, so each gets its own directory.
#
# WHY THIS COPIES INSTEAD OF LINKING
#
# It used to link:
#
#     ln -sf "${OD_EXP_OUT}/slices_p1/slice_1.parquet" .../slices/slice_1.parquet
#
# which writes an absolute host path into the link. The worker reads it inside
# a container where that path is not mounted, so the file was simply absent:
#
#     ❌ no slice_1.parquet or slice_1.txt in /out/phase1_single/node0/slices
#
# and every phase of experiment 0003 produced nothing. The `url_column` beside
# it was copied, and survived; the two differed in nothing else.
#
# A relative link would also work, but only while every consumer resolves it
# the same way. A copy has no such condition. The slices are a few megabytes.

set -uo pipefail

die() { printf '❌ %s\n' "$*" >&2; exit 1; }

[ "$#" -eq 4 ] || die "usage: $0 <exp_out> <phase> <node index> <slice rel path>"

EXP_OUT="$1"
PHASE="$2"
NODE="$3"
SLICE_REL="$4"

SRC="${EXP_OUT}/${SLICE_REL}"
SRC_DIR="${SRC%/*}"
DEST="${EXP_OUT}/${PHASE}/node${NODE}/slices"

[ -f "${SRC}" ] || die "no such slice: ${SLICE_REL} (looked in ${EXP_OUT})"
[ -f "${SRC_DIR}/url_column" ] \
  || die "no url_column beside ${SLICE_REL}; the worker needs it for --url_col"

mkdir -p "${DEST}" || die "cannot create ${DEST}"

# Always slice_1: the worker reads one slice per invocation, and which slice
# a node got is recorded by the directory it sits in.
cp -f "${SRC}" "${DEST}/slice_1.parquet" || die "cannot copy ${SRC}"
cp -f "${SRC_DIR}/url_column" "${DEST}/url_column" || die "cannot copy url_column"

printf '%s\n' "${DEST}"
