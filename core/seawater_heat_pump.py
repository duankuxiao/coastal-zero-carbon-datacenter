"""Compatibility wrapper for :mod:`energy.seawater_heat_pump`."""

from __future__ import annotations

import runpy
import sys

if __name__ == "__main__":
    runpy.run_module("energy.seawater_heat_pump", run_name="__main__")
else:
    from energy import seawater_heat_pump as _impl

    sys.modules[__name__] = _impl
