"""Boot the existing fixture guest directly with the pinned KubeVirt QEMU tools."""

import os
import secrets
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles

from agentplane.x.direct_vm_boot import launcher_tools
from devinfra.oci import oci
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def _runfile(name: str) -> Path:
    path = Runfiles.CreateOrRaise().Rlocation(os.environ[name])
    if path is None:
        raise FileNotFoundError(f"runfile not found for {name}")
    return Path(path)


def _copy_outputs(outputs: Path, artifacts: Path) -> None:
    for path in outputs.iterdir():
        shutil.copyfile(path, artifacts / path.name)


def test_real_guest_boot_and_missing_config_serial_control(tmp_path: Path) -> None:
    missing_config_control = os.environ.get("DIRECT_VM_BOOT_MISSING_CONFIG_CONTROL") == "1"
    inputs = tmp_path / "inputs"
    state = tmp_path / "state"
    outputs = tmp_path / "outputs"
    inputs.mkdir()
    state.mkdir()
    outputs.mkdir()
    for environment_name, filename in (
        ("KERNEL_RLOCATION", "vmlinuz"),
        ("INITRAMFS_RLOCATION", "initramfs.cpio"),
        ("ROOT_IMAGE_RLOCATION", "root.img"),
        ("RUNNER_IMAGE_RLOCATION", "runner.img"),
    ):
        shutil.copyfile(_runfile(environment_name), inputs / filename)

    positive_nonce = secrets.token_hex(16)
    negative_nonce = secrets.token_hex(16)
    container_name = f"agentplane-direct-vm-boot-{positive_nonce}"
    image = oci.load_oci_image(launcher_tools.LAYOUT_RLOCATION)
    script = textwrap.dedent(
        r"""
        set -eu

        qemu=/usr/libexec/qemu-kvm
        virtiofsd=/usr/libexec/virtiofsd
        xorriso=/usr/bin/xorriso
        mkdir -p /tmp/direct-vm/empty /tmp/direct-vm/sockets
        for volume in config launch trust; do
            "$xorriso" -as mkisofs -o "/tmp/direct-vm/$volume.iso" \
                -volid "agentplane-$volume" /tmp/direct-vm/empty >/dev/null 2>&1
        done
        truncate -s 1G /tmp/direct-vm/workspace.img

        "$virtiofsd" --socket-path=/tmp/direct-vm/sockets/state.sock \
            --shared-dir=/state --sandbox=none >/outputs/virtiofsd.log 2>&1 &
        virtiofsd_pid=$!
        qemu_pid=
        cleanup() {
            if [ -n "$qemu_pid" ]; then kill "$qemu_pid" 2>/dev/null || true; fi
            kill "$virtiofsd_pid" 2>/dev/null || true
            wait "$qemu_pid" 2>/dev/null || true
            wait "$virtiofsd_pid" 2>/dev/null || true
        }
        trap cleanup EXIT
        for _ in $(seq 1 100); do
            if [ -S /tmp/direct-vm/sockets/state.sock ]; then break; fi
            if ! kill -0 "$virtiofsd_pid" 2>/dev/null; then cat /outputs/virtiofsd.log >&2; exit 1; fi
            sleep 0.1
        done
        test -S /tmp/direct-vm/sockets/state.sock

        run_guest() {
            name=$1
            nonce=$2
            config_serial=$3
            deadline=$4
            serial="/outputs/$name.serial.log"
            qemu_log="/outputs/$name.qemu.log"
            set +e
            timeout --signal=TERM --kill-after=10s "${deadline}s" "$qemu" \
                -machine q35,accel=tcg -cpu max -nodefaults -m 1536M -smp 2 -no-reboot \
                -kernel /inputs/vmlinuz -initrd /inputs/initramfs.cpio \
                -append "console=ttyS0 net.ifnames=0 agentplane.direct-vm-boot-nonce=$nonce $5" \
                -display none -monitor none -serial "file:$serial" \
                -object memory-backend-memfd,id=mem,size=1536M,share=on \
                -numa node,memdev=mem \
                -netdev user,id=net0,net=10.0.2.0/24,host=10.0.2.1,dhcpstart=10.0.2.4 \
                -device virtio-net-pci,netdev=net0 \
                -drive file=/inputs/root.img,format=raw,if=none,id=root,readonly=on \
                -device virtio-blk-pci,drive=root,serial=guest-root \
                -drive file=/inputs/runner.img,format=raw,if=none,id=runner,readonly=on \
                -device virtio-blk-pci,drive=runner,serial=agentplane-runner \
                -drive file=/tmp/direct-vm/workspace.img,format=raw,if=none,id=workspace \
                -device virtio-blk-pci,drive=workspace,serial=workspace \
                -drive file=/tmp/direct-vm/config.iso,format=raw,if=none,id=config,readonly=on \
                -device virtio-blk-pci,drive=config,serial="$config_serial" \
                -drive file=/tmp/direct-vm/launch.iso,format=raw,if=none,id=launch,readonly=on \
                -device virtio-blk-pci,drive=launch,serial=agentplane-launch \
                -drive file=/tmp/direct-vm/trust.iso,format=raw,if=none,id=trust,readonly=on \
                -device virtio-blk-pci,drive=trust,serial=agentplane-trust \
                -chardev socket,id=state,path=/tmp/direct-vm/sockets/state.sock \
                -device vhost-user-fs-pci,chardev=state,tag=state \
                >"$qemu_log" 2>&1
            status=$?
            set -e
            printf 'qemu_status=%s\nconfig_serial=%s\n' "$status" "$config_serial" \
                >"/outputs/$name.result"
            cat "$qemu_log" >>"/outputs/$name.result"
            return "$status"
        }

        if [ "$MISSING_CONFIG_CONTROL" = 1 ]; then
            run_guest negative "$NEGATIVE_NONCE" wrong-config 30 \
                systemd.device-timeout=10s || true
            test ! -e "/state/direct-vm-boot-probe/$NEGATIVE_NONCE.ready"
            printf 'negative_control=no_ready_marker_for_wrong_required_config_serial\n'
        else
            run_guest positive "$POSITIVE_NONCE" agentplane-config 120 "" || exit $?
            test "$(cat "/state/direct-vm-boot-probe/$POSITIVE_NONCE.ready")" = "$POSITIVE_NONCE"
            printf 'positive_guest=powered_off_and_nonce_observed\n'
        fi
        """
    )
    artifacts = undeclared_outputs_dir()
    cleanup_failure: str | None = None
    try:
        docker = subprocess.run(
            [
                "docker",
                "run",
                "--name",
                container_name,
                "--network=none",
                "--volume",
                f"{inputs}:/inputs:ro",
                "--volume",
                f"{state}:/state",
                "--volume",
                f"{outputs}:/outputs",
                "--env",
                f"POSITIVE_NONCE={positive_nonce}",
                "--env",
                f"NEGATIVE_NONCE={negative_nonce}",
                "--env",
                f"MISSING_CONFIG_CONTROL={int(missing_config_control)}",
                "--entrypoint=/bin/sh",
                image,
                "-ec",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        (artifacts / "direct-vm-boot-docker.log").write_text(
            f"exit={docker.returncode}\nstdout:\n{docker.stdout}\nstderr:\n{docker.stderr}"
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout or ""
        stderr = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr or ""
        (artifacts / "direct-vm-boot-docker.log").write_text(f"timeout=180s\nstdout:\n{stdout}\nstderr:\n{stderr}")
        raise AssertionError("direct VM boot exceeded the bounded Docker timeout") from error
    finally:
        try:
            cleanup = subprocess.run(
                ["docker", "rm", "--force", container_name], capture_output=True, text=True, timeout=15, check=False
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout or ""
            stderr = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr or ""
            (artifacts / "docker-cleanup.log").write_text(f"timeout=15s\nstdout:\n{stdout}\nstderr:\n{stderr}")
            cleanup_failure = "removing the named direct VM boot container timed out"
        else:
            (artifacts / "docker-cleanup.log").write_text(
                f"exit={cleanup.returncode}\nstdout:\n{cleanup.stdout}\nstderr:\n{cleanup.stderr}"
            )
            if cleanup.returncode != 0:
                cleanup_failure = "removing the named direct VM boot container failed"
        _copy_outputs(outputs, artifacts)
    if cleanup_failure is not None:
        raise AssertionError(cleanup_failure)

    assert docker.returncode == 0, "direct VM guest boot failed; inspect undeclared outputs"
    if missing_config_control:
        negative_serial = (outputs / "negative.serial.log").read_text()
        assert "Linux version" in negative_serial
        assert "systemd[1]" in negative_serial
        assert "agentplane-config" in negative_serial
        assert not (state / "direct-vm-boot-probe" / f"{negative_nonce}.ready").exists()
        assert "config_serial=wrong-config" in (outputs / "negative.result").read_text()
        # Run the same readiness assertion as the healthy candidate: the absent required
        # config disk must prevent the guest from reaching the probe service.
        assert f"DIRECT_VM_BOOT_PROBE_PASS nonce={negative_nonce}" in negative_serial
    else:
        positive_serial = (outputs / "positive.serial.log").read_text()
        marker = state / "direct-vm-boot-probe" / f"{positive_nonce}.ready"
        assert marker.read_text() == f"{positive_nonce}\n"
        shutil.copyfile(marker, artifacts / "guest-ready-marker.txt")
        expected_mounts = (
            "path=/sys/fs/cgroup type=cgroup2",
            "path=/state type=virtiofs",
            "path=/state/workspaces type=ext4",
            "path=/run/agentplane/config type=iso9660",
            "path=/run/agentplane/sandbox type=iso9660",
            "path=/run/agentplane/trust type=iso9660",
            "path=/run/agentplane/environment type=erofs",
        )
        for mount in expected_mounts:
            assert f"DIRECT_VM_BOOT_PROBE_MOUNT {mount}" in positive_serial
        positive_result = (outputs / "positive.result").read_text()
        assert "qemu_status=0" in positive_result
        assert "reboot: Power down" in positive_serial
        assert f"DIRECT_VM_BOOT_PROBE_PASS nonce={positive_nonce}" in positive_serial
        mount_lines = [line for line in positive_serial.splitlines() if "DIRECT_VM_BOOT_PROBE_MOUNT" in line]
        poweroff_lines = [line for line in positive_serial.splitlines() if "reboot: Power down" in line]
        (artifacts / "boot-summary.txt").write_text(
            f"guest_nonce={marker.read_text().strip()}\n"
            f"qemu_status=0\n" + "\n".join(mount_lines + poweroff_lines) + "\n"
        )


if __name__ == "__main__":
    pytest_bazel.main()
