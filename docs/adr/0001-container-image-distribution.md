# ADR 0001: Build the image in CI, pull it on the cluster

Status: accepted
Date: 2026-08-13

## Context

The target cluster runs **SingularityCE 4.4.1**, not Docker. Both ways of
getting an image onto it were tested there and both work:

| Method | Result |
|---|---|
| `singularity build --fakeroot docker://...` | works |
| `singularity pull docker://...` | works |

Egress from the login node reaches public registries.

So the choice is not about capability. It is about which artifact ends up
running.

## Decision

**Build the image in CI, publish it to a registry, and pull it on the cluster.
Do not rebuild on the cluster.**

The registry is GitHub Container Registry (`ghcr.io`). The repository is
public, so the image can be public too and the cluster needs no credentials
to pull it.

## Rationale

**The artifact that was tested must be the artifact that runs.** Rebuilding on
the cluster produces a *different* image: base layers, wheels and transitive
resolution can all move between builds. Everything CI proved about the image
would then apply to something else. Pulling a published image by tag — and,
for production runs, by digest — removes that gap.

Two further points, both measured rather than assumed:

- Build temporary space landed in `$HOME` on the cluster
  (`/home/<account>/.tmp/...`). Home directories there are quota-limited, so
  repeated builds risk filling them. Pulling still uses a cache, but a much
  smaller one, and `SINGULARITY_CACHEDIR` can be redirected.
- The login node is shared. Building images on it consumes CPU and I/O that
  other users are also paying for.

## Consequences

- CI must build and publish on every merge to the default branch.
- Production runs pin the image by digest, not by a mutable tag.
- `SINGULARITY_CACHEDIR` and `SINGULARITY_TMPDIR` must point at group storage,
  not at `$HOME`.
- A registry outage blocks new pulls. Already-pulled `.sif` files keep working,
  so this delays updates rather than stopping production.

## Alternatives considered

**Build on the cluster with `--fakeroot`.** Works, needs no registry, but
breaks the "tested artifact is the running artifact" property and puts build
load on a shared login node.

**Ship a `.sif` file over `scp`.** No registry dependency, but the transfer is
manual, unversioned, and easy to get wrong. Provenance is lost.
