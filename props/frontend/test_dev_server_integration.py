"""Exercise the dev launcher against the real backend and an isolated database."""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from props.db.database import Database

# gazelle:include_dep //props/testing/fixtures:db
# gazelle:include_dep //util/testing:postgres_fixtures
pytest_plugins = ("util.testing.postgres_fixtures", "props.testing.fixtures.db")


def test_dev_server_reaches_backend(db: Database, tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'backend_url = "http://127.0.0.1:8000"\nllm_proxy_url = "http://127.0.0.1:8001"\nagent_env = {}\n'
    )
    env = os.environ | db.config.to_env_dict() | {"PROPS_CONFIG_FILE": str(config)}
    binary = Runfiles.CreateOrRaise().root() / os.environ["PROPS_DEV_RLOCATION"]
    log_path = undeclared_outputs_dir() / "dev-server-integration.log"
    with log_path.open("w+") as log:
        process = subprocess.Popen([binary], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            request = urllib.request.Request(
                "http://localhost:5173/api/stats/overview",
                headers={"Authorization": f"Bearer {db.config.basic_auth_token}"},
            )
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(f"dev server exited; see {log_path}")
                try:
                    with urllib.request.urlopen(request, timeout=1) as response:
                        assert response.headers.get_content_type() == "application/json"
                        assert "example_counts" in json.load(response)
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code != 502:
                        raise
                except OSError:
                    pass
                time.sleep(0.25)
            else:
                raise AssertionError(f"backend did not become available; see {log_path}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    pytest_bazel.main()
