# Runtime image for the OpenDINOv3 downloader.
#
# One image definition serves three environments:
#   * local development  (docker run)
#   * CI                 (GitHub Actions)
#   * cluster execution  (converted to SIF and run by SingularityCE)
#
# The cluster runs Python 3.12.11, so the minor version is matched here to keep
# behaviour comparable across all three.
#
# SINGULARITY COMPATIBILITY
#   Singularity runs the container as the invoking user, with a read-only root
#   filesystem and a bind-mounted $HOME. The image therefore must not:
#     * require root at runtime
#     * write anywhere inside itself
#     * depend on a writable $HOME
#   tests/test_container_runtime.sh emulates all three with Docker flags.

FROM python:3.12-slim

# Native libraries that opencv-python-headless and pyarrow link against.
# --no-install-recommends keeps the image small; the lists are removed in the
# same layer so they do not persist in the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies before copying source so that source edits do not
# invalidate the dependency layer.
WORKDIR /opt/opendinov3
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY tests/ ./tests/

# Caches default to $HOME, which may be read-only or absent under Singularity.
# Point them at /tmp, which Singularity always provides as writable.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    MPLCONFIGDIR=/tmp/.cache/matplotlib \
    # albumentations, a transitive dependency of img2dataset, contacts a remote
    # endpoint on import to check for a newer release. img2dataset imports once
    # per worker process, and DNS failures on the target cluster were measured
    # at up to ~15 s, so this turns into real latency at scale. Disable it.
    NO_ALBUMENTATIONS_UPDATE=1

# Run as a non-root user by default. Singularity overrides the UID anyway, but
# this keeps `docker run` honest and catches root-only assumptions early.
RUN useradd --create-home --uid 10001 runner
USER 10001

CMD ["python", "-c", "import img2dataset; print('opendinov3 image OK')"]
