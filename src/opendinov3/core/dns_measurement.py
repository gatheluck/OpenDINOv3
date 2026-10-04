"""Read shared gate counters without changing the DNS limit or its state.

Gate attempts include install probes and failed sends; they are not a packet
capture, HTTP request count, or measurement of unrelated software on the node.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import time
from pathlib import Path


def _snapshot(path: Path, *, allow_missing: bool = False) -> tuple[int, float]:
    try:
        handle = path.open()
    except FileNotFoundError:
        if allow_missing:
            return 0, time.monotonic()
        raise
    with handle:
        # Match the writer's lock so the JSON cannot be read mid-rewrite.
        fcntl.flock(handle, fcntl.LOCK_SH)
        data = json.load(handle)
        stamp = time.monotonic()
    count = data.get('write_attempts')
    if data.get('qps') != 50 or type(count) is not int or count < 0:
        raise ValueError('Expected a 50-QPS gate with a nonnegative integer counter')
    return count, stamp


def measure(command: list[str], state_path: Path, output_path: Path) -> dict:
    state_path, output_path = Path(state_path), Path(output_path)
    if output_path.exists():
        raise FileExistsError(output_path)
    before, start = _snapshot(state_path, allow_missing=True)
    # Never run the same measurement twice concurrently, even before its report
    # exists. Retain this claim on failure for explicit operator reconciliation.
    with output_path.with_suffix(output_path.suffix + '.claim').open('x'):
        pass
    completed = subprocess.run(command, check=False)
    after, end = _snapshot(state_path)
    if after < before:
        raise ValueError('DNS gate counter decreased; measurement is invalid')
    elapsed = end - start
    report = {
        'schema_version': 1,
        'command_exit': completed.returncode,
        'gate_qps_limit': 50,
        'gate_attempt_delta': after - before,
        'elapsed_seconds': elapsed,
        'gate_attempts_per_second': (after - before) / elapsed,
        'includes_install_probes_and_failed_sends': True,
        'whole_node_wire_measurement': False,
    }
    # Exclusive creation also protects pre-existing reports from being replaced.
    with output_path.open('x') as handle:
        json.dump(report, handle, indent=2)
        handle.write('\n')
    return report
