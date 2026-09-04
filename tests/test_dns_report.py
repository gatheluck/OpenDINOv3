"""Contract for knowing how many names we actually asked for.

WHY

ABCI asked us to stop: our DNS traffic was affecting other users. The reply
has to say how much we reduced it by, and "76% of lookups repeat, so the
cache should remove them" is a prediction, not a measurement. The cache is
per process and a node runs 32 of them, so the realised figure is lower by
an amount nobody has measured.

The number they care about is queries per second per node. That is
`misses / wall_seconds`, and both come out of a finished task.

WHY IT IS PARSED OUT OF THE LOG

The cache lives in each worker, and workers are spawned and recycled
(`maxtasksperchild=5`), so there is no shared object to read at the end. Each
process reports its own counts as it exits; the totals are the sum.
"""

from __future__ import annotations

import json

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
        socket.getaddrinfo("a.example", 443)   # miss
        socket.getaddrinfo("a.example", 443)   # hit
        socket.getaddrinfo("b.example", 443)   # miss

        assert dns_cache.stats() == (1, 2), dns_cache.stats()
    finally:
        dns_cache.uninstall()


def test_a_failure_counts_as_a_miss(monkeypatch) -> None:
    """It reached the resolver, which is what the count is for. Recording it
    as anything else would understate the load we place on it."""
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

def write_task(tmp_path, log: str, wall: int = 100) -> "Path":
    task = tmp_path / "task-000000"
    task.mkdir()
    (task / "img2dataset.log").write_text(log)
    (task / "DONE.json").write_text(json.dumps({
        "task_id": 0, "wall_seconds": wall, "candidates": 1_000_000,
        "successes": 640_000}))
    return task


def test_every_worker_is_counted(tmp_path) -> None:
    """32 processes, recycled every 5 shards, so a task's log holds many."""
    lines = "\n".join(dns_cache.report_line(hits=100, misses=50)
                      for _ in range(40))
    task = write_task(tmp_path, "downloading...\n" + lines + "\nfinished\n")

    summary = dns_report.summarise(task)

    assert summary.hits == 4_000
    assert summary.misses == 2_000
    assert summary.workers == 40


def test_the_number_the_site_asked_about_is_queries_per_second(tmp_path
                                                               ) -> None:
    """Not hit rate. What reaches their resolver, per node, per second."""
    lines = dns_cache.report_line(hits=900, misses=100)
    task = write_task(tmp_path, lines, wall=10)

    assert dns_report.summarise(task).queries_per_second == pytest.approx(10.0)


def test_the_reduction_is_reported_against_what_would_have_been_asked(tmp_path
                                                                      ) -> None:
    lines = dns_cache.report_line(hits=760, misses=240)
    task = write_task(tmp_path, lines)

    assert dns_report.summarise(task).reduction == pytest.approx(0.76)


def test_a_task_that_ran_without_the_cache_is_not_reported_as_zero(tmp_path
                                                                   ) -> None:
    """No lines means the cache was off, which is a different statement from
    "it made no queries" — and reporting 0 queries/s to the site would be a
    lie in the direction that flatters us."""
    task = write_task(tmp_path, "downloading...\nfinished\n")

    summary = dns_report.summarise(task)
    assert summary.workers == 0
    assert summary.queries_per_second is None
    assert summary.reduction is None


def test_a_task_with_no_wall_time_reports_counts_but_no_rate(tmp_path) -> None:
    task = tmp_path / "task-000001"
    task.mkdir()
    (task / "img2dataset.log").write_text(
        dns_cache.report_line(hits=1, misses=1))

    summary = dns_report.summarise(task)
    assert summary.misses == 1
    assert summary.queries_per_second is None


def test_an_unrelated_log_line_is_not_mistaken_for_a_report(tmp_path) -> None:
    task = write_task(tmp_path, "worker said OD_DNS_CACHE is enabled\n")
    assert dns_report.summarise(task).workers == 0
