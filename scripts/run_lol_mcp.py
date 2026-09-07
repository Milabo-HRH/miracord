"""Start the League Live Client MCP server from any working directory."""

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.lol_mcp.server import main

if __name__ == "__main__":
    main()
