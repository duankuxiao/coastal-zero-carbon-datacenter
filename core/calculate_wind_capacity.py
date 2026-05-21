"""Compatibility wrapper for :mod:`renewables.calculate_wind_capacity`."""

from __future__ import annotations

import runpy
import sys

if __name__ == "__main__":
    runpy.run_module("renewables.calculate_wind_capacity", run_name="__main__")
else:
    from renewables import calculate_wind_capacity as _impl

    sys.modules[__name__] = _impl
