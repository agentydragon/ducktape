# Anthropic TLS Inspection CA Certificates

Certificates for Anthropic's sandbox egress TLS inspection proxy, used in Claude Code remote environments.

## Files

| File | Description |
|------|-------------|
| `anthropic-tls-inspection-ca-production.crt` | Production CA (valid Jul 2025 - Jul 2035) |
| `anthropic-tls-inspection-ca-staging.crt` | Staging CA (valid Jul 2025 - Jul 2035) |
| `example-proxy-chain.pem` | Example full certificate chain showing proxy behavior |

## Certificate Details

**Production CA:**
- Subject: `O=Anthropic, CN=sandbox-egress-production TLS Inspection CA`
- Self-signed root CA
- RSA 2048-bit key

**Staging CA:**
- Subject: `O=Anthropic, CN=sandbox-egress-staging TLS Inspection CA`
- Self-signed root CA
- RSA 2048-bit key

## How These Were Captured

Extracted from a Claude Code remote sandbox environment on 2026-01-20.

### CA Certificates

The CA certificates were found pre-installed in the sandbox at:
```
/usr/local/share/ca-certificates/swp-ca-production.crt
/usr/local/share/ca-certificates/swp-ca-staging.crt
```

Verified with:
```bash
openssl x509 -in /usr/local/share/ca-certificates/swp-ca-production.crt \
  -noout -subject -issuer -dates
```

### Certificate Chain

The proxy chain was captured by connecting through the egress proxy to an external site:

```python
import ssl, socket, base64, os

# Get proxy credentials from environment
proxy_url = os.environ.get("HTTPS_PROXY")
# Parse: http://user:jwt_token@host:port

# Connect via CONNECT tunnel with Basic auth
sock = socket.socket()
sock.connect((proxy_host, proxy_port))
auth = base64.b64encode(f"{user}:{password}".encode()).decode()
sock.sendall(f"CONNECT example.com:443 HTTP/1.1\r\n"
             f"Host: example.com:443\r\n"
             f"Proxy-Authorization: Basic {auth}\r\n\r\n".encode())
sock.recv(4096)  # Read 200 OK

# Wrap in SSL and extract certificate
context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
ssl_sock = context.wrap_socket(sock, server_hostname="example.com")
cert = ssl_sock.getpeercert(binary_form=True)
print(ssl.DER_cert_to_PEM_cert(cert))
```

The proxy intercepts HTTPS connections and re-signs them with short-lived certificates (typically 24h validity) issued by the TLS Inspection CA.

## Environment Variables

The sandbox sets these proxy-related environment variables:

```
HTTPS_PROXY=http://<container_id>:<jwt_token>@<proxy_ip>:15004
HTTP_PROXY=<same>
NO_PROXY=localhost,127.0.0.1,*.googleapis.com,*.google.com,...
```

The JWT token in the proxy credentials contains claims like `organization_uuid`, `session_id`, `container_id`, and `allowed_hosts`.

## Usage

To trust these CAs on a Linux system:

```bash
sudo cp anthropic-tls-inspection-ca-production.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates
```

For Python requests:
```bash
export REQUESTS_CA_BUNDLE=/path/to/anthropic-tls-inspection-ca-production.crt
# or combine with system certs
```

For Node.js:
```bash
export NODE_EXTRA_CA_CERTS=/path/to/anthropic-tls-inspection-ca-production.crt
```
