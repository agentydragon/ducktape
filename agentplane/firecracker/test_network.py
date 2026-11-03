import pytest
import pytest_bazel

from agentplane.firecracker import network


@pytest.mark.parametrize("enabled", [False, True])
def test_tap_rules_stay_guarded_during_updates(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    commands: list[tuple[str, ...]] = []
    guards: dict[tuple[str, ...], int] = {}

    def run(*args: str) -> None:
        commands.append(args)
        if args[1] == "-I":
            guard = (args[0], args[2], *args[4:])
            guards[guard] = guards.get(guard, 0) + 1
        if args[1] == "-D":
            guard = (args[0], args[2], *args[3:])
            guards[guard] -= 1

    def exists(*args: str) -> bool:
        return guards.get((args[0], args[2], *args[3:]), 0) > 0

    monkeypatch.setattr(network, "run", run)
    monkeypatch.setattr(network, "exists", exists)
    subject = network.Network(relay_port=3128, platform_route_ports=[8443])
    if enabled:
        subject.enable()
    else:
        subject.disable()

    first_flush = next(index for index, command in enumerate(commands) if command[1] == "-F")
    last_build = max(index for index, command in enumerate(commands) if command[1] == "-A")
    first_delete = next(index for index, command in enumerate(commands) if command[1] == "-D")
    assert all(command[1] == "-I" for command in commands[:first_flush])
    assert first_delete > last_build
    assert all(count == 0 for count in guards.values())

    forward = [
        command for command in commands if len(command) > 3 and command[2] == "AP_TAP_FORWARD" and command[1] == "-A"
    ]
    if enabled:
        assert forward == [
            ("iptables", "-A", "AP_TAP_FORWARD", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"),
            ("iptables", "-A", "AP_TAP_FORWARD", "-j", "REJECT"),
            (
                "ip6tables",
                "-A",
                "AP_TAP_FORWARD",
                "-m",
                "conntrack",
                "--ctstate",
                "ESTABLISHED,RELATED",
                "-j",
                "ACCEPT",
            ),
            ("ip6tables", "-A", "AP_TAP_FORWARD", "-j", "REJECT"),
        ]
        assert (
            "iptables",
            "-A",
            "AP_TAP_INGRESS",
            "-d",
            network.GUEST_V4,
            "-p",
            "tcp",
            "--dport",
            "9090",
            "-j",
            "ACCEPT",
        ) in commands
        assert not any("NEW" in command for command in forward)
    else:
        assert forward == [
            ("iptables", "-A", "AP_TAP_FORWARD", "-j", "REJECT"),
            ("ip6tables", "-A", "AP_TAP_FORWARD", "-j", "REJECT"),
        ]


if __name__ == "__main__":
    pytest_bazel.main()
