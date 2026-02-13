# Certificate Caching Evidence

**Finding**: The TLS inspection proxy does NOT regenerate certificates per-connection.
Instead, it uses a **certificate cache** with multiple certificates in rotation.

## Test Results (2026-01-20T01:31:20Z)

5 sequential connections to `example.com` through the proxy:

| Connection | Serial Number | Fingerprint (SHA1) | Validity |
|------------|---------------|-------------------|----------|
| 1 | `1393CE5D93B549C0B25F0E93185D42BCDEA12E` | `5F:C4:C8:6B:F8:2E:...` | Jan 19 07:46 - Jan 20 09:28 |
| 2 | `ECA9D503A20ABD1E71EDA3EF228899AC001C84` | `26:33:B1:8E:6B:78:...` | Jan 19 02:58 - Jan 20 05:35 |
| 3 | `1393CE5D93B549C0B25F0E93185D42BCDEA12E` | `5F:C4:C8:6B:F8:2E:...` | Jan 19 07:46 - Jan 20 09:28 |
| 4 | `29896E9F338E1281F73BE88137D9A361910FB8` | `2B:B4:E1:56:69:12:...` | Jan 19 07:43 - Jan 20 13:31 |
| 5 | `ABDBEB69A2C38094812E6E660FF42997782542` | `BC:F5:4B:EE:59:DD:...` | Jan 19 07:45 - Jan 20 07:25 |

**Observations:**
- Connections 1 and 3 received the **same certificate** (identical serial & fingerprint)
- 4 unique certificates observed across 5 connections
- Certificates have ~24h validity windows
- Different certificates have overlapping validity periods

## Conclusion

The proxy maintains a pool of pre-generated certificates for each hostname, serving
them from cache. This is more efficient than generating a new certificate and key
pair for every TLS handshake.

## Raw Certificate Data

### Certificate 1 (also served for Connection 3)
```
serial=1393CE5D93B549C0B25F0E93185D42BCDEA12E
notBefore=Jan 19 07:46:23 2026 GMT
notAfter=Jan 20 09:28:15 2026 GMT
SHA1 Fingerprint=5F:C4:C8:6B:F8:2E:9F:02:00:FE:B3:C6:4C:E5:CB:F0:67:F7:E6:37
```

### Certificate 2
```
serial=ECA9D503A20ABD1E71EDA3EF228899AC001C84
notBefore=Jan 19 02:58:15 2026 GMT
notAfter=Jan 20 05:35:21 2026 GMT
SHA1 Fingerprint=26:33:B1:8E:6B:78:A7:03:FD:76:BF:50:24:4D:30:A9:8F:93:39:EE
```

### Certificate 4
```
serial=29896E9F338E1281F73BE88137D9A361910FB8
notBefore=Jan 19 07:43:03 2026 GMT
notAfter=Jan 20 13:31:52 2026 GMT
SHA1 Fingerprint=2B:B4:E1:56:69:12:AB:CA:1E:D6:24:3D:AE:6D:41:14:10:70:8D:C8
```

### Certificate 5
```
serial=ABDBEB69A2C38094812E6E660FF42997782542
notBefore=Jan 19 07:45:12 2026 GMT
notAfter=Jan 20 07:25:48 2026 GMT
SHA1 Fingerprint=BC:F5:4B:EE:59:DD:CD:78:E8:41:56:5D:95:B8:AA:B1:5D:EB:9F:ED
```

## Test Script

```python
import ssl, socket, base64, os

def get_cert_through_proxy(hostname):
    proxy_url = os.environ.get("HTTPS_PROXY")
    # Parse proxy URL and connect...
    # (see certs/README.md for full script)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    ssl_sock = context.wrap_socket(sock, server_hostname=hostname)
    return ssl_sock.getpeercert(binary_form=True)

# 5 connections, checking for certificate reuse
for i in range(5):
    cert = get_cert_through_proxy("example.com")
    # Extract serial, fingerprint, validity...
```
