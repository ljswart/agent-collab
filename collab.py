#!/usr/bin/env python3
"""Source-checkout entry point, including legacy runpy shims during explicit migration."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_collab.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
