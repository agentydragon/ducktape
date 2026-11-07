"""Run the manual KVM experiment inside its disposable launcher pod."""

import argparse
import hashlib
import http.client
import json
import os
import select
import shutil
import socket
import socketserver
import subprocess
import threading
import time
from contextlib import ExitStack, closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

GUEST = "192.0.2.2"
GATEWAY = "192.0.2.1"
OPENER = build_opener(ProxyHandler({}))


def run(*argv: str) -> str:
    return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT).strip()


def wait_for(function: Any, *, timeout: float = 30) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = function()
            if result:
                return result
        except (OSError, ValueError, http.client.HTTPException) as error:
            if time.monotonic() >= deadline:
                raise TimeoutError("condition did not become true") from error
        if time.monotonic() >= deadline:
            raise TimeoutError("condition did not become true")
        threading.Event().wait(0.1)


def guest(path: str, **params: str) -> dict[str, Any]:
    suffix = "?" + urlencode(params) if params else ""
    with OPENER.open(f"http://{GUEST}:8080{path}{suffix}", timeout=20) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("guest response must be an object")
    return payload


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path) -> None:
        super().__init__("localhost", timeout=20)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


def api(path: Path, method: str, route: str, body: dict[str, Any]) -> dict[str, Any]:
    with closing(UnixConnection(path)) as connection:
        connection.request(
            method, route, None if method == "GET" else json.dumps(body), {"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        payload = response.read().decode()
        if response.status >= 300:
            raise RuntimeError(f"{method} {route}: {response.status} {payload}")
        return json.loads(payload) if payload else {}


@contextmanager
def process(argv: list[str], log: Path, **kwargs: Any) -> Any:
    with log.open("ab") as output, subprocess.Popen(argv, stdout=output, stderr=output, **kwargs) as child:
        try:
            yield child
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


class Upstream(BaseHTTPRequestHandler):
    records: ClassVar[list[dict[str, Any]]] = []
    token: str = "spike-hop-v1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_CONNECT(self) -> None:
        valid = self.headers.get("Proxy-Authorization") == f"Bearer {self.token}"
        self.records.append(
            {"target": self.path, "valid_hop": valid, "projected": self.headers.get_all("X-Agentplane-Workload-Token")}
        )
        if not valid or self.path != "example.com:443":
            self.send_error(403)
            return
        with socket.create_connection(("example.com", 443), timeout=5) as upstream:
            self.send_response(200)
            self.end_headers()
            self.wfile.flush()
            while True:
                readable, _, _ = select.select([self.connection, upstream], [], [], 8)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.connection else self.connection).sendall(data)


class DualStackServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class EchoDatagram(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        payload, connection = self.request
        connection.sendto(payload, self.client_address)


def setup_network() -> None:
    run("ip", "tuntap", "add", "tap0", "mode", "tap")
    run("ip", "addr", "add", f"{GATEWAY}/30", "dev", "tap0")
    run("ip", "-6", "addr", "add", "fd00:fc::1/64", "dev", "tap0", "nodad")
    run("ip", "link", "set", "tap0", "mtu", "1280", "up")
    Path("/proc/sys/net/ipv4/ip_forward").write_text("1")
    run("iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "192.0.2.0/30", "-o", "eth0", "-j", "MASQUERADE")


def fence() -> None:
    for tool in ("iptables", "ip6tables"):
        run(tool, "-I", "FORWARD", "1", "-i", "tap0", "-j", "REJECT")
        run(tool, "-I", "INPUT", "1", "-i", "tap0", "-j", "REJECT")
        run(tool, "-I", "INPUT", "1", "-i", "tap0", "-p", "tcp", "--dport", "3128", "-j", "ACCEPT")
        run(
            tool,
            "-I",
            "INPUT",
            "1",
            "-i",
            "tap0",
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED,RELATED",
            "-j",
            "ACCEPT",
        )
    for message in ("neighbor-solicitation", "neighbor-advertisement"):
        run(
            "ip6tables",
            "-I",
            "INPUT",
            "1",
            "-i",
            "tap0",
            "-p",
            "ipv6-icmp",
            "--icmpv6-type",
            message,
            "-m",
            "hl",
            "--hl-eq",
            "255",
            "-j",
            "ACCEPT",
        )


def configure(api_path: Path, artifacts: Path, disks: Path) -> None:
    api(api_path, "PUT", "/machine-config", {"vcpu_count": 1, "mem_size_mib": 256})
    api(
        api_path,
        "PUT",
        "/boot-source",
        {
            "kernel_image_path": str(artifacts / "vmlinux"),
            "boot_args": "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/sbin/spike-init",
        },
    )
    drives = [
        ("rootfs", "rootfs.ext4", False),
        ("agentplane_runner", "runner.erofs", True),
        ("state", "state.ext4", False),
        ("workspace", "workspace.ext4", False),
        ("agentplane_config", "config.iso", True),
        ("agentplane_launch", "launch.iso", True),
        ("agentplane_trust", "trust.iso", True),
        ("credentials", "credentials.ext4", True),
    ]
    for index, (name, filename, readonly) in enumerate(drives):
        api(
            api_path,
            "PUT",
            f"/drives/{name}",
            {
                "drive_id": name,
                "path_on_host": str(disks / filename),
                "is_root_device": index == 0,
                "is_read_only": readonly,
            },
        )
    api(
        api_path,
        "PUT",
        "/network-interfaces/eth0",
        {"iface_id": "eth0", "host_dev_name": "tap0", "guest_mac": "06:00:ac:10:00:02"},
    )
    api(api_path, "PUT", "/actions", {"action_type": "InstanceStart"})


def check(condition: bool, detail: object) -> None:
    if not condition:
        raise AssertionError(detail)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path, default=Path("/work"))
    args = parser.parse_args()
    artifacts = args.work / "artifacts"
    evidence = args.work / "evidence"
    evidence.mkdir(exist_ok=True)
    disks = args.work / "live"
    disks.mkdir()
    for source in artifacts.iterdir():
        if source.suffix in {".ext4", ".erofs", ".iso"}:
            shutil.copyfile(source, disks / source.name)
    results: dict[str, Any] = {
        "firecracker": run(str(artifacts / "firecracker"), "--version"),
        "host": run("uname", "-a"),
    }
    try:
        setup_network()
        relay_token = args.work / "relay-token"
        relay_token.write_text(Upstream.token)
        audience_token = args.work / "audience-token"
        audience_token.write_text("spike-audience-v1")
        settings = args.work / "relay.json"
        settings.write_text(
            json.dumps(
                {
                    "proxy_host": "127.0.0.1",
                    "proxy_port": 8888,
                    "listen_address": "[::]:3128",
                    "readiness_listen_address": "127.0.0.1:3129",
                    "token_file": str(relay_token),
                    "audience_token_files": {"spike-audience": str(audience_token)},
                    "platform_routes": {"routes": {}},
                }
            )
        )
        with ExitStack() as stack:
            server = stack.enter_context(DualStackServer(("::", 8888), Upstream))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            stack.callback(server.shutdown)
            udp = stack.enter_context(socketserver.UDPServer((GATEWAY, 5353), EchoDatagram))
            threading.Thread(target=udp.serve_forever, daemon=True).start()
            stack.callback(udp.shutdown)
            relay = stack.enter_context(
                process(
                    [str(artifacts / "usr/local/bin/egress-sidecar")],
                    evidence / "relay.log",
                    env={**os.environ, "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE": str(settings)},
                )
            )
            wait_for(lambda: OPENER.open("http://127.0.0.1:3129/readyz", timeout=1).status == 200)
            api_path = args.work / "firecracker.sock"
            vm = stack.enter_context(
                process([str(artifacts / "firecracker"), "--api-sock", str(api_path)], evidence / "boot.log")
            )
            wait_for(api_path.exists)
            configure(api_path, artifacts, disks)
            results["initial"] = wait_for(lambda: guest("/state"), timeout=40)
            results["mount_proof"] = guest("/proof")
            proof = results["mount_proof"]
            for field in ("runner_help", "trust_readable_as_workload"):
                check(proof[field]["code"] == 0, {field: proof[field]})
            check(proof["execution_namespace_guard"]["code"] == 64, proof["execution_namespace_guard"])
            for field in ("runner_disk_write", "config_disk_write", "credential_as_workload", "state_owner_contender"):
                check(proof[field]["code"] != 0, {field: proof[field]})
            public_ip = socket.gethostbyname("example.com")
            resolve = f"example.com:443:{public_ip}"
            results["direct_before_fence"] = guest("/network", url="https://example.com/", resolve=resolve)
            check(results["direct_before_fence"]["code"] == 0, results["direct_before_fence"])
            results["controls_before_fence"] = {
                "upstream_v4": guest("/tcp", host=GATEWAY, port="8888"),
                "upstream_v6": guest("/tcp", host="fd00:fc::1", port="8888"),
                "udp": guest("/udp", host=GATEWAY, port="5353"),
            }
            check(results["controls_before_fence"]["upstream_v4"]["connected"], results["controls_before_fence"])
            check(results["controls_before_fence"]["upstream_v6"]["connected"], results["controls_before_fence"])
            check(results["controls_before_fence"]["udp"].get("echo") == "spike-udp", results["controls_before_fence"])
            fence()
            results["network"] = {}
            for name, params in {
                "proxy_v4": {"proxy": "http://192.0.2.1:3128", "url": "https://example.com/"},
                "proxy_v6": {"proxy": "http://[fd00:fc::1]:3128", "url": "https://example.com/"},
                "direct_public": {"url": "https://example.com/", "resolve": resolve},
                "proxy_bypass": {"url": "http://192.0.2.1:8888/"},
            }.items():
                result = guest("/network", **params)
                results["network"][name] = result
                check((result["code"] == 0) == name.startswith("proxy_v"), {name: result})
            results["network"]["dns_tcp"] = guest("/tcp", host=GATEWAY, port="53")
            results["network"]["host_other_v6"] = guest("/tcp", host="fd00:fc::1", port="8888")
            for name in ("dns_tcp", "host_other_v6"):
                check(not results["network"][name]["connected"], results["network"][name])
            results["network"]["udp_bypass"] = guest("/udp", host=GATEWAY, port="5353")
            check("error" in results["network"]["udp_bypass"], results["network"]["udp_bypass"])
            Upstream.token = "spike-hop-v2"
            relay_token.write_text(Upstream.token)
            results["token_rotation"] = guest("/network", proxy="http://192.0.2.1:3128", url="https://example.com/")
            check(results["token_rotation"]["code"] == 0, results["token_rotation"])
            before = guest("/advance")
            api(api_path, "PATCH", "/vm", {"state": "Paused"})
            results["paused_api"] = api(api_path, "GET", "/", {})
            check(results["paused_api"]["state"] == "Paused", results["paused_api"])
            try:
                OPENER.open(f"http://{GUEST}:8080/state", timeout=0.5)
            except (TimeoutError, OSError) as error:
                results["paused_guest_unresponsive"] = True
                results["paused_guest_error"] = type(error).__name__
            else:
                raise AssertionError("paused guest served a request")
            api(api_path, "PATCH", "/vm", {"state": "Resumed"})
            results["pause_resume"] = wait_for(lambda: guest("/state"))
            for key in (
                "nonce",
                "memory_sha256",
                "pid",
                "boot_id",
                "counter",
                "file_offset",
                "file_inode",
                "child_pid",
            ):
                check(before[key] == results["pause_resume"][key], {"pause_resume": key})
            guest("/sync")
            before = guest("/advance")
            api(api_path, "PATCH", "/vm", {"state": "Paused"})
            api(
                api_path,
                "PUT",
                "/snapshot/create",
                {
                    "snapshot_type": "Full",
                    "snapshot_path": str(evidence / "vmstate"),
                    "mem_file_path": str(evidence / "memory"),
                },
            )
            results["snapshot"] = {
                "memory_bytes": (evidence / "memory").stat().st_size,
                "vmstate_bytes": (evidence / "vmstate").stat().st_size,
                "before": before,
            }
            saved = args.work / "saved-disks"
            shutil.copytree(disks, saved)
            vm.kill()
            vm.wait()
            api_path.unlink()
            # Save every backing disk at the RAM boundary; mutate the first continuation, then
            # restore the saved set so the second continuation cannot observe future disk writes.
            for attempt in (1, 2):
                with process(
                    [str(artifacts / "firecracker"), "--api-sock", str(api_path)], evidence / f"restore-{attempt}.log"
                ) as restored:
                    wait_for(api_path.exists)
                    api(
                        api_path,
                        "PUT",
                        "/snapshot/load",
                        {
                            "snapshot_path": str(evidence / "vmstate"),
                            "mem_backend": {"backend_type": "File", "backend_path": str(evidence / "memory")},
                            "resume_vm": True,
                        },
                    )
                    after = wait_for(lambda: guest("/state"))
                    for key in (
                        "nonce",
                        "memory_sha256",
                        "pid",
                        "boot_id",
                        "counter",
                        "file_offset",
                        "file_inode",
                        "child_pid",
                        "sqlite_rows",
                    ):
                        check(
                            before[key] == after[key],
                            {"restore": attempt, "key": key, "before": before, "after": after},
                        )
                    check(after["child_alive"] and after["sqlite_integrity"] == "ok", after)
                    fd = guest("/read-fd")
                    check(fd["read"] == "world\n", fd)
                    network = guest("/network", proxy="http://192.0.2.1:3128", url="https://example.com/")
                    check(network["code"] == 0, network)
                    blocked = guest("/network", url="https://example.com/", resolve=resolve)
                    check(blocked["code"] != 0, blocked)
                    results[f"restore_{attempt}"] = {
                        "host_pid": restored.pid,
                        "state": after,
                        "open_fd": fd,
                        "proxy": network,
                        "direct": blocked,
                    }
                    guest("/advance")
                api_path.unlink()
                if attempt == 1:
                    shutil.rmtree(disks)
                    shutil.copytree(saved, disks)
            results["original_host_pid"] = vm.pid
            with process([str(artifacts / "firecracker"), "--api-sock", str(api_path)], evidence / "cold-restart.log"):
                wait_for(api_path.exists)
                configure(api_path, artifacts, disks)
                cold = wait_for(lambda: guest("/state"))
                check(cold["nonce"] != before["nonce"] and cold["boot_id"] != before["boot_id"], cold)
                check(cold["retained_state"] == before["nonce"] == cold["retained_workspace"], cold)
                check(cold["sqlite_rows"] == before["sqlite_rows"] + 1, cold)
                results["cold_restart"] = cold
                relay.terminate()
                relay.wait(timeout=5)
                results["proxy_unavailable"] = guest(
                    "/network", proxy="http://192.0.2.1:3128", url="https://example.com/"
                )
                results["direct_with_proxy_unavailable"] = guest(
                    "/network", url="https://example.com/", resolve=resolve
                )
                check(results["proxy_unavailable"]["code"] != 0, results["proxy_unavailable"])
                check(results["direct_with_proxy_unavailable"]["code"] != 0, results["direct_with_proxy_unavailable"])
            results["relay_records"] = Upstream.records
            check(all(record["valid_hop"] for record in Upstream.records), Upstream.records)
            check(
                all(record["projected"] == ["spike-audience spike-audience-v1"] for record in Upstream.records),
                Upstream.records,
            )
            results["status"] = "passed"
    except BaseException as exc:
        results["status"] = "failed"
        results["error"] = repr(exc)
        raise
    finally:
        results["iptables"] = run("iptables-save", "-c")
        results["ip6tables"] = run("ip6tables-save", "-c")
        results["artifact_hashes"] = {}
        for path in artifacts.iterdir():
            if path.is_file() and path.name != "agentplane-inputs.tar":
                with path.open("rb") as artifact:
                    results["artifact_hashes"][path.name] = hashlib.file_digest(artifact, "sha256").hexdigest()
        (evidence / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps({"status": results["status"], "evidence": str(evidence)}), flush=True)


if __name__ == "__main__":
    main()
