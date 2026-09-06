from cryptography import x509
from mitmproxy.addons.tlsconfig import TlsConfig
from mitmproxy.certs import CertStoreEntry
from mitmproxy.proxy.context import Context
from mitmproxy.proxy.layers.modes import HttpProxy
from mitmproxy.proxy.layers.tls import ClientTLSLayer


class OuterTlsConfig(TlsConfig):
    name = "tlsconfig"

    def __init__(self, hostname: str) -> None:
        super().__init__()
        self.hostname = hostname

    def get_cert(self, conn_context: Context) -> CertStoreEntry:
        # The outer handshake already has an HttpLayer child in mitmproxy 12.2.3.
        # Count TLS layers instead of assuming a fixed total stack length.
        # An unexpected outer SNI must not get a trusted interception-CA certificate.
        if (
            isinstance(conn_context.layers[0], HttpProxy)
            and sum(isinstance(layer, ClientTLSLayer) for layer in conn_context.layers) == 1
        ):
            return self.certstore.get_cert(self.hostname, [x509.DNSName(self.hostname)])
        return super().get_cert(conn_context)
