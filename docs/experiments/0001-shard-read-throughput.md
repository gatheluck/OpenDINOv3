# Experiment 0001: Shard read-and-decode throughput

Status: pre-registered, not yet run
Date: 2026-08-13

Pre-registered before running, because a measurement whose confounders are
listed only afterwards tends to list the ones that fit the answer. Two
earlier measurements in this project were designed in a way that could not
answer the question asked of them; both were avoidable by writing this
section first.

## Question

Can a training job stream the shards that have already been downloaded fast
enough to keep a GPU busy?

## Why it matters now

Roughly 700 tasks remain to download, which is weeks of wall time. If the
shards turn out to be unusable for training, that is far cheaper to discover
now than afterwards. Nobody has checked: validation so far has confirmed that
a sample *decodes*, which is a different claim from *a training loop can keep
up*.

## Hypothesis

Read-and-decode throughput per process is high enough that a handful of
workers saturates a GPU's input demand, so the shard format needs no change.

## Falsification criteria

The hypothesis is rejected if either holds:

- Per-process throughput is so low that the worker count needed to feed one
  GPU exceeds the cores available per GPU on the target node.
- The decode failure rate materially exceeds the rate the downloader itself
  recorded. `_stats.json` counts a sample as a success only after it decoded
  once at download time, so a much higher rate on read-back would mean data
  degraded or was written incorrectly.

## Measured quantities

- samples/sec and MB/sec, per shard and in total
- decode failures, and the failure rate over attempted samples
- elapsed time per shard
- machine identity and Python version, recorded with the result

## Confounders — what this measurement cannot separate

- **Page cache.** A second run over the same shard reads from memory, not
  from Lustre. First-touch and warm numbers differ, possibly by a lot. Run
  cold first and say which one a figure is.
- **Shared filesystem load.** Lustre is shared. Other users' I/O moves this
  number and cannot be controlled or observed from here.
- **Node type.** A login node and a compute node have different CPUs and
  different paths to storage. Numbers do not transfer between them.
- **Image size distribution.** Shards were written with `--resize_mode no`,
  so images are at original resolution and vary widely. Decode cost scales
  with pixels, so a shard of large images is slower for reasons unrelated to
  the pipeline.
- **Single process.** This measures one process. Scaling to N processes is
  not simply N times the rate; contention appears somewhere.

## What this cannot answer

- **Actual training throughput.** torch's DataLoader adds collation and
  inter-process transfer on top. torch is not in the image, because it would
  add several gigabytes to every pull. This gives a ceiling. If the ceiling
  is close to what training needs, the remaining overhead has to be measured
  separately, and torch gets added then.
- **Whether the data is any good.** Structure and speed only. Semantic
  quality, duplication and distribution are out of scope.
- **Behaviour under concurrent training.** Measured while nothing else of
  ours is running.

## Sample size

Three shards, roughly 6,000 samples each. Enough to see an order of
magnitude, which is what the falsification criteria turn on. Not enough to
resolve differences of a few percent between shards, and no such claim will
be made from it.

## Procedure

Run on a login node first, since it needs no reservation. If the result is
near either falsification boundary, repeat inside an interactive job on a
compute node, where the CPU and the storage path match production.

```bash
singularity exec --bind "${OD_ROOT}:/data:ro" opendinov3.sif \
  python /work/scripts/bench_shards.py /data/<task-dir> --limit 3 \
  --json bench.json
```

## Result

Not yet run.
