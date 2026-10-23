# Test TCP and HTTP servers

Use `util.net.listen_tcp()` when a test needs ownership of a listening TCP port, and
`refusing_tcp_endpoint()` when it needs a reliably closed endpoint. Both keep the
bound socket for the whole context and set no address or port reuse options.

For ASGI tests, `serve_app()` starts the app in a dedicated thread and yields its
ready origin. Use `serve_app_in_loop()` when the app shares loop-bound test
resources, or `serve_app_sync()` from synchronous code. Each helper owns its
listener and waits for lifespan startup before yielding.

Use `bind_http_server()` when app construction needs one or more origins first.
The returned `BoundHTTPServer` exposes its cached `origin` and `port`, and can
serve exactly once through `serve()`, `serve_in_loop()`, or `serve_sync()`. A
wildcard bind such as `0.0.0.0` requires `advertised_host` so the origin is
reachable by clients. `serve_fastmcp()` yields the streamable HTTP endpoint with
its `/mcp` path.

```python
async with serve_app_in_loop(app) as origin:
    await client.get(f"{origin}/healthz")
```

For mutually configured apps, bind both before constructing either one:

```python
with bind_http_server() as idp, bind_http_server() as api:
    idp_app = build_idp(issuer=idp.origin, redirect_uri=f"{api.origin}/callback")
    api_app = build_api(issuer=idp.origin, public_base_url=api.origin)
    async with idp.serve(idp_app), api.serve_in_loop(api_app):
        await exercise_login(api.origin)
```
