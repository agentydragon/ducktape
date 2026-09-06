import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from mitmproxy.addons.onboarding import Onboarding
from mitmproxy.addons.proxyauth import ProxyAuth
from mitmproxy.addons.save import Save
from mitmproxy.addons.tlsconfig import TlsConfig
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

from cluster.proxies.github_api_proxy.auth import Authenticate
from cluster.proxies.github_api_proxy.capture import PrivateSave, SessionMetadata
from cluster.proxies.github_api_proxy.config import Settings
from cluster.proxies.github_api_proxy.destinations import PublicOrigins
from cluster.proxies.github_api_proxy.metrics import Metrics
from cluster.proxies.github_api_proxy.tls import OuterTlsConfig


def private_pem(path: Path, cert_file: Path, key_file: Path, *, require_ca: bool) -> None:
    certificate = cert_file.read_bytes()
    private_key = key_file.read_bytes()
    cert = x509.load_pem_x509_certificate(certificate)
    key = serialization.load_pem_private_key(private_key, password=None)
    encoding = serialization.Encoding.DER
    public_format = serialization.PublicFormat.SubjectPublicKeyInfo
    if cert.public_key().public_bytes(encoding, public_format) != key.public_key().public_bytes(
        encoding, public_format
    ):
        raise ValueError("Mounted certificate and private key do not match")
    if require_ca and not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("Interception certificate is not a CA")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(private_key + b"\n" + certificate)


def create_master(settings: Settings, metrics: Metrics) -> DumpMaster:
    credentials = settings.credentials()
    settings.confdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_pem(
        settings.confdir / "mitmproxy-ca.pem",
        settings.interception_ca_cert_file,
        settings.interception_ca_key_file,
        require_ca=True,
    )
    outer_pem = settings.confdir / "proxy-tls.pem"
    private_pem(outer_pem, settings.proxy_tls_cert_file, settings.proxy_tls_key_file, require_ca=False)
    options = Options(
        listen_host=settings.listen_host,
        listen_port=settings.listen_port,
        confdir=str(settings.confdir),
        certs=[f"{settings.proxy_hostname}={outer_pem}"],
    )
    master = DumpMaster(options, with_termlog=False, with_dumper=False)
    builtin_auth = master.addons.get("proxyauth")
    builtin_save = master.addons.get("save")
    onboarding = master.addons.get("onboarding")
    builtin_tls = master.addons.get("tlsconfig")
    assert isinstance(builtin_auth, ProxyAuth)
    assert isinstance(builtin_save, Save)
    assert isinstance(onboarding, Onboarding)
    assert isinstance(builtin_tls, TlsConfig)
    for addon in (builtin_auth, builtin_save, onboarding, builtin_tls):
        master.addons.remove(addon)
    master.addons.add(
        OuterTlsConfig(settings.proxy_hostname),
        Authenticate(credentials, metrics, block_cloud_github_batch=settings.block_cloud_github_batch),
        PublicOrigins(settings.proxy_hostname),
        metrics,
        SessionMetadata(metrics),
        PrivateSave(settings.capture_path, metrics),
    )
    master.options.update(
        block_global=False,
        connection_strategy="lazy",
        save_stream_file=f"+{settings.capture_path}",
        store_streamed_bodies=True,
        record_cloud_session_ws=True,
        cloud_session_ws_events=str(settings.session_ws_events),
        ssl_verify_upstream_trusted_ca=str(settings.upstream_ca_file)
        if settings.upstream_ca_file is not None
        else None,
    )
    return master
