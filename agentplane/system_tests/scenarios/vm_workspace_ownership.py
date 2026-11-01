"""A fresh VM Session creates a Git-safe workspace as the configured harness identity."""

from __future__ import annotations

import json
import shlex
from uuid import uuid4

from agentplane.app.testing.client import Client
from agentplane.env.harnesshost.harness import Harness
from agentplane.proto import session_pb2
from agentplane.system_tests.sandbox import VM_TEMPLATE, Sandboxes
from agentplane.system_tests.session_setup import run_setup
from util.testing.undeclared_outputs import undeclared_outputs_dir

WORKSPACE_ROOT = "/state/workspaces"
AGENT_UID = 107
AGENT_GID = 107


async def fresh_vm_session_workspace_is_created_for_harness_identity(client: Client, sandbox: Sandboxes) -> None:
    """Check absence before opening, then use the ordinary Session setup and Git paths."""
    view = await sandbox(f"systest-vm-workspace-{uuid4().hex[:10]}", template=VM_TEMPLATE)
    harness = session_pb2.HARNESS_CODEX
    offered = (await client.models()).harnesses[Harness.CODEX]
    assert offered, "testing offers no Codex model for the model-free setup Session"
    model = offered[0]  # Session admission validates model configuration; the test sends no model turn.

    session_id = uuid4()
    preflight_session_id = uuid4()
    workspace = f"{WORKSPACE_ROOT}/{session_id}"
    absent, _ = await run_setup(
        client,
        sandbox_name=view.name,
        sandbox_uid=view.uid,
        session_id=preflight_session_id,
        harness=harness,
        model=model,
        cwd=f"{WORKSPACE_ROOT}/.home",
        setup_script=(
            "set -eu\n"
            f"if test -e {shlex.quote(workspace)} || test -L {shlex.quote(workspace)}; then\n"
            f"  printf 'workspace existed before Session open: %s\\n' {shlex.quote(workspace)} >&2\n"
            "  exit 1\n"
            "fi\n"
            f"printf 'WORKSPACE_ABSENT_BEFORE_OPEN=%s\\n' {shlex.quote(workspace)}\n"
        ),
    )
    assert f"WORKSPACE_ABSENT_BEFORE_OPEN={workspace}" in absent

    setup_script = "\n".join(
        [
            "set -eu",
            "export GIT_CONFIG_GLOBAL=/dev/null",
            "export GIT_CONFIG_NOSYSTEM=1",
            f'test "$(id -u)" = {AGENT_UID}',
            f'test "$(id -g)" = {AGENT_GID}',
            f"test \"$(stat -c '%u:%g' .)\" = {AGENT_UID}:{AGENT_GID}",
            "git init --quiet",
            f"test \"$(stat -c '%u:%g' .git)\" = {AGENT_UID}:{AGENT_GID}",
            "if git config --get-all safe.directory >/dev/null 2>&1; then",
            "  echo 'unexpected safe.directory setting' >&2",
            "  exit 1",
            "fi",
            "git_top=$(git rev-parse --show-toplevel)",
            'test "$git_top" = "$PWD"',
            "echo WORKSPACE_OWNER_SYSTEM_TEST",
            (
                'echo "uid=$(id -u) gid=$(id -g) dir_owner=$(stat -c %u:%g .) '
                'git_owner=$(stat -c %u:%g .git) git_top=$git_top"'
            ),
        ]
    )
    output, stderr = await run_setup(
        client,
        sandbox_name=view.name,
        sandbox_uid=view.uid,
        session_id=session_id,
        harness=harness,
        model=model,
        cwd=workspace,
        setup_script=setup_script,
    )
    assert "WORKSPACE_OWNER_SYSTEM_TEST" in output, stderr
    assert f"uid={AGENT_UID} gid={AGENT_GID} dir_owner={AGENT_UID}:{AGENT_GID}" in output, stderr
    assert f"git_owner={AGENT_UID}:{AGENT_GID} git_top={workspace}" in output, stderr

    evidence = {
        "namespace": view.namespace,
        "sandbox": view.name,
        "sandbox_uid": str(view.uid),
        "template": VM_TEMPLATE,
        "harness": "codex",
        "harness_uid": AGENT_UID,
        "harness_gid": AGENT_GID,
        "session_id": str(session_id),
        "preflight_session_id": str(preflight_session_id),
        "workspace": workspace,
        "absent_before_open": True,
        "preflight_output": absent,
        "setup_output": output,
        "setup_stderr": stderr,
        "git_config_global": "/dev/null",
        "git_config_system_disabled": True,
        "safe_directory_configured": False,
        "git_init_and_rev_parse_succeeded": True,
    }
    output_path = undeclared_outputs_dir() / "vm-workspace-ownership.json"
    output_path.write_text(json.dumps(evidence, indent=2) + "\n")


# gazelle:include_dep @pypi//protobuf
