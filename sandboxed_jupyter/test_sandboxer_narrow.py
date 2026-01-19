import subprocess
from pathlib import Path

import pytest
import pytest_bazel
import yaml

from sandboxed_jupyter.sandboxer import build_sandboxer_command


@pytest.mark.macos
@pytest.mark.xfail(
    strict=False, reason="Narrow sandbox policy is brittle across macOS dyld/SSV changes; keep as documentation test"
)
def test_sandboxer_yes_hello_world_narrow(tmp_path: Path):
    """Narrowed version of hello-world test: no read '/' — allow only minimal system paths.

    Runs a simple pipeline `yes hello | head -n 5` under a tight allowlist and prints
    diagnostic tails (seatbelt.trace + unified denies) when failing.
    """
    (tmp_path / "tmp").mkdir(parents=True, exist_ok=True)

    policy = tmp_path / "policy_narrow.yaml"
    policy_dict = {
        "env": {
            "set": {"TMPDIR": (tmp_path / "tmp").as_posix(), "HOME": tmp_path.as_posix(), "PYTHONUNBUFFERED": "1"},
            "passthrough": [],
        },
        "fs": {
            # Minimal system bins required by this test: sh/yes/head and loader essentials
            "read_paths": [
                tmp_path.as_posix(),
                (tmp_path / "tmp").as_posix(),
                "/bin",
                "/usr/bin",
                # loader + system framework essentials
                "/System",
                "/System/Library",
                "/usr/lib",
                "/usr/lib/dyld",
                "/private/var/db",
                "/private/var/db/dyld",
                "/var/db",
                "/System/Volumes/Preboot",
                "/System/Cryptexes",
                "/System/Volumes/Preboot/Cryptexes",
                # useful common dirs some tools read
                "/etc",
                "/private/etc",
                "/usr/share",
                "/usr/libexec",
                "/dev",
                "/usr/local",
                "/opt/homebrew",
            ],
            "write_paths": [tmp_path.as_posix()],
        },
        "net": {"mode": "none"},
        "platform": {
            "trace": True,
            "seatbelt": {
                "extra_allow": {"sysctl_read": True, "file_read_extra": [], "mach_lookup": ["com.apple.cfprefsd.agent"]}
            },
        },
    }
    policy.write_text(yaml.safe_dump(policy_dict, sort_keys=False))

    def _run_and_print(argv: list[str]) -> subprocess.CompletedProcess:
        cp = subprocess.run(argv, check=False, capture_output=True, text=True)
        print("CMD:", " ".join(argv))
        print("STDOUT:\n" + cp.stdout)
        print("STDERR:\n" + cp.stderr)
        if cp.returncode != 0:
            # Print sandbox trace tail
            trace_path = tmp_path / "tmp" / "seatbelt.trace.log"
            try:
                if trace_path.exists():
                    lines = trace_path.read_text(errors="ignore").splitlines()
                    tail = "\n".join(lines[-200:])
                    print("=== seatbelt.trace.log (tail) ===\n" + tail)
                else:
                    print(f"=== seatbelt.trace.log missing: {trace_path} ===")
            except Exception as e:
                print(f"=== seatbelt.trace.log read error: {e} ===")
            # Print unified sandbox denies from system log (last 7 minutes)
            try:
                res = subprocess.run(
                    [
                        "/usr/bin/log",
                        "show",
                        "--style",
                        "syslog",
                        "--last",
                        "7m",
                        "--predicate",
                        '(subsystem == "com.apple.sandbox") && (eventMessage CONTAINS[c] "deny")',
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                u_lines = res.stdout.strip().splitlines()
                u_tail = "\n".join(u_lines[-200:])
                print("=== unified sandbox denies (tail, last 7m) ===\n" + u_tail)
            except Exception as e:
                print(f"=== unified sandbox denies read error: {e} ===")
        return cp

    # 1) Basic echo using /bin/echo (no shell)
    cp_echo = _run_and_print(build_sandboxer_command(policy, ["/bin/echo", "HELLO_ECHO"], trace=True))
    # 2) Shell pipeline yes|head
    cp_pipe = _run_and_print(build_sandboxer_command(policy, ["/bin/sh", "-lc", "yes hello | head -n 5"], trace=True))

    assert cp_echo.returncode == 0
    assert "HELLO_ECHO" in cp_echo.stdout
    assert cp_pipe.returncode == 0
    assert "hello" in cp_pipe.stdout


if __name__ == "__main__":
    pytest_bazel.main()
