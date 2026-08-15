"""A real Synapse in a container, plus the client-server calls a test drives it with.

**Deviation from the other testcontainer fixtures** (<../../../util/testing/postgres_fixtures.py>):
Synapse does not serve until it holds a config it generated itself — a signing key and a macaroon
secret are part of that config — so bring-up is two container runs over one host directory. The
first is the image's `generate` mode; the second serves, reading the generated `homeserver.yaml`
**and** an overrides file this module wrote before generating. Synapse merges several
`--config-path` files by top-level key, so the test's settings land without rewriting a file the
container's own user owns.

Both runs pin `UID`/`GID` to this process's (the image's `start.py` honours them), so everything
Synapse writes into that directory is still deletable by the test that made it.

`Account` is the other half: the operator's side of every conversation, and the raw read of what
Haku's client actually put in the room. Deliberately not `MatrixClient` — that is the thing under
test, and a test that checks it against itself checks nothing.
"""

from __future__ import annotations

import datetime
import logging
import os
import tempfile
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import docker
import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed
from testcontainers.core.container import DockerContainer

from third_party.containers.rlocations import RYUK, SYNAPSE
from util.oci import load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

SERVER_NAME = "haku.test"

_CLIENT_PORT = 8008
_OVERRIDES_FILE = "test-overrides.yaml"
_STARTUP_BUDGET = datetime.timedelta(minutes=3)

_ENVIRONMENT = {
    "SYNAPSE_SERVER_NAME": SERVER_NAME,
    "SYNAPSE_REPORT_STATS": "no",
    # `start.py` gosus to these, so the config, the SQLite database and the media store all belong
    # to whoever is running the test rather than to the image's own user.
    "UID": str(os.getuid()),
    "GID": str(os.getgid()),
}

# Registration is open because the alternative — the shared-secret admin flow — means reading a
# secret back out of a config file the container generated, for no extra coverage.
#
# The rate limits matter more than they look. Stock Synapse allows a fifth of a message per second
# and ten logins a burst, so filling a room past `TIMELINE_LIMIT` trips `M_LIMIT_EXCEEDED` long
# before the gap under test exists — and `MatrixClient` deliberately bounds nio's otherwise
# unlimited 429 retry (`MAX_RATE_LIMIT_RETRIES`), so that arrives as a failure rather than as a
# wait. Every limit a test can reach is lifted out of the way.
_OVERRIDES = textwrap.dedent("""\
    enable_registration: true
    enable_registration_without_verification: true

    # Replaces the generated listener, which binds `::` as well as `0.0.0.0`. The test Docker
    # network has no IPv6, and Synapse treats a listener it cannot bind as fatal — so without this
    # it exits during startup with `Address family not supported by protocol`.
    listeners:
      - port: 8008
        type: http
        tls: false
        bind_addresses: ["0.0.0.0"]
        x_forwarded: true
        resources:
          - names: [client]
            compress: false

    rc_message:
      per_second: 1000
      burst_count: 1000
    rc_registration:
      per_second: 1000
      burst_count: 1000
    rc_login:
      address:
        per_second: 1000
        burst_count: 1000
      account:
        per_second: 1000
        burst_count: 1000
      failed_attempts:
        per_second: 1000
        burst_count: 1000
    rc_joins:
      local:
        per_second: 1000
        burst_count: 1000
      remote:
        per_second: 1000
        burst_count: 1000
    rc_joins_per_room:
      per_second: 1000
      burst_count: 1000
    rc_invites:
      per_room:
        per_second: 1000
        burst_count: 1000
      per_user:
        per_second: 1000
        burst_count: 1000
      per_issuer:
        per_second: 1000
        burst_count: 1000

    presence:
      enabled: false
""")

_REGISTER = "/_matrix/client/v3/register"


class HomeserverError(Exception):
    """The homeserver refused a call the test made, or never came up."""


def _body(response: httpx.Response) -> dict[str, Any]:
    """The JSON of a call that worked, or an error carrying Synapse's own explanation.

    `raise_for_status` drops the body, which for Matrix is where the errcode lives — and an
    `M_LIMIT_EXCEEDED` that reads as a bare 429 is the one failure here nobody would guess.
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


class Account:
    """One logged-in user, driven straight through the client-server API."""

    def __init__(self, base_url: str, user_id: str, access_token: str):
        self.user_id = user_id
        self._http = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)

    def close(self) -> None:
        self._http.close()

    def create_room(self, *, invite: str) -> str:
        room = self._http.post("/_matrix/client/v3/createRoom", json={"preset": "private_chat", "invite": [invite]})
        return _string(room, "room_id")

    def send_text(self, room_id: str, body: str) -> str:
        path = f"/_matrix/client/v3/rooms/{quote(room_id)}/send/m.room.message/{uuid4().hex}"
        return _string(self._http.put(path, json={"msgtype": "m.text", "body": body}), "event_id")

    def event(self, room_id: str, event_id: str) -> dict[str, Any]:
        return _body(self._http.get(f"/_matrix/client/v3/rooms/{quote(room_id)}/event/{quote(event_id)}"))

    def messages(self, room_id: str) -> list[dict[str, Any]]:
        """The room's timeline, newest first."""
        path = f"/_matrix/client/v3/rooms/{quote(room_id)}/messages"
        chunk: list[dict[str, Any]] = _body(self._http.get(path, params={"dir": "b", "limit": 500}))["chunk"]
        return chunk

    def relations(self, room_id: str, event_id: str, rel_type: str) -> list[dict[str, Any]]:
        """The events the homeserver has indexed as relating to *event_id* by *rel_type*."""
        path = f"/_matrix/client/v1/rooms/{quote(room_id)}/relations/{quote(event_id)}/{quote(rel_type)}"
        chunk: list[dict[str, Any]] = _body(self._http.get(path))["chunk"]
        return chunk

    def sync(self, since: str | None = None, timeout_ms: int = 0) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": timeout_ms}
        if since is not None:
            params["since"] = since
        return _body(self._http.get("/_matrix/client/v3/sync", params=params, timeout=timeout_ms / 1000 + 30))

    def devices(self) -> list[dict[str, Any]]:
        devices: list[dict[str, Any]] = _body(self._http.get("/_matrix/client/v3/devices"))["devices"]
        return devices


@dataclass(frozen=True)
class Synapse:
    base_url: str

    def create_user(self, localpart: str, password: str) -> str:
        """Register *localpart* without logging it in, returning its MXID.

        No device, on purpose: the caller decides which one it wants, and `MatrixClient` pins its
        own. Registration is user-interactive auth even when the only stage is `m.login.dummy`, so
        the first attempt is answered with the session id to complete rather than with a user.
        """
        request = {"username": localpart, "password": password, "inhibit_login": True}
        started = httpx.post(f"{self.base_url}{_REGISTER}", json=request, timeout=30)
        if started.status_code != 401:
            return _string(started, "user_id")
        auth = {"type": "m.login.dummy", "session": started.json()["session"]}
        completed = httpx.post(f"{self.base_url}{_REGISTER}", json=request | {"auth": auth}, timeout=30)
        return _string(completed, "user_id")

    def sign_in(self, user_id: str, password: str) -> Account:
        request = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": user_id},
            "password": password,
        }
        signed_in = _body(httpx.post(f"{self.base_url}/_matrix/client/v3/login", json=request, timeout=30))
        return Account(self.base_url, signed_in["user_id"], signed_in["access_token"])


@contextmanager
def _progress_to_disk() -> Iterator[None]:
    """Mirror bring-up progress into an undeclared output as it happens.

    Same reason as `util/testing/postgres_fixtures.py`: this runs in session fixture setup, before
    pytest has emitted a line, and pytest's captured output dies with the process when Bazel kills
    a wedged target. A file handler flushes per record, so what was written survives.
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
    raises `docker.errors.ContainerError` carrying the reason rather than leaving the next step to
    fail obscurely.
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
        (data / _OVERRIDES_FILE).write_text(_OVERRIDES)
        for image in (RYUK, SYNAPSE):
            load_oci_image(image)
        _generate_config(data)
        container = DockerContainer(SYNAPSE.tag)
        for key, value in _ENVIRONMENT.items():
            container.with_env(key, value)
        container.with_volume_mapping(str(data), "/data", "rw")
        container.with_command(
            ["run", "--config-path", "/data/homeserver.yaml", "--config-path", f"/data/{_OVERRIDES_FILE}"]
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
