import pytest_bazel

from laser.ruida.rd_writer import (
    RdJob,
    RdLayer,
    RdRect,
    encode_hex,
    encode_number,
    encode_percent,
    scramble_byte,
    unscramble_byte,
)


def test_scramble_roundtrip():
    """Every byte value survives a scramble -> unscramble round-trip."""
    for b in range(256):
        assert unscramble_byte(scramble_byte(b)) == b


def test_scramble_known_values():
    """Spot-check against values verified with jnweiger/ruida-laser."""
    # 0x00 -> swap bits -> 0x00 -> XOR 0x88 -> 0x88 -> +1 -> 0x89
    assert scramble_byte(0x00) == 0x89
    # 0xFF -> swap bits -> 0xFF -> XOR 0x88 -> 0x77 -> +1 -> 0x78
    assert scramble_byte(0xFF) == 0x78


def test_encode_number_100mm():
    """100 mm encodes to 100000 µm as 5 bytes of 7-bit big-endian."""
    result = encode_number(100.0)
    # 100000 = 0x186A0
    # In 7-bit groups (from MSB): 00, 03, 00, 54, 20
    # 0x186A0 = 0b11000011010100000
    # Split into 7-bit groups from LSB:
    #   0100000 = 0x20 (32)
    #   1010100 = 0x54 (84) -- wait, let me recompute
    # 100000 in binary: 11000011010100000
    # That's 17 bits. In 7-bit chunks from LSB:
    #   bits 0-6:  0100000 = 32 = 0x20
    #   bits 7-13: 0001101 = 13 = 0x0D
    #   bits 14-20: 110 = 6 = 0x06
    # So 5 bytes big-endian: [0, 0, 6, 13, 32]
    assert result == bytes([0, 0, 6, 13, 32])
    assert len(result) == 5


def test_encode_number_zero():
    assert encode_number(0.0) == bytes([0, 0, 0, 0, 0])


def test_encode_percent_100():
    """100% should encode to 0x3FFF split into 7-bit halves."""
    result = encode_percent(100.0)
    # 100 * 0x3FFF * 0.01 = 0x3FFF = 16383
    # High 7 bits: 16383 >> 7 = 127 = 0x7F
    # Low 7 bits: 16383 & 0x7F = 127 = 0x7F
    assert result == bytes([0x7F, 0x7F])


def test_encode_percent_0():
    assert encode_percent(0.0) == bytes([0, 0])


def test_encode_percent_50():
    result = encode_percent(50.0)
    # 50 * 16383 * 0.01 = 8191.5 -> int = 8191
    # 8191 >> 7 = 63 = 0x3F
    # 8191 & 0x7F = 127 = 0x7F
    assert result == bytes([0x3F, 0x7F])


def test_encode_hex():
    assert encode_hex("e7 03") == bytes([0xE7, 0x03])
    assert encode_hex("d7  # EOF marker") == bytes([0xD7])


def test_unscrambled_structure():
    """Verify the unscrambled .rd file starts with known header and ends with D7."""
    job = RdJob(
        layers=[RdLayer(index=0, min_power_pct=50, max_power_pct=80, speed_mm_s=30)],
        rects=[RdRect(layer_index=0, x_mm=5, y_mm=5, width_mm=20, height_mm=20)],
    )
    # Get raw unscrambled bytes for inspection
    raw = job._header() + job._body() + job._trailer()

    # Should start with D8 12 (red light on)
    assert raw[0:2] == bytes([0xD8, 0x12])
    # Should end with D7 (EOF)
    assert raw[-1] == 0xD7

    # Should contain the move opcode 0x88 and cut opcode 0xA8
    assert 0x88 in raw
    assert 0xA8 in raw


def test_60_layers():
    """Verify that 60+ layers work without error (beyond LightBurn's 30 limit)."""
    layers = [RdLayer(index=i, min_power_pct=20, max_power_pct=80, speed_mm_s=100) for i in range(60)]
    rects = [RdRect(layer_index=i, x_mm=i * 12.0, y_mm=0, width_mm=10, height_mm=10) for i in range(60)]
    job = RdJob(layers=layers, rects=rects)
    data = job.to_bytes()
    assert len(data) > 0

    # Verify raw structure has all 60 layers referenced
    raw = job._header() + job._body() + job._trailer()
    # Layer 59 (0x3B) should appear in the header as a layer byte
    assert bytes([59]) in raw


def test_multi_rect_per_layer():
    """Multiple rects on the same layer should all appear in the body."""
    job = RdJob(
        layers=[RdLayer(index=0, min_power_pct=50, max_power_pct=50, speed_mm_s=100)],
        rects=[
            RdRect(layer_index=0, x_mm=0, y_mm=0, width_mm=10, height_mm=10),
            RdRect(layer_index=0, x_mm=20, y_mm=0, width_mm=10, height_mm=10),
        ],
    )
    raw = job._header() + job._body() + job._trailer()

    # Count move commands (0x88) — should be 2 (one per rect)
    move_count = sum(1 for i in range(len(raw)) if raw[i] == 0x88)
    assert move_count == 2

    # Count cut commands (0xA8) — should be 8 (4 sides x 2 rects)
    cut_count = sum(1 for i in range(len(raw)) if raw[i] == 0xA8)
    assert cut_count == 8


def test_global_bbox_spans_all_rects():
    """Global bounding box should encompass all rects across layers."""
    job = RdJob(
        layers=[
            RdLayer(index=0, min_power_pct=50, max_power_pct=50, speed_mm_s=100),
            RdLayer(index=1, min_power_pct=50, max_power_pct=50, speed_mm_s=100),
        ],
        rects=[
            RdRect(layer_index=0, x_mm=10, y_mm=20, width_mm=5, height_mm=5),
            RdRect(layer_index=1, x_mm=50, y_mm=60, width_mm=10, height_mm=10),
        ],
    )
    xmin, ymin, xmax, ymax = job._global_bbox()
    assert xmin == 10.0
    assert ymin == 20.0
    assert xmax == 60.0
    assert ymax == 70.0


if __name__ == "__main__":
    pytest_bazel.main()
