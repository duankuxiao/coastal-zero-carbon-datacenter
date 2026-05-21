"""Compatibility wrapper for the legacy load-shifting module name."""

from __future__ import annotations

import runpy
import sys

if __name__ == "__main__":
    runpy.run_module("optimization.load_shift", run_name="__main__")
else:
    from optimization import load_shift as _impl

    sys.modules[__name__] = _impl
