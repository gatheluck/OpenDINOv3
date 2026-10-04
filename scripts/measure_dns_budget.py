#!/usr/bin/env python3
"""Run one existing job command and save numeric DNS gate utilization."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from opendinov3.core.dns_measurement import measure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a command after -- is required')
    report = measure(command, args.state, args.output)
    return report['command_exit'] if report['command_exit'] >= 0 else 128 - report['command_exit']


if __name__ == '__main__':
    sys.exit(main())
