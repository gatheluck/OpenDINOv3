"""Observe packets, not configured concurrency or diagnostic strings."""

import multiprocessing
import socket
import threading
import time

import dns.message
import dns.query
import dns.rrset
import pytest


def _send_worker(path, port):
    from opendinov3.net import dns_budget

    dns_budget.install(path, qps=20)
    for _ in range(3):
        dns.query.udp(
            dns.message.make_query("example.test", "A"),
            "127.0.0.1",
            port=port,
            timeout=5,
        )


def test_spawned_workers_share_wire_budget(tmp_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(10)
    times = []

    def server():
        for _ in range(12):
            wire, addr = sock.recvfrom(4096)
            times.append(time.monotonic())
            sock.sendto(
                dns.message.make_response(dns.message.from_wire(wire)).to_wire(), addr
            )

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    ctx = multiprocessing.get_context("spawn")
    children = [
        ctx.Process(
            target=_send_worker, args=(str(tmp_path / "budget"), sock.getsockname()[1])
        )
        for _ in range(4)
    ]
    for child in children:
        child.start()
    for child in children:
        child.join(15)
        assert child.exitcode == 0
    thread.join(1)
    sock.close()
    assert len(times) == 12
    assert times[-1] - times[0] >= 0.50
    assert max(sum(t <= x < t + 0.2 for x in times) for t in times) <= 5


def test_tcp_and_udp_share_gate_and_fail_closed(tmp_path):
    from opendinov3.net import dns_budget

    sends = []

    class FakeSocket:
        def send(self, data):
            sends.append(time.monotonic())
            return len(data)

        def sendto(self, data, address):
            sends.append(time.monotonic())
            return len(data)

    gate = dns_budget.Budget(tmp_path / "budget", qps=20)
    wrapped = dns_budget.BudgetSocket(FakeSocket(), gate)
    wrapped.send(b"tcp")
    wrapped.sendto(b"udp", ("127.0.0.1", 53))
    assert sends[1] - sends[0] >= 0.05
    (tmp_path / "budget").write_text("corrupted")
    with pytest.raises(ValueError):
        wrapped.send(b"blocked")
    assert len(sends) == 2


@pytest.mark.parametrize("qps", [0, -1, 81, float("nan"), float("inf")])
def test_invalid_budget_is_rejected(tmp_path, qps):
    from opendinov3.net import dns_budget

    with pytest.raises(ValueError):
        dns_budget.Budget(tmp_path / "budget", qps=qps)


def test_conflicting_budget_cannot_increase_rate(tmp_path):
    from opendinov3.net import dns_budget

    gate = dns_budget.Budget(tmp_path / "budget", qps=20)
    gate.send(lambda: 1)
    with pytest.raises(ValueError):
        dns_budget.Budget(tmp_path / "budget", qps=40).send(lambda: 1)


def test_required_budget_initialization_failure_stops_interpreter(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [sys.executable, "-c", 'print("UNSAFE_WORK_STARTED")'],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "OD_DNS_BUDGET": "1",
            "OD_DNS_BUDGET_FILE": str(tmp_path / "missing" / "budget"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 78
    assert "UNSAFE_WORK_STARTED" not in result.stdout


def test_required_budget_reaches_new_interpreter(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [sys.executable, "-c", "import socket; print(socket.getaddrinfo.__module__)"],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "OD_DNS_BUDGET": "1",
            "OD_DNS_BUDGET_FILE": str(tmp_path / "budget"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "dns.resolver"


@pytest.mark.parametrize("mode", ["tcp", "retry", "cache", "nxdomain"])
def test_real_resolver_transports_and_errors(tmp_path, mode):
    import dns.flags
    import dns.rcode
    import dns.resolver

    from opendinov3.net import dns_budget

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(0.1)
    tcp = socket.socket()
    tcp.bind(udp.getsockname())
    tcp.listen()
    tcp.settimeout(0.1)
    stop = threading.Event()
    seen = []

    def answer(wire):
        query = dns.message.from_wire(wire)
        response = dns.message.make_response(query)
        if mode == "nxdomain":
            response.set_rcode(dns.rcode.NXDOMAIN)
        else:
            response.answer.append(
                dns.rrset.from_text("example.test.", 60, "IN", "A", "127.0.0.1")
            )
        return response

    def serve_udp():
        while not stop.is_set():
            try:
                wire, addr = udp.recvfrom(4096)
            except TimeoutError:
                continue
            seen.append(("udp", time.monotonic()))
            if mode == "retry" and len(seen) == 1:
                continue
            response = answer(wire)
            if mode == "tcp":
                response.flags |= dns.flags.TC
            udp.sendto(response.to_wire(), addr)

    def serve_tcp():
        while not stop.is_set():
            try:
                conn, _ = tcp.accept()
            except TimeoutError:
                continue
            with conn:
                conn.settimeout(2)
                stream = conn.makefile("rb")
                size = int.from_bytes(stream.read(2), "big")
                wire = stream.read(size)
                seen.append(("tcp", time.monotonic()))
                response = answer(wire).to_wire()
                conn.sendall(len(response).to_bytes(2, "big") + response)

    threads = [threading.Thread(target=serve_udp), threading.Thread(target=serve_tcp)]
    for thread in threads:
        thread.start()
    original_factory = dns.query.socket_factory
    try:
        dns_budget.install(tmp_path / "budget", qps=20)
        resolver = dns.resolver._resolver
        resolver.nameservers = ["127.0.0.1"]
        resolver.port = udp.getsockname()[1]
        resolver.timeout = 0.2
        resolver.lifetime = 3

        def lookup():
            return socket.getaddrinfo(
                "example.test", 80, socket.AF_INET, socket.SOCK_STREAM
            )

        if mode == "nxdomain":
            with pytest.raises(socket.gaierror) as error:
                lookup()
            assert error.value.errno == socket.EAI_NONAME
        else:
            assert lookup()[0][4] == ("127.0.0.1", 80)
            if mode == "cache":
                assert lookup()[0][4] == ("127.0.0.1", 80)
                assert len(seen) == 1
            else:
                assert len(seen) == 2
                assert seen[1][1] - seen[0][1] >= 0.045
                assert [x[0] for x in seen] == (
                    ["udp", "tcp"] if mode == "tcp" else ["udp", "udp"]
                )
    finally:
        dns.resolver.restore_system_resolver()
        dns.query.socket_factory = original_factory
        stop.set()
        for thread in threads:
            thread.join(3)
        udp.close()
        tcp.close()
