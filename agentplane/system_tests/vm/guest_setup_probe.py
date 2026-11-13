"""Guest setup ownership, kernel limits and detached-descendant fencing probes."""

import shlex

from agentplane.system_tests.vm.configuration import CPU_QUOTA_PERIOD_MICROS, RESOURCES

VM_SETUP_PROBE = r"""set -eu
test -w .
test "$HOME" = /state/home
test "$(stat -c '%u:%g' "$HOME")" = 107:107
test "$(stat -c '%a' "$HOME")" = 700
test "$(awk -F: '$1 == "agent" {print $6}' /etc/passwd)" = /state/home
home_proof="$HOME/agentplane-home-supervision"
printf 'home-write-check\n' > "$home_proof"
test "$(cat "$home_proof")" = home-write-check
rm -- "$home_proof"
test ! -e "$home_proof"
printf 'UID %s\nGID %s\nCWD %s\nHOME %s\nOWNER %s\nPID1 %s\n' \
    "$(id -u)" "$(id -g)" "$PWD" "$HOME" "$(stat -c '%u:%g' .)" "$(cat /proc/1/comm)"
printf 'HOME_OWNER %s\nHOME_MODE %s\nHOME_WRITABLE yes\n' \
    "$(stat -c '%u:%g' "$HOME")" "$(stat -c '%a' "$HOME")"
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


def assert_vm_setup_probe(stdout: str, stderr: str, *, cwd: str) -> None:
    assert stderr == "setup-stderr\n", stderr
    values = dict(line.split(" ", 1) for line in stdout.splitlines())
    assert values["UID"] == values["GID"] == "107", values
    assert values["OWNER"] == "107:107", values
    assert values["PARENT_OWNER"] == "107:107", values
    assert values["CWD"] == cwd, values
    assert values["HOME"] == "/state/home", values
    assert values["HOME_OWNER"] == "107:107", values
    assert values["HOME_MODE"] == "700", values
    assert values["HOME_WRITABLE"] == "yes", values
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
        "CPUQuotaPerSecUSec": f"{RESOURCES.process.cpu_quota_micros // CPU_QUOTA_PERIOD_MICROS}s",
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
            "cpu.max": f"{cpu} {CPU_QUOTA_PERIOD_MICROS}",
            "memory.max": str(memory),
            "memory.swap.max": "0",
            "memory.oom.group": "0",
            "pids.max": str(pids),
        }.items():
            assert values[f"KERNEL_{scope}_{name}"] == expected, values
    assert values["MEMORY_HIGH"] == str(RESOURCES.service_memory_high_bytes), values


def setup_fence_probe(stdout: str) -> str:
    values = dict(line.split(" ", 1) for line in stdout.splitlines())
    child = int(values["CHILD"])
    group = values["GROUP"]
    return (
        "set -eu\n"
        f"if test -r /proc/{child}/stat; then grep -q ') Z ' /proc/{child}/stat; fi\n"
        f"group={shlex.quote('/sys/fs/cgroup' + group)}\n"
        'if test -d "$group"; then grep -qx "populated 0" "$group/cgroup.events"; fi\n'
        "printf 'SETUP_DOMAIN_EMPTY\\n'\n"
    )
