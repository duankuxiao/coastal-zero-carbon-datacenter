"""Compatibility wrapper for :mod:`energy.calculate_datacenter_energy`."""

from __future__ import annotations

import runpy
import sys

if __name__ == "__main__":
    runpy.run_module("energy.calculate_datacenter_energy", run_name="__main__")
else:
    from energy import calculate_datacenter_energy as _impl

    sys.modules[__name__] = _impl
