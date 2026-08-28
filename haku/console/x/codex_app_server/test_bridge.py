"""Codex over the shared runner transport, at the generation cut: fail closed until stage 5.

The v3 composition this file exercised — the Console-side Codex fold pumping native frames through
the runner's serve loop — went with that loop at the #4667 cut (git has it beside the fold). The
runner now serves only a backend with a `HarnessDriver`, and Codex's runner-side projector is
stage 5's scheduled deliverable, so what there is to pin today is the refusal: a Codex sandbox
fails at start, with the reason in the pod log, rather than launching a CLI whose stream nothing
can interpret. The Console-side fold itself stays covered by this package's other tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from haku.runtime.x.bridge.codex_options import codex_app_server_backend
from haku.runtime.x.bridge.runner import run


def test_codex_refuses_the_runner_until_its_projector_lands() -> None:
    with pytest.raises(NotImplementedError, match="stage 5"):
        codex_app_server_backend(Path("codex-under-test")).driver()


async def test_the_runner_refuses_codex_before_dialing_anything() -> None:
    # An unresolvable URL on purpose: the refusal must come from the missing driver, before any
    # dial — a sandbox that dialed first would hold its claim while failing somewhere stranger.
    with pytest.raises(NotImplementedError, match="stage 5"):
        await run("ws://nowhere.invalid/never-dialed", codex_app_server_backend(Path("codex-under-test")), None)


if __name__ == "__main__":
    pytest_bazel.main()
