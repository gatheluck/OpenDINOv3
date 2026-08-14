"""Contract for the short command wrapper.

Long one-line commands break when pasted. That is not a hypothetical: a
metadata inspection failed because the line split between `--bind ...` and
the image path, leaving `singularity exec` with no container. Every command
handed over should be short enough to survive a paste, which means the binds
and paths belong in a tested script rather than in the message.

Exercised against tests/stubs/singularity, which translates bind paths and
runs the command directly — the same stand-in the 0003 job test uses.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STUBS = Path(__file__).resolve().parent / "stubs"
OD = REPO / "scripts" / "od.sh"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "corpus"
    (root / "datacomp" / "datacomp_1b" / "upstream_metadata").mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    (out / "opendinov3.sif").write_bytes(b"")
    return {
        **os.environ,
        "PATH": f"{STUBS}:{os.environ['PATH']}",
        "OD_ROOT": str(root),
        "OD_OUT_ROOT": str(out),
    }


def run(env, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(OD), *map(str, args)],
                          capture_output=True, text=True, env=env)


def test_a_missing_subcommand_lists_what_is_available(env) -> None:
    result = run(env)
    assert result.returncode != 0
    assert "inspect" in result.stdout + result.stderr


def test_an_unknown_subcommand_is_refused(env) -> None:
    result = run(env, "definitely-not-a-command")
    assert result.returncode != 0


def test_inspect_reaches_the_metadata_directory(env) -> None:
    """The path the operator would otherwise have to type, and mistype."""
    result = run(env, "inspect")
    # No parquet in the fixture, so the script refuses — which proves it ran
    # and was pointed at the right place.
    assert "no parquet files" in result.stderr
    assert "upstream_metadata" in result.stderr


def test_resolution_reaches_the_metadata_directory(env) -> None:
    """Measuring the corpus's resolution needs the same binds and the same
    path, so it goes through the same wrapper rather than a pasted line."""
    result = run(env, "resolution")
    assert "no parquet files" in result.stderr
    assert "upstream_metadata" in result.stderr


def test_resolution_writes_its_answer_where_the_run_is_recorded(env) -> None:
    """A figure quoted in a decision has to be traceable to a file."""
    result = run(env, "--dry-run", "resolution")
    assert result.returncode == 0, result.stderr
    assert "measure_resolution.py" in result.stdout
    assert "resolution.json" in result.stdout


def test_the_environment_must_be_sourced_first(tmp_path) -> None:
    """Without OD_ROOT the wrapper says so instead of failing obscurely."""
    result = subprocess.run(
        ["bash", str(OD), "inspect"], capture_output=True, text=True,
        env={**os.environ, "PATH": f"{STUBS}:{os.environ['PATH']}",
             "OD_ROOT": "", "OD_OUT_ROOT": ""})
    assert result.returncode != 0
    assert "OD_ROOT" in result.stderr


def test_a_missing_image_is_reported_before_anything_runs(env, tmp_path) -> None:
    env = {**env, "OD_SIF": str(tmp_path / "absent.sif")}
    result = run(env, "inspect")
    assert result.returncode != 0
    assert "absent.sif" in result.stderr


def test_exec_passes_arbitrary_scripts_through(env, tmp_path) -> None:
    """Investigation should not need a new subcommand each time — but it
    should still go through the tested bind set."""
    result = run(env, "exec", "inspect_metadata.py", "--help")
    assert result.returncode == 0, result.stderr
    assert "upstream metadata schema" in result.stdout


def test_the_command_it_would_run_can_be_shown_without_running_it(env) -> None:
    result = run(env, "--dry-run", "inspect")
    assert result.returncode == 0
    assert "singularity exec" in result.stdout
    assert "--bind" in result.stdout


def test_the_predecessor_corpus_is_mounted_read_only(env) -> None:
    """It is not ours and must not be written to. A missing `:ro` would let
    any script in scripts/ modify it, and the predecessor is unreachable."""
    out = run(env, "--dry-run", "inspect").stdout
    assert "/corpus:ro" in out, out
    assert "/work:ro" in out, out


def test_our_own_output_is_writable(env) -> None:
    out = run(env, "--dry-run", "inspect").stdout
    assert ":/out " in out + " ", "the output bind must not be read-only"
    assert ":/out:ro" not in out
