"""The profile must replace stale experiment bounds without changing paths."""

import subprocess
from pathlib import Path


def test_profile_replaces_stale_settings_and_keeps_production_paths():
    profile = Path(__file__).resolve().parents[1] / "config/datacomp-hourly.env.sh"
    expected = {
        "OD_PROD_WALLTIME": "01:00:00",
        "OD_PROCESSES": "4",
        "OD_THREADS": "32",
        "OD_SAMPLES_PER_SHARD": "10000",
        "OD_BLUR_FACES": "1",
        "OD_TIMEOUT": "10",
        "OD_RETRIES": "2",
        "OD_MAX_URLS": "0",
        "OD_DNS_BUDGET": "1",
        "OD_HTTP_POOL": "1",
        "OD_DNS_CACHE": "0",
    }
    paths = {
        "OD_PLAN": "/production/new-plan.json",
        "OD_TASK_ROOT": "/production/tasks",
        "OD_SIF": "/production/pinned.sif",
    }
    for _ in range(2):
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; source "$1"; env', "profile", str(profile)],
            check=False,
            capture_output=True,
            text=True,
            env={**dict.fromkeys(expected, "999"), **paths},
        )
        assert result.returncode == 0, result.stderr
        actual = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        assert {k: actual[k] for k in expected} == expected
        assert {k: actual[k] for k in paths} == paths
