"""Compatibility wrapper for :mod:`energy.datacenter`."""

from __future__ import annotations

import runpy
import sys

if __name__ == "__main__":
    runpy.run_module("energy.datacenter", run_name="__main__")
else:
    from energy import datacenter as _impl

    sys.modules[__name__] = _impl
