"""
Record SPICE latency measurement data (Wayland/GNOME).

Captures a screencast with an embedded millisecond clock while typing
timestamps into a focused SPICE/vim window. Saves video, extracted frames,
and metadata for offline analysis by analyze.py.

Setup:
    1. cd debug/spice_lag
    2. Open SPICE client to VM, place window so it has focus
    3. In VM: nvim --clean -c "set guicursor=a:blinkon0" -c "startinsert"
    4. Run: python record.py [--samples N] [--fps F] [--delay D]

Requirements:
    - ffmpeg, ydotool (installed via ansible/atlas.yaml)
    - ydotoold running
    - python3-gi (system package)
"""

import argparse
import datetime
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402


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


def _now_str() -> str:
    """Current wall-clock time as HH:MM:SS.mmm."""
    now = datetime.datetime.now()
    return now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def run_clock(stop_event: threading.Event) -> None:
    """Print rapidly updating clock to terminal until stop_event is set."""
    while not stop_event.is_set():
        sys.stdout.write(f"\rClock: {_now_str()}  ")
        sys.stdout.flush()
        time.sleep(0.010)
    sys.stdout.write("\r" + " " * 40 + "\r")
    sys.stdout.flush()


# Non-confusable characters for markers, excluding visually ambiguous pairs
# (0/O, 1/l/I, 5/S, 2/Z, 8/B, etc.).
# Shuffled per-run so vision models aren't primed by alphabetical sequence.
_MARKER_POOL = list("acdefghjkmnpqrtuvwxyACDEFGHJKLMNPQRTUVWXY3467")


def _pick_markers(n: int) -> list[str]:
    """Pick n unique shuffled markers from the pool."""
    if n > len(_MARKER_POOL):
        raise ValueError(f"Max {len(_MARKER_POOL)} samples supported (requested {n})")
    return random.sample(_MARKER_POOL, n)


def type_marker(char: str) -> str:
    """Type a single-char marker via ydotool. Returns the timestamp at send time.

    Uses default ydotool delays (20ms hold + 20ms inter-key), which adds ~40ms
    to measured latency for the single character.
    """
    ts = _now_str()
    result = subprocess.run(["ydotool", "type", char], check=False, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ydotool failed (exit {result.returncode}):\n"
            f"  stdout: {result.stdout.decode(errors='replace')}\n"
            f"  stderr: {result.stderr.decode(errors='replace')}"
        )
    return ts


def main():
    parser = argparse.ArgumentParser(
        description="Record SPICE latency measurement data", formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--samples", type=int, default=3, help="Number of measurements")
    parser.add_argument("--fps", type=int, default=60, help="Recording framerate (requested)")
    parser.add_argument("--delay", type=float, default=3.0, help="Seconds between keystrokes")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory (default: auto tmpdir)")
    args = parser.parse_args()

    for tool in ["ydotool", "ffmpeg"]:
        if not shutil.which(tool):
            raise RuntimeError(f"{tool} not found in PATH")

    ydotool_socket = Path("/tmp/.ydotool_socket")
    if not ydotool_socket.exists():
        raise RuntimeError(
            "ydotoold not running (/tmp/.ydotool_socket not found)\n"
            "Start it with: sudo ydotoold --socket-path=/tmp/.ydotool_socket --socket-perm=0666"
        )

    uinput = Path("/dev/uinput")
    if uinput.exists() and not os.access(uinput, os.W_OK):
        raise RuntimeError("/dev/uinput not writable by current user\nFix with: sudo chmod 0666 /dev/uinput")

    work_dir = args.output_dir or Path(tempfile.mkdtemp(prefix="spice_latency_"))
    work_dir.mkdir(parents=True, exist_ok=True)
    video_path = work_dir / "recording.webm"

    print("SPICE Latency Recording")
    print("=======================")
    print(f"Samples: {args.samples}, FPS: {args.fps}, Output: {work_dir}")
    print("Note: ydotool default delays (20ms hold + 20ms inter-key) add ~40ms to measurements")
    print()
    print("Focus the SPICE window now. Starting in 5 seconds...")
    time.sleep(5.0)

    bus = _session_bus()

    print(f"Starting screencast at {args.fps}fps...")
    filename = start_screencast(bus, args.fps, video_path)
    recording_start = time.perf_counter()

    clock_stop = threading.Event()
    clock_thread = threading.Thread(target=run_clock, args=(clock_stop,), daemon=True)
    clock_thread.start()

    time.sleep(1.0)

    markers = _pick_markers(args.samples)

    sent_timestamps = []
    keystroke_perf_times = []
    for i, char in enumerate(markers):
        ts = type_marker(char)
        perf_time = time.perf_counter()
        sent_timestamps.append(ts)
        keystroke_perf_times.append(perf_time)
        sys.stdout.write(f"\n  [{i + 1}/{args.samples}] sent at {ts}\n")
        sys.stdout.flush()
        if i < args.samples - 1:
            time.sleep(args.delay)

    time.sleep(3.0)

    clock_stop.set()
    clock_thread.join()
    stop_screencast(bus)
    recording_end = time.perf_counter()
    recording_duration = recording_end - recording_start

    actual_path = Path(filename)
    print(f"Recording complete ({recording_duration:.1f}s), file: {actual_path}")

    if not actual_path.exists():
        print(f"Error: recording file not found at {actual_path}")
        sys.exit(1)

    metadata = {
        "markers": markers,
        "sent_timestamps": sent_timestamps,
        "keystroke_perf_times": keystroke_perf_times,
        "recording_start": recording_start,
        "recording_duration": recording_duration,
        "fps_requested": args.fps,
    }
    metadata_path = work_dir / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2))

    print(f"\nRecording saved to: {work_dir}")


if __name__ == "__main__":
    main()
