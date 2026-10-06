"""Pull the metrics behind the note `2026_10_04_dropout.md` out of Mimir.

Writes one JSON bundle: whole-period series, 30 s zoom windows around the dropouts, and, for every
episode where the NVMe's hottest sensor reached `--threshold`, which pods wrote in the 15 minutes
before the peak. Standard library only, so it can run in a bare Python image next to Mimir.
"""

import argparse
import json
import logging
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

SCRAPE_WINDOW_FULL = "5m"
SCRAPE_WINDOW_ZOOM = "2m"  # node_exporter is scraped every 60 s, so a rate needs two samples
STEP_FULL = 300
STEP_ZOOM = 30
EPISODE_GAP_SECONDS = 1800
ATTRIBUTION_WINDOW = "15m"
ATTRIBUTION_DELAY_SECONDS = 120


@dataclass(frozen=True)
class Window:
    label: str
    start: float
    end: float


class Mimir:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _post(self, path: str, form: dict[str, str]) -> list[dict]:
        request = urllib.request.Request(f"{self.base_url}/api/v1/{path}", data=urllib.parse.urlencode(form).encode())
        with urllib.request.urlopen(request, timeout=170) as response:
            body = json.load(response)
        if body["status"] != "success":
            raise RuntimeError(f"{path} {form['query']=} failed: {body}")
        return body["data"]["result"]

    def range(self, query: str, window: Window, step: int) -> list[dict]:
        """One entry per label set: `{"labels": {...}, "values": [[t, v], ...]}`."""
        result = self._post(
            "query_range", {"query": query, "start": str(window.start), "end": str(window.end), "step": str(step)}
        )
        return [{"labels": r["metric"], "values": [[float(t), float(v)] for t, v in r["values"]]} for r in result]

    def instant(self, query: str, at: float) -> list[dict]:
        return self._post("query", {"query": query, "time": str(at)})

    def instances(self, node: str, window: Window) -> set[str]:
        """node_exporter instances that reported `node` at any time in `window`."""
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/series",
            data=urllib.parse.urlencode(
                {"match[]": f'node_uname_info{{nodename="{node}"}}', "start": str(window.start), "end": str(window.end)}
            ).encode(),
        )
        with urllib.request.urlopen(request, timeout=170) as response:
            return {series["instance"] for series in json.load(response)["data"]}


def full_queries(instance: str, node: str, window: str) -> dict[str, str]:
    node_exporter = f'instance="{instance}"'
    disk = f'{node_exporter},device="nvme0n1"'
    return {
        "temp_nvme": f'max(node_hwmon_temp_celsius{{{node_exporter},chip=~".*nvme.*"}})',
        "temp_nvme_composite": f'max(node_hwmon_temp_celsius{{{node_exporter},chip=~".*nvme.*",sensor="temp1"}})',
        "temp_cpu": f'max(node_hwmon_temp_celsius{{{node_exporter},chip=~".*coretemp.*"}})',
        "write_bytes": f"rate(node_disk_written_bytes_total{{{disk}}}[{window}])",
        "write_latency": (
            f"rate(node_disk_write_time_seconds_total{{{disk}}}[{window}])"
            f" / rate(node_disk_writes_completed_total{{{disk}}}[{window}])"
        ),
        "disk_busy": f"rate(node_disk_io_time_seconds_total{{{disk}}}[{window}])",
        "io_pressure_stalled": f"rate(node_pressure_io_stalled_seconds_total{{{node_exporter}}}[{window}])",
        "cpu_iowait": f'avg(rate(node_cpu_seconds_total{{{node_exporter},mode="iowait"}}[{window}]))*100',
        "cpu_busy": f'100-avg(rate(node_cpu_seconds_total{{{node_exporter},mode="idle"}}[{window}]))*100',
        "load1": f"node_load1{{{node_exporter}}}",
        "net_receive_bytes": f'rate(node_network_receive_bytes_total{{{node_exporter},device="eno1"}}[{window}])',
        "pods_haku_ci": f'count(kube_pod_info{{node="{node}",namespace="haku-ci"}})',
        "pods_tf_runner": f'count(kube_pod_info{{node="{node}",pod=~".*-tf-runner"}})',
    }


def namespace_writes_query(node: str, window: str) -> str:
    return f'sum by(namespace)(rate(container_fs_writes_bytes_total{{node="{node}",pod!=""}}[{window}]))'


def episodes(points: list[list[float]], threshold: float) -> list[tuple[float, float]]:
    """(time, temperature) of the peak of each run of readings at or above `threshold`, runs merged
    when closer than `EPISODE_GAP_SECONDS`."""
    peaks: list[tuple[float, float]] = []
    previous_hot = None
    for t, value in sorted(points):
        if value < threshold:
            continue
        if previous_hot is not None and t - previous_hot <= EPISODE_GAP_SECONDS:
            if value > peaks[-1][1]:
                peaks[-1] = (t, value)
        else:
            peaks.append((t, value))
        previous_hot = t
    return peaks


def first_value(result: list[dict]) -> float:
    """An instant query's single sample, 0 when the series does not exist (a count of nothing)."""
    return float(result[0]["value"][1]) if result else 0.0


def attribute_episode(mimir: Mimir, instance: str, node: str, peak: float) -> dict:
    at = peak + ATTRIBUTION_DELAY_SECONDS
    span = ATTRIBUTION_WINDOW
    device = mimir.instant(
        f'increase(node_disk_written_bytes_total{{instance="{instance}",device="nvme0n1"}}[{span}])', at
    )
    pods = mimir.instant(
        f'topk(4, sum by(namespace,pod)(increase(container_fs_writes_bytes_total{{node="{node}",pod!=""}}[{span}])))',
        at,
    )
    tf_pods = mimir.instant(f'count(count_over_time(kube_pod_info{{node="{node}",pod=~".*-tf-runner"}}[{span}]))', at)
    ci_pods = mimir.instant(f'count(count_over_time(kube_pod_info{{node="{node}",namespace="haku-ci"}}[{span}]))', at)
    return {
        "device_bytes": first_value(device),
        "top_pods": [[f"{r['metric']['namespace']}/{r['metric']['pod']}", float(r["value"][1])] for r in pods],
        "tf_runner_pods": int(first_value(tf_pods)),
        "haku_ci_pods": int(first_value(ci_pods)),
    }


def parse_time(text: str) -> float:
    return datetime.fromisoformat(text).astimezone(UTC).timestamp()


def parse_zoom(text: str) -> Window:
    label, span = text.split("=", 1)
    start, end = span.split(",", 1)
    return Window(label, parse_time(start), parse_time(end))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="Mimir prometheus API base, e.g. http://host/prometheus")
    parser.add_argument(
        "--node", default="optiplex", help="Kubernetes node name (its node_exporter instance is looked up)"
    )
    parser.add_argument("--start", default=None, help="ISO time; default 21 days before --end")
    parser.add_argument("--end", default=None, help="ISO time; default now")
    parser.add_argument("--threshold", type=float, default=85.0, help="NVMe °C that makes an episode")
    parser.add_argument(
        "--zoom",
        action="append",
        default=[],
        metavar="LABEL=START,END",
        help="30 s resolution window, repeatable, e.g. 2026-10-04=2026-10-04T22:20Z,2026-10-04T23:55Z",
    )
    parser.add_argument("--out", default="-", help="output file, or - for stdout")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    end = parse_time(args.end) if args.end else datetime.now(UTC).timestamp()
    start = parse_time(args.start) if args.start else end - timedelta(days=21).total_seconds()
    period = Window("period", start, end)
    mimir = Mimir(args.url)

    found = mimir.instances(args.node, period)
    if len(found) != 1:
        raise RuntimeError(f"expected one node_exporter instance for {args.node=}, found {found=}")
    (instance,) = found
    logger.info("node %s is node_exporter instance %s", args.node, instance)

    bundle: dict = {
        "node": args.node,
        "instance": instance,
        "start": start,
        "end": end,
        "threshold": args.threshold,
        "series": {},
        "zoom": {},
        "episodes": [],
    }
    for name, query in full_queries(instance, args.node, SCRAPE_WINDOW_FULL).items():
        bundle["series"][name] = mimir.range(query, period, STEP_FULL)
        logger.info("%s: %d series", name, len(bundle["series"][name]))
    bundle["series"]["namespace_write_bytes"] = mimir.range(
        namespace_writes_query(args.node, SCRAPE_WINDOW_FULL), period, STEP_FULL
    )

    for zoom_arg in args.zoom:
        window = parse_zoom(zoom_arg)
        bundle["zoom"][window.label] = {"start": window.start, "end": window.end, "series": {}}
        for name, query in full_queries(instance, args.node, SCRAPE_WINDOW_ZOOM).items():
            bundle["zoom"][window.label]["series"][name] = mimir.range(query, window, STEP_ZOOM)
        bundle["zoom"][window.label]["series"]["namespace_write_bytes"] = mimir.range(
            namespace_writes_query(args.node, "1m"), window, 15
        )
        logger.info("zoom %s done", window.label)

    temperature = [point for series in bundle["series"]["temp_nvme"] for point in series["values"]]
    for peak, value in episodes(temperature, args.threshold):
        attribution = attribute_episode(mimir, instance, args.node, peak)
        bundle["episodes"].append({"time": peak, "temperature": value, **attribution})
        logger.info("episode %s %.0f °C", datetime.fromtimestamp(peak, UTC).isoformat(), value)

    text = json.dumps(bundle, separators=(",", ":"))
    if args.out == "-":
        sys.stdout.write(text)
    else:
        Path(args.out).write_text(text)


if __name__ == "__main__":
    main()
