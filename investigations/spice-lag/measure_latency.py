#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""
SPICE input-to-display latency measurement.

Measures end-to-end latency: keystroke on client → character visible in SPICE window.

Setup:
    1. Open SPICE client to VM on atlas
    2. In VM: switch to VT (Ctrl+Alt+F1), open vim or similar text buffer
    3. Run this script on atlas

Requirements:
    - ffmpeg, xdotool (apt install ffmpeg xdotool)
    - uv (for automatic Python dependency management)
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image


def find_spice_window() -> str | None:
    """Find SPICE/remote-viewer window ID."""
    patterns = ["remote-viewer", "SPICE", "virt-viewer", "wyrm"]

    for pattern in patterns:
        result = subprocess.run(["xdotool", "search", "--name", pattern], check=False, capture_output=True, text=True)
        if result.stdout.strip():
            window_id = result.stdout.strip().split("\n")[0]
            # Verify it exists
            name_result = subprocess.run(
                ["xdotool", "getwindowname", window_id], check=False, capture_output=True, text=True
            )
            if name_result.returncode == 0:
                return window_id
    return None


def get_window_geometry(window_id: str) -> tuple[int, int, int, int]:
    """Get window position and size: (x, y, width, height)."""
    result = subprocess.run(
        ["xdotool", "getwindowgeometry", "--shell", window_id], check=False, capture_output=True, text=True
    )

    geo = {}
    for line in result.stdout.strip().split("\n"):
        if "=" in line:
            key, val = line.split("=", 1)
            geo[key] = int(val)

    return geo["X"], geo["Y"], geo["WIDTH"], geo["HEIGHT"]


def record_window(
    window_id: str,
    output_path: Path,
    duration: float,
    fps: int = 60,
    crop_margins: tuple[int, int, int, int] = (0, 0, 0, 0),
) -> subprocess.Popen:
    """
    Start recording a window with ffmpeg.

    crop_margins: (top, bottom, left, right) pixels to exclude
    """
    x, y, w, h = get_window_geometry(window_id)

    # Apply crop margins
    top, bottom, left, right = crop_margins
    x += left
    y += top
    w -= left + right
    h -= top + bottom

    cmd = [
        "ffmpeg",
        "-y",  # Overwrite
        "-f",
        "x11grab",
        "-framerate",
        str(fps),
        "-video_size",
        f"{w}x{h}",
        "-i",
        f":0.0+{x},{y}",
        "-t",
        str(duration),
        "-c:v",
        "ffv1",  # Lossless for accurate frame analysis
        str(output_path),
    ]

    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def send_keystroke(window_id: str, key: str = "x") -> float:
    """Send keystroke to window, return timestamp."""
    # Focus window first
    subprocess.run(["xdotool", "windowactivate", "--sync", window_id], check=False, capture_output=True)
    time.sleep(0.05)  # Small delay for focus

    timestamp = time.perf_counter()
    subprocess.run(["xdotool", "key", "--window", window_id, key], check=False, capture_output=True)
    return timestamp


def analyze_frames(video_path: Path, keystroke_time: float, recording_start: float, fps: int) -> dict:
    """
    Analyze video frames to find when display changed after keystroke.

    Returns dict with timing info.
    """

    # Extract frames using ffmpeg
    frame_dir = video_path.parent / "frames"
    frame_dir.mkdir(exist_ok=True)

    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-vsync", "0", str(frame_dir / "frame_%05d.png")],
        check=False,
        capture_output=True,
    )

    # Load frames and compute diffs
    frame_files = sorted(frame_dir.glob("frame_*.png"))
    if len(frame_files) < 2:
        return {"error": "Not enough frames extracted"}

    # Calculate which frame corresponds to keystroke
    keystroke_offset = keystroke_time - recording_start
    keystroke_frame = int(keystroke_offset * fps)

    print(f"  Keystroke at {keystroke_offset:.3f}s into recording (frame ~{keystroke_frame})")

    # Compute frame diffs
    diffs = []
    prev_img = None

    for i, frame_file in enumerate(frame_files):
        img = Image.open(frame_file).convert("L")  # Grayscale

        if prev_img is not None:
            # Compute absolute difference
            diff = sum(abs(a - b) for a, b in zip(img.tobytes(), prev_img.tobytes(), strict=False))
            diff_normalized = diff / (img.width * img.height)
            diffs.append((i, diff_normalized))

        prev_img = img

    # Find first significant change after keystroke frame
    # "Significant" = diff > 2x median diff (to filter out noise)
    if not diffs:
        return {"error": "No frame diffs computed"}

    median_diff = sorted(d[1] for d in diffs)[len(diffs) // 2]
    threshold = max(median_diff * 3, 0.5)  # At least 0.5 to catch real changes

    print(f"  Median frame diff: {median_diff:.2f}, threshold: {threshold:.2f}")

    change_frame = None
    for frame_idx, diff in diffs:
        if frame_idx > keystroke_frame and diff > threshold:
            change_frame = frame_idx
            print(f"  Change detected at frame {frame_idx} (diff={diff:.2f})")
            break

    if change_frame is None:
        # Show top diffs for debugging
        top_diffs = sorted(diffs, key=lambda x: x[1], reverse=True)[:5]
        print(f"  No change detected. Top diffs: {top_diffs}")
        return {"error": "No display change detected after keystroke"}

    # Calculate latency
    change_time = change_frame / fps
    latency_sec = change_time - keystroke_offset
    latency_ms = latency_sec * 1000

    # Cleanup frames
    for f in frame_files:
        f.unlink()
    frame_dir.rmdir()

    return {"keystroke_frame": keystroke_frame, "change_frame": change_frame, "latency_ms": latency_ms, "fps": fps}


def measure_once(
    window_id: str,
    key: str = "x",
    fps: int = 60,
    crop_margins: tuple[int, int, int, int] = (0, 0, 0, 0),
    work_dir: Path | None = None,
) -> float | None:
    """
    Perform one latency measurement.

    Returns latency in milliseconds, or None on failure.
    """
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="spice_latency_"))

    video_path = work_dir / "recording.mkv"
    duration = 3.0

    print(f"  Recording {duration}s at {fps}fps...")

    # Start recording
    recording_start = time.perf_counter()
    recorder = record_window(window_id, video_path, duration, fps, crop_margins)

    # Wait a bit, then send keystroke
    time.sleep(1.0)
    print("  Sending keystroke...")
    keystroke_time = send_keystroke(window_id, key)

    # Wait for recording to finish
    recorder.wait()
    recording_end = time.perf_counter()

    print(f"  Recording complete ({recording_end - recording_start:.1f}s)")

    # Analyze
    print("  Analyzing frames...")
    result = analyze_frames(video_path, keystroke_time, recording_start, fps)

    if "error" in result:
        print(f"  Error: {result['error']}")
        return None

    return result["latency_ms"]


def main():
    parser = argparse.ArgumentParser(
        description="Measure SPICE input-to-display latency",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="""
Setup:
  1. Open SPICE client to your VM
  2. In VM: switch to a VT and open vim (or any text editor)
  3. Run this script

Example:
  python3 measure_latency.py --samples 5
        """,
    )
    parser.add_argument("--samples", type=int, default=10, help="Number of measurements")
    parser.add_argument("--fps", type=int, default=60, help="Recording framerate")
    parser.add_argument("--key", type=str, default="x", help="Key to press")
    parser.add_argument("--crop", type=str, default="0,0,0,0", help="Crop margins: top,bottom,left,right")
    parser.add_argument("--window-id", type=str, help="Window ID (auto-detected if not specified)")
    parser.add_argument("--keep-video", action="store_true", help="Keep video files for debugging")

    args = parser.parse_args()

    # Parse crop margins
    crop_margins = tuple(int(x) for x in args.crop.split(","))
    if len(crop_margins) != 4:
        print("Error: --crop must be 4 comma-separated values")
        sys.exit(1)

    # Find window
    if args.window_id:
        window_id = args.window_id
    else:
        print("Looking for SPICE window...")
        window_id = find_spice_window()
        if not window_id:
            print("Error: Could not find SPICE window.")
            print("Make sure remote-viewer or virt-viewer is running.")
            sys.exit(1)

    window_name = subprocess.run(
        ["xdotool", "getwindowname", window_id], check=False, capture_output=True, text=True
    ).stdout.strip()
    print(f"Found window: {window_id} ({window_name})")

    geo = get_window_geometry(window_id)
    print(f"Geometry: {geo[2]}x{geo[3]} at ({geo[0]},{geo[1]})")
    print()

    # Run measurements
    latencies = []
    work_dir = Path(tempfile.mkdtemp(prefix="spice_latency_"))

    print(f"Running {args.samples} measurements...")
    print()

    for i in range(args.samples):
        print(f"Measurement {i + 1}/{args.samples}:")
        latency = measure_once(window_id, key=args.key, fps=args.fps, crop_margins=crop_margins, work_dir=work_dir)

        if latency is not None:
            latencies.append(latency)
            print(f"  Latency: {latency:.1f}ms")
        else:
            print("  Measurement failed")
        print()

        # Small delay between measurements
        time.sleep(0.5)

    # Summary
    print("=" * 40)
    print("Results:")
    if latencies:
        avg = sum(latencies) / len(latencies)
        min_l = min(latencies)
        max_l = max(latencies)
        print(f"  Samples: {len(latencies)}/{args.samples}")
        print(f"  Average: {avg:.1f}ms")
        print(f"  Min: {min_l:.1f}ms")
        print(f"  Max: {max_l:.1f}ms")

        if avg < 50:
            print("  Rating: Excellent (<50ms)")
        elif avg < 100:
            print("  Rating: Good (50-100ms)")
        elif avg < 200:
            print("  Rating: Noticeable (100-200ms)")
        else:
            print("  Rating: Laggy (>200ms)")
    else:
        print("  No successful measurements")

    # Cleanup
    if not args.keep_video:
        shutil.rmtree(work_dir, ignore_errors=True)
    else:
        print(f"\nVideo files kept in: {work_dir}")


if __name__ == "__main__":
    main()
