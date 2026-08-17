"""A real Synapse in a container, plus the client-server calls a test drives it with.

**Deviation from the other testcontainer fixtures** (<../../../../../../util/testing/postgres_fixtures.py>):
Synapse does not serve until it holds a config it generated itself — a signing key and a macaroon
secret are part of it — so bring-up is two container runs over one host directory. The first is the
image's `generate` mode; the second serves, reading the generated `homeserver.yaml` **and**
`synapse_overrides.yaml`, which this module copies in before generating. Synapse merges several
`--config-path` files by top-level key, so the test's settings land without rewriting a file the
container's own user owns.

Both runs pin `UID`/`GID` to this process's (the image's `start.py` honours them), so everything
Synapse writes into that directory is still deletable by the test that made it.

Registration stays here rather than moving to nio with everything else a test does to the room
(`operator_room.py`): it is user-interactive auth even when the only stage is `m.login.dummy`, so
it is two HTTP calls and no session, and nio models the flow rather than this shortcut through it.
"""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed
from testcontainers.core.container import DockerContainer

from third_party.containers.rlocations import RYUK, SYNAPSE
from util.bazel.runfiles import get_required_path
from util.oci import load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

SERVER_NAME = "haku.test"

_CLIENT_PORT = 8008
_OVERRIDES = "_main/haku/console/x/channels/matrix/testing/synapse_overrides.yaml"
_STARTUP_BUDGET = datetime.timedelta(minutes=3)

_ENVIRONMENT = {
    "SYNAPSE_SERVER_NAME": SERVER_NAME,
    "SYNAPSE_REPORT_STATS": "no",
    # `start.py` gosus to these, so the config, the SQLite database and the media store all belong
    # to whoever is running the test rather than to the image's own user.
    "UID": str(os.getuid()),
    "GID": str(os.getgid()),
}

_REGISTER = "/_matrix/client/v3/register"


class HomeserverError(Exception):
    """The homeserver refused a call the test made, or never came up."""


def _body(response: httpx.Response) -> dict[str, Any]:
    """The JSON of a call that worked, or an error carrying Synapse's own explanation.

    `raise_for_status` drops the body, which for Matrix is where the errcode lives — and an
    `M_LIMIT_EXCEEDED` reading as a bare 429 is the one failure here nobody would guess.
    """
    if response.is_error:
        raise HomeserverError(
            f"{response.request.method} {response.request.url.path}: {response.status_code} {response.text}"
        )
    parsed: dict[str, Any] = response.json()
    return parsed


def _string(response: httpx.Response, field: str) -> str:
    """One string field out of a response, annotated because parsed JSON is `Any`."""
    value: str = _body(response)[field]
    return value


@dataclass(frozen=True)
class Synapse:
    base_url: str

    def create_user(self, localpart: str, password: str) -> str:
        """Register *localpart* without logging it in, returning its MXID.

        No device, on purpose: whoever signs this user in decides which one it wants. Registration
        is user-interactive auth even when the only stage is `m.login.dummy`, so the first attempt
        is answered with a session id to complete rather than with a user.
        """
        request = {"username": localpart, "password": password, "inhibit_login": True}
        started = httpx.post(f"{self.base_url}{_REGISTER}", json=request, timeout=30)
        if started.status_code != 401:
            return _string(started, "user_id")
        auth = {"type": "m.login.dummy", "session": started.json()["session"]}
        completed = httpx.post(f"{self.base_url}{_REGISTER}", json=request | {"auth": auth}, timeout=30)
        return _string(completed, "user_id")


@contextmanager
def _progress_to_disk() -> Iterator[None]:
    """Mirror bring-up progress into an undeclared output as it happens.

    Same reason as `util/testing/postgres_fixtures.py`: this runs in session fixture setup, before
    pytest has emitted a line, and pytest's captured output dies with the process when Bazel kills
    a wedged target.
    """
    handler = logging.FileHandler(undeclared_outputs_dir() / "synapse_setup.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    watched = [logging.getLogger("util.oci"), logger]
    levels = [each.level for each in watched]
    for each in watched:
        each.addHandler(handler)
        each.setLevel(logging.INFO)
    try:
        yield
    finally:
        for each, level in zip(watched, levels, strict=True):
            each.removeHandler(handler)
            each.setLevel(level)
        handler.close()


def _generate_config(data: Path) -> None:
    """Run the image's `generate` mode over *data*: a `homeserver.yaml`, a signing key, a log config.

    Blocking and one-shot — the server cannot start until it has finished — so a non-zero exit
    raises `docker.errors.ContainerError` rather than leaving the next step to fail obscurely.
    """
    logger.info("Generating the Synapse config in %s", data)
    logs = docker.from_env().containers.run(
        SYNAPSE.tag,
        command=["generate"],
        environment=_ENVIRONMENT,
        volumes={str(data): {"bind": "/data", "mode": "rw"}},
        remove=True,
    )
    (undeclared_outputs_dir() / "synapse_generate.log").write_bytes(logs)


class _NotServingYetError(Exception):
    """Synapse is up but has not finished starting."""


def _probe(container: DockerContainer, base_url: str) -> None:
    wrapped = container.get_wrapped_container()
    wrapped.reload()
    if wrapped.status != "running":
        # Not something to keep polling for: a config Synapse rejects exits in seconds, and the
        # reason is in the logs the caller writes out.
        raise HomeserverError(f"Synapse exited before it served: status={wrapped.status}")
    if (response := httpx.get(f"{base_url}/_matrix/client/versions", timeout=10)).status_code != 200:
        raise _NotServingYetError(f"{response.status_code}: {response.text[:200]!r}")


def _wait_until_serving(container: DockerContainer, base_url: str) -> None:
    for attempt in Retrying(
        stop=stop_after_delay(_STARTUP_BUDGET.total_seconds()),
        wait=wait_fixed(1),
        retry=retry_if_exception_type((_NotServingYetError, httpx.TransportError)),
        reraise=True,
    ):
        with attempt:
            _probe(container, base_url)
    logger.info("Synapse is serving at %s", base_url)


@contextmanager
def run_synapse() -> Iterator[Synapse]:
    """A homeserver of its own, torn down with everything anybody registered in it."""
    with _progress_to_disk(), tempfile.TemporaryDirectory(prefix="synapse-") as directory:
        data = Path(directory)
        overrides = data / Path(_OVERRIDES).name
        shutil.copyfile(get_required_path(_OVERRIDES), overrides)
        for image in (RYUK, SYNAPSE):
            load_oci_image(image)
        _generate_config(data)
        container = DockerContainer(SYNAPSE.tag)
        for key, value in _ENVIRONMENT.items():
            container.with_env(key, value)
        container.with_volume_mapping(str(data), "/data", "rw")
        container.with_command(
            ["run", "--config-path", "/data/homeserver.yaml", "--config-path", f"/data/{overrides.name}"]
        )
        container.with_exposed_ports(_CLIENT_PORT)
        logger.info("Starting %s", SYNAPSE.tag)
        with container:
            try:
                base_url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(_CLIENT_PORT)}"
                _wait_until_serving(container, base_url)
                yield Synapse(base_url)
            finally:
                # Written whatever happened: a homeserver that refused a call, or never came up at
                # all, explains itself here and nowhere else.
                (undeclared_outputs_dir() / "synapse.log").write_bytes(b"".join(container.get_logs()))
