#!/usr/bin/env python3
"""img2dataset, with its HTTP connections pooled and reused.

Takes exactly the arguments `img2dataset` takes and does exactly what it
does, except that `download_image` is replaced before the CLI is dispatched.
The upstream one opens a TCP connection and a TLS session per image and
closes it immediately; ours keeps them per host and reuses them.

WHY IT IS A SEPARATE ENTRY POINT

The patch has to be in place before any worker thread starts, and the
console script installed by the package offers no hook. Wrapping it here
keeps the image unmodified — the repository is bound into the container at
/work, so switching downloaders needs no rebuild and no new image to
distribute, and OD_HTTP_POOL=0 goes straight back to the upstream path.

The reasoning, and what must not change with it, is in
src/opendinov3/net/pooled_download.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opendinov3.net import pooled_download  # noqa: E402

pooled_download.install()

from img2dataset import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
