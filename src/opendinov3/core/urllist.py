"""Read a URL list whose exact layout is not known ahead of time.

The lists this project consumes were written by other people and other
tools. A file named `urls.tsv` may hold one URL per line, or columns with a
header, and the URL column is not reliably first.

Getting this wrong is silent. img2dataset accepts whatever it is handed; a
tab-separated row is a perfectly valid string to fail to fetch. The run
completes, reports a yield near zero, and looks like a finding.

So: detect, and refuse when detection is not possible. A refusal costs a
minute. A wrong guess costs the experiment.
"""

from __future__ import annotations

from typing import Iterable

#: Column names that have been seen to hold the image URL.
URL_COLUMN_NAMES = ("url", "image_url", "URL", "IMAGE_URL")

DELIMITER = "\t"


class UrlListFormatError(ValueError):
    """The layout could not be determined, so no URLs are returned."""


def find_url_column(header: str) -> int | None:
    """Index of the URL column in a delimited header, or None if not delimited.

    Raises if the line is delimited but names no column this module
    recognises: that is a file it cannot read, not a file with no header.
    """
    fields = [f.strip() for f in header.split(DELIMITER)]
    if len(fields) == 1:
        return None
    for name in URL_COLUMN_NAMES:
        if name in fields:
            return fields.index(name)
    raise UrlListFormatError(
        "tab-separated header names no URL column; "
        f"columns are {fields!r}, expected one of {list(URL_COLUMN_NAMES)}"
    )


def looks_like_header(line: str) -> bool:
    """Whether the first line names columns rather than holding data."""
    fields = [f.strip() for f in line.split(DELIMITER)]
    return any(name in fields for name in URL_COLUMN_NAMES)


def extract_urls(
    lines: Iterable[str],
    *,
    has_header: bool | None = None,
) -> list[str]:
    """One URL per element, from a plain list or a delimited file.

    `has_header` overrides detection for a caller that already knows.
    """
    rows = list(lines)
    if not rows:
        return []

    first = rows[0].rstrip("\n").rstrip("\r")
    header_present = looks_like_header(first) if has_header is None else has_header

    if header_present:
        column = find_url_column(first)
        body = rows[1:]
    else:
        # No header. A single column is unambiguous; more than one is not,
        # because nothing identifies which of them holds the URL.
        if DELIMITER in first:
            raise UrlListFormatError(
                "tab-separated input without a header: cannot tell which "
                "column holds the URL. Add a header line naming it "
                f"{list(URL_COLUMN_NAMES)}, or supply a one-URL-per-line file."
            )
        column = None
        body = rows

    urls: list[str] = []
    for row in body:
        value = _field(row.rstrip("\n").rstrip("\r"), column)
        if value:
            urls.append(value)
    return urls


def _field(row: str, column: int | None) -> str:
    if column is None:
        return row.strip()
    fields = row.split(DELIMITER)
    if column >= len(fields):
        # A short row is corrupt input. Padding it would emit an empty URL,
        # which becomes an indistinguishable download failure later.
        return ""
    return fields[column].strip()
