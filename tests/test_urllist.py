"""Contract for reading a URL list whose exact format is not known in advance.

The list this experiment slices was produced by someone else. It is named
`.tsv`, which may mean one URL per line or may mean columns with a header.
Guessing wrong is not a visible error: img2dataset would accept whole
tab-separated rows as URLs, fail to fetch every one of them, and report a
yield near zero that looks like a real measurement.

So the format is detected, and refused when it cannot be detected.
"""

from __future__ import annotations

import pytest

from opendinov3.core import urllist


def test_a_plain_list_of_urls_passes_through() -> None:
    lines = ["https://a.example/1.jpg", "https://b.example/2.jpg"]
    assert urllist.extract_urls(lines) == lines


def test_a_single_column_file_with_a_header_drops_the_header() -> None:
    """`url` on its own line is a column name, not something to fetch."""
    lines = ["url", "https://a.example/1.jpg"]
    assert urllist.extract_urls(lines) == ["https://a.example/1.jpg"]


def test_a_tab_separated_file_takes_the_named_column() -> None:
    """The URL is not always the first column, so position is not assumed."""
    lines = [
        "key\turl\twidth",
        "000000001\thttps://a.example/1.jpg\t640",
        "000000002\thttps://b.example/2.jpg\t480",
    ]
    assert urllist.extract_urls(lines) == [
        "https://a.example/1.jpg",
        "https://b.example/2.jpg",
    ]


def test_a_tab_separated_file_without_a_header_is_refused() -> None:
    """Which column holds the URL cannot be inferred from one row.

    Picking the first column that happens to parse as a URL would work on
    the sample and fail on the file.
    """
    lines = ["000000001\thttps://a.example/1.jpg\t640"]
    with pytest.raises(urllist.UrlListFormatError):
        urllist.extract_urls(lines)


def test_a_header_naming_no_url_column_is_refused() -> None:
    lines = ["key\twidth", "000000001\t640"]
    with pytest.raises(urllist.UrlListFormatError):
        urllist.extract_urls(lines)


def test_blank_lines_are_dropped_rather_than_fetched() -> None:
    lines = ["https://a.example/1.jpg", "", "  ", "https://b.example/2.jpg"]
    assert urllist.extract_urls(lines) == [
        "https://a.example/1.jpg",
        "https://b.example/2.jpg",
    ]


def test_rows_missing_the_url_column_are_dropped_not_padded() -> None:
    """A short row is corrupt input; emitting an empty URL would hide it."""
    lines = ["key\turl", "000000001\thttps://a.example/1.jpg", "000000002"]
    assert urllist.extract_urls(lines) == ["https://a.example/1.jpg"]


def test_trailing_newlines_are_stripped() -> None:
    """Read from a file the lines still carry their terminator."""
    lines = ["https://a.example/1.jpg\n", "https://b.example/2.jpg\n"]
    assert urllist.extract_urls(lines) == [
        "https://a.example/1.jpg",
        "https://b.example/2.jpg",
    ]


def test_the_caller_can_override_header_detection() -> None:
    """Detection is a heuristic. A caller who knows the format overrides it.

    Slicing is done in one pass over the whole file precisely so that later
    slices never have to re-run this guess on a line that is not a header.
    """
    lines = ["https://a.example/1.jpg", "https://b.example/2.jpg"]
    assert urllist.extract_urls(lines, has_header=True) == [
        "https://b.example/2.jpg"
    ]


def test_an_empty_input_is_empty_not_an_error() -> None:
    assert urllist.extract_urls([]) == []
