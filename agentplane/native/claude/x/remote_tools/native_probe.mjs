// Synthetic startup inputs; native transport, registry, provider, and tools are unmodified.
const root = NATIVE_ROOT;
const emit = (name, value) => console.log(JSON.stringify({ name, value }));
const { gct, Ae } = await import(`${root}/chunk-0ycjphb5.js`);
await gct();
const { V0t } = await import(`${root}/chunk-6b4khzmf.js`);
const { CLr } = await import(`${root}/chunk-ztdmpyqt.js`);
const { fbt } = await import(`${root}/chunk-7zrhxtr7.js`);
const { lae } = await import(`${root}/chunk-nc91yayz.js`);
const { Fn } = await import(`${root}/chunk-0ycjphb5.js`);
emit("default-gates", {
  enabled: await CLr({ cloudClient: true }),
  egress: fbt(),
  account: await lae(),
  wood: Fn("tengu_violin_wood", false),
  willow: Fn("tengu_violin_willow", false),
});
// Simulated entitlement and stored account, confined to the temporary native config.
await Ae((config) => ({
  ...config,
  cachedGrowthBookFeatures: { ...config.cachedGrowthBookFeatures, tengu_violin_wood: true, tengu_violin_willow: true },
  oauthAccount: {
    accountUuid: "00000000-0000-4000-8000-000000000003",
    organizationUuid: "00000000-0000-4000-8000-000000000002",
  },
}));
delete process.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC;
emit("synthetic-cache-gates", {
  enabled: await CLr({ cloudClient: true }),
  egress: fbt() ?? null,
  account: await lae(),
  wood: Fn("tengu_violin_wood", false),
  willow: Fn("tengu_violin_willow", false),
});
emit("runtime", Bun.version);
const sessionId = "00000000-0000-4000-8000-000000000001";
let socket;
let connected;
const connection = new Promise((resolve) => {
  connected = resolve;
});
const pending = new Map();
const server = Bun.serve({
  hostname: "127.0.0.1",
  port: 0,
  fetch(request, server) {
    emit("handshake", new URL(request.url).pathname);
    if (server.upgrade(request)) return;
    return new Response("upgrade required", { status: 400 });
  },
  websocket: {
    message(ws, data) {
      const message = JSON.parse(String(data));
      emit("bridge-received", message);
      if (message.type === "connect") {
        socket = ws;
        ws.send(JSON.stringify({ type: "connected", rtb_protocol_version: 1, hb_supported: true }));
        connected(message);
      } else if (message.type === "hb") ws.send(JSON.stringify({ type: "hb_ack" }));
      else if (message.id !== undefined) {
        const resolve = pending.get(message.id);
        pending.delete(message.id);
        resolve?.(message);
      }
    },
  },
});
const bridge = V0t({
  transport: "bridge",
  sessionId,
  orgUuid: "00000000-0000-4000-8000-000000000002",
  readiness: { whenReady: async () => {}, ready: true },
  getAccessToken: async () => "synthetic-loopback-token",
  getDeviceId: () => "synthetic-device",
  getHostname: () => "synthetic-host",
  getBridgeBaseUrl: () => `ws://127.0.0.1:${server.port}`,
  onEgressGateChange: () => () => {},
  onEnabledChange: () => () => {},
  registerExitCleanup: () => () => {},
  onNotice: (...values) => emit("notice", values),
});
const started = await bridge.started;
emit("started", started);
if (!started) {
  await bridge.stop();
  server.stop(true);
  throw new Error("native startup refused");
}
const announcement = await connection;
const serving = await bridge.serving;
emit(
  "roster",
  announcement.tools.map((tool) => tool.name)
);
let nextId = 0;
async function rpc(method, params) {
  const id = ++nextId;
  const response = new Promise((resolve) => pending.set(id, resolve));
  socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
  return await response;
}
emit(
  "raw-read",
  await rpc("tools/call", { name: "Read", arguments: { session_id: sessionId, file_path: process.env.PROBE_TARGET } })
);
function remoteCallEnvelope(callId, conversationId) {
  return {
    v: 1,
    call_id: callId,
    caller: {
      kind: "ccr_container",
      permission_mode: "default",
      instance_id: "synthetic-caller",
      conversation_id: conversationId,
    },
    expires_in_ms: 10000,
  };
}
async function tool(name, args, callId) {
  const envelope = remoteCallEnvelope(callId, sessionId);
  let result = await rpc("tools/call", {
    name,
    arguments: { session_id: sessionId, ...args, __remote_tool_call: envelope },
  });
  emit(callId, result);
  const status = result.result?.structuredContent?.["anthropic/remoteToolExecution"];
  if (status?.outcome === "needs_approval") {
    emit("file-before-approval", { callId, content: await Bun.file(process.env.PROBE_TARGET).text() });
    result = await rpc("tools/call", {
      name,
      arguments: {
        session_id: sessionId,
        ...args,
        __remote_tool_call: { ...envelope, approval: { ask_id: status.ask_id, decision: "allow" } },
      },
    });
    emit(`${callId}-approved`, result);
  }
  emit("server-read-state", {
    callId,
    observation: serving.kit.deps.sessions.peek(sessionId)?.readFileState.get(process.env.PROBE_TARGET) ?? null,
  });
  return result;
}
await tool("Edit", { file_path: process.env.PROBE_TARGET, old_string: "before", new_string: "unread" }, "edit-unread");
await tool("Read", { file_path: process.env.PROBE_TARGET }, "read-native");
await tool(
  "Edit",
  { file_path: process.env.PROBE_TARGET, old_string: "unread", new_string: "after" },
  "edit-after-read"
);
await tool("Read", { file_path: process.env.PROBE_TARGET }, "read-native");
const mismatchedSessionId = "00000000-0000-4000-8000-000000000099";
const mismatchArguments = {
  session_id: mismatchedSessionId,
  file_path: process.env.PROBE_TARGET,
  __remote_tool_call: remoteCallEnvelope("read-other-session", mismatchedSessionId),
};
emit("mismatch-read-request", mismatchArguments);
emit(
  "mismatch-read",
  await rpc("tools/call", {
    name: "Read",
    arguments: mismatchArguments,
  })
);
await bridge.stop();
server.stop(true);
process.exit(0);
