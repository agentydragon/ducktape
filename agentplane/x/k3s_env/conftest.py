from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest

from agentplane.x.k3s_env import bootstrap
from agentplane.x.k3s_env.bootstrap import Bootstrap


@dataclass
class Clock:
    now: float = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(bootstrap.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(bootstrap.time, "sleep", clock.sleep)
    return clock


@pytest.fixture
def k3s_process() -> Mock:
    return Mock(poll=Mock(return_value=None))


@pytest.fixture
def starting_k3s(tmp_path: Path, k3s_process: Mock, clock: Clock) -> Bootstrap:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.touch()
    log = tmp_path / "k3s.log"
    log.write_text("fixture server diagnostic\n")
    return Bootstrap(
        process=k3s_process, binary=Path("/fixture/k3s"), kubeconfig=kubeconfig, log_path=log, deadline=clock.now + 3
    )
