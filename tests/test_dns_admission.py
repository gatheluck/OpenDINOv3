"""Admission bounds hostname work before dnspython starts its deadlines."""
import concurrent.futures
import threading
import time

import dns.query
import dns.resolver

from opendinov3.net import dns_budget


def test_installed_resolver_bounds_active_hostname_lookups(tmp_path, monkeypatch):
    active = peak = calls = 0
    lock = threading.Lock()

    def resolve_name(self, *args, **kwargs):
        nonlocal active, peak, calls
        with lock:
            active += 1
            peak = max(peak, active)
            calls += 1
        try:
            time.sleep(0.15)
            return 'answer'
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(dns.resolver.Resolver, 'resolve_name', resolve_name)
    factory = dns.query.socket_factory
    try:
        dns_budget.install(tmp_path / 'budget')
        resolver = dns.resolver._resolver
        with concurrent.futures.ThreadPoolExecutor(32) as pool:
            answers = list(pool.map(lambda _: resolver.resolve_name('example.test'), range(32)))
        assert answers == ['answer'] * 32
        assert calls == 32
        assert 1 < peak <= 16
    finally:
        dns.resolver.restore_system_resolver()
        dns.query.socket_factory = factory


def test_admission_releases_on_exception_and_has_bounded_wait(tmp_path):
    import pytest

    gate = dns_budget.ResolutionAdmission(tmp_path / 'budget', limit=1, wait_seconds=.05)
    with gate.enter(), pytest.raises(TimeoutError), gate.enter():
        pytest.fail('occupied slot admitted another lookup')
    with pytest.raises(ValueError), gate.enter():
        raise ValueError('lookup failed')
    with gate.enter():
        pass


def _admission_worker(path, counters, start):
    gate = dns_budget.ResolutionAdmission(path, limit=2)
    start.wait(5)
    with gate.enter():
        with counters.get_lock():
            counters[0] += 1
            counters[1] = max(counters[1], counters[0])
        time.sleep(.15)
        with counters.get_lock():
            counters[0] -= 1
            counters[2] += 1


def test_admission_limit_is_shared_by_spawned_processes(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context('spawn')
    counters = ctx.Array('i', [0, 0, 0])
    start = ctx.Event()
    children = [ctx.Process(target=_admission_worker,
                            args=(str(tmp_path / 'budget'), counters, start)) for _ in range(6)]
    for child in children:
        child.start()
    start.set()
    for child in children:
        child.join(10)
        assert child.exitcode == 0
    assert counters[:] == [0, 2, 6]


def test_admission_wait_precedes_real_resolver_deadline(tmp_path):
    import contextlib
    import socket

    import dns.rrset

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(('127.0.0.1', 0))
    server.settimeout(3)
    def reply():
        wire, address = server.recvfrom(4096)
        response = dns.message.make_response(dns.message.from_wire(wire))
        response.answer.append(dns.rrset.from_text('example.test.', 60, 'IN', 'A', '127.0.0.1'))
        server.sendto(response.to_wire(), address)
    thread = threading.Thread(target=reply, daemon=True)
    factory = dns.query.socket_factory
    blockers = contextlib.ExitStack()
    gate = dns_budget.ResolutionAdmission(tmp_path / 'budget')
    for _ in range(16):
        blockers.enter_context(gate.enter())
    try:
        dns_budget.install(tmp_path / 'budget')
        resolver = dns.resolver._resolver
        resolver.nameservers = ['127.0.0.1']
        resolver.port = server.getsockname()[1]
        resolver.timeout = .3
        resolver.lifetime = .3
        thread.start()
        release = threading.Timer(.5, blockers.close)
        release.start()
        start = time.monotonic()
        answer = resolver.resolve_name('example.test', socket.AF_INET)
        assert time.monotonic() - start >= .5
        assert list(answer.addresses()) == ['127.0.0.1']
        release.join()
        thread.join(3)
    finally:
        blockers.close()
        dns.resolver.restore_system_resolver()
        dns.query.socket_factory = factory
        server.close()


def _hold_admission(path, ready):
    gate = dns_budget.ResolutionAdmission(path, limit=1)
    with gate.enter():
        ready.set()
        time.sleep(60)


def test_process_death_releases_admission(tmp_path):
    import multiprocessing

    ctx = multiprocessing.get_context('spawn')
    ready = ctx.Event()
    path = str(tmp_path / 'budget')
    child = ctx.Process(target=_hold_admission, args=(path, ready))
    child.start()
    try:
        assert ready.wait(5)
    finally:
        child.terminate()
        child.join(5)
    assert not child.is_alive()
    with dns_budget.ResolutionAdmission(path, limit=1, wait_seconds=.1).enter():
        pass
