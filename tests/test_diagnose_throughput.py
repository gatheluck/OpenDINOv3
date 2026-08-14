"""Contract for the slowness diagnosis.

Runs on a wave that is still going or was killed, so it must not need
DONE.json. The one thing it must never do is recommend a shorter timeout
when the timeout is not the cause — that would spend a wave on a change
that fixes nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent / "scripts"
          / "diagnose_throughput.py")


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


def shard(root: Path, task: int, index: int, count: int, successes: int,
          **failures) -> None:
    path = root / f"task-{task:06d}" / "shards" / f"{index:05d}_stats.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    status = {"success": successes, **failures}
    path.write_text(json.dumps({"count": count, "successes": successes,
                                "status_dict": status}))


DNS = "<urlopen error [Errno -2] Name or service not known>"
TIMEOUT = "<urlopen error timed out>"


def test_it_works_on_a_wave_with_no_DONE_json(tmp_path) -> None:
    """The wave being diagnosed was killed at the walltime; nothing in it
    is marked done."""
    shard(tmp_path, 0, 0, 10000, 4500, **{TIMEOUT: 5500})
    assert not list(tmp_path.rglob("DONE.json"))
    assert run(tmp_path, "--node-hours", "1.0").returncode == 0


def test_the_failure_rate_comes_from_the_shards_not_an_assumption(tmp_path
                                                                  ) -> None:
    shard(tmp_path, 0, 0, 10000, 4000, **{TIMEOUT: 6000})
    out = tmp_path / "d.json"
    assert run(tmp_path, "--node-hours", "1.0", "--json", out).returncode == 0
    assert json.loads(out.read_text())["failure_rate"] == 0.6


def test_timeouts_that_explain_the_rate_are_confirmed(tmp_path) -> None:
    """1,024 workers, 16.5 s per URL -> about 62 URLs/s -> 0.0625 node-hours
    for 10,000 URLs. At 55% failures the model predicts ~17 s, which agrees.
    """
    shard(tmp_path, 0, 0, 10000, 4500, **{TIMEOUT: 5500})
    result = run(tmp_path, "--node-hours", str(10000 / 62.0 / 3600))
    assert result.returncode == 0, result.stderr
    assert "is the cause" in result.stdout
    assert "timeout    3s" in result.stdout


def test_a_rate_the_timeout_cannot_explain_is_called_out(tmp_path) -> None:
    """Zero failures but still crawling: the timeout is irrelevant and
    recommending a shorter one would waste a wave."""
    shard(tmp_path, 0, 0, 10000, 10000)
    result = run(tmp_path, "--node-hours", "5.0")
    assert result.returncode == 0, result.stderr
    assert "do NOT explain" in result.stdout
    assert "timeout    3s" not in result.stdout


def test_the_yield_cost_of_a_shorter_timeout_is_declared_unknown(tmp_path
                                                                 ) -> None:
    """Recommending 3 s without measuring success latency could quietly
    convert live servers into failures. The report must say so."""
    shard(tmp_path, 0, 0, 10000, 4500, **{TIMEOUT: 5500})
    result = run(tmp_path, "--node-hours", str(10000 / 62.0 / 3600))
    assert "UNKNOWN" in result.stdout


def test_the_failure_mix_is_broken_down(tmp_path) -> None:
    shard(tmp_path, 0, 0, 10000, 4000, **{DNS: 3000, TIMEOUT: 3000})
    out = tmp_path / "d.json"
    run(tmp_path, "--node-hours", "1.0", "--json", out)
    assert json.loads(out.read_text())["failure_mix"]["dns"] == 3000


def test_several_tasks_are_aggregated(tmp_path) -> None:
    shard(tmp_path, 0, 0, 10000, 5000, **{TIMEOUT: 5000})
    shard(tmp_path, 1, 0, 10000, 5000, **{TIMEOUT: 5000})
    out = tmp_path / "d.json"
    run(tmp_path, "--node-hours", "1.0", "--json", out)
    assert json.loads(out.read_text())["urls"] == 20000


def test_no_completed_shards_is_an_error(tmp_path) -> None:
    assert run(tmp_path, "--node-hours", "1.0").returncode != 0
