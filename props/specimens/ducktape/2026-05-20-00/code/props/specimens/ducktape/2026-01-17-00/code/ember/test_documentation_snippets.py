from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest

import ember
from ember.system_prompt import load_system_prompt


def _embedded_text(relative: str) -> str:
    resource = resources.files(ember).joinpath(f"resources/{relative}")
    content = resource.read_text(encoding="utf-8").rstrip()
    header = f"# /var/emberd/{relative}"
    return "\n".join((header, content))


def test_python_session_demo_scripts_are_embedded_and_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompt = load_system_prompt()
    demo_relative = "examples/python-session/demo.sh"
    test_relative = "examples/python-session/test_demo.sh"
    matrix_relative = "examples/matrix-client/quickstart.py"

    assert _embedded_text(demo_relative) in prompt
    assert _embedded_text(test_relative) in prompt
    assert _embedded_text(matrix_relative) in prompt

    env = os.environ.copy()
    env["EMBER_WORKSPACE_DIR"] = str(tmp_path / "workspace")
    env["EMBER_PYTHON_SESSION_DIR"] = str(tmp_path / "session")
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env['PATH']}"
    if not shutil.which("ember-python", path=env["PATH"]):
        pytest.skip("ember-python CLI not available on PATH")

    test_script = resources.files(ember).joinpath(f"resources/{test_relative}")
    subprocess.run(["bash", str(test_script)], check=True, env=env, text=True)

    fake_session_state: dict[str, list[str]] = {"sent": [], "closed": []}

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            fake_session_state["closed"].append("yes")

        async def send_text_message(self, room_id: str, body: str, *, msgtype: str = "m.notice") -> None:
            fake_session_state["sent"].append(f"{room_id}:{body}:{msgtype}")

        async def get_events(self):
            return [SimpleNamespace(sender="@demo:example.org", body="hello world")]

    class FakeMatrixClient:
        def __init__(self) -> None:
            self._session = FakeSession()

        def session(self) -> FakeSession:
            return self._session

    monkeypatch.setenv("EMBER_MATRIX_ROOM_ID", "!room:example.org")
    monkeypatch.setattr(
        "ember.matrix_client.MatrixClient.from_projected_secrets", lambda options=None: FakeMatrixClient()
    )

    quickstart_path = resources.files(ember).joinpath("resources/examples/matrix-client/quickstart.py")
    runpy.run_path(str(quickstart_path), run_name="__main__")

    out = capsys.readouterr().out
    assert "Sent message to !room:example.org" in out
    assert "@demo:example.org: hello world" in out
    assert fake_session_state["sent"] == ["!room:example.org:Hello from Ember's matrix-client quickstart!:m.notice"]
    assert fake_session_state["closed"] == ["yes"]
