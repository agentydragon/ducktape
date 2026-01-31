"""Binary entry point for grader daemon agent."""

import asyncio
import sys

from props.grader.main import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
