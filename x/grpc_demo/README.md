# Bazel gRPC demo

This small app demonstrates one `.proto` definition shared by a Python gRPC
backend and a browser TypeScript frontend. Bazel generates the Python and
gRPC-Web bindings; generated sources are not checked in. When OIDC settings
are configured, the Python app validates Authentik access tokens itself and
does not trust identity headers from a proxy.

Build the complete stack:

```bash
bbr build //x/grpc_demo:server_bin \
  //x/grpc_demo:grpcwebproxy \
  //x/grpc_demo:bundle \
  //x/grpc_demo:static_server_bin
```

Run these targets in separate terminals:

```bash
GRPC_DEMO_ALLOW_ANONYMOUS=true bb run //x/grpc_demo:server_bin
```

```bash
bb run //x/grpc_demo:grpcwebproxy -- \
  --backend_addr=127.0.0.1:50051 \
  --server_http_debug_port=8080 \
  --run_tls_server=false \
  --allowed_headers=authorization \
  --allowed_origins=http://127.0.0.1:8081,http://localhost:8081
```

```bash
bb run //x/grpc_demo:static_server_bin -- --port 8081
```

Open <http://127.0.0.1:8081>. The anonymous command is only for local
development; production startup fails closed when OIDC configuration is
missing.

## Authentik wiring

Create an Authentik OAuth2/OIDC provider for the app, select an RSA (RS256)
signing key, and register the static server URL as the redirect URI. Configure
the backend with the provider's issuer and expected access-token audience
(normally the provider client ID):

```bash
GRPC_DEMO_OIDC_ISSUER=https://auth.example.com/application/o/grpc-demo/ \
GRPC_DEMO_OIDC_AUDIENCE=grpc-demo \
bb run //x/grpc_demo:server_bin
```

The backend discovers the provider's JWKS endpoint from OIDC discovery. Set
`GRPC_DEMO_OIDC_JWKS_URI` explicitly if the deployment cannot reach the
discovery URL. The browser gets an access token through Authorization Code +
PKCE and sends it as `authorization: Bearer ...` gRPC metadata. Configure the
static server with the public OIDC client settings:

```bash
GRPC_DEMO_OIDC_AUTHORITY=https://auth.example.com/application/o/grpc-demo/ \
GRPC_DEMO_OIDC_CLIENT_ID=grpc-demo \
GRPC_DEMO_GRPC_WEB_ENDPOINT=https://demo.example.com \
bb run //x/grpc_demo:static_server_bin -- --port 8081
```

Place the gRPC-Web proxy and static server behind a TLS-capable ingress. The
ingress/proxy handles routing and gRPC-Web transport only; it must forward the
`Authorization` metadata, while the Python service verifies the JWT signature,
issuer, audience, expiry, and subject. No `X-Authentik-*` identity header is
trusted by the app.

The Python tests cover missing, invalid, and valid bearer credentials as well
as signed-token and audience verification.
