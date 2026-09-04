# Experiment 0005: How many nodes, and what stops us

Run: 2026-08-17 to 2026-09-04, on production waves rather than as a
pre-registered experiment. Recorded here because the measurements decided
the schedule and would otherwise live only in a conversation.

## Question

How many nodes can download concurrently before throughput stops rising?

## What was found

**Two different ceilings, at different node counts, with different
signatures.** They were confused for each other for two weeks.

| Nodes | Connections | URL/s per node | URL/s total | yield | DNS | unreachable |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1,024 | 348 | 348 | 64% | 6.1% | 0.1% |
| 1 | 1,024 | 162 | 162 | 64% | 6.1% | 0.1% |
| 1 | 1,024 | 575 | 575 | 64.0% | 6.1% | 0.1% |
| 4 | 4,096 | ~430 | ~1,700 | 64.0% | 6.2% | 0.2% |
| 8 | 8,192 | 217 | ~1,738 | **17.4%** | **76.6%** | 0.1% |
| 20 | 20,480 | 9.8 | 196 | 14.9% | 6.1% | **35.3%** |

The three single-node rows are the same configuration on different days:
2026-08-15, 2026-08-20, and 2026-09-03. Nothing on our side changed between
them. The environment did.

### Ceiling one: connections, at 20 nodes

`Network is unreachable` for 35.3% of attempts and `timed out` for 38.5%,
while DNS stayed at its normal 6.1% and the 400 Gbps external link carried
0.005% of its capacity. 57 of 63 subjobs were killed at the 12 h walltime
having stored nothing.

Not bandwidth: 60.8 TiB of images is 22 minutes at 400 Gbps. The corpus takes
months because it arrives one 85 KB file at a time from hundreds of thousands
of hosts, and it was the count of those connections that ran out.

ABCI support confirmed it: a connection limit exists, its value is not
disclosed, and it is *"システム及び利用者全体で共有"* — shared across the
whole system and all its users. A mitigation was applied in early September;
the single-node rate went from 162 to 575 URL/s with no change on our side.

### Ceiling two: the resolver, at 8 nodes

Eight nodes ask the shared resolver for roughly 4,600 names a second. DNS
failures went from 6.2% to **76.6%** and yield from 64.0% to **17.4%**.
`unreachable` did not move — it stayed at 0.1%.

**That is the discriminator.** Connections were fine; names were not. Reading
the yield alone would have said "8 nodes is worse" and left the reason
unknown, and the obvious next move — reduce connections — would have been
aimed at the wrong thing.

## What it cost, and the defect that surfaced

The 8-node wave left 71 tasks at 17.4% yield. The health guard rejected all
71, correctly. But a retry inherited any shard above 5% yield while a task
needs 30% to pass, and `--incremental_mode incremental` does not refetch a
shard that already has output. Those 71 tasks could never have completed:
retry, inherit, reject, forever.

The 5% floor had been reasoned from the failures already seen — the
2026-07-28 outage stored 0.1%, healthy shards store 58-65%, so nothing real
sat near the line. 17.4% landed in the middle of the band that reasoning
assumed was empty.

`MIN_SHARD_YIELD` is now `task_health.MIN_YIELD` and the relation is
asserted. Repair was automatic: the next wave set the degraded shards aside
and refetched them. task-000358 went from 17.4% to 63.5%. Only 36 of its 100
shards were below the line, so the repair cost a third of what a blanket
redo would have.

## What was done about each ceiling

| Ceiling | Lever | Status |
|---|---|---|
| Connections | fewer nodes; connection reuse (`OD_HTTP_POOL`) | 4 nodes measured good |
| Connections | ABCI raised the limit | done, 3.6x |
| Resolver | per-process DNS cache (`OD_DNS_CACHE`) | this change |

`od.sh hosts` measured 500,000 URLs across 118,834 hosts: **76% of name
lookups repeat**. A cache has no equivalent of a server closing a keep-alive
connection, so unlike connection reuse that figure is largely realisable.

The cache is per **process**, and a node runs 32 of them, so a host can still
be resolved up to 32 times per node rather than once. The reduction is real
and smaller than a shared resolver daemon would give.

## The defect that made the first attempt do nothing

img2dataset's distributor calls `get_context("spawn")`. A spawned worker
starts a fresh interpreter and inherits nothing, so a monkeypatch applied in
a wrapper script reaches the parent — which downloads no images — and never
reaches the 32 workers, which download all of them. Measured:

```
parent : opendinov3.net.dns_cache._lookup
child  : socket.getaddrinfo
```

Connection reuse shipped that way and did nothing in production. Its
end-to-end test asserted that the task completed and that `img2dataset.cmd`
named the wrapper. Both were true. Neither was the point.

The patches now live in `src/sitecustomize.py`, which Python imports at
interpreter startup in every process that has it on `sys.path`, including
each replacement worker `maxtasksperchild=5` creates. `production_task.sh`
puts it on `PYTHONPATH` for img2dataset and its children only.

The test that would have caught it counts TCP connections at the far end of
the wire: with pooling on, sixteen images must cost fewer than sixteen
connections.

## What this does not answer

- **Where the ceilings are now.** Both were measured before ABCI's
  mitigation. 8 nodes failed on DNS at the old limit; whether it still does,
  and whether the cache moves it, is the next measurement.
- **The resolver's capacity.** Bracketed between 2,300 q/s (4 nodes, fine)
  and 4,600 q/s (8 nodes, saturated). Not narrowed further.
- **Whether 16 or more nodes is reachable.** Nothing above 8 has been tried
  since the mitigation, and the reservation is shared with other users.

## How to widen a wave from here

Change one thing, then measure both the rate and the failure mix. The rate
alone cannot tell the two ceilings apart.

```bash
bash scripts/od.sh assess "$OD_TASK_ROOT/task-NNNNNN"
```

| Reading | Meaning |
|---|---|
| `unreachable` climbing | connections — reduce nodes, or turn on `OD_HTTP_POOL` |
| `DNS` climbing | the resolver — turn on `OD_DNS_CACHE` |
| both flat, yield ~64% | there is headroom; widen again |
