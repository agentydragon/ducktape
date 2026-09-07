"""The channel surfaces that can hold a conversation copy."""

from enum import StrEnum


class ChannelSurface(StrEnum):
    """Which channel holds a copy of a conversation."""

    MATRIX = "matrix"
