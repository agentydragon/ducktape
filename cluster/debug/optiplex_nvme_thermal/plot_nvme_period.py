"""Plot and summarise a bundle written by `fetch_metrics.py`.

Writes into `--out`: `overview.png` for the whole period and one `period_N.png` per `--days` days (NVMe temperature, disk writes
stacked by workload, write latency, pods on the node), one `dropout_<label>.png` per zoom window,
and `stats.md` with the correlation, dose-response and per-episode tables the debug note quotes.

Colour belongs to workloads (the first three slots of the repo's validated chart palette); every
other quantity is ink or grey, so no colour means two things.
"""

import argparse
import json
import logging
import re
import sys
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import NamedTuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

logger = logging.getLogger(__name__)

SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
WORKLOAD_COLORS = {"haku-ci": "#2a78d6", "flux-system": "#eb6834", "cpap-sync": "#1baf7a"}
OTHER_COLOR = "#b9b8b2"
WORKLOADS = [*WORKLOAD_COLORS, "other"]
DRIVE_WARNING_C = 83.85
DRIVE_CRITICAL_C = 84.85
HOT_C = 80.0
MB = 1e6
GAP_SECONDS = 900.0  # the full-period series have a 300 s step
ZOOM_GAP_SECONDS = 75.0
DROPOUT_MIN_GAP_SECONDS = 1200.0

plt.switch_backend("Agg")
plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "axes.edgecolor": MUTED,
        "axes.labelcolor": INK2,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "text.color": INK,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "font.size": 10,
        "lines.linewidth": 1.6,
    }
)


class Series(NamedTuple):
    t: np.ndarray
    v: np.ndarray

    def at(self, grid: np.ndarray) -> np.ndarray:
        """Values on `grid`, NaN outside the series and across gaps wider than three of its steps."""
        if not len(self.t):
            return np.full(len(grid), np.nan)
        out = np.interp(grid, self.t, self.v, left=np.nan, right=np.nan)
        step = float(np.median(np.diff(self.t))) if len(self.t) > 1 else 1.0
        after = np.searchsorted(self.t, grid)
        for i, k in enumerate(after):
            if 0 < k < len(self.t) and self.t[k] - self.t[k - 1] > 3 * step:
                out[i] = np.nan
        return out

    def window(self, start: float, end: float) -> Series:
        keep = (self.t >= start) & (self.t <= end)
        return Series(self.t[keep], self.v[keep])


def merge(entries: list[dict]) -> Series:
    """One series from every label set a query returned (a node's series split when labels changed)."""
    values: dict[float, list[float]] = {}
    for entry in entries:
        for t, v in entry["values"]:
            values.setdefault(t, []).append(v)
    times = np.array(sorted(values))
    return Series(times, np.array([np.mean(values[t]) for t in times]))


def namespace_series(entries: list[dict]) -> dict[str, Series]:
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        namespace = entry["labels"]["namespace"]
        grouped.setdefault(namespace if namespace in WORKLOAD_COLORS else "other", []).append(entry)
    result: dict[str, Series] = {}
    for group, members in grouped.items():
        times = sorted({t for e in members for t, _ in e["values"]})
        totals = dict.fromkeys(times, 0.0)
        for entry in members:
            for t, v in entry["values"]:
                totals[t] += v
        result[group] = Series(np.array(times), np.array([totals[t] for t in times]))
    return result


def as_datetimes(times: np.ndarray) -> list[datetime]:
    return [datetime.fromtimestamp(t, UTC) for t in times]


def plot_line(ax: plt.Axes, series: Series, gap: float, **kwargs) -> None:
    """Draw `series` without bridging gaps wider than `gap` seconds."""
    times: list[float] = []
    values: list[float] = []
    for i, (t, v) in enumerate(zip(series.t, series.v, strict=True)):
        if i and t - series.t[i - 1] > gap:
            times.append(series.t[i - 1] + 1)
            values.append(np.nan)
        times.append(t)
        values.append(v)
    ax.plot(as_datetimes(np.array(times)), values, **kwargs)


def find_dropouts(nvme: Series, cpu: Series) -> list[float]:
    """Times the NVMe sensor went silent while the CPU sensor of the same host kept reporting."""
    dropouts = []
    for i in range(1, len(nvme.t)):
        before, after = nvme.t[i - 1], nvme.t[i]
        if after - before >= DROPOUT_MIN_GAP_SECONDS and np.any((cpu.t > before + 60) & (cpu.t < after - 60)):
            dropouts.append(float(before))
    if len(nvme.t) and cpu.t.max() - nvme.t.max() >= DROPOUT_MIN_GAP_SECONDS:
        dropouts.append(float(nvme.t.max()))
    return dropouts


def stack_panel(ax: plt.Axes, workloads: dict[str, Series], device: Series, grid: np.ndarray, gap: float) -> None:
    layers = [(name, np.nan_to_num(workloads[name].at(grid)) / MB) for name in WORKLOADS if name in workloads]
    colors = [WORKLOAD_COLORS.get(name, OTHER_COLOR) for name, _ in layers]
    ax.stackplot(
        as_datetimes(grid), [values for _, values in layers], colors=colors, labels=[n for n, _ in layers], lw=0
    )
    plot_line(ax, Series(device.t, device.v / MB), gap, color=INK, lw=1.0, label="device total")
    ax.set_ylabel("disk writes (MB/s)")
    legend_above(ax, ncol=5)


def legend_above(ax: plt.Axes, **kwargs) -> None:
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), frameon=False, fontsize=8.5, **kwargs)


def style_time_axis(ax: plt.Axes, start: float, end: float) -> None:
    locator = mdates.AutoDateLocator(tz=UTC)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=UTC))
    ax.set_xlim(datetime.fromtimestamp(start, UTC), datetime.fromtimestamp(end, UTC))


def figure_week(bundle: dict, start: float, end: float, dropouts: list[float], path: Path) -> None:
    s = {name: merge(entries) for name, entries in bundle["series"].items() if name != "namespace_write_bytes"}
    workloads = namespace_series(bundle["series"]["namespace_write_bytes"])
    grid = np.arange(start, end, 300.0)
    fig, ax = plt.subplots(4, 1, figsize=(12, 10), sharex=True, gridspec_kw={"hspace": 0.4})

    plot_line(ax[0], s["temp_nvme"], GAP_SECONDS, color=INK, label="NVMe, hottest sensor")
    plot_line(ax[0], s["temp_cpu"], GAP_SECONDS, color=MUTED, lw=1.1, label="CPU package")
    ax[0].axhline(DRIVE_CRITICAL_C, color=INK2, lw=0.9, ls=":")
    ax[0].text(
        datetime.fromtimestamp(start, UTC) + timedelta(hours=2),
        DRIVE_CRITICAL_C + 1,
        "drive critical 84.85 °C",
        fontsize=8.5,
    )
    hot = [(e["time"], e["temperature"]) for e in bundle["episodes"] if start <= e["time"] < end]
    ax[0].plot(
        as_datetimes(np.array([t for t, _ in hot])),
        [v for _, v in hot],
        "v",
        color=INK,
        ms=6,
        ls="none",
        label="episode peak",
    )
    ax[0].set_ylabel("temperature (°C)")
    ax[0].set_ylim(35, 108)
    legend_above(ax[0], ncol=4)

    stack_panel(ax[1], workloads, s["write_bytes"], grid, GAP_SECONDS)

    plot_line(ax[2], s["write_latency"], GAP_SECONDS, color=INK)
    ax[2].axhline(1.0, color=INK2, lw=0.9, ls=":")
    ax[2].set_yscale("log")
    ax[2].set_ylabel("write latency\n(s/op, log)")

    reporting = s["cpu_busy"].window(start, end)
    for name, label in (("pods_haku_ci", "haku-ci pods"), ("pods_tf_runner", "Tofu runner pods")):
        counts = Series(reporting.t, np.nan_to_num(s[name].at(reporting.t)))
        group = "haku-ci" if "ci" in name else "flux-system"
        plot_line(ax[3], counts, GAP_SECONDS, color=WORKLOAD_COLORS[group], label=label, lw=1.0)
    ax[3].set_ylabel("pods on the node")
    legend_above(ax[3], ncol=2)

    for axis in ax:
        for t in dropouts:
            if start <= t < end:
                axis.axvline(datetime.fromtimestamp(t, UTC), color=INK, lw=1.1)
        style_time_axis(axis, start, end)
    for t in dropouts:
        if start <= t < end:
            ax[0].text(
                datetime.fromtimestamp(t, UTC) + timedelta(hours=1), 101, "NVMe sensor vanishes", fontsize=8.5, va="top"
            )
    fig.suptitle(
        f"{bundle['node']}: {datetime.fromtimestamp(start, UTC):%Y-%m-%d} to {datetime.fromtimestamp(end, UTC):%Y-%m-%d} (UTC)",
        x=0.01,
        ha="left",
        fontsize=12.5,
        y=0.93,
    )
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def figure_zoom(bundle: dict, label: str, path: Path) -> None:
    zoom = bundle["zoom"][label]
    start, end = zoom["start"], zoom["end"]
    s = {name: merge(entries) for name, entries in zoom["series"].items() if name != "namespace_write_bytes"}
    workloads = namespace_series(zoom["series"]["namespace_write_bytes"])
    vanish = float(s["temp_nvme"].t.max()) if len(s["temp_nvme"].t) else end
    fig, ax = plt.subplots(7, 1, figsize=(11, 16), sharex=True, gridspec_kw={"hspace": 0.42})

    plot_line(ax[0], s["temp_nvme"], ZOOM_GAP_SECONDS, color=INK, label="NVMe, hottest sensor")
    plot_line(
        ax[0], s["temp_nvme_composite"], ZOOM_GAP_SECONDS, color=INK, lw=1.1, ls="--", label="NVMe, composite sensor"
    )
    plot_line(ax[0], s["temp_cpu"], ZOOM_GAP_SECONDS, color=MUTED, label="CPU package")
    ax[0].axhline(DRIVE_CRITICAL_C, color=INK2, lw=0.9, ls=":")
    ax[0].set_ylabel("temperature (°C)")
    legend_above(ax[0], ncol=3)

    grid = np.arange(start, end, 30.0)
    stack_panel(ax[1], workloads, s["write_bytes"], grid, ZOOM_GAP_SECONDS)

    plot_line(ax[2], s["write_latency"], ZOOM_GAP_SECONDS, color=INK)
    ax[2].set_yscale("log")
    ax[2].set_ylabel("write latency\n(s/op, log)")

    plot_line(ax[3], Series(s["disk_busy"].t, s["disk_busy"].v * 100), ZOOM_GAP_SECONDS, color=INK, label="device busy")
    plot_line(
        ax[3],
        Series(s["io_pressure_stalled"].t, s["io_pressure_stalled"].v * 100),
        ZOOM_GAP_SECONDS,
        color=MUTED,
        ls="--",
        label="I/O pressure (all tasks stalled)",
    )
    ax[3].set_ylabel("percent")
    legend_above(ax[3], ncol=2)

    plot_line(ax[4], s["cpu_busy"], ZOOM_GAP_SECONDS, color=INK, label="CPU busy %")
    plot_line(ax[4], s["cpu_iowait"], ZOOM_GAP_SECONDS, color=MUTED, ls="--", label="CPU iowait %")
    ax[4].set_ylabel("percent")
    legend_above(ax[4], ncol=2)

    plot_line(ax[5], s["load1"], ZOOM_GAP_SECONDS, color=INK)
    ax[5].set_ylabel("load average (1 min)")

    reporting = s["cpu_busy"]
    for name, group, text in (
        ("pods_haku_ci", "haku-ci", "haku-ci pods"),
        ("pods_tf_runner", "flux-system", "Tofu runner pods"),
    ):
        plot_line(
            ax[6],
            Series(reporting.t, np.nan_to_num(s[name].at(reporting.t))),
            ZOOM_GAP_SECONDS,
            color=WORKLOAD_COLORS[group],
            label=text,
        )
    ax[6].set_ylabel("pods on the node")
    legend_above(ax[6], ncol=2)

    for axis in ax:
        axis.axvline(datetime.fromtimestamp(vanish, UTC), color=INK, lw=1.1)
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=UTC))
        axis.xaxis.set_major_locator(mdates.MinuteLocator(byminute=range(0, 60, 10), tz=UTC))
        axis.set_xlim(datetime.fromtimestamp(start, UTC), datetime.fromtimestamp(end, UTC))
    ax[0].text(datetime.fromtimestamp(vanish + 60, UTC), 99, "NVMe sensor vanishes", fontsize=8.5, va="top")
    ax[6].set_xlabel(f"{datetime.fromtimestamp(start, UTC):%Y-%m-%d}, UTC")
    fig.suptitle(f"{bundle['node']}: window {label}", x=0.01, ha="left", fontsize=12.5, y=0.915)
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def short_pod(name: str) -> str:
    return re.sub(r"-[a-z0-9]{5}$", "", re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$", "", name))


def lag_table(bundle: dict) -> str:
    metrics = {
        "CPU busy": "cpu_busy",
        "device busy": "disk_busy",
        "I/O pressure": "io_pressure_stalled",
        "CPU iowait": "cpu_iowait",
        "network receive": "net_receive_bytes",
        "load average": "load1",
        "write latency": "write_latency",
        "disk writes": "write_bytes",
        "CPU package temperature": "temp_cpu",
    }
    lines = []
    for label, zoom in bundle["zoom"].items():
        s = {name: merge(entries) for name, entries in zoom["series"].items() if name != "namespace_write_bytes"}
        grid = s["temp_nvme"].t
        temperature = s["temp_nvme"].v
        rows = []
        for title, name in metrics.items():
            values = s[name].at(grid)
            row = []
            for lag_minutes in (0, 2, 4, 6):
                lag = lag_minutes * 2  # 30 s samples
                a, b = (temperature[lag:], values[: len(values) - lag]) if lag else (temperature, values)
                ok = ~np.isnan(a) & ~np.isnan(b)
                row.append(f"{np.corrcoef(a[ok], b[ok])[0, 1]:.2f}" if ok.sum() > 20 else "n/a")
            rows.append(f"| {title} | " + " | ".join(row) + " |")
        lines += [
            f"**{label}**: correlation (Pearson r) of the NVMe's hottest sensor with each metric, the metric leading by:",
            "",
            "| Metric | 0 min | 2 min | 4 min | 6 min |",
            "| --- | --- | --- | --- | --- |",
            *rows,
            "",
        ]
    return "\n".join(lines)


def dose_response(bundle: dict) -> str:
    temperature = merge(bundle["series"]["temp_nvme"])
    writes = merge(bundle["series"]["write_bytes"])
    joined = dict(zip(temperature.t, temperature.v, strict=True))
    pairs = np.array([(w / MB, joined[t]) for t, w in zip(writes.t, writes.v, strict=True) if t in joined])
    edges = [0.0, 1.0, 5.0, 10.0, 20.0, 40.0, np.inf]
    lines = [
        f"Dose and response: {len(pairs)} five-minute bins, overall r = {np.corrcoef(pairs[:, 0], pairs[:, 1])[0, 1]:.2f}.",
        "",
        "| Write rate (MB/s) | Bins | Mean °C | 95th percentile °C | Max °C | Bins at or above 80 °C |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for low, high in pairwise(edges):
        picked = pairs[(pairs[:, 0] >= low) & (pairs[:, 0] < high)][:, 1]
        if len(picked):
            name = f"{low:g} to {high:g}" if np.isfinite(high) else f"{low:g} and over"
            lines.append(
                f"| {name} | {len(picked)} | {picked.mean():.0f} | {np.percentile(picked, 95):.0f} | {picked.max():.0f} | {np.mean(picked >= HOT_C) * 100:.1f} % |"
            )
    return "\n".join(lines)


def episode_table(bundle: dict, dropouts: list[float]) -> str:
    lines = [
        f"Episodes at or above {bundle['threshold']:g} °C. Device GB is written in the 15 minutes to two minutes after the peak; "
        "container counters start at each pod's first sample, so short-lived pods are under-counted.",
        "",
        "| Peak (UTC) | °C | Device GB | haku-ci pods | Tofu runner pods | Largest writers (GB) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for e in bundle["episodes"]:
        near_dropout = any(0 <= d - e["time"] <= 3600 for d in dropouts)
        stamp = f"{datetime.fromtimestamp(e['time'], UTC):%m-%d %H:%M}"
        writers = ", ".join(f"{short_pod(p)} {b / 1e9:.1f}" for p, b in e["top_pods"] if b >= 5e7) or "none"
        lines.append(
            f"| {'**' + stamp + '**' if near_dropout else stamp} | {e['temperature']:.0f} | {e['device_bytes'] / 1e9:.1f} "
            f"| {e['haku_ci_pods']} | {e['tf_runner_pods']} | {writers} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", type=Path, help="JSON written by fetch_metrics.py")
    parser.add_argument("--out", type=Path, default=Path("plots"))
    parser.add_argument("--days", type=float, default=3.0, help="days per period page")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    bundle = json.loads(args.bundle.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    dropouts = find_dropouts(merge(bundle["series"]["temp_nvme"]), merge(bundle["series"]["temp_cpu"]))
    logger.info("dropouts: %s", [f"{datetime.fromtimestamp(t, UTC):%Y-%m-%d %H:%M}" for t in dropouts])

    start, end = bundle["start"], bundle["end"]
    figure_week(bundle, start, end, dropouts, args.out / "overview.png")
    page = args.days * 86400.0
    for n, page_start in enumerate(np.arange(start, end, page), start=1):
        figure_week(
            bundle, float(page_start), min(float(page_start) + page, end), dropouts, args.out / f"period_{n}.png"
        )
    for label in bundle["zoom"]:
        figure_zoom(bundle, label, args.out / f"dropout_{label}.png")

    stats = "\n\n".join([lag_table(bundle), dose_response(bundle), episode_table(bundle, dropouts)])
    (args.out / "stats.md").write_text(stats + "\n")
    sys.stdout.write(stats + "\n")


if __name__ == "__main__":
    main()
