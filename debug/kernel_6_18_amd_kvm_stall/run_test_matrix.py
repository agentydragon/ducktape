#!/usr/bin/env python3
"""KVM AMD stall test matrix — static IPs, parallel execution, structured output.

Creates test VMs on atlas (Proxmox), soaks them, collects full diagnostic artifacts,
and produces a summary table. Run from wyrm2.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent
ATLAS = "root@10.2.0.2"
IP_PREFIX = "10.0.200"
GW = "10.0.0.1"
SOAK_SECONDS = 300
BOOT_WAIT = 90
BOOT_WAIT_REBOOT = 150
VM_MEMORY_MB = 2048
VM_CORES = 4
TALOSCTL = "/tmp/claude-1001/talosctl"
COLLECT_SCRIPT = SCRIPT_DIR / "collect_guest_data.sh"

TALOS_SCHEMATIC = "ce4c980550dd2ab1b17bbf2b08801c7eb59418eafe8f279833297925d67c7515"
IMAGE_URLS = {
    "talos-v1.11.6": f"https://factory.talos.dev/image/{TALOS_SCHEMATIC}/v1.11.6/nocloud-amd64.qcow2",
    "talos-v1.12.3": f"https://factory.talos.dev/image/{TALOS_SCHEMATIC}/v1.12.3/nocloud-amd64.qcow2",
    "fedora-42": "https://download.fedoraproject.org/pub/fedora/linux/releases/42/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-42-1.1.x86_64.qcow2",
}


@dataclass
class TestConfig:
    test_id: str
    vmid: int
    ip_suffix: int
    image: str
    workload: str = "idle"
    guest_args: str = ""
    boot_wait: int = BOOT_WAIT

    @property
    def ip(self) -> str:
        return f"{IP_PREFIX}.{self.ip_suffix}"

    @property
    def is_talos(self) -> bool:
        return self.image.startswith("talos-")

    @property
    def ci_snippet(self) -> str:
        if self.is_talos:
            return ""
        if self.guest_args == "tsa=off":
            return "kvm-test-ci-tsa-off.yaml"
        if self.guest_args == "mitigations=off":
            return "kvm-test-ci-mitigations-off.yaml"
        return "kvm-test-ci.yaml"

    @property
    def needs_reboot(self) -> bool:
        return bool(self.guest_args) and not self.is_talos


@dataclass
class TestResult:
    test_id: str
    kernel: str = "?"
    nmi_count: int = 0
    stall_count: int = 0
    reachable: bool = False
    error: str = ""


# ============================================================================
# TEST MATRIX
# ============================================================================

TESTS = [
    TestConfig("t01-talos-6.12-idle", 9901, 1, "talos-v1.11.6"),
    TestConfig("t02-talos-6.18-idle", 9902, 2, "talos-v1.12.3"),
    TestConfig("t03-fedora-6.14-idle", 9903, 3, "fedora-42"),
    TestConfig("t04-fedora-6.14-stress", 9904, 4, "fedora-42", workload="stress"),
    # Arch cloud images don't boot with OVMF — skipped for now.
    # TODO: add Arch tests with SeaBIOS or find a UEFI Arch image.
]


# ============================================================================
# REMOTE EXECUTION
# ============================================================================


SSH_OPTS = [
    "-o",
    "ConnectTimeout=10",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
]


def ssh(host: str, cmd: str, *, timeout: int = 30, check: bool = True) -> str:
    """Run a command on a remote host via SSH."""
    result = subprocess.run(["ssh", *SSH_OPTS, host, cmd], check=False, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        logger.warning("SSH to %s failed (rc=%d): %s", host, result.returncode, result.stderr[:200])
    return result.stdout.strip()


def scp_to(src: str, dst: str, *, timeout: int = 30) -> bool:
    """Copy a file to a remote host."""
    result = subprocess.run(["scp", *SSH_OPTS, src, dst], check=False, capture_output=True, timeout=timeout)
    return result.returncode == 0


def scp_from(src: str, dst: str, *, timeout: int = 30) -> bool:
    """Copy a directory from a remote host (use -r, no glob)."""
    result = subprocess.run(["scp", *SSH_OPTS, "-r", src, dst], check=False, capture_output=True, timeout=timeout)
    return result.returncode == 0


def ping(ip: str, *, count: int = 1, timeout: int = 5) -> bool:
    result = subprocess.run(["ping", "-c", str(count), "-W", str(timeout), ip], check=False, capture_output=True)
    return result.returncode == 0


def talosctl(ip: str, *args: str, timeout: int = 15) -> str:
    """Run talosctl --insecure against a Talos VM."""
    result = subprocess.run(
        [TALOSCTL, "--insecure", "-e", ip, "-n", ip, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout


# ============================================================================
# SETUP
# ============================================================================


def ensure_images() -> None:
    logger.info("Ensuring images are downloaded on atlas...")
    for name, url in IMAGE_URLS.items():
        out = ssh(
            ATLAS,
            f"test -f /tmp/kvm-test-{name}.qcow2 && echo exists || "
            f"{{ wget -q -O /tmp/kvm-test-{name}.qcow2 '{url}' && echo downloaded; }}",
            timeout=120,
            check=False,
        )
        logger.info("  %s: %s", name, out or "failed")


def upload_snippets() -> None:
    logger.info("Uploading cloud-init snippets...")
    ssh_pubkey = Path("~/.ssh/id_ed25519.pub").expanduser().read_text().strip()

    # Base cloud-init for cloud images
    _upload_ci_snippet("kvm-test-ci.yaml", ssh_pubkey)
    _upload_ci_snippet("kvm-test-ci-tsa-off.yaml", ssh_pubkey, extra_args="tsa=off")
    _upload_ci_snippet("kvm-test-ci-mitigations-off.yaml", ssh_pubkey, extra_args="mitigations=off")

    # Network configs for Talos VMs
    for suffix in range(1, 9):
        network_yaml = dedent(f"""\
            "network":
              "ethernets":
                "eth0":
                  "addresses":
                  - "{IP_PREFIX}.{suffix}/16"
                  "dhcp4": false
                  "dhcp6": false
                  "gateway4": "{GW}"
                  "nameservers":
                    "addresses":
                    - "1.1.1.1"
                    - "8.8.8.8"
              "version": 2""")
        ssh(ATLAS, f"cat > /var/lib/vz/snippets/kvm-test-net-{suffix}.yaml << 'NETEOF'\n{network_yaml}\nNETEOF")


def _upload_ci_snippet(name: str, ssh_pubkey: str, *, extra_args: str = "") -> None:
    runcmd = ""
    if extra_args:
        runcmd = dedent(f"""\
            runcmd:
              - sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="/GRUB_CMDLINE_LINUX_DEFAULT="{extra_args} /' /etc/default/grub
              - grub-mkconfig -o /boot/grub/grub.cfg
              - reboot""")

    ci = dedent(f"""\
        #cloud-config
        users:
          - name: test
            ssh_authorized_keys:
              - {ssh_pubkey}
            sudo: ALL=(ALL) NOPASSWD:ALL
            shell: /bin/bash
        packages:
          - stress-ng
        {runcmd}""")
    ssh(ATLAS, f"cat > /var/lib/vz/snippets/{name} << 'CIEOF'\n{ci}\nCIEOF")


# ============================================================================
# VM LIFECYCLE
# ============================================================================


def create_vm(t: TestConfig) -> None:
    logger.info("  Creating VM %d (%s) at %s...", t.vmid, t.image, t.ip)
    if t.is_talos:
        ci_args = f"--ide2 local-zfs:cloudinit --cicustom network=local:snippets/kvm-test-net-{t.ip_suffix}.yaml"
    else:
        ci_args = (
            f"--ide2 local-zfs:cloudinit"
            f" --cicustom user=local:snippets/{t.ci_snippet}"
            f" --ipconfig0 ip={t.ip}/16,gw={GW}"
            f" --agent enabled=1"
        )

    ssh(
        ATLAS,
        dedent(f"""\
        qm create {t.vmid} --name kvm-test-{t.vmid} --memory {VM_MEMORY_MB} --cores {VM_CORES} \\
          --cpu host --bios ovmf --machine q35 --vga virtio \\
          --net0 virtio,bridge=vmbr0,firewall=0 \\
          --scsihw virtio-scsi-single --balloon 0 --onboot 0 --ostype l26 \\
          --efidisk0 local-zfs:1,efitype=4m,pre-enrolled-keys=0 \\
          {ci_args} 2>&1 | grep -v 'parse error'
        qm importdisk {t.vmid} /tmp/kvm-test-{t.image}.qcow2 local-zfs 2>&1 | tail -1
        qm set {t.vmid} --scsi0 local-zfs:vm-{t.vmid}-disk-1,discard=on,iothread=1,ssd=1 \\
          --boot order=scsi0 2>&1 | grep -v 'parse error'
        qm start {t.vmid}"""),
        timeout=120,
        check=False,
    )


def destroy_vm(vmid: int) -> None:
    ssh(ATLAS, f"qm stop {vmid} 2>/dev/null; sleep 2; qm destroy {vmid} --purge 2>/dev/null", timeout=30, check=False)


def screenshot_vm(vmid: int, out: Path) -> None:
    ssh(
        ATLAS,
        dedent(f"""\
        echo 'screendump /tmp/vm{vmid}-console.ppm' | qm monitor {vmid} >/dev/null 2>&1
        sleep 1
        python3 -c "from PIL import Image; Image.open('/tmp/vm{vmid}-console.ppm').save('/tmp/vm{vmid}-console.png')" """),
        timeout=15,
        check=False,
    )
    scp_from(f"{ATLAS}:/tmp/vm{vmid}-console.png", str(out / "console.png"))


# ============================================================================
# DATA COLLECTION
# ============================================================================


def collect_cloud(t: TestConfig, out: Path) -> bool:
    logger.info("  Collecting from cloud VM %s at %s...", t.test_id, t.ip)
    if not scp_to(str(COLLECT_SCRIPT), f"test@{t.ip}:/tmp/collect.sh"):
        logger.warning("  SCP failed to %s", t.ip)
        return False
    ssh(f"test@{t.ip}", "chmod +x /tmp/collect.sh && sudo /tmp/collect.sh /tmp/test-results", timeout=60, check=False)
    scp_from(f"test@{t.ip}:/tmp/test-results/", str(out))
    scp_from(f"test@{t.ip}:/tmp/stress.log", str(out / "stress_log.txt"))
    return True


def collect_talos(t: TestConfig, out: Path) -> bool:
    logger.info("  Collecting from Talos VM %s at %s...", t.test_id, t.ip)
    files = {
        "dmesg.txt": ["dmesg"],
        "interrupts.txt": ["read", "/proc/interrupts"],
        "cmdline.txt": ["read", "/proc/cmdline"],
        "cpuinfo.txt": ["read", "/proc/cpuinfo"],
        "softirqs.txt": ["read", "/proc/softirqs"],
        "meminfo.txt": ["read", "/proc/meminfo"],
        "stat.txt": ["read", "/proc/stat"],
        "version.txt": ["version"],
    }
    for filename, args in files.items():
        try:
            data = talosctl(t.ip, *args)
            (out / filename).write_text(data)
        except Exception:
            logger.warning("  Failed to collect %s from %s", filename, t.ip)

    # Build summary
    dmesg = (out / "dmesg.txt").read_text() if (out / "dmesg.txt").exists() else ""
    interrupts = (out / "interrupts.txt").read_text() if (out / "interrupts.txt").exists() else ""
    summary_lines = ["=== Quick Summary ==="]
    if (out / "version.txt").exists():
        summary_lines.append((out / "version.txt").read_text().strip())
    summary_lines.append("NMI counts:")
    summary_lines.extend(line for line in interrupts.splitlines() if "NMI" in line)
    summary_lines.append(
        f"RCU stalls: {sum(1 for line in dmesg.splitlines() if 'rcu' in line.lower() and 'stall' in line.lower())}"
    )
    summary_lines.append(f"NMI messages: {sum(1 for line in dmesg.splitlines() if 'nmi' in line.lower())}")
    (out / "summary.txt").write_text("\n".join(summary_lines) + "\n")
    return True


# ============================================================================
# TEST EXECUTION
# ============================================================================


def save_test_config(t: TestConfig, out: Path) -> None:
    host_info = {
        "halt_poll_ns": ssh(ATLAS, "cat /sys/module/kvm/parameters/halt_poll_ns", check=False),
        "kernel": ssh(ATLAS, "uname -r", check=False),
        "cmdline": ssh(ATLAS, "cat /proc/cmdline", check=False),
    }
    config = {
        "test_id": t.test_id,
        "vmid": t.vmid,
        "ip": t.ip,
        "image": t.image,
        "guest_args": t.guest_args,
        "workload": t.workload,
        "boot_wait": t.boot_wait,
        "soak_seconds": SOAK_SECONDS,
        "host": host_info,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out / "test_config.json").write_text(json.dumps(config, indent=2) + "\n")


def parse_result(t: TestConfig, out: Path) -> TestResult:
    r = TestResult(test_id=t.test_id)
    dmesg_path = out / "dmesg.txt"
    if dmesg_path.exists():
        dmesg = dmesg_path.read_text()
        r.nmi_count = sum(1 for line in dmesg.splitlines() if "nmi" in line.lower())
        r.stall_count = sum(1 for line in dmesg.splitlines() if "rcu" in line.lower() and "stall" in line.lower())
        r.reachable = True
    version_path = out / "version.txt"
    if version_path.exists():
        first_line = version_path.read_text().strip().split("\n")[0]
        parts = first_line.split()
        r.kernel = parts[2] if len(parts) > 2 else first_line[:30]
    error_path = out / "error.txt"
    if error_path.exists():
        r.error = error_path.read_text().strip()
    return r


def run_batch(tests: list[TestConfig], results_dir: Path) -> list[TestResult]:
    """Run a batch of tests in parallel."""
    logger.info("=== BATCH: %s ===", ", ".join(t.test_id for t in tests))

    # Create output dirs and save configs
    for t in tests:
        out = results_dir / t.test_id
        out.mkdir(parents=True, exist_ok=True)
        save_test_config(t, out)

    # Create and start all VMs
    for t in tests:
        create_vm(t)

    # Wait for longest boot time
    max_wait = max(t.boot_wait for t in tests)
    logger.info("Waiting %ds for all VMs to boot...", max_wait)
    time.sleep(max_wait)

    # Verify connectivity
    for t in tests:
        out = results_dir / t.test_id
        if ping(t.ip):
            logger.info("  %s (%s): reachable", t.test_id, t.ip)
        else:
            logger.warning("  %s (%s): UNREACHABLE", t.test_id, t.ip)
            (out / "error.txt").write_text("UNREACHABLE\n")

    # Start workloads
    for t in tests:
        if t.workload == "stress" and not t.is_talos:
            out = results_dir / t.test_id
            if (out / "error.txt").exists():
                continue
            logger.info("  Starting stress-ng on %s...", t.test_id)
            ssh(
                f"test@{t.ip}",
                f"nohup stress-ng --cpu 4 --vm 2 --vm-bytes 256M --timeout {SOAK_SECONDS}s > /tmp/stress.log 2>&1 &",
                timeout=15,
                check=False,
            )

    # Soak — skip if all VMs unreachable
    reachable_tests = [t for t in tests if not (results_dir / t.test_id / "error.txt").exists()]
    if reachable_tests:
        logger.info("Soaking %d reachable VMs for %ds...", len(reachable_tests), SOAK_SECONDS)
        time.sleep(SOAK_SECONDS)
    else:
        logger.warning("All VMs unreachable, skipping soak")

    # Collect artifacts
    results = []
    for t in tests:
        out = results_dir / t.test_id
        if (out / "error.txt").exists():
            results.append(parse_result(t, out))
            continue
        if t.is_talos:
            collect_talos(t, out)
        else:
            collect_cloud(t, out)
        screenshot_vm(t.vmid, out)
        results.append(parse_result(t, out))

    # Destroy all VMs
    for t in tests:
        destroy_vm(t.vmid)

    return results


# ============================================================================
# MAIN
# ============================================================================


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

    results_dir = SCRIPT_DIR / "results" / time.strftime("%Y%m%d-%H%M%S")
    results_dir.mkdir(parents=True, exist_ok=True)

    logger.info("KVM AMD Stall Test Matrix")
    logger.info("Results: %s", results_dir)
    logger.info("Host: %s", ssh(ATLAS, "uname -r", check=False))
    logger.info("halt_poll_ns: %s", ssh(ATLAS, "cat /sys/module/kvm/parameters/halt_poll_ns", check=False))
    logger.info("Host cmdline: %s", ssh(ATLAS, "cat /proc/cmdline", check=False))

    ensure_images()
    upload_snippets()

    # Run all tests in one batch (4 VMs, 2GiB each = 8GiB total)
    all_results = run_batch(TESTS, results_dir)

    # Final summary
    logger.info("")
    logger.info("=== FINAL SUMMARY ===")
    logger.info("%-35s %-20s %-8s %s", "TEST", "KERNEL", "NMIs", "STALLS")
    logger.info("-" * 75)
    for r in all_results:
        if r.error:
            logger.info("%-35s ERROR: %s", r.test_id, r.error)
        else:
            logger.info("%-35s %-20s %-8d %d", r.test_id, r.kernel, r.nmi_count, r.stall_count)
    logger.info("")
    logger.info("Full artifacts in %s", results_dir)


if __name__ == "__main__":
    main()
