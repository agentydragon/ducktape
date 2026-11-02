"""Ruida .rd format primitives and job serialization.

Generates scrambled binary .rd files that Ruida laser controllers understand,
with up to 128 layers. Byte-level scrambling and unscrambling are available for
inspecting encoded files; parsing complete .rd jobs is not implemented.

The .rd format uses a byte-level scramble and 7-bit variable-length integer
encoding. See jnweiger/ruida-laser for format research and reference impl.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Byte-level encoding primitives ────────────────────────────────────────────


def scramble_byte(b: int) -> int:
    """Scramble a single byte for writing into .rd files.

    Algorithm: swap bit 0 and bit 7, XOR with 0x88, add 1 (mod 256).
    """
    fb = b & 0x80
    lb = b & 0x01
    res = b - fb - lb
    res |= lb << 7
    res |= fb >> 7
    res ^= 0x88
    res += 1
    return res & 0xFF


def unscramble_byte(b: int) -> int:
    """Unscramble a single byte read from .rd files (inverse of scramble)."""
    res = (b - 1) & 0xFF
    res ^= 0x88
    fb = res & 0x80
    lb = res & 0x01
    res = res - fb - lb
    res |= lb << 7
    res |= fb >> 7
    return res


def scramble_bytes(data: bytes) -> bytes:
    return bytes(scramble_byte(b) for b in data)


def unscramble_bytes(data: bytes) -> bytes:
    """Decode the byte-level scramble of a Ruida .rd file."""
    return bytes(unscramble_byte(b) for b in data)


def encode_number(value_mm: float, length: int = 5, scale: int = 1000) -> bytes:
    """Encode a value as a multi-byte 7-bit integer.

    Default: 5 bytes (35-bit), scale x1000 converts mm to micrometers.
    Each byte uses bits 0-6 for data, big-endian order.
    """
    n = int(value_mm * scale)
    result: list[int] = []
    while n > 0:
        result.append(n & 0x7F)
        n >>= 7
    while len(result) < length:
        result.append(0)
    result.reverse()
    return bytes(result)


def encode_percent(pct: float) -> bytes:
    """Encode a percentage (0-100) as 2 bytes (14-bit, 7 bits each)."""
    a = int(pct * 0x3FFF * 0.01)
    return bytes([a >> 7, a & 0x7F])


def encode_color(r: int, g: int, b: int) -> bytes:
    """Encode an RGB color as a 5-byte number (blue<<16 | green<<8 | red)."""
    cc = ((b & 0xFF) << 16) | ((g & 0xFF) << 8) | (r & 0xFF)
    return encode_number(cc, length=5, scale=1)


def encode_hex(hex_str: str) -> bytes:
    """Parse a hex string with optional comments into bytes.

    Example: "e7 03  # bounding box" -> b'\\xe7\\x03'
    """
    cleaned = re.sub(r"#.*$", "", hex_str, flags=re.MULTILINE)
    return bytes(int(x, 16) for x in cleaned.split())


# ── Layer colors ──────────────────────────────────────────────────────────────

# Default palette matching RDWorks/LightBurn layer colors.
_LAYER_COLORS: list[tuple[int, int, int]] = [
    (0, 0, 0),  # black
    (0, 0, 255),  # blue
    (0, 255, 0),  # green
    (255, 255, 0),  # yellow
    (255, 0, 255),  # magenta
    (0, 255, 255),  # cyan
    (255, 128, 0),  # orange
    (128, 0, 255),  # purple
]


def _layer_color(index: int) -> tuple[int, int, int]:
    return _LAYER_COLORS[index % len(_LAYER_COLORS)]


# ── High-level API ────────────────────────────────────────────────────────────


@dataclass
class RdLayer:
    """A single cut layer with its laser parameters."""

    index: int  # 0-127
    min_power_pct: float
    max_power_pct: float
    speed_mm_s: float
    num_passes: int = 1
    z_offset_mm: float = 0.0
    z_per_pass_mm: float = 0.0


@dataclass
class RdRect:
    """A rectangle to cut, assigned to a layer."""

    layer_index: int
    x_mm: float  # left edge (Ruida coords, Y-down)
    y_mm: float  # top edge (Ruida coords, Y-down)
    width_mm: float
    height_mm: float


@dataclass
class RdJob:
    """A complete Ruida laser job containing layers and rectangles."""

    layers: list[RdLayer] = field(default_factory=list)
    rects: list[RdRect] = field(default_factory=list)

    def to_bytes(self) -> bytes:
        """Serialize to a complete scrambled .rd binary file."""
        raw = self._header() + self._body() + self._trailer()
        return scramble_bytes(raw)

    def _global_bbox(self) -> tuple[float, float, float, float]:
        """Compute (xmin, ymin, xmax, ymax) across all rects."""
        if not self.rects:
            return (0.0, 0.0, 0.0, 0.0)
        xmin = min(r.x_mm for r in self.rects)
        ymin = min(r.y_mm for r in self.rects)
        xmax = max(r.x_mm + r.width_mm for r in self.rects)
        ymax = max(r.y_mm + r.height_mm for r in self.rects)
        return (xmin, ymin, xmax, ymax)

    def _layer_bbox(self, layer_idx: int) -> tuple[float, float, float, float]:
        """Compute bounding box for rects on a specific layer."""
        layer_rects = [r for r in self.rects if r.layer_index == layer_idx]
        if not layer_rects:
            return (0.0, 0.0, 0.0, 0.0)
        xmin = min(r.x_mm for r in layer_rects)
        ymin = min(r.y_mm for r in layer_rects)
        xmax = max(r.x_mm + r.width_mm for r in layer_rects)
        ymax = max(r.y_mm + r.height_mm for r in layer_rects)
        return (xmin, ymin, xmax, ymax)

    def _header(self) -> bytes:
        xmin, ymin, xmax, ymax = self._global_bbox()

        data = encode_hex("d8 12")  # red light on
        data += encode_hex("f0 f1 02 00")  # file type
        data += encode_hex("d8 00")  # green light off

        # Global bounding box (multiple redundant entries as RDWorks emits)
        data += encode_hex("e7 06") + encode_number(0) + encode_number(0)  # feeding
        data += encode_hex("e7 03") + encode_number(xmin) + encode_number(ymin)
        data += encode_hex("e7 07") + encode_number(xmax) + encode_number(ymax)
        data += encode_hex("e7 50") + encode_number(xmin) + encode_number(ymin)
        data += encode_hex("e7 51") + encode_number(xmax) + encode_number(ymax)
        data += encode_hex("e7 04 00 01 00 01") + encode_number(0) + encode_number(0)
        data += encode_hex("e7 05 00")

        # Per-layer configuration
        for layer in self.layers:
            lnum = layer.index
            lbyte = bytes([lnum & 0x7F])

            # Speed: c9 04 <layer> <speed_5bytes>
            data += encode_hex("c9 04") + lbyte + encode_number(layer.speed_mm_s)

            # Power — laser 1 min/max
            data += encode_hex("c6 31") + lbyte + encode_percent(layer.min_power_pct)
            data += encode_hex("c6 32") + lbyte + encode_percent(layer.max_power_pct)
            # Laser 2/3/4 — set to same values (required for format completeness)
            data += encode_hex("c6 41") + lbyte + encode_percent(layer.min_power_pct)
            data += encode_hex("c6 42") + lbyte + encode_percent(layer.max_power_pct)
            data += encode_hex("c6 35") + lbyte + encode_percent(layer.min_power_pct)
            data += encode_hex("c6 36") + lbyte + encode_percent(layer.max_power_pct)
            data += encode_hex("c6 37") + lbyte + encode_percent(layer.min_power_pct)
            data += encode_hex("c6 38") + lbyte + encode_percent(layer.max_power_pct)

            # Layer color, flags, and per-layer bounding box
            r, g, b = _layer_color(lnum)
            lx0, ly0, lx1, ly1 = self._layer_bbox(lnum)

            data += encode_hex("ca 06") + lbyte + encode_color(r, g, b)
            data += encode_hex("ca 41") + lbyte + bytes([0])
            data += encode_hex("e7 52") + lbyte + encode_number(lx0) + encode_number(ly0)
            data += encode_hex("e7 53") + lbyte + encode_number(lx1) + encode_number(ly1)
            data += encode_hex("e7 61") + lbyte + encode_number(lx0) + encode_number(ly0)
            data += encode_hex("e7 62") + lbyte + encode_number(lx1) + encode_number(ly1)

        # Max layer number
        max_layer = max((layer.index for layer in self.layers), default=0)
        data += encode_hex("ca 22") + bytes([max_layer & 0x7F])

        # Trailing header fields
        data += encode_hex("e7 54 00 00 00 00 00 00")
        data += encode_hex("e7 54 01 00 00 00 00 00")
        data += encode_hex("e7 55 00 00 00 00 00 00")
        data += encode_hex("e7 55 01 00 00 00 00 00")
        data += encode_hex("f1 03 00 00 00 00 00 00 00 00 00 00")
        data += encode_hex("f1 00 00")
        data += encode_hex("f1 01 00")
        data += encode_hex("f2 00 00")
        data += encode_hex("f2 01 00")
        data += encode_hex("f2 02 05 2a 39 1c 41 04 6a 15 08 20")
        data += encode_hex("f2 03") + encode_number(xmin) + encode_number(ymin)
        data += encode_hex("f2 04") + encode_number(xmax) + encode_number(ymax)
        data += encode_hex("f2 06") + encode_number(xmin) + encode_number(ymin)
        data += encode_hex("f2 07 00")
        data += encode_hex("f2 05 00 01 00 01") + encode_number(xmax) + encode_number(ymax)
        data += encode_hex("ea 00")
        data += encode_hex("e7 60 00")
        data += encode_hex("e7 13") + encode_number(xmin) + encode_number(ymin)
        data += encode_hex("e7 17") + encode_number(xmax) + encode_number(ymax)
        data += encode_hex("e7 23") + encode_number(xmin) + encode_number(ymin)
        data += encode_hex("e7 24 00")
        data += encode_hex("e7 08 00 01 00 01") + encode_number(xmax) + encode_number(ymax)

        return data

    def _body(self) -> bytes:
        """Generate per-layer motion commands for all rectangles."""
        # Group rects by layer, preserving layer order
        layer_indices = [layer.index for layer in self.layers]
        rects_by_layer: dict[int, list[RdRect]] = {idx: [] for idx in layer_indices}
        for rect in self.rects:
            rects_by_layer[rect.layer_index].append(rect)

        data = b""
        for layer in self.layers:
            layer_rects = rects_by_layer[layer.index]
            if not layer_rects:
                continue

            lnum = layer.index

            # Body prolog — sets speed/power for this layer's motion
            data += encode_hex("ca 01 00")
            data += encode_hex("ca 02") + bytes([lnum & 0x7F])
            data += encode_hex("ca 01 30")
            data += encode_hex("ca 01 10")
            data += encode_hex("ca 01 13")  # blower on

            # Speed
            data += encode_hex("c9 02") + encode_number(layer.speed_mm_s)

            # Delays (zero)
            data += encode_hex("c6 15 00 00 00 00 00")
            data += encode_hex("c6 16 00 00 00 00 00")

            # Power (laser 1-4, min/max)
            data += encode_hex("c6 01") + encode_percent(layer.min_power_pct)
            data += encode_hex("c6 02") + encode_percent(layer.max_power_pct)
            data += encode_hex("c6 21") + encode_percent(layer.min_power_pct)
            data += encode_hex("c6 22") + encode_percent(layer.max_power_pct)
            data += encode_hex("c6 05") + encode_percent(layer.min_power_pct)
            data += encode_hex("c6 06") + encode_percent(layer.max_power_pct)
            data += encode_hex("c6 07") + encode_percent(layer.min_power_pct)
            data += encode_hex("c6 08") + encode_percent(layer.max_power_pct)

            data += encode_hex("ca 03 01")
            data += encode_hex("ca 10 00")

            # Cut each rectangle: move to top-left, then cut the 4 sides
            for rect in layer_rects:
                x0 = rect.x_mm
                y0 = rect.y_mm
                x1 = rect.x_mm + rect.width_mm
                y1 = rect.y_mm + rect.height_mm

                # Move to top-left corner (laser off)
                data += bytes([0x88]) + encode_number(x0) + encode_number(y0)
                # Cut: top-left -> top-right -> bottom-right -> bottom-left -> top-left
                data += bytes([0xA8]) + encode_number(x1) + encode_number(y0)
                data += bytes([0xA8]) + encode_number(x1) + encode_number(y1)
                data += bytes([0xA8]) + encode_number(x0) + encode_number(y1)
                data += bytes([0xA8]) + encode_number(x0) + encode_number(y0)

        return data

    def _trailer(self) -> bytes:
        data = encode_hex("eb e7 00")
        data += encode_hex("da 01 06 20") + encode_number(0) + encode_number(0)
        data += encode_hex("d7")  # EOF
        return data
