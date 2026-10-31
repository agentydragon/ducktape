"""Bounded API readiness probes while the k3s server starts."""

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from agentplane.system_tests.tracing import tracer


@dataclass(frozen=True)
class Bootstrap:
    process: subprocess.Popen[bytes]
    binary: Path
    kubeconfig: Path
    log_path: Path
    deadline: float

    @tracer.start_as_current_span("system_test.k3s.bootstrap")
    def wait_for(self, *args: str, expected: str) -> None:
        last_output = "kubeconfig has not been written"
        while True:
            code = self.process.poll()
            remaining = self.deadline - time.monotonic()
            if code is not None or remaining <= 0:
                reason = f"k3s exited with {code}" if code is not None else "bootstrap deadline expired"
                raise RuntimeError(
                    f"{reason} waiting for {args} to return {expected!r}: {last_output}\n"
                    f"{self.log_path.read_text(errors='replace')[-6000:]}"
                )
            if self.kubeconfig.exists():
                try:
                    result = subprocess.run(
                        [str(self.binary), "kubectl", "--kubeconfig", str(self.kubeconfig), *args],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=min(5, remaining),
                    )
                except subprocess.TimeoutExpired:
                    last_output = "kubectl probe timed out"
                else:
                    last_output = (result.stdout + result.stderr).strip()
                    if result.returncode == 0 and last_output == expected and self.process.poll() is None:
                        return
            time.sleep(min(1, max(0, self.deadline - time.monotonic())))
