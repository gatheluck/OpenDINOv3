"""Offline failure evidence is aggregate-only and never invents root causes."""

import pytest

from opendinov3.net import dns_report


def test_split_timeout_phases_and_temporary_dns_without_exposing_urls():
    report = dns_report.failure_phases(
        {
            "success": 20,
            "Max retries exceeded with url: /private (Caused by NameResolutionError('[Errno -3] Temporary failure in name resolution'))": 3,
            "Max retries exceeded (Caused by ConnectTimeoutError('timed out'))": 4,
            "Max retries exceeded (Caused by ReadTimeoutError('Read timed out'))": 5,
            "HTTP body transfer timed out": 6,
            "[Errno -2] Name or service not known": 7,
            "HTTP Error 404: Not Found": 8,
            "HTTP Error 429: Too Many Requests": 2,
            "unknown": 1,
        }
    )
    assert report == {
        "success": 20,
        "dns_temporary": 3,
        "dns_name_unknown": 7,
        "connect_timeout": 4,
        "read_timeout": 5,
        "transfer_deadline": 6,
        "http_404": 8,
        "http_429": 2,
        "other": 1,
    }


@pytest.mark.parametrize("count", [-1, True, 1.5])
def test_malformed_counts_are_rejected(count):
    with pytest.raises(ValueError):
        dns_report.failure_phases({"success": count})


def test_url_error_text_is_ignored_when_a_wrapped_cause_is_present():
    assert dns_report.failure_phases(
        {
            "url: /[Errno -3] (Caused by SSLError('certificate verify failed'))": 2,
        }
    ) == {"other": 2}
