"""Contract for reusing HTTP connections across a worker's URLs.

img2dataset opens a fresh TCP connection and TLS session for every image and
closes it immediately: `download_image` calls `urllib.request.urlopen` per
row, with no pool. At 1,024 threads a node therefore creates 1,024 connection
setups continuously, and every one of them is a separate entry in whatever
stateful device sits between the cluster and the internet.

That is the measured failure. A wave at 20 nodes returned `Network is
unreachable` for 35.3% of attempts and `timed out` for 38.5%, with DNS
normal at 6.1% and the 400 Gbps link at 0.005% utilisation. Connection
*count*, not bandwidth, is what ran out.

Reuse attacks that directly: URLs sharing a host cost one setup instead of N.

WHAT THIS MUST NOT CHANGE

The replacement is only worth having if everything downstream still works,
and two things break silently if they are not pinned:

- **Failure messages.** download_stats classifies by matching `[Errno 101]`,
  `HTTP Error <ddd>` and `timed out`. urllib raises HTTPError for 4xx/5xx;
  urllib3 returns a response object and raises nothing. Get that wrong and
  every 403 lands in `other`, `od.sh slow` reports a failure mix that is not
  real, and the number we tune on is fiction.
- **X-Robots-Tag.** A publisher's opt-out. Dropping it would be a compliance
  regression that no throughput measurement would reveal.

The User-Agent keeps img2dataset's token. Site operators use it to identify
and block this traffic; removing it to raise the success rate would be
evading a control that is theirs to set.
"""

from __future__ import annotations

import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from opendinov3.core import download_stats as ds
from opendinov3.net import pooled_download as pd

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 512 + b"\xff\xd9"


class _Handler(BaseHTTPRequestHandler):
    # Keep-alive needs HTTP/1.1; under 1.0 every response closes the socket
    # and the test could not tell reuse from its absence.
    protocol_version = "HTTP/1.1"

    def setup(self):   # once per CONNECTION, not per request
        super().setup()
        type(self).connections += 1

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", headers=()):
        self.send_response(code)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/forbidden"):
            self._send(403)
        elif self.path.startswith("/robots"):
            self._send(200, JPEG, [("X-Robots-Tag", "noai")])
        elif self.path.startswith("/moved"):
            self._send(302, b"", [("Location", "/ok0.jpg")])
        elif self.path.startswith("/boom"):
            self._send(500)
        elif self.path.startswith("/slow"):
            # Longer than any timeout the tests ask for, so the timeout case
            # is deterministic. A tiny timeout against loopback is not: the
            # response can beat it, and the test then fails at random.
            time.sleep(3)
            self._send(200, JPEG)
        else:
            self._send(200, JPEG)


class _QuietServer(ThreadingHTTPServer):
    """Abandoning a connection without reading the body is what the client
    does on a 403, deliberately. The server logging a broken pipe for it is
    expected, and a traceback per occurrence would bury a real failure in
    the CI output.

    `handle_error` belongs to the server, not the handler — put on the
    handler it is never called and the output stays noisy.
    """

    def handle_error(self, request, client_address):
        pass


@pytest.fixture
def server():
    _Handler.connections = 0
    httpd = _QuietServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", _Handler
    finally:
        httpd.shutdown()
        httpd.server_close()


def fetch(url, timeout=10, token="img2dataset", disallowed=None):
    return pd.download_image((0, url), timeout, token, disallowed or [])


# --------------------------------------------------------------------------
# The contract download_image already has
# --------------------------------------------------------------------------

def test_a_fetched_image_comes_back_as_a_stream(server) -> None:
    base, _ = server
    key, stream, err = fetch(f"{base}/ok0.jpg")

    assert err is None, err
    assert key == 0
    assert isinstance(stream, io.BytesIO)
    assert stream.getvalue() == JPEG


def test_a_refused_host_reports_the_error_and_no_stream() -> None:
    # Port 1 on loopback: nothing listens, so this fails at connect.
    key, stream, err = fetch("http://127.0.0.1:1/x.jpg", timeout=5)

    assert stream is None
    assert key == 0
    assert err, "a failure must carry a message; the stats are built from it"


# --------------------------------------------------------------------------
# The point: one connection, many images
# --------------------------------------------------------------------------

def test_images_from_one_host_share_a_connection(server) -> None:
    """The whole reason this module exists.

    Ten images from one host must not cost ten TCP setups. Counted at the
    server, because that is where a connection is real — the client's own
    bookkeeping would pass even if urllib3 were opening a socket per call.
    """
    base, handler = server
    for i in range(10):
        _, stream, err = fetch(f"{base}/ok{i}.jpg")
        assert err is None, err
        assert stream is not None

    # Measured at 1 for fifty sequential images on one host. Two allows for a
    # single reconnect; anything looser would pass on barely any reuse at all,
    # which is the failure this test exists to catch.
    assert handler.connections <= 2, (
        f"{handler.connections} connections for 10 images: not being reused")


def test_a_failed_url_does_not_cost_the_host_its_connection(server) -> None:
    """A 403 returns before the body is read, so the socket is not freed by
    the read that frees it on the success path.

    Left unreleased it is discarded, and the next image from that host pays
    for a new connection. At the measured 4.2% permanent-failure rate that
    is a steady leak of exactly the resource that ran out.
    """
    base, handler = server
    for i in range(10):
        fetch(f"{base}/forbidden{i}.jpg")
        _, stream, err = fetch(f"{base}/ok{i}.jpg")
        assert err is None, err

    assert handler.connections <= 2, (
        f"{handler.connections} connections for 20 fetches: failures are "
        "throwing the connection away")


def test_the_pool_keeps_enough_hosts_to_matter() -> None:
    """urllib3's default is ten host pools, evicted LRU.

    A corpus drawn from CommonCrawl touches far more hosts than that, so the
    default would evict a host's pool long before its next URL came round and
    reuse would never happen — while every unit test above still passed,
    because they use a single host.
    """
    assert pd.NUM_POOLS >= 1000, pd.NUM_POOLS


# --------------------------------------------------------------------------
# What breaks silently: the failure messages the stats are built from
# --------------------------------------------------------------------------

def test_an_http_error_classifies_as_permanent(server) -> None:
    """urllib raises HTTPError; urllib3 returns a response and raises nothing.

    If the message does not carry `HTTP Error 403`, download_stats files it
    under `other` and the failure mix that we tune concurrency on is wrong.
    """
    base, _ = server
    _, stream, err = fetch(f"{base}/forbidden.jpg")

    assert stream is None
    assert "HTTP Error 403" in err, err
    counts = ds.classify({err: 1})
    assert counts.permanent == 1, (err, counts)


def test_a_server_error_classifies_as_transient(server) -> None:
    base, _ = server
    _, stream, err = fetch(f"{base}/boom.jpg")

    assert stream is None
    assert "HTTP Error 500" in err, err
    assert ds.classify({err: 1}).transient == 1, err


def test_a_refused_connection_is_not_filed_as_permanent() -> None:
    """It is a network failure, and the wave is judged on that distinction."""
    _, _, err = fetch("http://127.0.0.1:1/x.jpg", timeout=5)

    counts = ds.classify({err: 1})
    assert counts.permanent == 0, (err, counts)


def test_a_timeout_says_so_in_the_words_the_classifier_reads(server) -> None:
    """`timed out` is matched as a substring, and nothing else marks a
    timeout. A urllib3 message that phrased it differently would move 38.5%
    of a wave's failures into `other`."""
    base, _ = server
    # The endpoint sleeps for three seconds; half a second cannot reach it.
    _, stream, err = fetch(f"{base}/slow.jpg", timeout=0.5)

    assert stream is None
    assert "timed out" in err.lower(), err


# --------------------------------------------------------------------------
# Behaviour that must survive the swap
# --------------------------------------------------------------------------

def test_a_publishers_opt_out_is_still_honoured(server) -> None:
    """X-Robots-Tag is theirs to set. The image arrives and is refused."""
    base, _ = server
    _, stream, err = fetch(f"{base}/robots.jpg", disallowed=["noai"])

    assert stream is None
    assert "disallowed" in err.lower(), err


def test_an_opt_out_we_were_not_asked_to_respect_is_not_invented(server
                                                                 ) -> None:
    base, _ = server
    _, stream, err = fetch(f"{base}/robots.jpg", disallowed=[])
    assert err is None and stream is not None


def test_a_redirect_is_followed(server) -> None:
    """urllib follows redirects; urllib3 only does when its Retry says so.

    Turning retries off — which the measured setting requires — switches
    redirects off with them unless they are configured back on. Image URLs
    redirect often, and the loss would look like an ordinary yield drop.
    """
    base, _ = server
    _, stream, err = fetch(f"{base}/moved.jpg")

    assert err is None, err
    assert stream.getvalue() == JPEG


def test_a_failure_is_attempted_exactly_once(server) -> None:
    """Retries stay img2dataset's business.

    `retries 0` was chosen by measurement — 3.31x the throughput of
    `retries 2` for 0.2pt of yield. A pool that silently retried would undo
    that and the arms comparison would no longer describe the production run.
    """
    base, handler = server
    before = handler.connections
    fetch(f"{base}/boom.jpg")

    assert handler.connections - before <= 1, (
        "the failed fetch was attempted more than once")


# --------------------------------------------------------------------------
# Installing it
# --------------------------------------------------------------------------

def test_installing_replaces_the_downloader_img2dataset_calls() -> None:
    """`download_image_with_retry` resolves `download_image` from module
    globals at call time, so replacing the attribute is enough — but only if
    the name matches exactly."""
    import img2dataset.downloader as dl

    original = dl.download_image
    try:
        pd.install()
        assert dl.download_image is pd.download_image
    finally:
        dl.download_image = original
