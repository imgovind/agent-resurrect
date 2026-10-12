#!/usr/bin/env python3
"""Entry point. The launcher in ~/.local/bin runs this file with `python3 -I`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if __name__ == "__main__":  # required: text search uses multiprocessing (spawn re-imports this file)
    from agent_resurrect.cli import main
    sys.exit(main())
