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


def write_plan(env, tasks: int = 8) -> Path:
    """A plan submit_production.sh will accept: its range check refuses a
    --to outside the plan, which is correct and would otherwise mask what
    these tests are about."""
    import json
    plan = Path(env["OD_OUT_ROOT"]) / "production" / "plan.json"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(json.dumps({"urls_per_task": 1, "total_rows": tasks,
                                "tasks": [{"task_id": i, "rows": 1,
                                           "pieces": []}
                                          for i in range(tasks)]}))
    # submit_production.sh refuses a wave that has not stated face
    # blurring, because the generated job script is the only channel to the
    # compute node. These tests are about path derivation, so state it.
    env["OD_BLUR_FACES"] = "1"
    return plan


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


def test_verify_reaches_the_existing_shards(env) -> None:
    """The comparison runs over the predecessor's downloaded tree, which is
    bound read-only, and against the baseline the resolution run wrote."""
    result = run(env, "--dry-run", "verify")
    assert result.returncode == 0, result.stderr
    assert "verify_recorded_sizes.py" in result.stdout
    assert "/corpus/datacomp/datacomp_1b/raw_shards" in result.stdout
    assert "resolution.json" in result.stdout


def test_submit_derives_the_plan_and_metadata_paths(env, tmp_path) -> None:
    """The raw invocation needs OD_PLAN and OD_META_ROOT inline, which comes
    to 152 characters — past the paste-safety limit and straight into the
    failure mode this wrapper exists to prevent."""
    write_plan(env)
    result = run(env, "--dry-run", "submit", "--from", "0", "--to", "7")
    assert "plan.json" in result.stdout, result.stdout + result.stderr
    assert "upstream_metadata" in result.stdout


def test_submit_does_not_go_through_the_container(env, tmp_path) -> None:
    """qsub does not exist inside the image. Routing the submitter through
    singularity would fail on the login node, after the operator believed
    the wave had gone in."""
    write_plan(env)
    result = run(env, "--dry-run", "submit", "--from", "0", "--to", "7")
    assert "singularity" not in result.stdout.split("submit_production")[0]


def test_a_dry_run_submit_really_is_a_rehearsal(env) -> None:
    """od.sh strips --dry-run before dispatch. Not forwarding it would make
    a rehearsal put a wave in the queue."""
    write_plan(env)
    result = run(env, "--dry-run", "submit", "--from", "0", "--to", "7")
    assert "not submitting" in result.stdout, result.stdout + result.stderr


def test_submit_without_a_plan_says_how_to_make_one(env) -> None:
    result = run(env, "submit", "--from", "0", "--to", "7")
    assert result.returncode != 0
    assert "od.sh plan" in result.stderr


def test_report_reaches_our_own_task_root(env) -> None:
    """The pilot report reads what we produced, under our own account, not
    the predecessor's tree."""
    result = run(env, "--dry-run", "report")
    assert result.returncode == 0, result.stderr
    assert "inspect_pilot.py" in result.stdout
    assert "/out/datacomp/datacomp_1b/raw_shards" in result.stdout
    assert "pilot_report.json" in result.stdout


def test_slow_reaches_our_own_task_root(env) -> None:
    """Every other subcommand has a dispatch test; this one did not, and
    was shipped without one."""
    result = run(env, "--dry-run", "slow", "--node-hours", "15.7")
    assert result.returncode == 0, result.stderr
    assert "diagnose_throughput.py" in result.stdout
    assert "/out/datacomp/datacomp_1b/raw_shards" in result.stdout
    assert "--node-hours 15.7" in result.stdout


@pytest.mark.parametrize("name", [
    "inspect", "resolution", "verify", "plan", "report", "slow", "submit",
])
def test_every_advertised_subcommand_is_dispatchable(env, name) -> None:
    """The usage text and the dispatcher must not drift apart. `slow` was
    added to both, but nothing would have noticed if it had reached only
    one of them."""
    write_plan(env)
    result = run(env, "--dry-run", name, "--from", "0", "--to", "0")
    combined = result.stdout + result.stderr
    assert "unknown subcommand" not in combined, combined


def test_experiment_submits_four_arms_not_a_production_wave(env) -> None:
    """The arms are numbered 1..4 and map to tasks 8..11 through the offset
    experiment_0004_job.sh sets. Submitting -J 9-12 instead would run four
    ordinary tasks with the default settings and measure nothing."""
    write_plan(env, tasks=1388)
    result = run(env, "--dry-run", "experiment")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "-J 1-4" in result.stdout, result.stdout
    assert "experiment_0004_job.sh" in result.stdout


def test_experiment_still_range_checks_against_the_plan(env) -> None:
    """The arms need tasks 8..11 to exist. A plan too short would otherwise
    only fail on the node, after the queue wait."""
    write_plan(env, tasks=4)
    # --dry-run: submit_production.sh checks for a submitter before it
    # range-checks the plan, and this fixture configures none.
    result = run(env, "--dry-run", "experiment")
    assert result.returncode != 0
    assert "outside it" in result.stdout + result.stderr


def test_a_production_wave_still_uses_the_production_body(env) -> None:
    """The override must not leak into ordinary submissions."""
    write_plan(env, tasks=1388)
    result = run(env, "--dry-run", "submit", "--from", "0", "--to", "7")
    assert "experiment_0004_job.sh" not in result.stdout
    assert "-J 1-8" in result.stdout


def test_the_experiment_asks_for_a_walltime_its_arms_can_use(env) -> None:
    """An arm is 100,000 URLs — 48 minutes at the measured rate. Asking for
    the production default of 12 h makes it harder to schedule and more
    likely to hit the end of the reservation, for nothing."""
    write_plan(env, tasks=1388)
    result = run(env, "--dry-run", "experiment")
    assert "walltime   : 02:00:00" in result.stdout, result.stdout


def test_the_dry_run_shows_what_each_arm_will_do(env) -> None:
    """Four arms are being compared. A rehearsal that does not say what
    they differ in cannot be checked before two hours are spent."""
    write_plan(env, tasks=1388)
    result = run(env, "--dry-run", "experiment")
    for fragment in ("OD_THREADS=32", "OD_THREADS=128",
                     "OD_RETRIES=2", "OD_RETRIES=0"):
        assert fragment in result.stdout, (fragment, result.stdout)


def test_arms_reaches_our_own_task_root(env) -> None:
    result = run(env, "--dry-run", "arms")
    assert result.returncode == 0, result.stderr
    assert "compare_arms.py" in result.stdout
    assert "/out/datacomp/datacomp_1b/raw_shards" in result.stdout


def test_salvage_reaches_the_named_tasks(env) -> None:
    root = f"{env['OD_OUT_ROOT']}/datacomp/datacomp_1b/raw_shards"
    result = run(env, "--dry-run", "salvage",
                 f"{root}/task-000016", f"{root}/task-000017")
    assert result.returncode == 0, result.stderr
    assert "salvage_task.py" in result.stdout
    assert "/out/datacomp/datacomp_1b/raw_shards/task-000016" in result.stdout
    assert "task-000017" in result.stdout


def test_salvage_without_a_task_is_refused(env) -> None:
    assert run(env, "salvage").returncode != 0
