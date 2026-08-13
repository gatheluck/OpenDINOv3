# Experiment 0001: Shard read-and-decode throughput

Status: **complete — hypothesis not rejected**
Pre-registered: 2026-08-13
Run: 2026-08-13

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

Run on a login node, 3 shards of an untouched task, cold then warm.

| | cold | warm |
|---|---:|---:|
| samples | 19,502 | 19,502 |
| **samples/sec** | **2,078** | **2,183** |
| MB/sec | 40.9 | 42.9 |
| **decode failures** | **0** | **0** |
| elapsed | 9.4 s | 8.9 s |

Per-shard rates varied by under 1%, so the figure is stable across shards.

### Against the falsification criteria

**Worker count.** One process sustains ~2,078 samples/sec. The node has 192
cores. Even granting a GPU an appetite of 2,000 images/sec, roughly one core
feeds one GPU; loading would occupy a small fraction of the node for any
plausible GPU count. Not rejected, by a wide margin.

**Decode failure rate.** Zero failures in 19,502 samples. This is the expected
result rather than a surprising one: `_stats.json` counts a sample as a
success only after it decoded once at download time, and failed samples are
never written. Zero on read-back means nothing degraded after writing. Not
rejected.

### What the cold/warm pair shows

Warming the page cache bought only **5%**. If Lustre I/O were the constraint,
a second pass over the same bytes would be far faster than that. Lustre
sustains gigabytes per second, so 40.9 MB/sec is nowhere near its limit.

**The pipeline is decode-bound, not I/O-bound.** That also means the cold
figure can be used directly; the page-cache confounder listed above turns out
to be small for this measurement.

### Compute-node repeat: judged unnecessary

The protocol called for repeating on a compute node if the result landed near
a falsification boundary. It did not — the margin is an order of magnitude.
Differences in CPU or storage path between node types cannot plausibly close
a gap that size.

### Conclusion

**The shard format is usable for training. No change is needed.** G1, the
largest unvalidated assumption in the project, is resolved.

### Caveats that still stand

- The figure excludes DataLoader collation and inter-process transfer. It is
  a ceiling. The margin is large enough that even a 2–3× overhead leaves the
  conclusion intact, but the number should not be quoted as training
  throughput.
- Single process. Scaling to N processes is not N times this, and was not
  measured here.
- Measured on a login node. Reported as such.
