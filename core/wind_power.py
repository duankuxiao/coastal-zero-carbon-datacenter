"""Compatibility wrapper for :mod:`renewables.wind_power`."""

from __future__ import annotations

import runpy
import sys

if __name__ == "__main__":
    runpy.run_module("renewables.wind_power", run_name="__main__")
else:
    from renewables import wind_power as _impl

    sys.modules[__name__] = _impl
