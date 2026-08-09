# Claude sandbox as a Haku runtime

Status: **the conversational sandbox is deployed; making it wake as Haku is next.** Haku Console
already provisions one isolated Claude Code sandbox per conversation, owns the Agent SDK client,
streams multi-turn chat, supports interruption, and exposes the standard Haku Console MCP server
under Haku's existing policy and approval boundary.

The remaining objective is small: give each conversation a current clone of `haku-state`, load Haku's
versioned operating context from that clone, and make useful Git work durable before the sandbox is
disposed.

The feasibility and transport history is in
[agent_sdk_sandbox_runtime.md](agent_sdk_sandbox_runtime.md).

## Runtime contract

- One Console conversation owns one SandboxClaim, one `ClaudeSDKClient`, and one Claude CLI process.
  The sandbox is reused across turns and expires when the conversation is closed or idle.
- Trusted Console code owns Agent SDK orchestration, hooks, policy integration, and the pointer back
  to the standard Haku Console MCP server. The sandbox contains only the checkout, Claude CLI, and
  thin stream-JSON bridge.
- `/workspace/haku-state` is the conversation's working directory and Haku's durable memory/source
  surface. Git, not the sandbox filesystem, preserves changes across disposal.
- The runtime may use Haku's existing Forgejo credential and therefore receives exactly the
  repository authority assigned to that account. It does not receive provider credentials,
  Kubernetes authority, approval authority, or downstream MCP credentials.
- Concurrent conversations use distinct branches. They must not race uncoordinated writes to one
  shared branch.

## Ownership boundary

**Ducktape owns the trusted substrate:** sandbox construction and lifecycle, credentials and egress,
MCP identity and approval policy, the bootstrap rule that loads repository context, hook execution,
and Git authentication/protection. These controls cannot be weakened by editing `haku-state`.

**`haku-state` owns Haku's evolvable self:** memory, general context, operating procedure, Haku UI
source, and repository-local workflow guidance. Haku may propose changes to those files through Git.
The entrypoint is read from the checkout's starting revision, so edits affect a future session rather
than rewriting the current conversation's instructions in place.

Whether a path may be pushed directly or requires review is a Git policy question. A conservative
starting point is direct updates for routine memory/content and PRs for operating instructions,
hooks, or executable/UI code. Deployment and security changes remain reviewed in Ducktape.

## Already deployed — do not rebuild

- Agent SDK stream-JSON transport, dedicated runner image and SandboxTemplate;
- Claude subscription OAuth substitution in the Claude-specific iron-proxy;
- convergent claim cleanup and granular provisioning state;
- standard Console MCP access through the shared static Haku Agent;
- persisted assistant/tool-use boundaries, sanitized Markdown, SSE transcript updates,
  `LISTEN`/`NOTIFY` prompt dispatch, and stop-generation support;
- Console-owned policy, approval records, Web Push decisions, and audit boundary.

## Remaining work

### 1. Bootstrap a durable working checkout

- Reflect Haku's existing Forgejo Git credential into `haku-claude-sandbox` without exposing it in
  claims, logs, transcripts, APIs, or tool projections.
- Permit direct in-cluster egress to Forgejo and verify authentication as Haku.
- Clone/fetch `haku/haku-state.git` into `/workspace/haku-state` before Claude starts.
- Record the starting revision, upstream relationship, and deterministic conversation branch in
  trusted session details.
- Start Claude with `/workspace/haku-state` as its pinned working directory.
- Prove that two simultaneous conversations cannot overwrite each other and that a pushed commit
  survives sandbox disposal.

### 2. Wake the checkout as Haku

- Add a reviewed, versioned entrypoint in `haku-state` describing Haku's context, repository layout,
  memory conventions, and Git workflow.
- Keep a small deployment-owned bootstrap that identifies the runtime as Haku and requires loading
  that entrypoint before accepting the first ordinary prompt.
- At startup, report the checked-out revision and whether it is current, ahead, behind, or diverged
  from upstream.
- Add lifecycle guidance around Git: before push, refresh upstream state; on stop, if the checkout is
  dirty or commits remain unpushed, remind Haku to preserve or intentionally discard the work.
  Hooks must avoid recursive stop loops.
- Add one end-to-end acceptance: orient from `haku-state`, use one normal Console MCP tool, make a
  small repository change, and leave the result recoverable through Git.

The hook engine lives in Console. Any hook that inspects the checkout should use a narrow structured
sandbox operation (for example, Git status/upstream metadata) rather than moving policy or general
credentials into the runner.

### 3. Harden long-lived conversations

These are follow-ups, not prerequisites for the first useful Haku wake:

- complete ordered tool-call status/result/error projection and per-conversation origin metadata;
- reconnect after Console rollout without creating two live bridge consumers;
- resume the Claude transcript when available, otherwise start a new SDK session and re-orient from
  `haku-state`;
- choose transcript retention, idle expiry, orphan reconciliation, and lifecycle telemetry;
- add named conversations, forks/handoff, scheduled wakes, or alternate chat surfaces only after
  the single-conversation path is reliable.

## Fixed exclusions

- no GitHub or general Ducktape development credential in the sandbox;
- no direct Kubernetes authority or provider MCP access from the runner;
- no provider/operator credentials in SandboxClaims, Pods, transcripts, or tool results;
- no agent-authored approval button or approval-decision tool;
- no reliance on Claude's in-model permission system as a security boundary;
- no warm-sandbox complexity until post-claim authority assignment is designed.
