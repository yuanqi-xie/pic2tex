"""Pic2TeX GUI launcher (double-click me, or make a shortcut to me)."""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pic2tex.app import main  # noqa: E402

if __name__ == "__main__":
    # Passing an image path opens the GUI with that image loaded.
    sys.exit(main(["--gui", *sys.argv[1:]]))
