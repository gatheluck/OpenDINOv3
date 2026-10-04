"""Counters and summaries describe API calls in available process reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opendinov3.net import dns_cache, dns_report

# --------------------------------------------------------------------------
# The counters
# --------------------------------------------------------------------------


def test_a_hit_and_a_miss_are_counted_separately(monkeypatch) -> None:
    calls = []

    def resolver(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        calls.append(host)
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr("socket.getaddrinfo", resolver)
    dns_cache.install()
    try:
        import socket

        socket.getaddrinfo("a.example", 443)  # miss
        socket.getaddrinfo("a.example", 443)  # hit
        socket.getaddrinfo("b.example", 443)  # miss

        assert dns_cache.stats() == (1, 2), dns_cache.stats()
    finally:
        dns_cache.uninstall()


def test_a_failure_counts_as_a_miss(monkeypatch) -> None:
    """Failed underlying API calls count even without a successful result."""
    import socket

    def resolver(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr("socket.getaddrinfo", resolver)
    dns_cache.install()
    try:
        for _ in range(3):
            with pytest.raises(socket.gaierror):
                socket.getaddrinfo("dead.example", 443)

        assert dns_cache.stats() == (0, 3)
    finally:
        dns_cache.uninstall()


def test_the_report_line_has_one_definition() -> None:
    """Emitted by every worker and parsed back here. Two spellings would
    make the totals silently partial."""
    line = dns_cache.report_line(hits=7, misses=3)

    assert dns_report.parse_line(line) == (7, 3)


# --------------------------------------------------------------------------
# Adding them up over a task
# --------------------------------------------------------------------------


def write_task(tmp_path, log: str, wall: int = 100) -> Path:
    task = tmp_path / "task-000000"
    task.mkdir()
    (task / "img2dataset.log").write_text(log)
    (task / "DONE.json").write_text(
        json.dumps(
            {
                "task_id": 0,
                "wall_seconds": wall,
                "candidates": 1_000_000,
                "successes": 640_000,
            }
        )
    )
    return task


def test_every_available_report_is_counted(tmp_path) -> None:
    """32 processes, recycled every 5 shards, so a task's log holds many."""
    lines = "\n".join(dns_cache.report_line(hits=100, misses=50) for _ in range(40))
    task = write_task(tmp_path, "downloading...\n" + lines + "\nfinished\n")

    summary = dns_report.summarise(task)

    assert summary.hits == 4_000
    assert summary.misses == 2_000
    assert summary.reports == 40


def test_observed_api_calls_are_averaged_over_task_wall_time(tmp_path) -> None:
    """The task average describes observed API calls, not wire DNS traffic."""
    lines = dns_cache.report_line(hits=900, misses=100)
    task = write_task(tmp_path, lines, wall=10)

    assert dns_report.summarise(
        task
    ).observed_resolver_calls_per_second == pytest.approx(10.0)


def test_cache_hit_fraction_is_computed_from_observed_reports(tmp_path) -> None:
    lines = dns_cache.report_line(hits=760, misses=240)
    task = write_task(tmp_path, lines)

    assert dns_report.summarise(task).cache_hit_fraction == pytest.approx(0.76)


def test_a_task_without_reports_is_not_reported_as_zero(tmp_path) -> None:
    """No lines could mean cache off, lost logs, or forced termination."""
    task = write_task(tmp_path, "downloading...\nfinished\n")

    summary = dns_report.summarise(task)
    assert summary.reports == 0
    assert summary.observed_resolver_calls_per_second is None
    assert summary.cache_hit_fraction is None


def test_a_task_with_no_wall_time_reports_counts_but_no_rate(tmp_path) -> None:
    task = tmp_path / "task-000001"
    task.mkdir()
    (task / "img2dataset.log").write_text(dns_cache.report_line(hits=1, misses=1))

    summary = dns_report.summarise(task)
    assert summary.misses == 1
    assert summary.observed_resolver_calls_per_second is None


def test_an_unrelated_log_line_is_not_mistaken_for_a_report(tmp_path) -> None:
    task = write_task(tmp_path, "worker said OD_DNS_CACHE is enabled\n")
    assert dns_report.summarise(task).reports == 0
