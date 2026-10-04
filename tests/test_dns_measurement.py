import json
import sys
from pathlib import Path

import pytest

from opendinov3.core.dns_measurement import measure


@pytest.mark.parametrize('exit_code', [0, 7])
def test_real_child_gate_delta_and_failed_command_are_reported(tmp_path, monkeypatch, exit_code):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'qps': 50, 'write_attempts': 123}))
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1] / 'src'))
    command = [sys.executable, '-c',
               ('import sys; from opendinov3.net.dns_budget import Budget; '
               'gate=Budget(sys.argv[1]); '
               '[gate.send(lambda: None) for _ in range(3)]; '
               'sys.exit(int(sys.argv[2]))'), str(state), str(exit_code)]
    output = tmp_path / 'report.json'
    result = measure(command, state, output)
    assert result['gate_attempt_delta'] == 3
    assert result['command_exit'] == exit_code
    assert result['elapsed_seconds'] >= 0.06
    assert result['gate_attempts_per_second'] == pytest.approx(3 / result['elapsed_seconds'])
    assert json.loads(output.read_text()) == result
    assert json.loads(state.read_text())['write_attempts'] == 126


@pytest.mark.parametrize('value', [{'qps': 80, 'write_attempts': 1},
                                  {'qps': 50, 'write_attempts': -1},
                                  {'qps': 50, 'write_attempts': 1.5}])
def test_invalid_or_different_budget_refuses_command(tmp_path, value):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps(value))
    marker = tmp_path / 'command-ran'
    with pytest.raises(ValueError):
        measure([sys.executable, '-c', 'import sys; open(sys.argv[1],"w").close()',
                 str(marker)], state, tmp_path / 'report.json')
    assert not marker.exists()


def test_counter_reset_is_not_misreported_as_low_traffic(tmp_path):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'qps': 50, 'write_attempts': 20}))
    output = tmp_path / 'report.json'
    with pytest.raises(ValueError):
        measure([sys.executable, '-c',
                 'import sys,json; json.dump({"qps":50,"write_attempts":2},open(sys.argv[1],"w"))',
                 str(state)], state, output)
    assert not output.exists()


def test_existing_report_is_not_overwritten_or_rerun(tmp_path):
    output = tmp_path / 'report.json'
    output.write_text('preserve')
    with pytest.raises(FileExistsError):
        measure([sys.executable, '-c', 'raise RuntimeError("must not run")'],
                tmp_path / 'state.json', output)
    assert output.read_text() == 'preserve'


def test_cli_creates_new_gate_and_preserves_child_exit(tmp_path):
    import subprocess

    script = Path(__file__).resolve().parents[1] / 'scripts/measure_dns_budget.py'
    state, output = tmp_path / 'new-state.json', tmp_path / 'report.json'
    child = [sys.executable, '-c',
             'import json,sys; json.dump({"qps":50,"write_attempts":3},open(sys.argv[1],"w")); sys.exit(7)',
             str(state)]
    result = subprocess.run([sys.executable, str(script), '--state', str(state),
                             '--output', str(output), '--', *child], check=False,
                            capture_output=True, text=True)
    assert result.returncode == 7, result.stderr
    assert json.loads(output.read_text())['gate_attempt_delta'] == 3


def test_missing_post_run_state_is_not_reported_as_zero_dns(tmp_path):
    output = tmp_path / 'report.json'
    with pytest.raises(FileNotFoundError):
        measure([sys.executable, '-c', 'pass'], tmp_path / 'missing', output)
    assert not output.exists()


def test_claim_prevents_repeating_an_unfinished_measurement(tmp_path):
    output = tmp_path / 'report.json'
    output.with_suffix('.json.claim').touch()
    marker = tmp_path / 'ran'
    with pytest.raises(FileExistsError):
        measure([sys.executable, '-c', 'import sys; open(sys.argv[1],"w").close()',
                 str(marker)], tmp_path / 'state', output)
    assert not marker.exists()
