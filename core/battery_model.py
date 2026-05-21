"""Compatibility wrapper for :mod:`optimization.battery_model`."""

from __future__ import annotations

import runpy
import sys

if __name__ == "__main__":
    runpy.run_module("optimization.battery_model", run_name="__main__")
else:
    from optimization import battery_model as _impl

    sys.modules[__name__] = _impl
