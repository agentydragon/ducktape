# Claude native remote tools, 2026-10-27

## Result and evidence boundary

Claude Code 2.1.296's native device bridge can serve its own Read/Edit implementation to a loopback
peer when supplied **simulated entitlement and account state**. The maintained experiment now exercises
that feature's registration, WebSocket protocol, native registry, request envelope, approval and tool
pipeline. It does not establish availability for a real account or a complete self-hosted cloud caller.

The [fixture](../native/claude/x/remote_tools/README.md) pins the npm Linux x86-64 executable independently
of production. Its SHA-256 is `24972e3bc859fab2b46ed4c1e51f7d6130f06d3bd550811a114640de3370d0de`.
The previously inspected Nix executable is also version 2.1.296 but has a different executable hash;
these captures and source modules come from the pinned npm artifact itself.

There are two distinct evidence paths:

- **Unmodified CLI:** one local Read comparator captures the next scripted model request. Root
  `CLAUDE.md` appears before access; reading `nested/editable.txt` adds nested `CLAUDE.md` in the next
  request, with no sibling instruction. A separate supported `--cloud` startup probe records its
  unauthenticated refusal. There is no live inference.
- **Instrumented source entry:** the existing Bun standalone extractor preserves the npm artifact's
  embedded modules. The experiment relocates virtual imports and runs their source under pinned
  Bun 1.3.14. Three insertions expose existing debug/error logging and the existing startup catch;
  they do not change outcomes. The native transport, provider, MCP handler, permission checks,
  serving kit and tool implementations are retained. `source-provenance.json` records the binary
  hash and each logging insertion. This is source-path evidence, not an unchanged-binary run.

All identities and tokens in the source probe are synthetic. HOME/config and file contents are
fixtures. External HTTP(S) proxies point at a rejecting loopback port. No real account, provider,
bridge, deployed service or feature eligibility is changed.

## Actual enablement path

The ordinary stream-json command used by the earlier comparison did not exercise registration.
Setting `CLAUDE_CODE_REMOTE_TOOLS_FORWARD=1` is insufficient to turn that command into a native
remote-tools server. The cloud-attach implementation in `chunk-ha9v64hy.js` imports
`startDeviceRegistration` from `chunk-qa76rgxj.js`, which re-exports `V0t` from
`chunk-6b4khzmf.js`. It calls registration only when its bound device ID exists, supplying the cloud
session ID, organization UUID and OAuth-token getter. `VIr`/`chunk-7pgqkwz7.js` is another bound-session
attach path. Public `--help` exposes `--cloud [description|session_id|url]`; this is separate from
ordinary local print/stream-json and from the Remote Control/RemoteIO experiment.

These are version-specific predicates, not a promise of a supported public server API:

| Decision                    | Native callsite and input                                             | Observation / way to satisfy it                                                                                                                                                                                                                                                |
| --------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Local forwarding            | `R2`/`fW` in `chunk-qwvy7ma3.js`; `hh` → `zre` in `chunk-4r70ar86.js` | Parsed `CLAUDE_CODE_REMOTE_TOOLS_FORWARD=true` plus wood flag. It controls forwarding, not registration by itself.                                                                                                                                                             |
| Registration eligibility    | `V0t` → `CLr` in `chunk-ztdmpyqt.js` → `dR` in `chunk-md7g8y4m.js`    | `tengu_violin_wood`; cloud-client registration additionally requires `tengu_violin_willow`.                                                                                                                                                                                    |
| Feature input               | `Fe`/`checkGateCachedOrBlocking` in `chunk-0ycjphb5.js`               | When feature fetching is enabled, a cached boolean `true` is accepted before fetching. Public `getEnvironmentOverrides` and `readConfigOverrides` are stubs; an internal override env var does not enable this build.                                                          |
| Traffic/provider/org policy | `V0t` → `age`/`fbt` in `chunk-7zrhxtr7.js`                            | Refuses essential-traffic mode, non-`firstParty` provider classification, or denied `allow_remote_sessions`. The former fixture explicitly set `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, disabling flags and egress. Remove that setting to evaluate the feature normally. |
| Bridge trust                | `V0t` → `pmo` in `chunk-5s4qvyjz.js`                                  | When `tengu_sessions_elevated_auth_enforcement` is active, enforced/refused-cache/unknown `require_trusted_devices` policy can refuse a bridge that cannot verify proof. The synthetic feature cache does not enable that flag.                                                |
| Account and organization    | `V0t` → `lae` in `chunk-nc91yayz.js`; explicit `orgUuid`              | Stored `oauthAccount.accountUuid` or an accepted env/fd-auth account UUID must resolve; an org UUID must be supplied. Missing/mismatched account and missing org return false. A real cloud attachment also needs its real OAuth/session/device setup.                         |
| Serving scope               | `p4`/`qO` in `chunk-qwvy7ma3.js`                                      | Serve switch defaults on; `CLAUDE_CODE_REMOTE_TOOLS_SERVE=0` disables it. Home-directory launch and unreadable mount/profile discovery can withdraw native tools. This Linux fixture launches outside its isolated HOME.                                                       |
| Readiness                   | `createServingReadiness` in `chunk-ywx1r1yk.js`                       | Normal startup waits for hooks/plugins/settings/policies and bounded first flag fetch. The source entry calls native config initialization `gct()` then supplies an already-ready fixture. It does not test full readiness or hooks.                                           |
| Transport                   | `AL` and `V0t`                                                        | `CLAUDE_CODE_REMOTE_TOOLS_SESSION_CHANNEL=false` selects bridge; the source entry selects `transport:"bridge"` explicitly. `CLAUDE_REMOTE_TOOLS_BRIDGE_URL` supplies an origin, not eligibility.                                                                               |

The observed default source trace is `enabled=false`, `egress="egress"`, account `missing`, and both
feature values `false` with source `disabled`. This identifies actual predicates rather than inferring
a blocker from zero bridge requests. The source probe then clears the traffic-disable variable and
uses native config saving to put boolean wood/willow flags and a synthetic `oauthAccount` into its
temporary `.claude.json`. The second trace is `enabled=true`, `egress=null`, account `resolved` with
source `stored`, and both flags `true` with source `disk`. Registration then uses the **default**
eligibility, account, egress and bridge-trust functions; none is replaced with an always-allow callback.

That demonstrates how the cache input reaches these gates, not how to obtain real entitlement.
No real account is available in this experiment. Supported availability still needs an eligible
cloud/OAuth account, bound session/device and the policies above. Synthetic cache values are a
controlled simulation and must not be treated as a production enablement procedure.

The unchanged `--cloud session_synthetic_remote_tools_probe` command, supplied a prompt on stdin,
exits 1 without an account: `Cloud sessions need a claude.ai sign-in. Run claude auth login`.
That supported-entry refusal is separate from the source probe's simulated stored account and token getter.

## Bridge and call protocol

The native dial path is
`/devices/<org_uuid>_<account_uuid>/<hostname>-cc-<session_suffix>/bridge`.
The source fixture supplies a loopback origin, synthetic hostname and token getter, and captures the
actual native WebSocket connection. The first frame has `type:"connect"`, `oauth_token`, native tool
schemas, `rtb_protocol_version:2`, `hb_capable:true`, `cancel_capable:true`, `device_id` and `session_id`.
The peer replies with `type:"connected"`, its protocol version and heartbeat support. Native transport
then injects its own MCP initialize message; that initialize response is consumed locally. Heartbeat
frames use `hb`/`hb_ack`. Subsequent traffic is JSON-RPC over that socket.

The actual default roster is **get_device_info, Bash, Read, Edit, Write**. The fixture implements the
bridge peer only; it has no file-operation handler and supplies no custom tool provider.

A bare `tools/call` Read is refused with `invalid_request`. Native `createDeviceMcpServer` strips the
bridge-asserted `session_id`/`trigger_id`, checks the attached session, and dispatches to the registry.
The native serving handler requires an additional `__remote_tool_call` envelope containing `v:1`,
`call_id`, `caller` (including kind and permission mode), and positive `expires_in_ms`.
`structuredContent["anthropic/remoteToolExecution"]` carries the result outcome. An approval request
returns `needs_approval` and `ask_id`; a second leg repeats the call with `approval:{ask_id,decision}`.
The fixture answers those explicit requests with synthetic approval decisions rather than replacing
native permission evaluation.

## Native tool state versus caller instructions

The source captures establish real native dispatch. An unread Edit first asks for approval and then
succeeds after the fixture's allow answer. The subsequent Read returns numbered file contents.
This differs from the ordinary local CLI's prior-read validation; do not assume that native remote
serving reproduces every local controller precondition. The suite also checks a later Edit, replayed
call identity and a mismatched attached-session refusal, and captures native server-side read state.

The mismatched request includes a complete forwarding envelope with a fresh `call_id`; its caller
`conversation_id` agrees with its requested session, which differs from the device's attached session.
`createDeviceMcpServer` in `chunk-f1j5pmqs.js` rejects it before registry dispatch by comparing the normalized
outer `session_id` against the attached session. This layer returns MCP `isError:true` with a specific
“different Claude Code cloud session” text detail, without a structured error code. The assertion checks that detail and the
native diagnostic naming both asserted and attached IDs, distinguishing this refusal from the raw
Read's `invalid_request` envelope denial. Its analytics reason in the source is `session_mismatch`.

The approval requests leave the fixture file unchanged until answered. After the unread Edit succeeds,
server read state contains `unread\n` with `contentNotInModelContext:true`. Read returns numbered
`unread` contents and changes that state to `offset:1`, without `contentNotInModelContext`. The later
approved Edit records `after\n`. Repeating the original Read call ID returns the original `unread`
contents with `replayed:true`; the file and server read state remain `after\n`.

The served Read response contains neither fixture root nor nested `CLAUDE.md` marker. Its native
context constructor `ro` in `chunk-n9tjv0a2.js` carries the serving session's `readFileState`/`toolState`,
but uses `messages:[]` and a no-op `enqueuePendingMemoryUpdate`. Its result collector admits
read-truncation and hook attachments, and skips other attachments. These facts describe server-side
execution and the returned payload.

**The full cloud caller's next model request remains unobserved.** We have not run its repository
synchronization, caller instruction discovery, cloud conversation, resume or compaction. Absence of
instructions in the served response does not prove that those other caller paths omit them. Conversely,
successful Read/Edit and server read state do not establish caller instruction parity. The maintained
local comparator shows the local next-model contract separately.

## Reproduction and remaining scope

Run the two Bazel targets in the fixture README. Diagnostics and complete wire captures are Bazel
undeclared outputs, including `native-source-stdout.jsonl`, `native-source-stderr.log`,
`source-provenance.json`, `unmodified-cloud-startup.json` and local before/after model requests.

The [acceptance run](https://app.buildbuddy.io/invocation/d31d9bb0-43c3-44cd-b80b-29d015355f18)
passed both test targets (three pytest cases): native serving and unauthenticated cloud startup ran
in 21.8 seconds; the unchanged local comparator reused its 17.9-second passing result. The same
invocation named the changed `:probe` library, so its build and lint aspects also passed. The native
serving case asserts gates, registration, approval-before-write, Read/Edit results, server state,
cached replay, attached-session refusal and the limited instruction-payload observation above.

The [focused session-refusal follow-up](https://app.buildbuddy.io/invocation/b027178a-c086-4b8c-8f17-4407106d677e)
passed both `test_native_serving` pytest cases in a 19.0-second Bazel action. Its
`native-source-stdout.jsonl` records the valid mismatched request and refusal; `native-source-stderr.log`
records the native asserted/attached-session diagnostic. An earlier follow-up failed only because
the diagnostic assertion omitted the native logger's quotes around the asserted session ID.

Earlier iterations are retained in BuildBuddy: [first native handshake](https://app.buildbuddy.io/invocation/40ae2510-72f7-44b6-89ee-5677e986671e),
[native dispatch after config initialization](https://app.buildbuddy.io/invocation/9eaf1db3-7592-4850-abdf-752414c9282a),
and [synthetic cache/account gates](https://app.buildbuddy.io/invocation/2b790cb4-9f52-4c62-a914-7e2d68ca78ee).
Those diagnostic invocations include incomplete assertions or a failing cloud-startup fixture; they
are evidence for their captures, not the final acceptance run. Initial module-entry calls lacked
`gct()` and returned native `Config accessed before allowed`; initializing the native config removed
that fixture error without changing the tool implementation.

The previous generic MCP, local resume and hook comparison was reduced to one local instruction
comparator because it did not answer native remote availability. Its historical six-case run is
[71a785c2](https://app.buildbuddy.io/invocation/71a785c2-51e4-428e-ba91-677e6ac92087).

An actual eligible-account launch, production bridge acceptance and full caller instruction/resume
contract remain outside the evidence. This spike does not select a production transport or authorize
a runtime cutover. It makes native protocol/semantics reviewable while keeping those gaps explicit.
