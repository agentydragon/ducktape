"""Exercise the pinned recorder image against a local HTTPS service."""

from __future__ import annotations

import hashlib
import io as bytes_io
import os
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import docker
import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from docker.models.containers import Container
from mitmproxy import http, io

from cluster.cdk8s.platform_sessions import busybox_image, mitmproxy_image, remote
from devinfra.oci.oci import load_oci_image
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

_ORIGIN_SERVER = r"""\
import http.server
import ssl

class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        marker = self.headers.get("X-Capture-Marker", "missing")
        response = b"synthetic-response-body:" + body
        self.send_response(201)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("X-Upstream-Response", "response-header-" + marker)
        self.send_header("Set-Cookie", "synthetic-session=unredacted-response-cookie; HttpOnly")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):
        pass

server = http.server.ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/origin/upstream.crt", "/origin/upstream.key")
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
"""

_CLIENT = r"""\
import ssl
import sys
import urllib.request

marker = sys.argv[1]
upstream = sys.argv[2]
body = ("synthetic-request-body:" + marker + ":unredacted-request-secret").encode()
request = urllib.request.Request(
    "https://" + upstream + ":8443/recorded?marker=" + marker,
    data=body,
    headers={
        "Authorization": "Bearer synthetic-unredacted-request-token",
        "X-Capture-Marker": marker,
        "X-Request-Secret": "synthetic-unredacted-request-header",
        "Content-Type": "application/octet-stream",
    },
    method="POST",
)
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"https": "http://127.0.0.1:18080"}),
    urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile="/ca/tls.crt")),
)
with opener.open(request, timeout=15) as response:
    assert response.status == 201
    assert response.headers["X-Upstream-Response"] == "response-header-" + marker
    assert response.headers["Set-Cookie"] == "synthetic-session=unredacted-response-cookie; HttpOnly"
    assert response.read() == b"synthetic-response-body:" + body
"""


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _private_key_pem(key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _ca_key_usage() -> x509.KeyUsage:
    # cert-manager defaults to digitalSignature + keyEncipherment and adds
    # keyCertSign when isCA is true.
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=True,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )


def _ca(common_name: str) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = _name(common_name)
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(_ca_key_usage(), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _recorder_ca(common_name: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = _name(common_name)
    now = datetime.now(UTC)
    # cert-manager's interception_root_ca uses RFC 7093 method 1: SHA-256 of the
    # uncompressed EC point, truncated to the leftmost 160 bits. Mitmproxy 12.2.3
    # must copy this explicit SKI instead of deriving the legacy SHA-1 value.
    point = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ski = hashlib.sha256(point).digest()[:20]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(_ca_key_usage(), critical=True)
        .add_extension(x509.SubjectKeyIdentifier(ski), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _origin_certificate(
    ca_key: rsa.RSAPrivateKey, ca_cert: x509.Certificate, hostname: str
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(hostname))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = bytes_io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.mode = 0o644
            entry.size = len(content)
            archive.addfile(entry, bytes_io.BytesIO(content))
    return buffer.getvalue()


def _put_files(container: Container, destination: str, files: dict[str, bytes]) -> None:
    assert container.put_archive(destination, _tar(files)), f"Could not stage fixture files in {destination}"


def _output_bytes(output: object) -> bytes:
    if isinstance(output, bytes):
        return output
    if isinstance(output, str):
        return output.encode()
    try:
        return b"".join(output)  # type: ignore[arg-type]
    except TypeError:
        return repr(output).encode()


def _save_container_diagnostics(container: Container, label: str, details: bytes = b"") -> Path:
    safe_label = "".join(character if character.isalnum() or character in "-." else "_" for character in label)
    path = undeclared_outputs_dir() / f"platform-sessions-recorder-{safe_label}.log"
    try:
        logs = container.logs(stdout=True, stderr=True)
    except Exception as error:
        logs = f"Could not collect container logs: {error!r}".encode()
    path.write_bytes(details + b"\n--- container logs ---\n" + logs)
    return path


def _pem_cert(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _wait_for_exec(container: Container, command: list[str], *, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    probes: list[str] = []
    while time.monotonic() < deadline:
        result = container.exec_run(command)
        probes.append(f"exit={result.exit_code} output={_output_bytes(result.output)!r}")
        if result.exit_code == 0:
            return
        if container.status != "running":
            break
        time.sleep(0.2)
    path = _save_container_diagnostics(container, "readiness", "\n".join(probes).encode())
    raise AssertionError(f"Container did not become ready; diagnostics saved to {path}")


def _read_capture_volume(container: Container) -> dict[str, tuple[bytes, int]]:
    chunks, _ = container.get_archive("/captures")
    archive_bytes = b"".join(chunks)
    result: dict[str, tuple[bytes, int]] = {}
    with tarfile.open(fileobj=bytes_io.BytesIO(archive_bytes), mode="r:") as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.endswith(".mitm"):
                continue
            stream = archive.extractfile(member)
            assert stream is not None
            result[Path(member.name).name] = (stream.read(), member.mode & 0o777)
    return result


def _flows(data: bytes) -> list[http.HTTPFlow]:
    with bytes_io.BytesIO(data) as stream:
        return [flow for flow in io.FlowReader(stream).stream() if isinstance(flow, http.HTTPFlow)]


def _assert_full_fidelity(flows: list[http.HTTPFlow], marker: str) -> None:
    [flow] = [item for item in flows if f"marker={marker}" in item.request.url]
    assert flow.response is not None
    expected_body = f"synthetic-request-body:{marker}:unredacted-request-secret".encode()
    assert flow.request.content == expected_body
    assert flow.request.headers["Authorization"] == "Bearer synthetic-unredacted-request-token"
    assert flow.request.headers["X-Request-Secret"] == "synthetic-unredacted-request-header"
    assert flow.request.headers["X-Capture-Marker"] == marker
    assert flow.response.status_code == 201
    assert flow.response.content == b"synthetic-response-body:" + expected_body
    assert flow.response.headers["X-Upstream-Response"] == f"response-header-{marker}"
    assert flow.response.headers["Set-Cookie"] == "synthetic-session=unredacted-response-cookie; HttpOnly"


def _container_mounts(config: str, captures: str, ca: str, origin: str) -> dict[str, dict[str, str]]:
    return {
        config: {"bind": "/config", "mode": "rw"},
        captures: {"bind": "/captures", "mode": "rw"},
        ca: {"bind": "/ca", "mode": "ro"},
        origin: {"bind": "/origin", "mode": "ro"},
    }


def _deployed_recorder_contract() -> tuple[str, list[str]]:
    objects = Cdk8sTesting.synth(remote.chart(App(), record_upstream_traffic=True))
    recorders = [
        container
        for obj in objects
        if obj["kind"] == "Deployment"
        for container in obj["spec"]["template"]["spec"].get("initContainers", [])
        if container["name"] == "upstream-recorder"
    ]
    assert recorders
    contracts = {(recorder["image"], tuple(recorder["command"])) for recorder in recorders}
    assert len(contracts) == 1, f"Providers have different recorder images/commands: {contracts}"
    image, command = next(iter(contracts))
    return image, list(command)


def test_recorder_proxy_trusts_client_and_appends_full_flows_after_restart() -> None:
    deployed_image, deployed_command = _deployed_recorder_contract()
    pinned_image = os.environ["MITMPROXY_TEST_IMAGE_REFERENCE"]
    for registry in ("mirror.gcr.io/", "docker.io/"):
        if pinned_image.startswith(registry):
            pinned_image = pinned_image.removeprefix(registry)
            break
    assert deployed_image == pinned_image
    assert deployed_command[:2] == ["sh", "-c"]
    assert len(deployed_command) == 3
    production_command = deployed_command[2]
    assert production_command.count(" -w ") == 1
    recorder_command = production_command.replace(
        " -w ", " --set ssl_verify_upstream_trusted_ca=/origin/upstream-ca.crt -w ", 1
    )
    mitmproxy_ref = load_oci_image(mitmproxy_image.LAYOUT_RLOCATION)
    busybox_ref = load_oci_image(busybox_image.LAYOUT_RLOCATION)
    client = docker.from_env()
    suffix = uuid4().hex
    config_volume = client.volumes.create(name=f"platform-sessions-recorder-config-{suffix}")
    captures_volume = client.volumes.create(name=f"platform-sessions-recorder-captures-{suffix}")
    ca_volume = client.volumes.create(name=f"platform-sessions-recorder-ca-{suffix}")
    origin_volume = client.volumes.create(name=f"platform-sessions-recorder-origin-{suffix}")
    fixture_volume = client.volumes.create(name=f"platform-sessions-recorder-fixture-{suffix}")
    network = client.networks.create(name=f"platform-sessions-recorder-{suffix}", internal=True)
    containers: list[Container] = []
    volumes = [config_volume, captures_volume, ca_volume, origin_volume, fixture_volume]
    try:
        recorder_key, recorder_cert = _recorder_ca("platform-sessions-recorder-test")
        assert isinstance(recorder_key.curve, ec.SECP256R1)
        recorder_ski = recorder_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
        recorder_point = recorder_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        assert recorder_ski == hashlib.sha256(recorder_point).digest()[:20]
        assert recorder_ski != x509.SubjectKeyIdentifier.from_public_key(recorder_key.public_key()).digest
        upstream_ca_key, upstream_ca_cert = _ca("controlled-upstream-test-ca")
        upstream_name = f"controlled-upstream-{suffix}"
        upstream_key, upstream_cert = _origin_certificate(upstream_ca_key, upstream_ca_cert, upstream_name)

        # Prepare the mounted-volume ownership expected after the pod's fsGroup fix-up.
        setup = client.containers.run(
            mitmproxy_ref,
            entrypoint=["sh", "-c"],
            command=["while :; do sleep 3600; done"],
            detach=True,
            user="0:0",
            volumes={
                config_volume.name: {"bind": "/config", "mode": "rw"},
                captures_volume.name: {"bind": "/captures", "mode": "rw"},
                ca_volume.name: {"bind": "/ca", "mode": "rw"},
                origin_volume.name: {"bind": "/origin", "mode": "rw"},
                fixture_volume.name: {"bind": "/fixture", "mode": "rw"},
            },
        )
        containers.append(setup)
        _put_files(setup, "/ca", {"tls.key": _private_key_pem(recorder_key), "tls.crt": _pem_cert(recorder_cert)})
        _put_files(
            setup,
            "/origin",
            {
                "upstream-ca.crt": _pem_cert(upstream_ca_cert),
                "upstream.crt": _pem_cert(upstream_cert),
                "upstream.key": _private_key_pem(upstream_key),
            },
        )
        _put_files(setup, "/fixture", {"origin.py": _ORIGIN_SERVER.encode()})
        ownership = setup.exec_run(
            [
                "sh",
                "-c",
                "chown -R 1000:1000 /config /captures /ca /origin /fixture && "
                "chmod 0770 /config /captures && chmod 0440 /ca/tls.key /ca/tls.crt "
                "/origin/upstream-ca.crt /origin/upstream.crt /origin/upstream.key && "
                "chmod 0550 /fixture /fixture/origin.py",
            ]
        )
        if ownership.exit_code != 0:
            path = _save_container_diagnostics(setup, "volume-setup", _output_bytes(ownership.output))
            raise AssertionError(f"Could not set fixture volume ownership; diagnostics saved to {path}")
        setup.remove(force=True)
        containers.remove(setup)

        # Run the exact pod init command as UID 1000 with a read-only root filesystem.
        init = client.containers.run(
            busybox_ref,
            entrypoint=["sh", "-c"],
            command=["umask 077; cat /ca/tls.key /ca/tls.crt > /config/mitmproxy-ca.pem"],
            user="1000:1000",
            read_only=True,
            volumes={
                ca_volume.name: {"bind": "/ca", "mode": "ro"},
                config_volume.name: {"bind": "/config", "mode": "rw"},
            },
            detach=True,
        )
        containers.append(init)
        init_result = init.wait(timeout=30)
        if init_result["StatusCode"] != 0:
            path = _save_container_diagnostics(init, "ca-init")
            raise AssertionError(
                f"BusyBox CA init failed with {init_result['StatusCode']}; diagnostics saved to {path}"
            )
        init.reload()
        assert init.attrs["Config"]["User"] == "1000:1000"
        assert init.attrs["HostConfig"]["ReadonlyRootfs"] is True
        ca_archive, _ = init.get_archive("/config/mitmproxy-ca.pem")
        with tarfile.open(fileobj=bytes_io.BytesIO(b"".join(ca_archive)), mode="r:") as archive:
            ca_file = archive.next()
            assert ca_file is not None
            ca_contents = archive.extractfile(ca_file)
            assert ca_contents is not None
            assert ca_contents.read() == _private_key_pem(recorder_key) + _pem_cert(recorder_cert)
            assert ca_file.mode & 0o777 == 0o600
        init.remove()
        containers.remove(init)

        origin = client.containers.run(
            mitmproxy_ref,
            entrypoint=["python", "-u"],
            command=["/fixture/origin.py"],
            user="1000:1000",
            read_only=True,
            network=network.name,
            name=upstream_name,
            volumes={
                origin_volume.name: {"bind": "/origin", "mode": "ro"},
                fixture_volume.name: {"bind": "/fixture", "mode": "ro"},
            },
            environment={"PYTHONDONTWRITEBYTECODE": "1"},
            detach=True,
        )
        containers.append(origin)

        proxy = client.containers.run(
            mitmproxy_ref,
            entrypoint=["sh", "-c"],
            command=[recorder_command],
            user="1000:1000",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            network=network.name,
            volumes=_container_mounts(config_volume.name, captures_volume.name, ca_volume.name, origin_volume.name),
            detach=True,
        )
        containers.append(proxy)
        proxy.reload()
        assert proxy.attrs["Config"]["User"] == "1000:1000"
        assert proxy.attrs["HostConfig"]["ReadonlyRootfs"] is True
        _wait_for_exec(
            proxy, ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 18080), 1).close()"]
        )
        _wait_for_exec(
            proxy, ["python", "-c", f"import socket; socket.create_connection(({upstream_name!r}, 8443), 1).close()"]
        )

        first_marker = "before-recorder-restart"
        first = proxy.exec_run(["python", "-c", _CLIENT, first_marker, upstream_name])
        if first.exit_code != 0:
            path = _save_container_diagnostics(proxy, "first-request", _output_bytes(first.output))
            raise AssertionError(f"Client request before restart failed; diagnostics saved to {path}")
        proxy.stop(timeout=15)

        first_reader = client.containers.run(
            mitmproxy_ref,
            entrypoint=["sh", "-c"],
            command=["sleep 3600"],
            user="1000:1000",
            read_only=True,
            volumes={captures_volume.name: {"bind": "/captures", "mode": "ro"}},
            detach=True,
        )
        containers.append(first_reader)
        first_capture = _read_capture_volume(first_reader)
        first_reader.remove(force=True)
        containers.remove(first_reader)
        assert len(first_capture) == 1, list(first_capture)
        first_file, (first_bytes, first_mode) = next(iter(first_capture.items()))
        assert first_mode == 0o600
        _assert_full_fidelity(_flows(first_bytes), first_marker)

        # Starting the same container reuses its persistent capture volume and CA confdir.
        proxy.start()
        _wait_for_exec(
            proxy, ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 18080), 1).close()"]
        )
        second_marker = "after-recorder-restart"
        second = proxy.exec_run(["python", "-c", _CLIENT, second_marker, upstream_name])
        if second.exit_code != 0:
            path = _save_container_diagnostics(proxy, "second-request", _output_bytes(second.output))
            raise AssertionError(f"Client request after restart failed; diagnostics saved to {path}")
        proxy.stop(timeout=15)

        final_reader = client.containers.run(
            mitmproxy_ref,
            entrypoint=["sh", "-c"],
            command=["sleep 3600"],
            user="1000:1000",
            read_only=True,
            volumes={captures_volume.name: {"bind": "/captures", "mode": "ro"}},
            detach=True,
        )
        containers.append(final_reader)
        final_capture = _read_capture_volume(final_reader)
        assert first_file in final_capture
        flows_by_file = {name: _flows(data) for name, (data, _) in final_capture.items()}
        all_flows = [flow for flows in flows_by_file.values() for flow in flows]
        _assert_full_fidelity(all_flows, first_marker)
        _assert_full_fidelity(all_flows, second_marker)
        assert len(all_flows) == 2
        assert all(mode == 0o600 for _, mode in final_capture.values())
        second_file = next(
            name
            for name, flows in flows_by_file.items()
            if any(f"marker={second_marker}" in flow.request.url for flow in flows)
        )
        if second_file == first_file:
            assert len(flows_by_file[first_file]) == 2
        else:
            # Daily rotation is expected if a test happens to cross UTC midnight.
            assert len(final_capture) == 2
            assert len(flows_by_file[first_file]) == len(flows_by_file[second_file]) == 1
        final_reader.remove(force=True)
        containers.remove(final_reader)
    except BaseException:
        for index, container in enumerate(containers):
            _save_container_diagnostics(container, f"container-{index}-{container.name}")
        raise
    finally:
        for container in reversed(containers):
            container.remove(force=True)
        network.remove()
        for volume in volumes:
            volume.remove(force=True)
        client.close()


if __name__ == "__main__":
    pytest_bazel.main()
