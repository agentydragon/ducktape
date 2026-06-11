"""
Main entry point for the Habitify MCP server.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
