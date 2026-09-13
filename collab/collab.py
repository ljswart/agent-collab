#!/usr/bin/env python3
"""Portable shim; install agent-collab in the active Python environment."""

from agent_collab.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
