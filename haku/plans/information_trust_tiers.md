# Information trust tiers

**Status: design needs a new implementation baseline.** The Console-hosted session runtime,
Matrix channel, and Console Recall integration were retired; the former Haku recall index was
removed. Choose the agent and conversation runtime and establish its enforcement boundaries before
implementing this plan. The separate identity and communication work is tracked in
<../../agentplane/plans/multiagent_communication.md>.

## Goal and boundary

The operator wants Haku to delegate less-sensitive work to agents using other model providers,
with coordination visible to the operator (2026-08-15). Everything in an agent's context reaches
its provider. Label the corpus and authorize the provider to see that tier; an agent's name or
workspace is not itself an information boundary.

A lower-tier agent must receive only permitted workspaces, credentials, tool results, conversation
history and provider routes. Workspace separation controls the starting corpus, not information
later supplied by tools or messages. Check every read and write path, including search, derived
memory and inter-agent communication, against <../docs/security.md>. Fail closed for unlabelled
legacy conversation data. A structured task envelope can reduce accidental disclosure but cannot
make an untrusted sender safe.

The original v0 deliberately relied on an instruction to keep sensitive message content out of
lower-tier rooms, while using structural controls for workspaces, credentials, providers and room
membership. This is an explicit exception to Haku's perimeter-enforcement doctrine, not protection
against a prompt-injected sender. Reassess that risk for the selected runtime before exposing a
cross-tier channel. A probabilistic classifier can be a backstop; it does not replace structural
isolation or make an adversarial author's output trustworthy.

## Work to design

1. **Choose the communication and enforcement owners.** For every source and destination, identify
   who creates the identity, grants access, admits membership, mediates sends, and enforces the
   conversation's and provider's tiers. The old Console-owned Matrix send and membership paths no
   longer exist. The agent must not be able to bypass whichever send boundary is chosen.
2. **Scope history and memory.** Give conversations and retained derived data a tier; enforce it at
   every read and retrieval path, including search. Decide how a lower-tier agent's durable state
   inherits the tier of information it receives, and who can authorize cross-tier access.
3. **Design coordination.** The operator's preferred shape was shared Matrix rooms with the
   operator present (2026-08-15). A room has an immutable maximum sensitivity and admits only
   providers cleared for it, including access to prior history. Distinct agents need distinct
   identities where membership is used for enforcement. Decide who declares room tiers, how
   incorrect membership is reconciled, and how to prevent a direct invite or alternate send path
   from bypassing the boundary.
4. **Separate membership, attachment and attention.** Membership defines what an agent can read;
   operator-controlled tier enforcement owns it. An attached room is the transcript home;
   subscriptions are additional rooms, with explicit addressed sends. Agent-controlled attention
   can mute permitted rooms but must not grant membership. Make muting visible and decide its
   expiry, and give a replacement session its current subscriptions and message provenance.
5. **Bound loops and cost.** Ignore an agent's own sends; cap turns and budget per room. The 2026-08-15
   proposal wakes on every subscribed message, with mention gating as an optional noise reduction,
   not the loop-safety boundary. Decide how to debounce and serialize work in the chosen runtime.

## Optional classifier backstop

If a classifier is introduced, put it before external delivery on an operator-controlled,
non-bypassable path. The proposed ordered per-room outbox lets the agent continue without waiting
while retaining messages until a verdict. On a failed check, halt the originating agent, hold that
message and later queued messages for investigation, and avoid exposing a detailed rejection that
invites iterative probing (operator, 2026-08-15). Decide the operator and other participants'
visible indication; the earlier sketches disagreed on whether to publish a withheld-message notice.
A false negative can expose data before the operator sees it; evaluate recall and adversarial
failure explicitly.

Run the classifier only with a provider authorized to receive the content being classified. Local
execution is an option, not an absolute requirement: Anthropic is acceptable for content already
within Anthropic's authorized tier (operator, 2026-08-15). A tier that Anthropic must not see needs
a different authorized placement. The previous Console outbox was retired; select and validate a
new durable queue and send owner before relying on this design.

Git-content inspection is separate and lower priority. A destination-side pre-receive hook can
inspect unpacked changes before accepting a push; required PR CI only runs after the branch receives
the data. Per-repository push authorization and runtime-steerable egress are separate questions in
<../TODO.md>; do not assume the retired Haku proxy or Console approval queue is their owner.

## First candidate workloads

Prefer delegating a bounded capability without sending the higher-trust corpus:

- **Kubernetes diagnostics:** scope to the secret-free `cluster-diagnostics-reader`; do not add the
  separate logs/configmaps or secrets readers. Verify the current grants in
  <../../cluster/docs/agent_rbac.md> before provisioning.
- **Public Ducktape work:** give the worker only authorized repository access; operator review is
  the authority boundary for changes, not proof that arbitrary code is safe to execute.
- **Kitchen stocking:** <kitchen_stocking_subagent.md> needs an independently reviewed bounded
  write surface, data policy and runtime before a lower-trust model can receive household data.

For each candidate, define its input data, provider, capabilities, egress, output destination and
operator review path, then test both intended operations and denied cross-tier access. None requires
an information classifier if no higher-tier corpus is delegated.
