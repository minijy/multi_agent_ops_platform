"""CLI entry point for generating and importing mock analytics fixtures."""

from __future__ import annotations

import runpy
from pathlib import Path


def main() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "install_mock_data.py"
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
