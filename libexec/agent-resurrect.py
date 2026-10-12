#!/usr/bin/env python3
"""Entry point. bin/agent-resurrect (the plugin's command) and the launcher in ~/.local/bin run this file with
`python3 -I`. It lives in libexec/ rather than bin/ so that it is not a second command on the Bash tool's PATH."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if __name__ == "__main__":  # required: text search uses multiprocessing (spawn re-imports this file)
    from agent_resurrect.cli import main
    sys.exit(main())
