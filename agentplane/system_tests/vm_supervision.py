"""Check native systemd supervision through ordinary Session setup and History."""

import shlex
from uuid import UUID, uuid4

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.agent import Agent
from agentplane.system_tests.session_setup import run_setup
from agentplane.system_tests.vm_configuration import RESOURCES

_SETUP = r"""set -eu
test -w .
printf 'UID %s\nGID %s\nCWD %s\nHOME %s\nOWNER %s\nPID1 %s\n' \
    "$(id -u)" "$(id -g)" "$PWD" "$HOME" "$(stat -c '%u:%g' .)" "$(cat /proc/1/comm)"
printf 'PARENT_OWNER %s\n' "$(stat -c '%u:%g' ..)"
printf 'setup-stderr\n' >&2
group=$(awk -F: '$1 == 0 {print $3}' /proc/self/cgroup)
printf 'GROUP %s\n' "$group"
awk '/^(NoNewPrivs|CapEff|CapPrm|CapAmb|CapBnd):/ {print $1, $2}' /proc/self/status
for name in MemoryMax MemorySwapMax TasksMax CPUQuotaPerSecUSec OOMPolicy Slice; do
    printf 'PROPERTY_%s %s\n' "$name" "$(/usr/bin/systemctl show "${group##*/}" --property="$name" --value)"
done
for scope in native aggregate service; do
    case "$scope" in
        native) path=/sys/fs/cgroup$group ;;
        aggregate) path=/sys/fs/cgroup/agentplane.slice/agentplane-workloads.slice ;;
        service) path=/sys/fs/cgroup/agentplane.slice ;;
    esac
    for name in cpu.max memory.max memory.swap.max memory.oom.group pids.max; do
        printf 'KERNEL_%s_%s %s\n' "$scope" "$name" "$(cat "$path/$name")"
    done
done
printf 'MEMORY_HIGH %s\n' "$(cat /sys/fs/cgroup/agentplane.slice/memory.high)"
# Escape the setup leader's process group and ignore TERM. Runner must fence the whole
# systemd service before publishing setup_finished, including this detached descendant.
setsid /bin/sh -c 'trap "" TERM; : > child-ready; while :; do sleep 1; done' </dev/null >/dev/null 2>&1 &
printf 'CHILD %s\n' "$!"
while ! test -e child-ready; do sleep 0.01; done
"""


async def session_setup_is_supervised(
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox_name: str,
    sandbox_uid: UUID,
    harness: session_pb2.Harness,
    model: str,
) -> Agent:
    session_id = uuid4()
    # A fresh nested path exercises Runner's workspace helper rather than a pre-created cwd.
    cwd = f"/state/workspaces/{session_id}/nested"
    stdout, stderr = await run_setup(
        client,
        sandbox_name=sandbox_name,
        sandbox_uid=sandbox_uid,
        session_id=session_id,
        harness=harness,
        model=model,
        cwd=cwd,
        setup_script=_SETUP,
    )
    assert stderr == "setup-stderr\n", stderr
    values = dict(line.split(" ", 1) for line in stdout.splitlines())
    assert values["UID"] == values["GID"] == "107", values
    assert values["OWNER"] == "107:107", values
    assert values["PARENT_OWNER"] == "107:107", values
    assert values["CWD"] == cwd, values
    assert values["HOME"] == "/state/workspaces/.home", values
    assert values["PID1"] == "systemd", values
    group = values["GROUP"]
    assert group.startswith("/agentplane.slice/agentplane-workloads.slice/agentplane-native-"), group
    assert group.count("/") == 3, group
    assert group.endswith(".service"), group
    assert values["NoNewPrivs:"] == "1", values
    for name in ("CapEff:", "CapPrm:", "CapAmb:", "CapBnd:"):
        assert values[name] == "0000000000000000", values
    for name, expected in {
        "MemoryMax": str(RESOURCES.process.memory_max_bytes),
        "MemorySwapMax": "0",
        "TasksMax": str(RESOURCES.process.pids_max),
        "CPUQuotaPerSecUSec": "1s",
        "OOMPolicy": "continue",
        "Slice": "agentplane-workloads.slice",
    }.items():
        assert values[f"PROPERTY_{name}"] == expected, values
    for scope, cpu, memory, pids in (
        ("native", RESOURCES.process.cpu_quota_micros, RESOURCES.process.memory_max_bytes, RESOURCES.process.pids_max),
        (
            "aggregate",
            RESOURCES.aggregate.cpu_quota_micros,
            RESOURCES.aggregate.memory_max_bytes,
            RESOURCES.aggregate.pids_max,
        ),
        ("service", RESOURCES.service_cpu_quota_micros, RESOURCES.service_memory_max_bytes, RESOURCES.service_pids_max),
    ):
        for name, expected in {
            "cpu.max": f"{cpu} 100000",
            "memory.max": str(memory),
            "memory.swap.max": "0",
            "memory.oom.group": "0",
            "pids.max": str(pids),
        }.items():
            assert values[f"KERNEL_{scope}_{name}"] == expected, values
    assert values["MEMORY_HIGH"] == str(RESOURCES.service_memory_high_bytes), values

    child = int(values["CHILD"])
    # A second setup observes the first one's cleanup from outside its execution domain.
    # Checking the kernel domain also catches descendants missed by a PID-only assertion.
    checked, errors = await run_setup(
        client,
        sandbox_name=sandbox_name,
        sandbox_uid=sandbox_uid,
        session_id=uuid4(),
        harness=harness,
        model=model,
        cwd=cwd,
        setup_script=(
            "set -eu\n"
            f"if test -r /proc/{child}/stat; then grep -q ') Z ' /proc/{child}/stat; fi\n"
            f"group={shlex.quote('/sys/fs/cgroup' + group)}\n"
            'if test -d "$group"; then grep -qx "populated 0" "$group/cgroup.events"; fi\n'
            "printf 'SETUP_DOMAIN_EMPTY\\n'\n"
        ),
    )
    assert checked == "SETUP_DOMAIN_EMPTY\n", (checked, errors)
    assert not errors, errors
    return Agent(client, dispatcher, thread_id=session_id, cursor=0, sandbox=sandbox_name, sandbox_uid=str(sandbox_uid))


# gazelle:include_dep @pypi//protobuf
