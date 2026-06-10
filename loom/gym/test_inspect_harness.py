from __future__ import annotations

import asyncio
import json
import math
import subprocess
import threading
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest
import pytest_bazel
import yaml

# Aliased to avoid shadowing the builtin.
from inspect_ai import eval as inspect_eval
from inspect_ai.model import ModelOutput, get_model
from more_itertools import one

from loom.gym.dossier import series_dossier
from loom.gym.inspect_harness import WAYBACK_PROXY_IMAGE_TAG, agent_eval_task, sample_for_task, write_sandbox_compose
from loom.gym.monthly_series import MonthlySeries, add_months
from loom.gym.series_tasks import SeriesTaskSpec, tasks_for_spec
from loom.gym.task import BinaryOutcome, EvidenceItem
from loom.wayback_proxy import fake_ia
from third_party.containers.rlocations import PYTHON_3_13_SLIM
from util.oci import OciImage, load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

WAYBACK_PROXY_IMAGE = OciImage("_main/loom/wayback_proxy/image_info.rloc", WAYBACK_PROXY_IMAGE_TAG)

# Linear ramp over 2020-01..2020-12: from anchor 2020-01 (level 100) the ramp
# reaches 112 by +6m, crossing the 1.05x threshold (105) → outcome YES. Its
# as_of (2020-02-01) postdates the fake IA's canned capture (2020-01-15), so
# the clamped proxy serves that capture to the agent.
RAMP = MonthlySeries(
    series_id="ramp",
    description="test ramp",
    unit="units",
    provenance="synthetic",
    values={add_months(date(2020, 1, 1), n): 100.0 + 2 * n for n in range(12)},
)

GYM_TASK = one(
    tasks_for_spec(
        SeriesTaskSpec(series=RAMP, binary_thresholds=((6, 1.05),), scalar_horizons=()),
        anchor_start=date(2020, 1, 1),
        anchor_step_months=12,
    )
)

# The same task with an evidence lead pointing at the fake IA's canned page,
# given as an https:// URL: the agent discovers it in /data/sources.txt and
# fetches it through the clamped MITM proxy without rewriting to http://.
EVIDENCE_TASK = GYM_TASK.model_copy(
    update={
        "evidence": (
            EvidenceItem(
                url=fake_ia.EXAMPLE_ORIGINAL,
                archived_url=f"https://web.archive.org/web/{fake_ia.GOOD_TS}/{fake_ia.EXAMPLE_ORIGINAL}",
                date=date(2020, 1, 15),
                title="archived example.com homepage",
            ),
        )
    }
)


def test_sample_carries_dossier_task_and_sandbox(tmp_path: Path) -> None:
    assert GYM_TASK.outcome == BinaryOutcome(value=True)
    compose_path = write_sandbox_compose(tmp_path, GYM_TASK.as_of, "https://web.archive.org")
    sample = sample_for_task(GYM_TASK, series_dossier([RAMP], GYM_TASK.as_of), compose_path)
    assert sample.files is not None
    assert {"/data/README.txt", "/data/ramp_monthly.csv"} <= set(sample.files)
    assert "/data/sources.txt" not in sample.files
    assert sample.metadata is not None
    assert sample.metadata["gym_task"]["task_id"] == GYM_TASK.task_id
    assert json.loads(str(sample.target))["value"] is True
    assert "Submit ONLY a JSON object" in str(sample.input)
    assert sample.sandbox is not None
    assert sample.sandbox.config == str(compose_path)


def test_evidence_lands_as_url_file_never_in_prompt(tmp_path: Path) -> None:
    compose_path = write_sandbox_compose(tmp_path, EVIDENCE_TASK.as_of, "https://web.archive.org")
    sample = sample_for_task(EVIDENCE_TASK, series_dossier([RAMP], EVIDENCE_TASK.as_of), compose_path)
    assert sample.files is not None
    sources = sample.files["/data/sources.txt"]
    # URLs only — no titles (a title could carry the curator's framing).
    url_lines = [line for line in sources.splitlines() if line.startswith("http")]
    assert url_lines == [fake_ia.EXAMPLE_ORIGINAL]
    assert "archived example.com homepage" not in sources
    # Evidence is data the agent chooses to read, not prompt content.
    assert fake_ia.EXAMPLE_ORIGINAL not in str(sample.input)


def test_sandbox_compose_isolates_agent_behind_clamped_proxy(tmp_path: Path) -> None:
    compose_path = write_sandbox_compose(tmp_path, date(2020, 2, 1), "http://host.docker.internal:9999")
    config = yaml.safe_load(compose_path.read_text())
    # The agent's only network is internal; its sole peer is the proxy, whose
    # clamp date is a baked literal the agent cannot influence.
    agent = config["services"]["default"]
    assert agent["networks"] == ["sandbox"]
    assert "network_mode" not in agent
    assert config["networks"]["sandbox"]["internal"] is True
    # https egress goes through the same proxy, and the agent trusts the MITM
    # CA from the shared volume — so https:// works without rewriting to http.
    assert agent["environment"]["HTTPS_PROXY"] == "http://proxy:8080"
    assert agent["environment"]["SSL_CERT_FILE"] == "/wayback-ca/mitmproxy-ca-cert.pem"
    assert "wayback-ca:/wayback-ca:ro" in agent["volumes"]
    proxy = config["services"]["proxy"]
    assert proxy["environment"]["WAYBACK_AS_OF"] == "2020-02-01"
    assert proxy["environment"]["WAYBACK_UPSTREAM"] == "http://host.docker.internal:9999"
    assert proxy["environment"]["WAYBACK_CONFDIR"] == "/wayback-ca"
    assert set(proxy["networks"]) == {"sandbox", "egress"}


@pytest.fixture
def fake_upstream_port() -> Iterator[int]:
    # The fake IA needs its event loop RUNNING for the whole test (a parked
    # loop still TCP-accepts via the kernel backlog but never serves — the
    # proxy then hangs on its upstream call). inspect_eval() owns this
    # thread's loop, so the fake gets a dedicated loop on a daemon thread.
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    runner = asyncio.run_coroutine_threadsafe(fake_ia.start(host="0.0.0.0"), loop).result(timeout=30)
    yield int(runner.addresses[0][1])
    asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(timeout=30)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=30)
    loop.close()


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False).stdout


def _dump_sandbox_diagnostics_and_cleanup(upstream_port: int) -> None:
    """Proxy-side truth (container logs incl. the evidence manifest, network
    config, host-gateway reachability) into undeclared outputs, then tear down
    what sandbox_cleanup=False kept alive — RBE workers share a docker daemon
    across actions."""
    out = undeclared_outputs_dir()
    listing = _docker(
        "ps",
        "-a",
        "--filter",
        f"ancestor={WAYBACK_PROXY_IMAGE_TAG}",
        "--format",
        '{{.ID}} {{.Label "com.docker.compose.project"}}',
    )
    (out / "proxy_containers.txt").write_text(listing)
    projects = set()
    for line in listing.splitlines():
        container_id, _, project = line.partition(" ")
        result = subprocess.run(["docker", "logs", container_id], capture_output=True, text=True, check=False)
        (out / f"proxy-{container_id}.log").write_text(result.stdout + result.stderr)
        (out / f"proxy-{container_id}-hostconfig.json").write_text(
            _docker(
                "inspect",
                "--format",
                "{{json .HostConfig.ExtraHosts}} {{json .NetworkSettings.Networks}}",
                container_id,
            )
        )
        probe = subprocess.run(
            [
                "docker",
                "exec",
                container_id,
                "python3",
                "-c",
                "import socket\n"
                "print('resolves to', socket.gethostbyname('host.docker.internal'))\n"
                f"socket.create_connection(('host.docker.internal', {upstream_port}), timeout=5)\n"
                "print('upstream reachable')",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        (out / f"proxy-{container_id}-upstream-probe.txt").write_text(probe.stdout + probe.stderr)
        if project:
            projects.add(project)
    for project in sorted(projects):
        network_ids = _docker("network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}")
        for network_id in network_ids.split():
            (out / f"network-{network_id}.json").write_text(_docker("network", "inspect", network_id))
        subprocess.run(
            ["docker", "compose", "-p", project, "down", "-v", "--remove-orphans"], capture_output=True, check=False
        )


def test_agent_answers_in_sandbox(tmp_path: Path, fake_upstream_port: int) -> None:
    # Scripted model: discover the evidence lead in /data/evidence.jsonl,
    # fetch it through the clamped proxy, read the mounted dossier with bash,
    # then submit p=0.8. Proves end-to-end: sandbox up with the proxy
    # sidecar, archived web reachable from an otherwise route-less container,
    # evidence-as-files, the served-evidence manifest landing in the score,
    # and the gym's proper loss (outcome YES → -ln(0.8)).
    load_oci_image(WAYBACK_PROXY_IMAGE)
    load_oci_image(PYTHON_3_13_SLIM)
    # Bash-only tool: the agent runs python via the shell. Fetch the https://
    # evidence lead through the clamped MITM proxy unmodified — urllib honors
    # https_proxy and trusts the proxy CA via SSL_CERT_FILE. The harness's rich
    # sandbox image is for real runs; the mechanics test uses the lightweight
    # bazel-loaded python:3.13-slim (also has bash + urllib).
    fetch_cmd = dedent(
        """
        python3 - <<'PY'
        import urllib.request
        url = next(line for line in open("/data/sources.txt") if line.startswith("http")).strip()
        print("lead:", url)
        with urllib.request.urlopen(url, timeout=30) as response:
            print(response.status, response.headers["X-Wayback-Timestamp"])
            print(response.read().decode())
        PY
        """
    )
    model = get_model(
        "mockllm/model",
        custom_outputs=[
            ModelOutput.for_tool_call("mockllm/model", "bash", {"cmd": fetch_cmd}),
            ModelOutput.for_tool_call("mockllm/model", "bash", {"cmd": "head -3 /data/ramp_monthly.csv"}),
            ModelOutput.for_tool_call("mockllm/model", "submit", {"answer": json.dumps({"p": 0.8})}),
        ],
    )
    try:
        logs = inspect_eval(
            agent_eval_task(
                [EVIDENCE_TASK],
                [RAMP],
                wayback_upstream=f"http://host.docker.internal:{fake_upstream_port}",
                agent_image="python:3.13-slim",
                compose_dir=tmp_path,
            ),
            model=model,
            log_dir=str(tmp_path / "logs"),
            display="none",
            sandbox_cleanup=False,
        )
    finally:
        _dump_sandbox_diagnostics_and_cleanup(fake_upstream_port)
    log = one(logs)
    assert log.status == "success", log.error
    assert log.samples is not None
    sample = one(log.samples)
    tool_texts = [message.text for message in sample.messages if message.role == "tool"]
    assert any(fake_ia.GOOD_TS in text and "archived example.com" in text for text in tool_texts), tool_texts
    assert any("2020-01" in text for text in tool_texts), tool_texts
    assert sample.scores is not None
    score = sample.scores["gym_proper_loss"]
    assert score.value == pytest.approx(-math.log(0.8))
    # W3: the proxy's served-evidence manifest rides along in the score.
    assert score.metadata is not None
    served = one(score.metadata["served_evidence"])
    assert served["url"] == fake_ia.EXAMPLE_ORIGINAL
    assert served["capture_ts"] == fake_ia.GOOD_TS
    assert served["sha256"]


if __name__ == "__main__":
    pytest_bazel.main()
