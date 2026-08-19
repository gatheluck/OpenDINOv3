"""Fetch images over pooled, reused HTTP connections.

WHY

img2dataset's `download_image` calls `urllib.request.urlopen` once per row
and closes the socket immediately. There is no pool, so every image costs a
fresh TCP handshake and, over https, a fresh TLS handshake. A node running
32 processes x 32 threads is therefore creating 1,024 connections
continuously, and each one is an entry in whatever stateful device sits
between the cluster and the internet.

That is what the measurements say ran out. A production wave at 20 nodes
(20,480 concurrent connections) returned `Network is unreachable` for 35.3%
of attempts and `timed out` for 38.5%, while DNS stayed normal at 6.1% and
the 400 Gbps external link carried 0.005% of its capacity. Cutting to 4
nodes raised the per-node rate 3.3x. Bandwidth was never the limit;
connection count was.

Reuse attacks the mechanism rather than the symptom: URLs that share a host
cost one setup between them instead of one each.

WHAT IS DELIBERATELY UNCHANGED

- The User-Agent still carries img2dataset's token. Operators use it to
  identify and block this traffic, and it is theirs to block.
- Retries stay off. `retries 0` was chosen by measurement (3.31x the
  throughput of `retries 2`, for 0.2pt of yield), and a pool that quietly
  retried would make the arms comparison describe a run that never happened.
- X-Robots-Tag is still parsed by img2dataset's own `is_disallowed`, rather
  than reimplemented here, so a publisher's opt-out cannot drift.
- Failure messages keep the shapes download_stats matches on: `HTTP Error
  <ddd>`, `[Errno N]`, `timed out`. urllib raises HTTPError for 4xx/5xx and
  urllib3 does not, so the status case is rebuilt here on purpose.
"""

from __future__ import annotations

import io
import os
import threading

import urllib3

#: Distinct hosts whose connection pools are kept. urllib3's default is 10,
#: evicted least-recently-used — and a corpus drawn from CommonCrawl touches
#: orders of magnitude more hosts than that, so the default would evict a
#: host's pool long before its next URL arrived and reuse would never happen.
#: Every unit test on a single host would still pass.
NUM_POOLS = int(os.environ.get("OD_HTTP_POOL_HOSTS", "10000"))

#: Connections kept per host. Matching the thread count would let one
#: popular host monopolise the pool; well under it lets many hosts hold a
#: connection each, which is where the reuse is.
MAX_PER_HOST = int(os.environ.get("OD_HTTP_POOL_PER_HOST", "8"))

#: HTTP status codes carried back as an error rather than a stream. Anything
#: outside 2xx has no image in it; img2dataset's urllib path raised HTTPError
#: for these and the classifier still reads that wording.
_OK = range(200, 300)

_manager: urllib3.PoolManager | None = None
_manager_lock = threading.Lock()


def manager() -> urllib3.PoolManager:
    """The process-wide pool.

    One per process, shared by that process's threads: urllib3's PoolManager
    is thread-safe, and a per-thread manager would pool nothing, since each
    thread takes one URL at a time from a queue that is not host-ordered.
    """
    global _manager  # noqa: PLW0603 — one pool per process is the point
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = urllib3.PoolManager(
                    num_pools=NUM_POOLS,
                    maxsize=MAX_PER_HOST,
                    # Past maxsize, hand out a connection anyway and discard
                    # it after use. Blocking would stall a thread behind a
                    # popular host instead of just losing that one's reuse.
                    block=False,
                    retries=_retries(),
                )
    return _manager


def _retries() -> urllib3.Retry:
    """No retries, but redirects still followed.

    `Retry(total=0)` switches off redirects along with retries, because
    urllib3 counts a redirect against the same budget. urllib followed up to
    30 of them, image URLs redirect often, and the loss would have looked
    like an ordinary drop in yield rather than a change we made.
    """
    return urllib3.Retry(
        total=None, connect=0, read=0, status=0, other=0,
        redirect=5, backoff_factor=0,
        raise_on_redirect=False, raise_on_status=False,
    )


class _HeaderView:
    """img2dataset's `is_disallowed` expects `email.message.Message`.

    urllib3 spells the multi-valued lookup `getlist`. Adapting is better than
    reimplementing the directive parsing, which is a compliance rule and
    should have exactly one definition.
    """

    def __init__(self, headers) -> None:
        self._headers = headers

    def get_all(self, name, default=None):
        values = self._headers.getlist(name)
        return values if values else default


def _user_agent(user_agent_token: str | None) -> str:
    """Byte-identical to img2dataset's.

    Not a detail: the token is how a site identifies this traffic in its
    logs and blocks it if it wants to. Dropping it would raise the success
    rate by evading a control that belongs to them.
    """
    agent = ("Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:72.0) "
             "Gecko/20100101 Firefox/72.0")
    if user_agent_token:
        agent += (f" (compatible; {user_agent_token}; "
                  "+https://github.com/rom1504/img2dataset)")
    return agent


def _status_error(status: int, reason: str | None) -> str:
    """The wording urllib's HTTPError produced, because the stats match it.

    download_stats reads `HTTP Error (\\d{3})` to split permanent (403/404)
    from transient (5xx) from rate limited (429). urllib3 raises nothing for
    a status, so without this every one of them would land in `other` and
    the failure mix the concurrency decision rests on would be fiction.
    """
    return f"HTTP Error {status}: {reason or ''}".rstrip()


def download_image(row, timeout, user_agent_token, disallowed_header_directives):
    """Drop-in replacement for `img2dataset.downloader.download_image`.

    Same signature, same three-tuple, same meanings: a stream on success, a
    message on failure, never both.
    """
    import img2dataset.downloader as downloader

    key, url = row
    response = None
    try:
        response = manager().request(
            "GET", url,
            headers={"User-Agent": _user_agent(user_agent_token)},
            timeout=urllib3.Timeout(total=timeout),
            preload_content=False,
        )
        if response.status not in _OK:
            return key, None, _status_error(response.status, response.reason)
        if disallowed_header_directives and downloader.is_disallowed(
            _HeaderView(response.headers),
            user_agent_token,
            disallowed_header_directives,
        ):
            return key, None, "Use of image disallowed by X-Robots-Tag directive"
        return key, io.BytesIO(response.read()), None
    except Exception as err:  # noqa: BLE001 — reported, exactly as upstream
        return key, None, str(err)
    finally:
        # Returns the socket to the pool. Without it the connection is held
        # by a dead response and the next URL for that host opens a new one —
        # reuse would silently never happen.
        if response is not None:
            response.release_conn()


def install() -> None:
    """Point img2dataset at this downloader.

    `download_image_with_retry` looks `download_image` up in its module
    globals at call time, so rebinding the attribute is enough.
    """
    import img2dataset.downloader as downloader

    downloader.download_image = download_image
