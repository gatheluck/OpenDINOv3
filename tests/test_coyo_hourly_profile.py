"""COYO preparation cannot silently inherit DataComp paths or blur policy."""

import os
import subprocess
from pathlib import Path

import pytest

PROFILE = Path(__file__).resolve().parents[1] / "config/coyo-hourly.env.sh"
PATHS = {
    key: "/production/coyo/" + key
    for key in ("OD_PLAN", "OD_META_ROOT", "OD_TASK_ROOT", "OD_SIF", "OD_LOGDIR")
}


def load(env):
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" || exit $?; source "$1" || exit $?; env',
            "profile",
            str(PROFILE),
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_explicit_policy_and_paths_preserved():
    expected = {
        "OD_PROD_WALLTIME": "01:00:00",
        "OD_PROCESSES": "4",
        "OD_THREADS": "32",
        "OD_SAMPLES_PER_SHARD": "10000",
        "OD_MAX_URLS": "0",
        "OD_DNS_BUDGET": "1",
        "OD_HTTP_POOL": "1",
        "OD_DNS_CACHE": "0",
        "OD_RETRIES": "2",
        "OD_TIMEOUT": "10",
        "OD_BLUR_FACES": "0",
    }
    env = {
        **os.environ,
        **dict.fromkeys(expected, "999"),
        **PATHS,
        "OD_BLUR_FACES": "0",
    }
    result = load(env)
    assert result.returncode == 0, result.stderr
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    assert {k: values[k] for k in expected} == expected
    assert {k: values[k] for k in PATHS} == PATHS


@pytest.mark.parametrize("blur", [None, "1", "bad"])
def test_unapproved_or_unsupported_blur_refused(blur):
    env = {**os.environ, **PATHS}
    env.pop("OD_BLUR_FACES", None)
    if blur is not None:
        env["OD_BLUR_FACES"] = blur
    assert load(env).returncode == 2


@pytest.mark.parametrize("missing", list(PATHS))
def test_missing_explicit_path_refused(missing):
    env = {
        **os.environ,
        **PATHS,
        "OD_BLUR_FACES": "0",
        "OD_OUT_ROOT": "/production/datacomp",
    }
    env.pop(missing, None)
    assert load(env).returncode == 2
