import asyncio
import base64
import json
import ssl
import stat
from pathlib import Path

import pytest_bazel
from aiohttp import ClientConnectionError, ClientSession, ClientTimeout

from cluster.proxies.github_api_proxy.testing import certificates
from util.oci import OciImage, load_oci_image
from util.testing.container_logs import LoggedContainer


def test_image_entrypoint_as_unprivileged_user() -> None:
    tag = load_oci_image(OciImage("_main/cluster/proxies/github_api_proxy/image_layout.rloc", "github-api-proxy:test"))
    with LoggedContainer(tag, test_name="proxy-image-entrypoint", command=["--help"], network_mode="none") as container:
        wrapped = container.get_wrapped_container()
        assert wrapped.wait(timeout=15)["StatusCode"] == 0
        assert b"--config" in wrapped.logs()
        assert wrapped.attrs["Config"]["User"] == "1000:1000"


async def test_image_boots_with_readonly_secrets_and_persistent_capture(tmp_path: Path) -> None:
    tag = load_oci_image(OciImage("_main/cluster/proxies/github_api_proxy/image_layout.rloc", "github-api-proxy:test"))
    public_tls = tmp_path / "public-tls"
    interception_ca = tmp_path / "interception-ca"
    client = tmp_path / "client"
    configuration = tmp_path / "config"
    capture = tmp_path / "capture"
    for directory in (public_tls, interception_ca, client, configuration, capture):
        directory.mkdir(mode=0o755)
    outer = certificates(public_tls, "outer", "localhost")
    certificates(interception_ca, "interception", None)
    password = "test-private-image-client-password"
    (client / "credentials.json").write_text(json.dumps({"test-image": password}))
    (configuration / "config.json").write_text(
        json.dumps(
            {
                "proxy_hostname": "localhost",
                "credential_files": ["/run/client/credentials.json"],
                "proxy_tls_cert_file": "/run/public-tls/outer.crt",
                "proxy_tls_key_file": "/run/public-tls/outer.key",
                "interception_ca_cert_file": "/run/interception-ca/interception.crt",
                "interception_ca_key_file": "/run/interception-ca/interception.key",
                "confdir": "/private-conf",
                "capture_path": "/capture/raw.flows",
                "session_ws_events": "/capture/sessions.jsonl",
            }
        )
    )
    # Emulate fsGroup ownership of the persistent volume before unprivileged boot.
    # Only the synthetic fixture volume is prepared by this short init container.
    with LoggedContainer(
        tag,
        test_name="proxy-capture-volume-init",
        entrypoint="/bin/chown",
        command=["1000:1000", "/capture"],
        user="0:0",
        network_mode="none",
        volumes=[(str(capture), "/capture", "rw")],
    ) as initializer:
        assert initializer.get_wrapped_container().wait(timeout=5)["StatusCode"] == 0
    previous = b""
    for attempt in range(2):
        with LoggedContainer(
            tag,
            test_name=f"proxy-mounted-boot-{attempt}",
            command=["--config", "/run/config/config.json"],
            volumes=[
                (str(public_tls), "/run/public-tls", "ro"),
                (str(interception_ca), "/run/interception-ca", "ro"),
                (str(client), "/run/client", "ro"),
                (str(configuration), "/run/config", "ro"),
                (str(capture), "/capture", "rw"),
            ],
            tmpfs={"/private-conf": "rw,noexec,nosuid,size=32m,uid=1000,gid=1000,mode=0700"},
        ).with_exposed_ports(8080, 9090) as container:
            host = container.get_container_host_ip()
            health_url = f"http://{host}:{container.get_exposed_port(9090)}/healthz"
            async with asyncio.timeout(15):
                # Use a bounded readiness wait; the image entrypoint builds its venv.
                async with ClientSession(timeout=ClientTimeout(total=1)) as session:
                    while True:
                        try:
                            async with session.get(health_url) as response:
                                if response.status == 200:
                                    break
                        except ClientConnectionError:
                            pass
                        await asyncio.sleep(0.1)
            context = ssl.create_default_context(cafile=str(outer.ca))
            async with asyncio.timeout(5):
                reader, writer = await asyncio.open_connection(
                    host, int(container.get_exposed_port(8080)), ssl=context, server_hostname="localhost"
                )
                try:
                    authorization = base64.b64encode(f"test-image:{password}".encode()).decode()
                    writer.write(
                        (
                            "GET http://mitm.it/ HTTP/1.1\r\nHost: mitm.it\r\n"
                            f"Proxy-Authorization: Basic {authorization}\r\nConnection: close\r\n\r\n"
                        ).encode()
                    )
                    await writer.drain()
                    response_bytes = await reader.read()
                    assert response_bytes.startswith(b"HTTP/1.1 200")
                    assert response_bytes.endswith(b"Authenticated proxy ready\n")
                finally:
                    writer.close()
                    await writer.wait_closed()
            assert container.get_wrapped_container().attrs["Config"]["User"] == "1000:1000"
        raw = capture / "raw.flows"
        captured = raw.read_bytes()
        assert len(captured) > len(previous)
        assert captured.startswith(previous)
        assert password.encode() not in captured
        assert stat.S_IMODE(raw.stat().st_mode) == 0o600
        assert raw.stat().st_uid == raw.stat().st_gid == 1000
        assert (capture / "sessions.jsonl").stat().st_size > 0
        previous = captured


if __name__ == "__main__":
    pytest_bazel.main()
