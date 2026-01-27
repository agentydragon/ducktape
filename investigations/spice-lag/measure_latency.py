#!/usr/bin/python3
"""
SPICE input-to-display latency measurement (Wayland/GNOME).

Measures end-to-end latency: keystroke on client → character visible in SPICE window.

Setup:
    1. Open SPICE client to VM on atlas
    2. In VM: switch to VT, open nvim in insert mode:
       nvim --clean -c "set guicursor=a:blinkon0" -c "startinsert"
    3. Run this script on atlas

Requirements:
    - ffmpeg, ydotool (installed via ansible/atlas.yaml)
    - ydotoold running
    - python3-gi (system package, installed with GNOME)
    - python3-pil: sudo apt install python3-pil
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402
from PIL import Image  # noqa: E402


def _session_bus() -> Gio.DBusConnection:
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _dbus_call(
    bus: Gio.DBusConnection,
    bus_name: str,
    object_path: str,
    interface: str,
    method: str,
    params: GLib.Variant | None = None,
    reply_type: GLib.VariantType | None = None,
) -> GLib.Variant:
    return bus.call_sync(bus_name, object_path, interface, method, params, reply_type, Gio.DBusCallFlags.NONE, -1, None)


def start_screencast(bus: Gio.DBusConnection, fps: int, output_path: Path) -> str:
    """Start GNOME Shell full-screen screencast. Returns filename."""
    builder = GLib.VariantBuilder.new(GLib.VariantType("(sa{sv})"))
    builder.add_value(GLib.Variant("s", str(output_path)))
    options_builder = GLib.VariantBuilder.new(GLib.VariantType("a{sv}"))
    options_builder.add_value(GLib.Variant("{sv}", ("framerate", GLib.Variant("i", fps))))
    options_builder.add_value(GLib.Variant("{sv}", ("draw-cursor", GLib.Variant("b", False))))
    builder.add_value(options_builder.end())
    params = builder.end()

    result = _dbus_call(
        bus,
        "org.gnome.Shell.Screencast",
        "/org/gnome/Shell/Screencast",
        "org.gnome.Shell.Screencast",
        "Screencast",
        params,
        GLib.VariantType("(bs)"),
    )

    success = result.get_child_value(0).get_boolean()
    filename = result.get_child_value(1).get_string()
    if not success:
        raise RuntimeError("Failed to start GNOME screencast")
    return filename


def stop_screencast(bus: Gio.DBusConnection) -> None:
    """Stop an active GNOME Shell screencast."""
    _dbus_call(
        bus, "org.gnome.Shell.Screencast", "/org/gnome/Shell/Screencast", "org.gnome.Shell.Screencast", "StopScreencast"
    )


def send_keystroke(key: str = "x") -> float:
    """Send keystroke via ydotool, return timestamp."""
    timestamp = time.perf_counter()
    subprocess.run(["ydotool", "key", key], check=True, capture_output=True)
    return timestamp


def analyze_frames(video_path: Path, keystroke_time: float, recording_start: float, fps: int) -> dict:
    """Analyze video frames to find when display changed after keystroke."""
    frame_dir = video_path.parent / "frames"
    frame_dir.mkdir(exist_ok=True)

    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-vsync", "0", str(frame_dir / "frame_%05d.png")],
        check=False,
        capture_output=True,
    )

    frame_files = sorted(frame_dir.glob("frame_*.png"))
    if len(frame_files) < 2:
        return {"error": f"Not enough frames extracted ({len(frame_files)})"}

    keystroke_offset = keystroke_time - recording_start
    keystroke_frame = int(keystroke_offset * fps)

    print(f"  Keystroke at {keystroke_offset:.3f}s into recording (frame ~{keystroke_frame})")
    print(f"  Total frames extracted: {len(frame_files)}")

    # Compute frame diffs
    diffs = []
    prev_img = None

    for i, frame_file in enumerate(frame_files):
        img = Image.open(frame_file).convert("L")  # Grayscale

        if prev_img is not None:
            diff = sum(abs(a - b) for a, b in zip(img.tobytes(), prev_img.tobytes(), strict=True))
            diff_normalized = diff / (img.width * img.height)
            diffs.append((i, diff_normalized))

        prev_img = img

    if not diffs:
        return {"error": "No frame diffs computed"}

    median_diff = sorted(d[1] for d in diffs)[len(diffs) // 2]
    threshold = max(median_diff * 3, 0.5)

    print(f"  Median frame diff: {median_diff:.2f}, threshold: {threshold:.2f}")

    change_frame = None
    for frame_idx, diff in diffs:
        if frame_idx > keystroke_frame and diff > threshold:
            change_frame = frame_idx
            print(f"  Change detected at frame {frame_idx} (diff={diff:.2f})")
            break

    if change_frame is None:
        top_diffs = sorted(diffs, key=lambda x: x[1], reverse=True)[:5]
        print(f"  No change detected. Top diffs: {top_diffs}")
        return {"error": "No display change detected after keystroke"}

    change_time = change_frame / fps
    latency_sec = change_time - keystroke_offset
    latency_ms = latency_sec * 1000

    # Cleanup frames
    for f in frame_files:
        f.unlink()
    frame_dir.rmdir()

    return {"keystroke_frame": keystroke_frame, "change_frame": change_frame, "latency_ms": latency_ms, "fps": fps}


def measure_once(bus: Gio.DBusConnection, key: str = "x", fps: int = 60, work_dir: Path | None = None) -> float | None:
    """Perform one latency measurement. Returns latency in ms, or None on failure."""
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="spice_latency_"))

    video_path = work_dir / "recording.webm"

    print(f"  Starting screencast at {fps}fps...")
    filename = start_screencast(bus, fps, video_path)
    recording_start = time.perf_counter()

    # Wait for recording to stabilize
    time.sleep(1.0)

    print("  Sending keystroke...")
    keystroke_time = send_keystroke(key)

    # Wait for the change to be captured
    time.sleep(1.0)

    print("  Stopping screencast...")
    stop_screencast(bus)
    recording_end = time.perf_counter()

    actual_path = Path(filename)
    print(f"  Recording complete ({recording_end - recording_start:.1f}s), file: {actual_path}")

    if not actual_path.exists():
        print(f"  Error: recording file not found at {actual_path}")
        return None

    # Analyze
    print("  Analyzing frames...")
    result = analyze_frames(actual_path, keystroke_time, recording_start, fps)

    if "error" in result:
        print(f"  Error: {result['error']}")
        return None

    return result["latency_ms"]


def main():
    parser = argparse.ArgumentParser(
        description="Measure SPICE input-to-display latency (Wayland/GNOME)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--samples", type=int, default=3, help="Number of measurements")
    parser.add_argument("--fps", type=int, default=60, help="Recording framerate")
    parser.add_argument("--key", type=str, default="x", help="Key to press")
    args = parser.parse_args()

    # Preflight checks
    for tool in ["ydotool", "ffmpeg"]:
        if not shutil.which(tool):
            print(f"Error: {tool} not found")
            sys.exit(1)

    print("SPICE Latency Measurement (Wayland/GNOME)")
    print("==========================================")
    print("Maximize the SPICE window before running.")
    print(f"Samples: {args.samples}, FPS: {args.fps}, Key: {args.key}")
    print()

    bus = _session_bus()
    latencies = []
    work_dir = Path(tempfile.mkdtemp(prefix="spice_latency_"))

    for i in range(args.samples):
        print(f"Measurement {i + 1}/{args.samples}:")
        latency = measure_once(bus, key=args.key, fps=args.fps, work_dir=work_dir)

        if latency is not None:
            latencies.append(latency)
            print(f"  Latency: {latency:.1f}ms")
        else:
            print("  Measurement failed")
        print()

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

    print(f"\nVideo files kept in: {work_dir}")


if __name__ == "__main__":
    main()
