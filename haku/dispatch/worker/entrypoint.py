"""Worker-image entrypoint: run the zone's harness on the prompt, turn in the result.

Runs inside the zone perimeter with exactly the credentials mounted into the
Job (per-job LiteLLM key, result token) — stdlib-only so the image needs no
Python packages. Contract (haku/dispatch/README.md → worker image): run the
harness headless on the prompt in an empty /workspace (if the job needs a
repo, the prompt tells the agent to git clone it), then AFTER the agent
process exits POST /output/result.md plus exit status to the dispatcher with
the job-scoped token.

Env (stamped by the dispatcher from the reviewed Job template):
  JOB_ID, MODEL, HARNESS, DISPATCHER_URL,
  PROMPT_PATH (default /prompt/prompt.md), RESULT_TOKEN,
  ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN (consumed by the harness itself).
"""

import json
import logging
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_WORKSPACE = Path("/workspace")
_OUTPUT = Path("/output/result.md")


def _run_harness(harness: str, prompt: str, model: str) -> int:
    match harness:
        case "claude":
            argv = ["claude", "-p", prompt, "--model", model, "--dangerously-skip-permissions"]
        case "codex":
            argv = ["codex", "exec", "--full-auto", "--model", model, prompt]
        case _:
            raise ValueError(f"unknown {harness=}")
    logger.info("running harness %s with model %s", harness, model)
    # Harness stdout/stderr flow to the pod log (operator-visible; Haku log
    # access is a pre-approved later affordance).
    return subprocess.run(argv, cwd=_WORKSPACE, check=False).returncode


def _submit(dispatcher_url: str, job_id: str, token: str, result: str, exit_code: int) -> None:
    request = urllib.request.Request(
        f"{dispatcher_url}/jobs/{job_id}/result",
        data=json.dumps({"result": result, "exit_code": exit_code}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        logger.info("result submitted: HTTP %s", response.status)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    job_id = os.environ["JOB_ID"]
    prompt = Path(os.environ.get("PROMPT_PATH", "/prompt/prompt.md")).read_text()

    _WORKSPACE.mkdir(exist_ok=True)
    _OUTPUT.parent.mkdir(exist_ok=True)

    exit_code = _run_harness(os.environ["HARNESS"], prompt, os.environ["MODEL"])

    if _OUTPUT.exists():
        result = _OUTPUT.read_text()
    else:
        result = f"(agent did not write {_OUTPUT}; see pod log for harness output)"
        logger.warning("agent wrote no %s; submitting placeholder text", _OUTPUT)
    _submit(os.environ["DISPATCHER_URL"], job_id, os.environ["RESULT_TOKEN"], result, exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
