#!/usr/bin/env python3
"""
InvenTree Beautifier Script

Commands:
  1) fix-lcsc-links
       Minimizes long LCSC links attached to Parts.

  2) assign-jellybean
       Lets user assign "Jellybean P/N" values to parts which don't have them
       set yet.

  3) upload-lcsc-images
       Scrapes images from LCSC for LCSC parts where Inventree has no image.
"""

import sys
from textwrap import dedent

import structlog

from inventree_utils.beautifier.assign_jellybean import assign_jellybean
from inventree_utils.beautifier.config import api_from_config
from inventree_utils.beautifier.fix_lcsc_links import fix_lcsc_links
from inventree_utils.beautifier.upload_lcsc_images import upload_lcsc_images

logger = structlog.get_logger()


def main():
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            # structlog.processors.JSONRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
    )
    if len(sys.argv) < 2:
        print(
            dedent(
                """\
            Usage: python beautifier.py <command>

            Commands:
              fix-lcsc-links      - Interactively rewrite old-format LCSC links
              assign-jellybean    - Bulk assign 'Jellybean P/N' to parts missing it
              upload-lcsc-images  - Upload images from LCSC for parts lacking images
                """
            )
        )
        sys.exit(1)

    command = sys.argv[1].strip().lower()
    api = api_from_config()

    if command == "fix-lcsc-links":
        fix_lcsc_links(api)
    elif command == "assign-jellybean":
        assign_jellybean(api)
    elif command == "upload-lcsc-images":
        upload_lcsc_images(api)
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
