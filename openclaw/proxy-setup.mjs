// Preloaded via NODE_OPTIONS=--import=file:///app/proxy-setup.mjs before openclaw starts.
//
// Replaces globalThis.fetch so LLM API calls (via pi-ai, which uses globalThis.fetch) route
// through mitmproxy even after Telegram's setGlobalDispatcher() call on Node.js 22.
//
// On Node.js 22, src/telegram/fetch.ts calls setGlobalDispatcher(new Agent({autoSelectFamily:true}))
// at startup to work around IPv6 issues. This overrides any EnvHttpProxyAgent set as the global
// dispatcher. Replacing globalThis.fetch with a closure that captures the proxy agent sidesteps
// this: grammY and pi-ai both use globalThis.fetch, which we control; neither replaces the
// function itself.
import { EnvHttpProxyAgent, setGlobalDispatcher } from "undici";

const hasProxy = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"].some((k) =>
  process.env[k]?.trim()
);

if (hasProxy) {
  const proxyAgent = new EnvHttpProxyAgent();
  // Fallback: covers any undici.fetch() calls that bypass globalThis.fetch (e.g. direct
  // import { fetch } from "undici" usage), before Telegram overrides the global dispatcher.
  setGlobalDispatcher(proxyAgent);
  // Primary: capture proxyAgent in closure so proxy survives Telegram's later
  // setGlobalDispatcher(new Agent({autoSelectFamily:true})) call.
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (input, init) => originalFetch(input, { ...init, dispatcher: proxyAgent });
}
