"""Print the before-vs-after report for all scenarios.

Thin wrapper: `python3 report.py` == `python3 run_scenario.py --report`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_scenario import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["--report"] + sys.argv[1:]))
