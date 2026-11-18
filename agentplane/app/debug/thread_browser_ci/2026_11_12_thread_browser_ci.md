# Thread browser OIDC setup timeout

In shard 14 on [PR #15534](https://github.com/agentydragon/ducktape/pull/15534) and
[PR #15718](https://github.com/agentydragon/ducktape/pull/15718), `AppProcess.authenticate()`
raised `httpx.ReadTimeout` while awaiting response headers during the `GET /auth/login` redirect
chain. The fixture had yet to start the browser page. The old artifacts leave the stalled hop
and its cause unknown. [Issue #16032](https://github.com/agentydragon/ducktape/issues/16032).

Run `bbr test //agentplane/app:test_thread_browser --nocache_test_results` on RBE or use the
next failing CI shard. In that attempt's undeclared outputs, find the last unmatched request in
`auth-login-<trace_id>.jsonl` and join it to the `auth login phase` records in `go-app-*.log`.
Match `oidc-provider-*.jsonl` by trace ID on `/authorize`, and by provider host, port and
timestamps on Go's outbound discovery, token and key requests, which carry no trace ID. Compare
app admission and session load, provider discovery and exchange, session commit, and the
client's receipt of headers. [Landed instrumentation](https://github.com/agentydragon/ducktape/commit/55c4616002c293ddf733ee39b3b34f3d318d9c51).

The [controlled TLS discovery stall](2026_11_12_thread_browser_auth_setup_timeout.md)
shows how to reproduce the timeout signature and read the traces. The natural failure's cause
remains open.
