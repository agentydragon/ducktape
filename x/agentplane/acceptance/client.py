"""The staging app's HTTP surface, as an acceptance test drives it.

The app's own response models are what the responses are parsed with: this suite tests whether the
deployed system behaves, not whether it serialises, and a second copy of the schema here would only
drift. The bearer token is a Kubernetes ServiceAccount token scoped to the app's audience; the app
admits the subjects its `AGENTPLANE_TOKEN_SUBJECTS` names and refuses every other one.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx

from x.agentplane.app.decisions import Decision
from x.agentplane.app.egress import BindingView, PolicyView
from x.agentplane.app.inventory import NewSandbox, ProvisioningState, SandboxView

# A turn is a model call plus tool calls; a sandbox's first Pod has to be scheduled and pull images.
SANDBOX_READY_SECONDS = 300.0
REQUEST_SECONDS = 30.0


def mint_token(*, namespace: str, service_account: str, audience: str, lifetime: str = "1800s") -> str:
    """An audience-scoped token for `service_account`, from whatever kubeconfig the caller holds.

    RBAC on `serviceaccounts/token` is what gates this; the audience is chosen freely by the
    requester and proves nothing on its own, which is why the app checks the subject as well.
    """
    minted = subprocess.run(
        [
            "kubectl",
            "-n",
            namespace,
            "create",
            "token",
            service_account,
            f"--audience={audience}",
            f"--duration={lifetime}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return minted.stdout.strip()


@dataclass(frozen=True)
class ServerEvent:
    """One SSE frame: the event name, its decoded data, and the runner sequence it carries."""

    name: str
    data: dict[str, Any]
    sequence: int | None


class Client:
    """Async context manager over one deployment of the app."""

    def __init__(self, *, base_url: str, token: str) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(REQUEST_SECONDS),
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._http.aclose()

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._http.request(method, path, **kwargs)
        response.raise_for_status()
        return None if response.status_code == httpx.codes.NO_CONTENT else response.json()

    async def policies(self) -> list[PolicyView]:
        return [PolicyView.model_validate(row) for row in await self._json("GET", "/egress/policies")]

    async def create_sandbox(self, spec: NewSandbox) -> SandboxView:
        return SandboxView.model_validate(await self._json("POST", "/sandboxes", json=spec.model_dump()))

    async def sandbox(self, name: str) -> SandboxView:
        return SandboxView.model_validate(await self._json("GET", f"/sandboxes/{name}"))

    async def suspend_sandbox(self, name: str) -> None:
        await self._json("POST", f"/sandboxes/{name}/suspend")

    async def delete_sandbox(self, name: str) -> None:
        await self._json("DELETE", f"/sandboxes/{name}")

    async def grant_egress(self, name: str, policies: list[str]) -> BindingView:
        body = {"policies": policies}
        return BindingView.model_validate(await self._json("POST", f"/sandboxes/{name}/egress", json=body))

    async def bindings(self, name: str) -> list[BindingView]:
        return [BindingView.model_validate(row) for row in await self._json("GET", f"/sandboxes/{name}/egress")]

    async def decisions(self, name: str) -> list[Decision]:
        return [Decision.model_validate(row) for row in await self._json("GET", f"/sandboxes/{name}/egress/decisions")]

    async def open_session(self, name: str, session_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        """The runner's `Attached`, as proto-JSON: the session's spec and the sequence it resumes at."""
        body = {"session_id": session_id, "spec": spec}
        attached = await self._json("POST", f"/sandboxes/{name}/sessions", json=body)
        if not isinstance(attached, dict):
            raise TypeError(f"the session endpoint answered with {type(attached).__name__}, not an object")
        return attached

    async def send_input(self, name: str, session_id: str, input_id: str, text: str) -> None:
        body = {"inputId": input_id, "text": text}
        await self._json("POST", f"/sandboxes/{name}/sessions/{session_id}/inputs", json=body)

    async def events(
        self, name: str, session_id: str, *, after: int, read_seconds: float
    ) -> AsyncIterator[ServerEvent]:
        """The session's SSE stream from `after`. Ends on the stream's own `end` or `error` frame;
        `read_seconds` bounds how long one frame may take to arrive, so a wedged session fails the
        test instead of hanging it."""
        timeout = httpx.Timeout(REQUEST_SECONDS, read=read_seconds)
        url = f"/sandboxes/{name}/sessions/{session_id}/events"
        async with self._http.stream(
            "GET", url, params={"after": after}, headers={"Accept": "text/event-stream"}, timeout=timeout
        ) as response:
            response.raise_for_status()
            event_name, data, sequence = "message", None, None
            async for line in response.aiter_lines():
                if not line:
                    if data is not None:
                        yield ServerEvent(name=event_name, data=json.loads(data), sequence=sequence)
                        if event_name in {"end", "error"}:
                            return
                    event_name, data, sequence = "message", None, None
                elif line.startswith(":"):
                    continue
                elif line.startswith("event:"):
                    event_name = line.removeprefix("event:").strip()
                elif line.startswith("id:"):
                    sequence = int(line.removeprefix("id:").strip())
                elif line.startswith("data:"):
                    data = line.removeprefix("data:").strip()


def is_running(view: SandboxView) -> bool:
    return view.state is ProvisioningState.RUNNING and view.pod is not None and view.pod.ip is not None
