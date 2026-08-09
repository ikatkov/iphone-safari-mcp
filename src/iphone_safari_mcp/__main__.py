"""Support `python -m iphone_safari_mcp` / `uv run -m iphone_safari_mcp`."""

import sys

from iphone_safari_mcp.server import main

if __name__ == "__main__":
    sys.exit(main())
