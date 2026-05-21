"""Compatibility wrapper for :mod:`optimization.optimize_zero_carbon`."""

from __future__ import annotations

import sys

from optimization import optimize_zero_carbon as _impl

if __name__ == "__main__":
    from scripts.run_optimize import main

    main()
else:
    sys.modules[__name__] = _impl
