"""
Type definitions for the Tana library.
"""

from typing import NewType

# Type for node IDs to prevent mixing with regular strings
NodeId = NewType("NodeId", str)
