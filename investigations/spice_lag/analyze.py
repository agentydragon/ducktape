#!/usr/bin/python3
"""
Analyze SPICE latency measurement recordings.

Reads a recording directory produced by record.py and computes
input-to-display latency using pixel-diff or OpenAI vision API.

Usage:
    python analyze.py <recording-dir>              # pixel-diff (default)
    python analyze.py <recording-dir> --vision     # OpenAI vision API

Requirements:
    - python3-pil, numpy (system packages)
    - For --vision: openai package (installed by direnv), OPENAI_API_KEY
"""

import argparse
import asyncio
import base64
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import openai
from PIL import Image

VISION_PROMPT = (
    "This is a screenshot of a desktop showing two windows.\n\n"
    "One window is a terminal running a measurement script that prints 'Clock: HH:MM:SS.mmm' lines.\n"
    "The other window is a SPICE remote desktop viewer showing vim/nvim in INSERT mode "
    "(dark background, green '-- INSERT --' at bottom).\n\n"
    "The windows may be side by side or overlapping, in any order. "
    "Identify them by their content, not position.\n\n"
    "CRITICAL: Report ONLY what you can actually see rendered in the vim buffer. "
    "Do NOT guess, predict, or infer what text should be there. "
    "If the vim buffer line 1 appears empty (no visible characters before the cursor), "
    "report vim_text as empty string and vim_col as 1.\n\n"
    "The vim status bar shows the cursor position (e.g. '1,6' means row 1, column 6). "
    "Column 1 means no text typed yet. Column N means N-1 characters on the line. "
    "The block cursor itself is NOT a character - do not count it.\n\n"
    "IMPORTANT: vim_col from the status bar is the authoritative source of truth. "
    "Read it first, then verify the text matches (length must equal col minus 1).\n\n"
    "Output JSON with:\n"
    '- "clock": the time on the most recent Clock: line (e.g. "02:34:56.789"), or null\n'
    '- "vim_text": the exact text on vim line 1 (only characters you can see, no cursor), or "" if empty\n'
    '- "vim_col": the column number from the vim status bar (number after comma in e.g. "1,6"), or null'
)

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "clock": {"type": ["string", "null"]},
        "vim_text": {"type": "string"},
        "vim_col": {"type": ["integer", "null"]},
    },
    "required": ["clock", "vim_text", "vim_col"],
    "additionalProperties": False,
}

VISION_CACHE_DIR = Path.home() / ".cache" / "spice-latency" / "vision"

MAX_CONCURRENT = 20


@dataclass
class LatencyMeasurement:
    latency_ms: float
    lower_ms: float
    upper_ms: float
    frame_interval_ms: float


def _vision_cache_key(image_path: Path) -> str:
    """Cache key from prompt hash + image content hash."""
    prompt_hash = hashlib.sha256(VISION_PROMPT.encode()).hexdigest()[:12]
    image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()[:16]
    return f"{prompt_hash}_{image_hash}"


def _cache_path(frame_file: Path) -> Path:
    return VISION_CACHE_DIR / f"{_vision_cache_key(frame_file)}.json"


def _parse_clock(clock_str: str) -> datetime.datetime:
    """Parse HH:MM:SS.mmm clock string."""
    padded = clock_str + "000" if len(clock_str.split(".")[-1]) == 3 else clock_str
    return datetime.datetime.strptime(padded, "%H:%M:%S.%f")


def analyze_pixeldiff(
    frame_files: list[Path], keystroke_times: list[float], recording_start: float, recording_duration: float
) -> list[LatencyMeasurement | None]:
    """Pixel-diff analysis."""
    if len(frame_files) < 2:
        print(f"  Not enough frames ({len(frame_files)})")
        return [None] * len(keystroke_times)

    actual_fps = len(frame_files) / recording_duration
    frame_interval_ms = 1000.0 / actual_fps
    print(f"  Pixel-diff: {actual_fps:.1f} fps ({len(frame_files)} frames, {frame_interval_ms:.1f}ms/frame)")

    diffs = []
    prev_arr = None
    for i, frame_file in enumerate(frame_files):
        arr = np.array(Image.open(frame_file).convert("L"))
        if prev_arr is not None:
            changed_pixels = int(np.sum(np.abs(arr.astype(np.int16) - prev_arr.astype(np.int16)) > 10))
            diffs.append((i, changed_pixels))
        prev_arr = arr

    if not diffs:
        return [None] * len(keystroke_times)

    median_diff = sorted(d[1] for d in diffs)[len(diffs) // 2]
    threshold = max(median_diff * 3, 50)
    print(f"  Median changed pixels: {median_diff}, threshold: {threshold:.0f}")

    results = []
    for keystroke_time in keystroke_times:
        keystroke_offset = keystroke_time - recording_start
        keystroke_frame = int(keystroke_offset * actual_fps)

        change_frame = None
        for frame_idx, diff in diffs:
            if frame_idx > keystroke_frame and diff > threshold:
                change_frame = frame_idx
                break

        if change_frame is None:
            results.append(None)
        else:
            change_time = change_frame / actual_fps
            latency_ms = (change_time - keystroke_offset) * 1000
            results.append(
                LatencyMeasurement(
                    latency_ms=latency_ms,
                    lower_ms=latency_ms - frame_interval_ms,
                    upper_ms=latency_ms,
                    frame_interval_ms=frame_interval_ms,
                )
            )

    return results


async def _query_frame(client: openai.AsyncOpenAI, frame_file: Path, sem: asyncio.Semaphore) -> dict:
    """Query vision API for a single frame. Returns parsed result dict."""
    image_b64 = base64.b64encode(frame_file.read_bytes()).decode()
    async with sem:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}", "detail": "high"},
                        },
                    ],
                }
            ],
            max_tokens=256,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "frame_analysis", "strict": True, "schema": VISION_SCHEMA},
            },
        )
    content = resp.choices[0].message.content
    if content is None:
        raise RuntimeError(
            f"API returned no content for {frame_file.name}: "
            f"finish_reason={resp.choices[0].finish_reason}, refusal={resp.choices[0].message.refusal}"
        )
    return json.loads(content)


async def _analyze_vision_async(frame_files: list[Path]) -> list[dict]:
    """Query all uncached frames concurrently, return results in order."""
    VISION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    client = openai.AsyncOpenAI()
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    results: list[dict | None] = [None] * len(frame_files)
    to_query: list[tuple[int, Path]] = []
    cache_hits = 0

    for i, frame_file in enumerate(frame_files):
        cp = _cache_path(frame_file)
        if cp.exists():
            cached = json.loads(cp.read_text())
            results[i] = cached.get("result", cached)
            cache_hits += 1
        else:
            to_query.append((i, frame_file))

    print(f"  {cache_hits} cached, {len(to_query)} to query (concurrency={MAX_CONCURRENT})")

    if to_query:
        tasks = [_query_frame(client, frame_file, sem) for _, frame_file in to_query]
        query_results = await asyncio.gather(*tasks)

        for (i, frame_file), result in zip(to_query, query_results, strict=True):
            results[i] = result
            cache_entry = {
                "prompt": VISION_PROMPT,
                "schema": VISION_SCHEMA,
                "model": "gpt-4o",
                "frame": frame_file.name,
                "result": result,
            }
            _cache_path(frame_file).write_text(json.dumps(cache_entry, indent=2))

    return results  # type: ignore[return-value]


async def analyze_vision(
    frame_files: list[Path], markers: list[str], sent_timestamps: list[str], recording_duration: float
) -> list[LatencyMeasurement | None]:
    """Vision API analysis."""
    frame_results = await _analyze_vision_async(frame_files)
    frame_interval_ms = recording_duration * 1000.0 / len(frame_files)

    def _vim_col(fr: dict) -> int:
        """Number of characters typed, derived from vim_col (col=N means N-1 chars)."""
        col = fr.get("vim_col")
        if col is not None and col >= 1:
            return col - 1
        # Fall back to text length if col missing.
        text = fr.get("vim_text", "".join(fr.get("vim_buffer_text", [])))
        return len(text)

    # Detect each marker by vim_col transition: marker N appears when char count
    # goes from N-1 to N (or higher). This only requires reading one integer per
    # frame, avoiding character-perfect transcription of random strings.
    results = []
    for marker_idx, (marker, ts) in enumerate(zip(markers, sent_timestamps, strict=True)):
        target_chars = marker_idx + 1  # after Nth marker, N chars should be visible
        found_frame = None
        for i, fr in enumerate(frame_results):
            if _vim_col(fr) >= target_chars:
                prev_chars = _vim_col(frame_results[i - 1]) if i > 0 else 0
                if prev_chars < target_chars:
                    found_frame = i
                    break

        if found_frame is None:
            print(f"  Marker '{marker}' (sent {ts}) not found in any frame")
            results.append(None)
            continue

        this_clock = frame_results[found_frame].get("clock")
        prev_clock = frame_results[found_frame - 1].get("clock") if found_frame > 0 else None

        try:
            t_sent = _parse_clock(ts)
            t_upper = _parse_clock(this_clock)
            upper_ms = (t_upper - t_sent).total_seconds() * 1000

            if prev_clock is not None:
                t_lower = _parse_clock(prev_clock)
                lower_ms = (t_lower - t_sent).total_seconds() * 1000
            else:
                lower_ms = upper_ms - frame_interval_ms

            latency_ms = (upper_ms + lower_ms) / 2
            print(
                f"  Marker '{marker}' (sent {ts}) first in frame {found_frame + 1}: "
                f"prev_clock={prev_clock}, clock={this_clock} -> "
                f"{lower_ms:.0f}-{upper_ms:.0f}ms (mid={latency_ms:.0f}ms)"
            )
            results.append(
                LatencyMeasurement(
                    latency_ms=latency_ms, lower_ms=lower_ms, upper_ms=upper_ms, frame_interval_ms=frame_interval_ms
                )
            )
        except (ValueError, TypeError) as e:
            print(f"  Failed to parse: clock={this_clock}, sent={ts}: {e}")
            results.append(None)

    return results


def extract_frames(video_path: Path, frame_dir: Path) -> int:
    """Extract all frames from video as PNGs. Returns frame count."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found in PATH")
    frame_dir.mkdir(exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-vsync", "0", str(frame_dir / "frame_%05d.png")],
        check=True,
        capture_output=True,
    )
    return len(list(frame_dir.glob("frame_*.png")))


async def main():
    parser = argparse.ArgumentParser(
        description="Analyze SPICE latency measurement recordings",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("recording_dir", type=Path, help="Path to recording directory from record.py")
    parser.add_argument("--vision", action="store_true", help="Use OpenAI vision API (default: pixel-diff)")
    args = parser.parse_args()

    metadata_path = args.recording_dir / "metadata.json"
    if not metadata_path.exists():
        print(f"Error: {metadata_path} not found")
        sys.exit(1)

    metadata = json.loads(metadata_path.read_text())
    sent_timestamps = metadata["sent_timestamps"]
    markers = metadata.get("markers", [str(i) for i in range(len(sent_timestamps))])
    recording_duration = metadata["recording_duration"]
    frame_dir = args.recording_dir / "frames"

    if not frame_dir.exists() or not any(frame_dir.glob("frame_*.png")):
        video_path = args.recording_dir / "recording.webm"
        if not video_path.exists():
            print(f"Error: no frames/ directory and no recording.webm in {args.recording_dir}")
            sys.exit(1)
        print("Extracting frames from recording.webm...")
        count = extract_frames(video_path, frame_dir)
        print(f"  {count} frames extracted")

    frame_files = sorted(frame_dir.glob("frame_*.png"))
    frame_interval_ms = recording_duration * 1000.0 / len(frame_files) if frame_files else 0

    print(f"Recording: {args.recording_dir}")
    print(f"  {len(frame_files)} frames, {len(sent_timestamps)} samples")
    print(f"  {len(frame_files) / recording_duration:.1f} fps, {frame_interval_ms:.1f}ms frame interval")
    print()

    if args.vision:
        print("Analyzing with vision API...")
        latencies = await analyze_vision(frame_files, markers, sent_timestamps, recording_duration)
    else:
        print("Analyzing with pixel-diff...")
        latencies = analyze_pixeldiff(
            frame_files, metadata["keystroke_perf_times"], metadata["recording_start"], recording_duration
        )

    print()
    print("=" * 50)
    print("Results:")
    valid: list[LatencyMeasurement] = []
    for i, (marker, ts, m) in enumerate(zip(markers, sent_timestamps, latencies, strict=True)):
        if m is not None:
            valid.append(m)
            print(f"  [{i + 1}] marker={marker} sent={ts} → {m.latency_ms:.0f}ms [{m.lower_ms:.0f}-{m.upper_ms:.0f}ms]")
        else:
            print(f"  [{i + 1}] marker={marker} sent={ts} → FAILED")

    if valid:
        avg = sum(m.latency_ms for m in valid) / len(valid)
        avg_lower = sum(m.lower_ms for m in valid) / len(valid)
        avg_upper = sum(m.upper_ms for m in valid) / len(valid)
        print(f"\n  Samples: {len(valid)}/{len(sent_timestamps)}")
        print(f"  Average: {avg:.0f}ms [{avg_lower:.0f}-{avg_upper:.0f}ms]")
        print(f"  Min: {min(m.latency_ms for m in valid):.0f}ms")
        print(f"  Max: {max(m.latency_ms for m in valid):.0f}ms")
        print(f"  Frame interval: ±{frame_interval_ms:.1f}ms")
    else:
        print("  No successful measurements")


if __name__ == "__main__":
    asyncio.run(main())
