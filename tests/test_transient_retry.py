"""Exercise img2dataset's installed retry entry point, not a helper name."""

import io
import socket
from types import SimpleNamespace

import pytest
import urllib3

from opendinov3.net import pooled_download as pd


def failure_message(monkeypatch, exception):
    def request(*args, **kwargs):
        raise exception

    with monkeypatch.context() as patch:
        patch.setattr(pd, "manager", lambda: SimpleNamespace(request=request))
        return pd.download_image((7, "http://example.org"), 10, None, [])[2]


def test_connection_refusal_is_not_misclassified_as_timeout(monkeypatch):
    from img2dataset import downloader as dl

    exception = urllib3.exceptions.NewConnectionError(None, "Connection refused")
    exception.__cause__ = ConnectionRefusedError(111, "Connection refused")
    error = failure_message(monkeypatch, exception)
    monkeypatch.setattr(dl, "download_image", dl.download_image)
    monkeypatch.setattr(dl, "download_image_with_retry", dl.download_image_with_retry)
    pd.install()
    monkeypatch.setattr(dl, "download_image", lambda *args: (7, None, error))
    delays = []
    monkeypatch.setattr(pd.time, "sleep", delays.append)
    dl.download_image_with_retry((7, "http://example.org"), 10, 2, None, [])
    assert delays == []


@pytest.mark.parametrize(
    "error",
    [
        socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
        urllib3.exceptions.ReadTimeoutError(
            None, "http://example.org", "Read timed out"
        ),
        urllib3.exceptions.ConnectTimeoutError("Connection timed out"),
        TimeoutError("HTTP body transfer timed out"),
    ],
)
def test_recovery_waits_between_existing_attempts(monkeypatch, error):
    error = failure_message(monkeypatch, error)
    from img2dataset import downloader as dl

    monkeypatch.setattr(dl, "download_image", dl.download_image)
    monkeypatch.setattr(dl, "download_image_with_retry", dl.download_image_with_retry)
    pd.install()
    events = []

    def attempt(*args):
        events.append("attempt")
        if events.count("attempt") < 3:
            return 7, None, error
        return 7, io.BytesIO(b"image"), None

    monkeypatch.setattr(dl, "download_image", attempt)
    monkeypatch.setattr(pd.time, "sleep", lambda seconds: events.append(seconds))
    key, stream, err = dl.download_image_with_retry(
        (7, "http://example.org"), 10, 2, "token", []
    )
    assert events == ["attempt", 0.5, "attempt", 1.0, "attempt"]
    assert key == 7 and stream.getvalue() == b"image" and err is None


@pytest.mark.parametrize(
    "retries,expected",
    [(0, ["attempt"]), (2, ["attempt", 0.5, "attempt", 1.0, "attempt"])],
)
def test_exhaustion_does_not_sleep_after_last_attempt(monkeypatch, retries, expected):
    from img2dataset import downloader as dl

    monkeypatch.setattr(dl, "download_image", dl.download_image)
    monkeypatch.setattr(dl, "download_image_with_retry", dl.download_image_with_retry)
    pd.install()
    events = []
    error = failure_message(
        monkeypatch,
        socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
    )

    def attempt(*args):
        events.append("attempt")
        return 7, None, error

    monkeypatch.setattr(dl, "download_image", attempt)
    monkeypatch.setattr(pd.time, "sleep", lambda seconds: events.append(seconds))
    assert dl.download_image_with_retry(
        (7, "http://example.org"), 10, retries, "token", []
    ) == (7, None, error)
    assert events == expected


@pytest.mark.parametrize(
    "error",
    [
        "[Errno -2] Name or service not known",
        "HTTP Error 404: Not Found",
        "HTTP Error 403: Forbidden",
        "HTTP Error 429: Too Many Requests",
        "certificate verify failed",
        "Use of image disallowed by X-Robots-Tag directive",
    ],
)
def test_other_failures_keep_upstream_attempt_policy(monkeypatch, error):
    from img2dataset import downloader as dl

    monkeypatch.setattr(dl, "download_image", dl.download_image)
    monkeypatch.setattr(dl, "download_image_with_retry", dl.download_image_with_retry)
    pd.install()
    calls = []
    monkeypatch.setattr(
        dl, "download_image", lambda *args: calls.append(args) or (7, None, error)
    )
    delays = []
    monkeypatch.setattr(pd.time, "sleep", delays.append)
    args = ((7, "http://example.org"), 10, "token", ["noai"])
    assert dl.download_image_with_retry(args[0], 10, 2, args[2], args[3]) == (
        7,
        None,
        error,
    )
    assert calls == [args] * 3
    assert delays == []


def test_success_has_no_delay_or_additional_attempt(monkeypatch):
    from img2dataset import downloader as dl

    monkeypatch.setattr(dl, "download_image", dl.download_image)
    monkeypatch.setattr(dl, "download_image_with_retry", dl.download_image_with_retry)
    pd.install()
    calls = []
    stream = io.BytesIO(b"image")
    monkeypatch.setattr(
        dl, "download_image", lambda *args: calls.append(args) or (7, stream, None)
    )
    delays = []
    monkeypatch.setattr(pd.time, "sleep", delays.append)
    assert dl.download_image_with_retry((7, "http://example.org"), 10, 2, None, []) == (
        7,
        stream,
        None,
    )
    assert len(calls) == 1 and delays == []


def test_url_text_cannot_turn_permanent_dns_into_transient(monkeypatch):
    from img2dataset import downloader as dl

    reason = urllib3.exceptions.NameResolutionError(
        "example.org",
        None,
        socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
    )
    reason.__cause__ = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    error = failure_message(
        monkeypatch,
        urllib3.exceptions.MaxRetryError(
            None,
            "http://example.org/[Errno -3]/read timeout=10",
            reason,
        ),
    )
    monkeypatch.setattr(dl, "download_image", dl.download_image)
    monkeypatch.setattr(dl, "download_image_with_retry", dl.download_image_with_retry)
    pd.install()
    monkeypatch.setattr(dl, "download_image", lambda *args: (7, None, error))
    delays = []
    monkeypatch.setattr(pd.time, "sleep", delays.append)
    dl.download_image_with_retry((7, "http://example.org"), 10, 2, None, [])
    assert delays == []
