"""Contract that this project's network patches reach the processes that download.

WHY THIS FILE EXISTS

img2dataset's distributor calls `get_context("spawn")`. A spawned child does
not inherit the parent's memory: it starts a fresh interpreter and imports
everything again. A monkeypatch applied in the wrapper therefore reaches the
parent — which downloads nothing — and never reaches the 32 workers, which
download everything.

Connection pooling shipped that way. Its end-to-end test asserted that the
task completed and that `img2dataset.cmd` named the wrapper, both of which
were true while the pooling itself did nothing at all. The test checked the
log, not the effect.

Measured before writing this:

    parent : opendinov3.net.dns_cache._lookup
    child  : socket.getaddrinfo

`sitecustomize` is the fix. Python imports it at interpreter startup from
anything on `sys.path`, so every spawned child runs it, including the
replacements `maxtasksperchild` creates part-way through a shard.

WHAT IS ASSERTED HERE

That a child process — started the way img2dataset starts one — has the
patches. Not that the wrapper installed them; that a worker has them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

#: Runs the parent, spawns a child exactly as img2dataset's distributor does,
#: and reports what the CHILD sees. Written to a file because `spawn` re-imports
#: the main module, which it cannot do when that module came from stdin.
PROBE = '''
import json, socket, sys
from multiprocessing import get_context

def seen(_):
    import socket
    import img2dataset.downloader as dl
    return {
        "getaddrinfo": getattr(socket.getaddrinfo, "__module__", "?"),
        "download_image": getattr(dl.download_image, "__module__", "?"),
    }

if __name__ == "__main__":
    with get_context("spawn").Pool(1) as pool:
        print(json.dumps(pool.map(seen, [0])[0]))
'''


def child_sees(tmp_path, **env) -> dict:
    """What a spawned worker has, under the given environment."""
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    result = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(SRC), **env},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------
# Off by default
# --------------------------------------------------------------------------

def test_a_worker_is_untouched_when_nothing_is_asked_for(tmp_path) -> None:
    """The default path must be exactly what upstream does."""
    seen = child_sees(tmp_path, OD_DNS_CACHE="0", OD_HTTP_POOL="0")

    assert seen["getaddrinfo"] == "socket"
    assert seen["download_image"] == "img2dataset.downloader"


# --------------------------------------------------------------------------
# On when asked
# --------------------------------------------------------------------------

def test_the_dns_cache_reaches_a_spawned_worker(tmp_path) -> None:
    seen = child_sees(tmp_path, OD_DNS_CACHE="1")

    assert seen["getaddrinfo"] == "opendinov3.net.dns_cache", (
        "the worker resolves names without the cache; the patch stopped at "
        "the parent, which downloads nothing")


def test_connection_pooling_reaches_a_spawned_worker(tmp_path) -> None:
    """This is the case that shipped broken."""
    seen = child_sees(tmp_path, OD_HTTP_POOL="1")

    assert seen["download_image"] == "opendinov3.net.pooled_download", (
        "the worker downloads with upstream's per-image connection; the "
        "patch stopped at the parent")


def test_both_can_be_on_at_once(tmp_path) -> None:
    seen = child_sees(tmp_path, OD_DNS_CACHE="1", OD_HTTP_POOL="1")

    assert seen["getaddrinfo"] == "opendinov3.net.dns_cache"
    assert seen["download_image"] == "opendinov3.net.pooled_download"


def test_each_switch_is_independent(tmp_path) -> None:
    """DNS saturation and connection exhaustion are different limits and were
    hit at different node counts. Tying them together would make one
    untestable without the other."""
    seen = child_sees(tmp_path, OD_DNS_CACHE="1", OD_HTTP_POOL="0")

    assert seen["getaddrinfo"] == "opendinov3.net.dns_cache"
    assert seen["download_image"] == "img2dataset.downloader"


# --------------------------------------------------------------------------
# It must not be able to break the interpreter
# --------------------------------------------------------------------------

def test_a_bad_setting_is_reported_and_does_not_kill_the_worker(tmp_path
                                                                ) -> None:
    """sitecustomize runs before anything else. Raising there takes down
    every worker in the wave, so a bad value must degrade to the upstream
    behaviour — loudly, because a silent failure is how the pooling no-op
    survived review.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    result = subprocess.run(
        [sys.executable, str(probe)], capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(SRC),
             "OD_DNS_CACHE": "1", "OD_DNS_CACHE_TTL": "not-a-number"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OD_DNS_CACHE_TTL" in result.stderr, result.stderr


@pytest.mark.parametrize("value", ["", "0", "no", "false"])
def test_only_an_explicit_one_turns_it_on(tmp_path, value) -> None:
    """`OD_BLUR_FACES` is validated to 0 or 1 and refuses anything else,
    because a typo there is irreversible. These are reversible, so they
    default off instead of refusing — but not by accident."""
    seen = child_sees(tmp_path, OD_DNS_CACHE=value)
    assert seen["getaddrinfo"] == "socket"
