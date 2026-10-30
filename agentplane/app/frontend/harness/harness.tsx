/**
 * Visual-test harness: Python configures canned data and explicitly mounts the app. No scene registry.
 * `fetch` (stubbed by network.ts, imported first so the
 * app's client captures the stub) answers API, Electric Shape and Dispatcher watch routes. `EventSource`
 * remains only for the live sandbox, thread, and action-inventory views.
 */
import {
  electricLive,
  electricShape,
  electricSubset,
  liveNdjsonWatch,
  ndjsonWatch,
  publishNdjsonWatch,
  routes,
  UNANSWERED,
} from "./network";
import type { SandboxView, ThreadView } from "../schema";
import type { ActionGroupView, ActionPolicyView, ActionRequestView } from "../actions/client";
import type { BindingView, Decision, PolicySetView } from "../sandboxes/client";
import type { DecisionsSnapshot, SandboxSnapshot, SandboxesSnapshot } from "../sandboxes/live";
import type { WatchHealth } from "../live";
import type { ThreadsSnapshot } from "../threads/live";
import { TEST_REASONING_EFFORTS } from "../testing/model_catalog";
import "@mantine/core/styles.css";

import { create, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";

import App from "../app";
import {
  actionGroupsSnapshotCompleteProtoJson,
  actionGroupsSnapshotProtoJson,
  actionGroupUpsertProtoJson,
  actionRequestsSnapshotCompleteProtoJson,
  actionRequestsSnapshotProtoJson,
  actionRequestUpsertProtoJson,
  actionStateProtoJson,
  decideActionRequestProtoJson,
  getActionRequestProtoJson,
  listActionGroupsProtoJson,
  listActionHistoryProtoJson,
  listActionRequestsProtoJson,
  mcpLinkagesSnapshotCompleteProtoJson,
  mcpLinkagesSnapshotProtoJson,
  mcpLinkageUpsertProtoJson,
} from "../actions/testing/proto_fixtures";
import { sampleConnection } from "../connections/testing/fixture";
import type { Connection } from "../connections/models";

import { dispatchFromJson } from "../threads/client";
import { ACTION_RENDERERS } from "../actions/rendering/index";
import type { McpLinkageView } from "../mcp_linkage_client";
import type {
  GitHubSubject,
  InboxNoticeState,
  InboxStatus,
  SandboxNotificationStatus,
  SessionNoticeStates,
  SubscriptionStatus,
} from "../notification_types";
import { EventSchema, ItemKind, RecoveryDisposition, TurnStatus } from "../../../proto/event_pb";
import { CommandSchema } from "../../../proto/command_pb";
import { EventEntrySchema } from "../../../proto/event_log_pb";
import { DisclosureVisual } from "./disclosure_visual";
import { OperationPresentationVisual } from "./operation_presentation_visual";
import { LocalCommands } from "../threads/local_commands";
import { streamRegistry } from "../stream_status";
import { ThemeProvider } from "../theme";
import rollout from "./realistic_rollout.json";
import reportedRollout from "./reported_rollout.json";

// Each test owns its local-command fixtures.
localStorage.clear();

// The visual harness freezes the wall clock before this bundle runs, so relative ages stay put.
const NOW = Date.now();
const HOUR = 3_600_000;
const NULL_SANDBOX_FIELDS = {
  binding: null,
  preset: null,
  idle_suspend_after_seconds: null,
  suspended_by: null,
  idle_suspend_at: null,
  autosuspend_blockers: [],
};
const NULL_THREAD_FIELDS = {
  sandbox_uid: null,
  last_model_activity_at: null,
  idle_since: null,
  setup_state: null,
  launch_outcome: null,
  active_turn_id: null,
  reasoning_effort: null,
  feed_status: null,
  last_turn_status: null,
};

function ago(ms: number): string {
  return new Date(NOW - ms).toISOString();
}

const SANDBOXES: SandboxView[] = [
  {
    ...NULL_SANDBOX_FIELDS,
    name: "ready-sandbox",
    uid: "0f9c1d2e-0000-4000-8000-00000000a1b2",
    namespace: "agentplane-visual",
    created_at: ago(3 * HOUR),
    operating_mode: "Running",
    service_account: { namespace: "agentplane-visual", name: "ready-sandbox" },
    wake_on_send: true,
    idle_suspend_after_seconds: 3600,
    status: { conditions: [{ type: "Ready", status: "True", reason: "PodReady", lastTransitionTime: ago(HOUR) }] },
    phase: "Running",
    pod: {
      name: "ready-sandbox",
      namespace: "agentplane-visual",
      uid: "visual-pod-a1b2",
      deleting: false,
      owner_references: [
        {
          api_version: "agentplane.allegedly.works/v1alpha1",
          kind: "Sandbox",
          name: "ready-sandbox",
          uid: "0f9c1d2e-0000-4000-8000-00000000a1b2",
          controller: true,
        },
      ],
      node_name: "harness-node",
      status: {
        phase: "Running",
        podIP: "10.0.0.7",
        startTime: ago(2 * HOUR),
        qosClass: "Burstable",
        conditions: [
          { type: "PodScheduled", status: "True", lastTransitionTime: ago(2 * HOUR) },
          { type: "Ready", status: "True", lastTransitionTime: ago(HOUR) },
        ],
        containerStatuses: [
          { name: "runner", state: { running: { startedAt: ago(HOUR) } }, ready: true, restartCount: 0 },
        ],
      },
    },
  },
  {
    ...NULL_SANDBOX_FIELDS,
    name: "pending-sandbox",
    uid: "0f9c1d2e-0000-4000-8000-00000000c3d4",
    namespace: "agentplane-visual",
    created_at: ago(2 * 60_000),
    operating_mode: "Running",
    service_account: { namespace: "agentplane-visual", name: "pending-sandbox" },
    wake_on_send: false,
    status: { conditions: [{ type: "Ready", status: "False", reason: "PodPending" }] },
    phase: "Running",
    pod: {
      name: "pending-sandbox",
      namespace: "agentplane-visual",
      uid: "visual-pod-c3d4",
      deleting: false,
      owner_references: [
        {
          api_version: "agentplane.allegedly.works/v1alpha1",
          kind: "Sandbox",
          name: "pending-sandbox",
          uid: "0f9c1d2e-0000-4000-8000-00000000c3d4",
          controller: true,
        },
      ],
      node_name: "harness-node",
      status: {
        phase: "Pending",
        conditions: [{ type: "Ready", status: "False", reason: "ContainersNotReady" }],
        containerStatuses: [
          {
            name: "runner",
            state: {
              waiting: {
                reason: "ImagePullBackOff",
                message: 'Back-off pulling image "registry.test/runner:harness"',
              },
            },
            ready: false,
            restartCount: 0,
          },
        ],
      },
    },
  },
  {
    ...NULL_SANDBOX_FIELDS,
    name: "suspended-sandbox",
    uid: "0f9c1d2e-0000-4000-8000-00000000e5f6",
    namespace: "agentplane-visual",
    created_at: ago(48 * HOUR),
    operating_mode: "Suspended",
    service_account: { namespace: "agentplane-visual", name: "suspended-sandbox" },
    wake_on_send: false,
    status: { conditions: [{ type: "Ready", status: "False", reason: "Suspended" }] },
    phase: "Suspended",
    pod: null,
  },
];

/** One Sandbox suspended because its runner was idle, and one because a requester suspended it. */
function addSuspendedSandboxes(): void {
  SANDBOXES.push(
    {
      ...NULL_SANDBOX_FIELDS,
      name: "idle-suspended-sandbox",
      uid: "0f9c1d2e-0000-4000-8000-00000000b7c8",
      namespace: "agentplane-visual",
      created_at: ago(30 * HOUR),
      operating_mode: "Suspended",
      service_account: { namespace: "agentplane-visual", name: "idle-suspended-sandbox" },
      wake_on_send: true,
      idle_suspend_after_seconds: 1800,
      suspended_by: "Idle",
      status: { conditions: [{ type: "Ready", status: "False", reason: "Stopped" }] },
      phase: "Suspended",
      pod: null,
    },
    {
      ...NULL_SANDBOX_FIELDS,
      name: "requested-suspended-sandbox",
      uid: "0f9c1d2e-0000-4000-8000-00000000d9e0",
      namespace: "agentplane-visual",
      created_at: ago(26 * HOUR),
      operating_mode: "Suspended",
      service_account: { namespace: "agentplane-visual", name: "requested-suspended-sandbox" },
      wake_on_send: false,
      idle_suspend_after_seconds: 7200,
      suspended_by: "Requested",
      status: { conditions: [{ type: "Ready", status: "False", reason: "Stopped" }] },
      phase: "Suspended",
      pod: null,
    }
  );
}

function addProvisioningSandbox(): void {
  SANDBOXES.push({
    ...NULL_SANDBOX_FIELDS,
    name: "test-provisioning",
    uid: "0f9c1d2e-0000-4000-8000-000000000007",
    namespace: "agentplane-visual",
    created_at: ago(30_000),
    operating_mode: "Running",
    service_account: { namespace: "agentplane-visual", name: "test-provisioning" },
    wake_on_send: false,
    status: {},
    phase: "Creating",
    pod: null,
  });
}

const TEST_CODER_POLICY_SETS = [
  "test-basic",
  "test-packages",
  "test-github-writes",
  "test-github-clone",
  "test-github-actions-logs",
  "test-build-cache",
  "test-pr-visuals",
  "test-github-reads",
  "test-github-identity-reads",
  "test-ssh-reads",
  "test-shared-diagnostics",
  "test-environment-access",
  "test-inventory",
  "test-pr-failed-jobs",
];

const POLICY_SETS: PolicySetView[] = [
  {
    name: "github-public",
    generation: 1,
    refused: null,
    rules: [
      {
        kind: "egress",
        number: 0,
        hosts: ["api.github.com", "github.com", "*.githubusercontent.com"],
        methods: ["GET", "POST"],
        paths: null,
        credential: {
          name: "harness-github-pat",
          description: "A token for the demo bot account; requests carrying it act as that account.",
          placeholder: "agentplane-credential-harness-github-pat",
          secret: "harness-github-pat",
          key: "token",
          targets: [{ header: "Authorization", method: "schemeToken", scheme: "Bearer" }],
        },
        missing_credential: null,
      },
    ],
  },
  {
    name: "pypi",
    generation: 1,
    refused: null,
    rules: [
      {
        kind: "egress",
        number: 0,
        hosts: ["pypi.org", "files.pythonhosted.org"],
        methods: ["GET"],
        paths: ["/simple/**"],
        credential: null,
        missing_credential: null,
      },
    ],
  },
  {
    name: "test-coder",
    generation: 2,
    refused: null,
    rules: [
      {
        kind: "other",
        number: 0,
        type: "action_auto_approve",
        fields: {
          policy: { type: "exact_actions", actions: { github: ["get_file_contents", "list_commits", "search_code"] } },
        },
      },
      {
        kind: "other",
        number: 1,
        type: "action_auto_approve",
        fields: {
          policy: {
            type: "argument_schema",
            actions: { github: ["create_issue"] },
            argument_schema: {
              properties: { owner: { const: "harness-owner" }, repo: { const: "harness-repo" } },
              required: ["owner", "repo"],
            },
          },
        },
      },
    ],
  },
  {
    name: "harness-reviews",
    generation: 1,
    refused: null,
    rules: [
      {
        kind: "other",
        number: 0,
        type: "action_auto_approve",
        fields: { policy: { type: "exact_actions", actions: { github: ["pull_request_read", "list_pull_requests"] } } },
      },
    ],
  },
  {
    name: "harness-push",
    generation: 3,
    refused: null,
    rules: [
      {
        kind: "other",
        number: 0,
        type: "action_auto_approve",
        fields: {
          policy: {
            type: "argument_schema",
            actions: { github: ["push_files"] },
            argument_schema: { properties: { branch: { pattern: "^harness/" } }, required: ["branch"] },
          },
        },
      },
    ],
  },
  {
    name: "harness-edited",
    generation: 1,
    refused: "rules[0]: hosts: List should have at least 1 item after validation, not 0",
    rules: [],
  },
];

function policySet(name: string): PolicySetView {
  const found = POLICY_SETS.find((candidate) => candidate.name === name);
  if (!found) throw new Error(`No harness PolicySet ${name}`);
  return found;
}

/** A reader's condition on a binding, written at the generation it judged. */
function verdict(
  type: string,
  status: "True" | "False",
  message: string,
  observedGeneration: number
): BindingView["conditions"][number] {
  return {
    type,
    status,
    reason: status === "True" ? "Valid" : "PolicySetUnavailable",
    message,
    observed_generation: observedGeneration,
  };
}

/**
 * ready-sandbox's bindings, one of every state a binding mark shows: the Sandbox's own and two others
 * that every reader accepts (one from git, one an operator grant that has since expired), one an operator
 * grant no reader has judged yet, and one a reader refuses for the sets it names.
 */
const BINDINGS: BindingView[] = [
  {
    name: "ready-sandbox-k2m9x",
    provenance: "sandbox",
    generation: 1,
    subjects: [{ namespace: "agentplane-visual", name: "ready-sandbox" }],
    expires_at: null,
    conditions: [verdict("ActionsReady", "True", "spec accepted", 1)],
    // The preset names test-coder alone; harness-reviews was picked at launch.
    policy_sets: [policySet("test-coder"), policySet("harness-reviews")],
    missing_policy_sets: [],
  },
  {
    name: "ready-sandbox-github-public",
    provenance: "git",
    generation: 1,
    subjects: [{ namespace: "agentplane-visual", name: "ready-sandbox" }],
    expires_at: null,
    conditions: [verdict("EgressReady", "True", "egress rules enforced", 1)],
    policy_sets: [policySet("github-public")],
    missing_policy_sets: [],
  },
  {
    name: "ready-sandbox-7q4xk",
    provenance: "operator",
    generation: 1,
    subjects: [{ namespace: "agentplane-visual", name: "ready-sandbox" }],
    expires_at: ago(2 * HOUR),
    conditions: [verdict("EgressReady", "True", "egress rules enforced", 1)],
    policy_sets: [policySet("pypi")],
    missing_policy_sets: [],
  },
  {
    name: "ready-sandbox-unjudged-grant",
    provenance: "operator",
    generation: 1,
    subjects: [{ namespace: "agentplane-visual", name: "ready-sandbox" }],
    expires_at: new Date(NOW + HOUR).toISOString(),
    conditions: [],
    policy_sets: [policySet("harness-reviews")],
    missing_policy_sets: [],
  },
  {
    name: "ready-sandbox-push-afternoon",
    provenance: "operator",
    generation: 2,
    subjects: [{ namespace: "agentplane-visual", name: "ready-sandbox" }],
    expires_at: new Date(NOW + 3 * HOUR).toISOString(),
    conditions: [verdict("ActionsReady", "False", "missing PolicySets: harness-release", 2)],
    policy_sets: [policySet("harness-push"), policySet("harness-edited")],
    missing_policy_sets: ["harness-release"],
  },
];

/** What the Action Service auto-decides for ready-sandbox, from the sets those bindings name. */
const ACTION_POLICY: ActionPolicyView = {
  synced: true,
  auto_approve_if: [
    {
      binding: "ready-sandbox-k2m9x",
      policy_set: "test-coder",
      source: "rules",
      index: 0,
      policy: { type: "exact_actions", actions: { github: ["get_file_contents", "list_commits", "search_code"] } },
    },
    {
      binding: "ready-sandbox-k2m9x",
      policy_set: "test-coder",
      source: "rules",
      index: 1,
      policy: {
        type: "argument_schema",
        actions: { github: ["create_issue"] },
        argument_schema: {
          properties: { owner: { const: "harness-owner" }, repo: { const: "harness-repo" } },
          required: ["owner", "repo"],
        },
      },
    },
    {
      binding: "ready-sandbox-k2m9x",
      policy_set: "harness-reviews",
      source: "rules",
      index: 0,
      policy: { type: "exact_actions", actions: { github: ["pull_request_read", "list_pull_requests"] } },
    },
    {
      binding: "ready-sandbox-push-afternoon",
      policy_set: "harness-push",
      source: "rules",
      index: 0,
      policy: {
        type: "argument_schema",
        actions: { github: ["push_files"] },
        argument_schema: { properties: { branch: { pattern: "^harness/" } }, required: ["branch"] },
      },
    },
  ],
};

const DECISIONS: Decision[] = [
  {
    at: ago(9 * 60_000),
    method: "CONNECT",
    host: "api.github.com",
    port: 443,
    outcome: "allow",
    address: "140.82.116.5",
    reason: null,
    binding: "ready-sandbox-github-public",
    policy: "github-public",
    rule: 0,
    substituted: false,
  },
  {
    at: ago(9 * 60_000 - 200),
    method: "GET",
    host: "api.github.com",
    port: 443,
    outcome: "allow",
    address: "140.82.116.5",
    reason: null,
    binding: "ready-sandbox-github-public",
    policy: "github-public",
    rule: 0,
    substituted: true,
  },
  {
    at: ago(7 * 60_000),
    method: "GET",
    host: "api.github.com",
    port: 443,
    outcome: "allow",
    address: "140.82.116.5",
    reason: null,
    binding: "ready-sandbox-github-public",
    policy: "github-public",
    rule: 0,
    substituted: true,
  },
  {
    at: ago(6 * 60_000),
    method: "GET",
    host: "api.github.com",
    port: 443,
    outcome: "allow",
    address: "140.82.116.5",
    reason: null,
    binding: "ready-sandbox-github-public",
    policy: "github-public",
    rule: 0,
    substituted: true,
  },
  {
    at: ago(5 * 60_000),
    method: "GET",
    host: "pypi.org",
    port: 443,
    outcome: "deny",
    address: null,
    reason: "no-rule",
    binding: null,
    policy: null,
    rule: null,
    substituted: false,
  },
  {
    at: ago(4 * 60_000),
    method: "GET",
    host: "pypi.org",
    port: 443,
    outcome: "deny",
    address: null,
    reason: "no-rule",
    binding: null,
    policy: null,
    rule: null,
    substituted: false,
  },
  {
    at: ago(3 * 60_000),
    method: "GET",
    host: "pypi.org",
    port: 443,
    outcome: "deny",
    address: null,
    reason: "no-rule",
    binding: null,
    policy: null,
    rule: null,
    substituted: false,
  },
  {
    at: ago(60_000),
    method: "CONNECT",
    host: "example.invalid",
    port: 443,
    outcome: "deny",
    address: null,
    reason: "no-binding",
    binding: null,
    policy: null,
    rule: null,
    substituted: false,
  },
];

/** The phone fixture shows the proxy unreachable, the desktop one its decisions; both fit on a page. */
function egressDecisions(): DecisionsSnapshot {
  if (window.matchMedia("(max-width: 600px)").matches) {
    return { watching: true, decisions: { detail: "the egress proxy did not answer: connection refused" } };
  }
  return { watching: true, decisions: DECISIONS };
}

const IDLE_THREAD = "5f1c4a2e-0000-4000-8000-000000000001";
const UNNAMED_STOPPED_THREAD = "5f1c4a2e-0000-4000-8000-000000000000";
const RUNNING_THREAD = "5f1c4a2e-0000-4000-8000-000000000002";
const SUSPENDED_THREAD = "5f1c4a2e-0000-4000-8000-000000000004";
const THREE_UNPROCESSED_THREAD = "5f1c4a2e-0000-4000-8000-000000000007";
const ONE_UNPROCESSED_THREAD = "5f1c4a2e-0000-4000-8000-000000000008";
const DELIVERY_FAILED_THREAD = "5f1c4a2e-0000-4000-8000-000000000009";

const ACTION_REQUEST_ID = "d49b85b5-849f-4e7d-a644-d4a8b8c16127";

function pullRequest(number: number): GitHubSubject {
  return { pullRequest: { number } };
}

/** What the Notification Service reports for a Sandbox's inboxes: one with a notice awaiting confirmation and a
 * subscription in each state, one debouncing, one failing delivery. A suspended Sandbox has none. */
function notificationStatus(sandbox: string): SandboxNotificationStatus {
  const failures = ["SOURCE_FAILURE_KIND_RATE_LIMITED", "SOURCE_FAILURE_KIND_ACCESS_DENIED"] as const;
  const inboxes: InboxStatus[] =
    sandbox === "ready-sandbox"
      ? [
          {
            inbox: {
              id: "inbox-one",
              sessionId: IDLE_THREAD,
              lastCursor: "9",
              acknowledged: "4",
              covered: "7",
              expiredThrough: "0",
              retired: false,
            },
            subscriptions: [
              {
                id: "subscription-one",
                source: { actions: { requestId: ACTION_REQUEST_ID } },
                cancelled: false,
                expiresAt: ago(-HOUR),
                lastSuccessAt: ago(60000),
                nextSourceCheckAt: ago(-30000),
              },
              {
                id: "subscription-two",
                source: { github: { repository: "agentydragon/ducktape", subject: pullRequest(42) } },
                cancelled: true,
                expiresAt: ago(HOUR),
                lastSuccessAt: ago(60000),
                github: {
                  access: [
                    {
                      appId: "42",
                      installationId: "11",
                      repositoryId: "102",
                      checkedAt: ago(60000),
                      validUntil: ago(30000),
                      currentlyValid: false,
                      lastSuccessAt: ago(60000),
                    },
                  ],
                  subject: { repositoryId: "102", subject: pullRequest(42), lastSuccessAt: ago(60000) },
                },
              },
              ...failures.map((kind, index): SubscriptionStatus => ({
                id: `subscription-failure-${index + 1}`,
                source: { github: { repository: "owner/repo", subject: pullRequest(index + 1) } },
                cancelled: false,
                expiresAt: ago(-HOUR),
                lastSuccessAt: ago(60000),
                github: {
                  access: [
                    {
                      appId: "42",
                      installationId: "11",
                      repositoryId: String(100 + index),
                      checkedAt: ago(10000),
                      currentlyValid: false,
                      lastSuccessAt: ago(60000),
                      errorKind: kind,
                      errorSince: ago(30000),
                      errorObservedAt: ago(10000),
                      error:
                        kind === "SOURCE_FAILURE_KIND_RATE_LIMITED"
                          ? "GitHub rate limited (HTTP 429)"
                          : "GitHub App access unavailable (HTTP 403)",
                      retryAt: ago(-60000),
                    },
                  ],
                  subject: {
                    repositoryId: String(100 + index),
                    subject: pullRequest(index + 1),
                    lastSuccessAt: ago(60000),
                  },
                },
                nextSourceCheckAt: ago(-60000),
              })),
            ],
            notice: { throughCursor: "7", attempted: true, admitted: true, confirmed: false },
            unannouncedCount: 2,
            pendingAcknowledgementCount: 5,
            pendingEntries: [
              {
                cursor: "5",
                createdAt: ago(30000),
                provider: "actions",
                summary: `Action ${ACTION_REQUEST_ID} · event 5 · decision_pending`,
              },
              {
                cursor: "6",
                createdAt: ago(20000),
                provider: "github",
                summary: "GitHub check_run · completed · success",
              },
              { cursor: "7", createdAt: ago(10000), provider: "github", summary: "GitHub issue_comment · created" },
              {
                cursor: "8",
                createdAt: ago(5000),
                provider: "actions",
                summary: `Action ${ACTION_REQUEST_ID} · event 8 · running`,
              },
              {
                cursor: "9",
                createdAt: ago(0),
                provider: "actions",
                summary: `Action ${ACTION_REQUEST_ID} · event 9 · succeeded`,
              },
            ],
            pendingEntriesMore: false,
            noticeWaitReason: "NOTICE_WAIT_REASON_AWAITING_CONFIRMATION",
            nextWorkAt: ago(-5000),
          },
          {
            inbox: {
              id: "inbox-two",
              sessionId: "s-2",
              lastCursor: "3",
              acknowledged: "1",
              covered: "1",
              expiredThrough: "0",
              retired: false,
            },
            subscriptions: [],
            unannouncedCount: 2,
            pendingAcknowledgementCount: 2,
            pendingEntries: [
              {
                cursor: "2",
                createdAt: ago(10000),
                provider: "github",
                summary: "GitHub check_run · completed · failure",
              },
              { cursor: "3", createdAt: ago(0), provider: "github", summary: "GitHub pull_request · synchronize" },
            ],
            pendingEntriesMore: false,
            noticeDueAt: ago(-60000),
            quietUntil: ago(-60000),
            maxWaitAt: ago(-120000),
            noticeWaitReason: "NOTICE_WAIT_REASON_DEBOUNCING",
            nextWorkAt: ago(-60000),
          },
          {
            inbox: {
              id: "inbox-three",
              sessionId: "s-3",
              lastCursor: "2",
              acknowledged: "0",
              covered: "2",
              expiredThrough: "0",
              retired: false,
              deliveryError: "runner unavailable",
            },
            subscriptions: [
              {
                id: "subscription-three",
                source: { actions: { requestId: ACTION_REQUEST_ID } },
                cancelled: false,
                expiresAt: ago(HOUR),
                error: "source unavailable",
                nextSourceCheckAt: ago(-60000),
              },
            ],
            notice: { throughCursor: "2", attempted: true, admitted: false, confirmed: false },
            unannouncedCount: 0,
            pendingAcknowledgementCount: 2,
            pendingEntries: [
              {
                cursor: "1",
                createdAt: ago(10000),
                provider: "actions",
                summary: `Action ${ACTION_REQUEST_ID} · event 1 · decision_pending`,
              },
              {
                cursor: "2",
                createdAt: ago(0),
                provider: "actions",
                summary: `Action ${ACTION_REQUEST_ID} · event 2 · running`,
              },
            ],
            pendingEntriesMore: false,
            noticeWaitReason: "NOTICE_WAIT_REASON_DELIVERY_RETRY",
            nextWorkAt: ago(-5000),
          },
        ]
      : [];
  return { observedAt: ago(0), inboxes };
}

/** The store's copy of the sessions: the idle one named, the stopped one not, so both renderings are on the page. */
const THREADS: ThreadView[] = [
  {
    ...NULL_THREAD_FIELDS,
    id: IDLE_THREAD,
    sandbox: "ready-sandbox",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(HOUR),
    name: "Idle thread",
    archived: false,
    last_cursor: 14,
    last_event_at: ago(60_000),
    harness_state: "HARNESS_STATE_RUNNING",
    feed_status: "active",
  },
  {
    ...NULL_THREAD_FIELDS,
    id: UNNAMED_STOPPED_THREAD,
    sandbox: "ready-sandbox",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(2 * HOUR),
    name: null,
    archived: false,
    last_cursor: 31,
    last_event_at: ago(90 * 60_000),
    harness_state: "HARNESS_STATE_STOPPED",
    feed_status: "active",
  },
  {
    ...NULL_THREAD_FIELDS,
    id: RUNNING_THREAD,
    sandbox: "ready-sandbox",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(30 * 60_000),
    name: "Running thread",
    archived: false,
    last_cursor: 23,
    last_event_at: ago(10_000),
    harness_state: "HARNESS_STATE_RUNNING",
    feed_status: "active",
    active_turn_id: "t2",
  },
];

/**
 * The sidebar's cross-sandbox fixture: the three THREADS above and one per notice badge state
 * (all `ready-sandbox`), one each for the pending and suspended sandboxes, one archived, and one whose
 * `sandbox` names no live SandboxView at all -- the struck-through, read-only group.
 */
const THREADS_WITH_SANDBOXES: ThreadView[] = [
  ...THREADS,
  {
    ...NULL_THREAD_FIELDS,
    id: THREE_UNPROCESSED_THREAD,
    sandbox: "ready-sandbox",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(3 * HOUR),
    name: "Three unprocessed thread",
    archived: false,
    last_cursor: 7,
    last_event_at: ago(2 * 60_000),
    harness_state: "HARNESS_STATE_RUNNING",
    feed_status: "active",
  },
  {
    ...NULL_THREAD_FIELDS,
    id: ONE_UNPROCESSED_THREAD,
    sandbox: "ready-sandbox",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(3 * HOUR),
    name: "One unprocessed thread",
    archived: false,
    last_cursor: 7,
    last_event_at: ago(3 * 60_000),
    harness_state: "HARNESS_STATE_RUNNING",
    feed_status: "active",
  },
  {
    ...NULL_THREAD_FIELDS,
    id: DELIVERY_FAILED_THREAD,
    sandbox: "ready-sandbox",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(3 * HOUR),
    name: "Delivery-failed thread",
    archived: false,
    last_cursor: 7,
    last_event_at: ago(4 * 60_000),
    harness_state: "HARNESS_STATE_STOPPED",
    feed_status: "active",
  },
  {
    ...NULL_THREAD_FIELDS,
    id: "5f1c4a2e-0000-4000-8000-000000000003",
    sandbox: "pending-sandbox",
    harness: "HARNESS_CODEX",
    model: "harness-codex-model",
    cwd: "/state/work",
    created_at: ago(5 * 60_000),
    name: "Pending thread",
    archived: false,
    last_cursor: 2,
    last_event_at: ago(5 * 60_000),
    harness_state: "HARNESS_STATE_STOPPED",
  },
  {
    ...NULL_THREAD_FIELDS,
    id: SUSPENDED_THREAD,
    sandbox: "suspended-sandbox",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(47 * HOUR),
    name: "Suspended thread",
    archived: false,
    last_cursor: 9,
    last_event_at: ago(46 * HOUR),
    harness_state: "HARNESS_STATE_STOPPED",
    setup_state: "SETUP_STATE_SUCCEEDED",
    launch_outcome: "LAUNCH_OUTCOME_STARTED",
  },
  {
    ...NULL_THREAD_FIELDS,
    id: "5f1c4a2e-0000-4000-8000-000000000005",
    sandbox: "deleted-sandbox",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(72 * HOUR),
    name: "Read-only thread",
    archived: false,
    last_cursor: 18,
    last_event_at: ago(70 * HOUR),
    harness_state: "HARNESS_STATE_STOPPED",
  },
  {
    ...NULL_THREAD_FIELDS,
    id: "5f1c4a2e-0000-4000-8000-000000000006",
    sandbox: "ready-sandbox",
    harness: "HARNESS_CLAUDE",
    model: "harness-claude-model",
    cwd: "/state/work",
    created_at: ago(96 * HOUR),
    name: "Archived thread",
    archived: true,
    last_cursor: 3,
    last_event_at: ago(95 * HOUR),
    harness_state: "HARNESS_STATE_STOPPED",
  },
];

function withDormantSession(thread: ThreadView): ThreadView {
  return thread.id === RUNNING_THREAD
    ? {
        ...thread,
        name: "Dormant harness thread",
        feed_status: "active",
        harness_state: "HARNESS_STATE_STOPPED",
        active_turn_id: null,
      }
    : thread;
}

/** The `failedTurn` scenarios: the idle thread's history ends in a failed turn, which is also its last completed one. */
function withFailedTurn(thread: ThreadView): ThreadView {
  return thread.id === IDLE_THREAD
    ? { ...thread, name: "Failed-turn thread", last_turn_status: "TURN_STATUS_FAILED" }
    : thread;
}

function autosuspendCountdown(): void {
  const sandbox = SANDBOXES.find((candidate) => candidate.name === "ready-sandbox");
  if (!sandbox) throw new Error("Missing ready Sandbox fixture");
  sandbox.idle_suspend_after_seconds = 900;
  sandbox.idle_suspend_at = ago(-12 * 60_000);
  threadEntityRows = interleavedRows;
  mapThread = (thread) =>
    thread.id === IDLE_THREAD
      ? {
          ...thread,
          name: "Idle thread awaiting autosuspend",
          active_turn_id: null,
          last_model_activity_at: ago(13 * 60_000 + 25_000),
          idle_since: ago(13 * 60_000 + 5_000),
        }
      : thread;
}

function autosuspendBusy(): void {
  autosuspendCountdown();
  const sandbox = SANDBOXES.find((candidate) => candidate.name === "ready-sandbox");
  if (!sandbox) throw new Error("Missing ready Sandbox fixture");
  // Exercise busy facts taking precedence over a retained idle deadline.
  sandbox.autosuspend_blockers = [
    { reason: "ActiveTurn", session_id: RUNNING_THREAD },
    { reason: "Setup", session_id: UNNAMED_STOPPED_THREAD },
    { reason: "Bootstrap", session_id: null },
  ];
  const countdownThread = mapThread;
  mapThread = (thread) => {
    const mapped = countdownThread(thread);
    if (thread.id === IDLE_THREAD) return { ...mapped, name: "Idle thread sharing a busy Sandbox" };
    if (thread.id === RUNNING_THREAD) return { ...mapped, name: "Session running a turn" };
    if (thread.id === UNNAMED_STOPPED_THREAD) {
      return { ...mapped, name: "Session running setup", setup_state: "SETUP_STATE_RUNNING" };
    }
    return mapped;
  };
}

function autosuspendStarting(): void {
  autosuspendCountdown();
  const ready = SANDBOXES.find((candidate) => candidate.name === "ready-sandbox");
  if (!ready) throw new Error("Missing ready Sandbox fixture");
  SANDBOXES.push({
    ...ready,
    name: "starting-sandbox",
    uid: "0f9c1d2e-0000-4000-8000-000000000077",
    service_account: { namespace: ready.namespace, name: "starting-sandbox" },
    phase: "Creating",
    pod: null,
    status: {},
    idle_suspend_at: null,
    autosuspend_blockers: [],
  });
  const countdownThread = mapThread;
  mapThread = (thread) => {
    const mapped = countdownThread(thread);
    return thread.id === IDLE_THREAD
      ? {
          ...mapped,
          sandbox: "starting-sandbox",
          name: "Thread waiting for Sandbox startup",
          harness_state: "HARNESS_STATE_STOPPED",
          feed_status: "ended",
        }
      : mapped;
  };
  rememberDispatch(IDLE_THREAD);
}

let mapThread = (thread: ThreadView): ThreadView => thread;

// A 32x32 checkerboard, 95 bytes: a real image, small enough to inline.
const DIAGRAM_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgAQMAAABJtOi3AAAABlBMVEX///8ii+b/FUc9AAAAFElEQVR42mNg+A+ERBBEKmOgsnkA7b0/wU6R7xwAAAAASUVORK5CYII=";

// What the ssh MCP server's `exec` (ssh_mcp_server/server.py) returns for a listing that names one
// missing path: ls exits 2, and its output runs past the lines the widget shows before "Show all".
const SSH_EXEC_RESULT = {
  host: "test-archive-host",
  user: "test-user",
  exit_code: 2,
  stdout: [
    "/home/test-user/test-archive:",
    "total 1536",
    ...Array.from({ length: 30 }, (_, index) => {
      const day = index + 1;
      return `-rw-r--r-- 1 test-user test-user 51200 Sep ${String(day).padStart(2)} 03:00 test-backup-2026-09-${String(day).padStart(2, "0")}.tar.zst`;
    }),
    "",
  ].join("\n"),
  stderr: "ls: cannot access '/home/test-user/test-archive/test-missing': No such file or directory\n",
  stdout_truncated: false,
  stderr_truncated: false,
};

const ACTIONS: ActionRequestView[] = [
  {
    id: "70000000-0000-4000-8000-000000000001",
    action: { group: "everything", name: "echo" },
    arguments: { repository: "test-owner/test-repository", token: "test-token" },
    title: "echo the test repository handle back",
    description: "Confirms the fixture connection still reaches the echo Action before the demo run.",
    idempotency_key: "visual-pending",
    caller: { namespace: "agentplane-visual", name: "test-coder" },
    state: "decision_pending",
    version: 1,
    created_at: ago(3 * 60_000),
    updated_at: ago(3 * 60_000),
    decision: null,
    execution: null,
  },
  {
    id: "70000000-0000-4000-8000-000000000006",
    // The ssh group in MCP_GROUPS: its `exec` Action has widgets of its own (actions/rendering/ssh/exec.tsx).
    action: { group: "ssh", name: "exec" },
    arguments: {
      host: "test-archive-host",
      user: "test-user",
      command: 'systemctl --user restart test-backup.service && echo "restarted at $(date -Is)"',
      timeout_seconds: 60,
    },
    title: "restart the test backup service",
    description: "Its last run stopped on a stale lock, which a restart clears.",
    idempotency_key: "visual-ssh-pending",
    caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
    state: "decision_pending",
    version: 1,
    created_at: ago(2 * 60_000),
    updated_at: ago(2 * 60_000),
    decision: null,
    execution: null,
  },
  {
    id: "70000000-0000-4000-8000-000000000007",
    action: { group: "ssh", name: "exec" },
    arguments: {
      host: "test-archive-host",
      user: "test-user",
      command: 'ls -l "$HOME/test-archive" "$HOME/test-archive/test-missing"',
    },
    title: "list the test backup archive",
    description: null,
    idempotency_key: "visual-ssh-completed",
    caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
    state: "succeeded",
    version: 4,
    created_at: ago(5 * 60_000),
    updated_at: ago(4 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000007",
      verdict: "allow",
      provider: "human_operator",
      operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
      decision_note: null,
      reason_code: null,
      reason_description: null,
      policy_evidence: null,
      idempotency_key: "visual-allow-ssh",
      decided_at: ago(4 * 60_000),
    },
    execution: {
      id: "72000000-0000-4000-8000-000000000007",
      state: "succeeded",
      // The whole CallToolResult, as FastMCP answers with the ExecResult: the value as structured
      // content and again as one JSON text block, and the server's info in `_meta`.
      result: {
        _meta: { "io.modelcontextprotocol/serverInfo": { name: "ssh-mcp", version: "4.0.3" } },
        content: [{ type: "text", text: JSON.stringify(SSH_EXEC_RESULT) }],
        structuredContent: SSH_EXEC_RESULT,
        isError: false,
      },
      error: null,
      created_at: ago(4 * 60_000),
      started_at: ago(4 * 60_000 - 500),
      completed_at: ago(4 * 60_000 - 1_900),
      reconciled_at: null,
    },
  },
  {
    id: "70000000-0000-4000-8000-000000000002",
    // An MCP group in MCP_GROUPS below, so its stored result is the tool's whole CallToolResult.
    action: { group: "example_docs", name: "render_diagram" },
    arguments: { path: "docs/test-diagram.mmd" },
    title: "render the test diagram",
    description: null,
    idempotency_key: "visual-completed",
    caller: { namespace: "test-agentplane", name: "test-workload" },
    state: "succeeded",
    version: 4,
    created_at: ago(40 * 60_000),
    updated_at: ago(39 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000002",
      verdict: "allow",
      provider: "human_operator",
      operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
      decision_note: null,
      reason_code: null,
      reason_description: null,
      policy_evidence: null,
      idempotency_key: "visual-allow",
      decided_at: ago(39 * 60_000),
    },
    execution: {
      id: "72000000-0000-4000-8000-000000000002",
      state: "succeeded",
      result: {
        content: [
          { type: "text", text: "Rendered docs/test-diagram.mmd as a 32x32 PNG." },
          { type: "image", data: DIAGRAM_PNG, mimeType: "image/png" },
          {
            type: "resource_link",
            uri: "https://docs-mcp.example.test/diagrams/test-diagram",
            name: "test-diagram",
            title: "Test diagram page",
          },
        ],
        structuredContent: { path: "docs/test-diagram.mmd", width: 32, height: 32 },
        isError: false,
      },
      error: null,
      created_at: ago(39 * 60_000),
      started_at: ago(39 * 60_000 - 500),
      completed_at: ago(39 * 60_000 - 900),
      reconciled_at: null,
    },
  },
  {
    id: "70000000-0000-4000-8000-000000000003",
    action: { group: "everything", name: "echo" },
    arguments: { repository: "test-owner/other-repository" },
    title: "echo a repository outside the binding",
    description: "Reads test-owner/other-repository, which this workload has no binding for.",
    idempotency_key: "visual-denied",
    caller: { namespace: "agentplane-visual", name: "test-coder" },
    state: "denied",
    version: 2,
    created_at: ago(80 * 60_000),
    updated_at: ago(79 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000003",
      verdict: "deny",
      provider: "human_operator",
      operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
      decision_note: "Out of scope for this workload's binding.",
      reason_code: null,
      reason_description: null,
      policy_evidence: null,
      idempotency_key: "visual-deny",
      decided_at: ago(79 * 60_000),
    },
    execution: null,
  },
  {
    id: "70000000-0000-4000-8000-000000000004",
    action: { group: "github", name: "search_code" },
    arguments: { repository: "agentydragon/ducktape", query: "auto_allow" },
    title: "search ducktape for auto_allow",
    description: null,
    idempotency_key: "visual-auto-approved",
    caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
    state: "succeeded",
    version: 3,
    created_at: ago(15 * 60_000),
    updated_at: ago(14 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000004",
      verdict: "allow",
      provider: "policy_engine",
      operator: null,
      decision_note: null,
      reason_code: null,
      reason_description: null,
      idempotency_key: "visual-policy-allow",
      decided_at: ago(14 * 60_000),
      policy_evidence: {
        bindings: [{ namespace: "agentplane-visual", name: "ready-sandbox-github-public", resource_version: "12345" }],
        policy_sets: [{ namespace: "agentplane-visual", name: "fixture_auto_allow", generation: 1 }],
        matched: {
          namespace: "agentplane-visual",
          policy_set: "fixture_auto_allow",
          source: "rules",
          index: 0,
          type: "exact_actions",
          repository: null,
        },
      },
    },
    execution: {
      id: "72000000-0000-4000-8000-000000000004",
      state: "succeeded",
      result: { matches: 3 },
      error: null,
      created_at: ago(14 * 60_000),
      started_at: ago(14 * 60_000 - 500),
      completed_at: ago(14 * 60_000 - 900),
      reconciled_at: null,
    },
  },
  {
    id: "70000000-0000-4000-8000-000000000005",
    // A tool's error answer: the Action succeeded, and the CallToolResult says the tool failed.
    action: { group: "example_notes", name: "get_note" },
    arguments: { note_id: "test-missing-note" },
    title: "read the missing test note",
    description: null,
    idempotency_key: "visual-tool-error",
    caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
    state: "succeeded",
    version: 4,
    created_at: ago(10 * 60_000),
    updated_at: ago(9 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000005",
      verdict: "allow",
      provider: "human_operator",
      operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
      decision_note: null,
      reason_code: null,
      reason_description: null,
      policy_evidence: null,
      idempotency_key: "visual-allow-tool-error",
      decided_at: ago(9 * 60_000),
    },
    execution: {
      id: "72000000-0000-4000-8000-000000000005",
      state: "succeeded",
      result: { content: [{ type: "text", text: "No note has the id test-missing-note." }], isError: true },
      error: null,
      created_at: ago(9 * 60_000),
      started_at: ago(9 * 60_000 - 500),
      completed_at: ago(9 * 60_000 - 900),
      reconciled_at: null,
    },
  },
];

// A single losslessly rendered request, for compact approval screenshots.
const COMPACT_POD_ACTION: ActionRequestView = {
  ...ACTIONS[1]!,
  id: "70000000-0000-4000-8000-000000000009",
  action: { group: "kubernetes_admin", name: "pods_list_in_namespace" },
  arguments: { namespace: "test-apps", labelSelector: "app=demo", fieldSelector: "status.phase=Running" },
  title: "inspect running demo pods",
  description: "Read-only inspection of the demo workload.",
  idempotency_key: "visual-pods-compact-pending",
};

const COMPACT_RESOURCE_ACTION: ActionRequestView = {
  ...COMPACT_POD_ACTION,
  id: "70000000-0000-4000-8000-000000000011",
  action: { group: "kubernetes_admin", name: "resources_get" },
  arguments: { apiVersion: "v1", kind: "ConfigMap", namespace: "test-apps", name: "test-demo-config" },
  title: "read the demo config",
  description: "Read-only inspection of the demo configuration.",
  idempotency_key: "visual-resource-compact-pending",
};

const GMAIL_DRAFT_ACTION: ActionRequestView = {
  id: "70000000-0000-4000-8000-000000000010",
  action: { group: "gmail", name: "drafts_create" },
  arguments: {
    to: ["reviewer@example.com"],
    cc: ["release-team@example.com"],
    subject: "Release notes",
    body: "The release is ready.\nPlease review it before sending.",
    thread_id: "release-thread-123",
  },
  title: "prepare the release email",
  description: "Check the recipients and body before approving.",
  idempotency_key: "visual-gmail-draft-completed",
  caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
  state: "succeeded",
  version: 4,
  created_at: ago(7 * 60_000),
  updated_at: ago(6 * 60_000),
  decision: {
    id: "71000000-0000-4000-8000-000000000010",
    verdict: "allow",
    provider: "human_operator",
    operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
    decision_note: null,
    reason_code: null,
    reason_description: null,
    policy_evidence: null,
    idempotency_key: "visual-allow-gmail-draft",
    decided_at: ago(6 * 60_000),
  },
  execution: {
    id: "72000000-0000-4000-8000-000000000010",
    state: "succeeded",
    result: {
      content: [],
      structuredContent: {
        id: "draft-release-123",
        message: { payload: { headers: [{ name: "Subject", value: "Release notes" }] } },
      },
      isError: false,
    },
    error: null,
    created_at: ago(6 * 60_000),
    started_at: ago(6 * 60_000 - 500),
    completed_at: ago(6 * 60_000 - 1_500),
    reconciled_at: null,
  },
};

const CONVERSATION_SOURCE = "visual-runner";
const CONVERSATION_EPOCH = "20260921";
const payloadBodies = new Map<string, string>();

function payloadKey(ownerId: string, field: string, generation: string): string {
  return `${ownerId}:${field}:${generation}`;
}

function payload(
  ownerCursor: number,
  ownerId: string,
  field: string,
  body: string,
  revisionCursor = ownerCursor
): Record<string, string> {
  const reference = {
    projection_epoch: CONVERSATION_EPOCH,
    owner_cursor: String(ownerCursor),
    owner_id: ownerId,
    field,
    generation: "1",
    revision_cursor: String(revisionCursor),
    chunk_count: "1",
  };
  payloadBodies.set(payloadKey(ownerId, field, reference.generation), body);
  return reference;
}

function entity(
  kind: "view_state" | "item" | "confirmed_input" | "lifecycle" | "command",
  id: string,
  cursor: number,
  state: Record<string, unknown>,
  extra: Record<string, unknown> = {}
): Record<string, unknown> {
  return {
    thread_id: extra.thread_id ?? THREADS[0].id,
    projection_epoch: CONVERSATION_EPOCH,
    entity_kind: kind,
    entity_id: id,
    cursor: String(cursor),
    revision_cursor: String(extra.revision_cursor ?? cursor),
    pending: extra.pending ?? false,
    turn_id: extra.turn_id ?? null,
    state,
    text_ref: extra.text_ref ?? null,
    arguments_ref: extra.arguments_ref ?? null,
    output_ref: extra.output_ref ?? null,
    input_ref: extra.input_ref ?? null,
  };
}

function viewState(
  throughCursor: number,
  activeTurn: string | null,
  operational: Record<string, unknown> = {},
  appliedModel: string | null = "harness-claude-model",
  blockedReason: string | null = null
): Record<string, unknown> {
  return entity(
    "view_state",
    "current",
    throughCursor,
    {
      controls: {
        applied_model: appliedModel,
        applied_reasoning_effort: null,
        active_turn_id: activeTurn,
        harness_state: activeTurn === null ? "stopped" : "running",
        blocked_reason: blockedReason,
      },
      operational: {
        status: "active",
        last_verified_cursor: String(throughCursor),
        feed_error: null,
        ...operational,
      },
    },
    { revision_cursor: throughCursor }
  );
}

function lifecycle(
  cursor: number,
  observation: string,
  eventValue: Record<string, unknown>,
  threadId?: string
): Record<string, unknown> {
  return entity(
    "lifecycle",
    `${observation}:${cursor}`,
    cursor,
    { observation, event: eventValue },
    { thread_id: threadId }
  );
}

function item(
  cursor: number,
  id: string,
  kind: ItemKind,
  text: string | null,
  extra: {
    tool?: string;
    arguments?: string;
    output?: string;
    failed?: boolean;
    recovery?: RecoveryDisposition;
    recoveryReason?: string;
    complete?: boolean;
    turn?: string;
    threadId?: string;
  } = {}
): Record<string, unknown> {
  return entity(
    "item",
    id,
    cursor,
    {
      kind,
      tool_name: extra.tool ?? "",
      completion: extra.complete === false ? null : kind === ItemKind.TOOL_CALL ? "tool" : "text",
      tool_succeeded: extra.output === undefined ? null : !extra.failed,
      recovery: extra.recovery ?? null,
      recovery_reason: extra.recoveryReason ?? null,
    },
    {
      thread_id: extra.threadId,
      turn_id: extra.turn ?? "turn-visual",
      text_ref: text === null ? null : payload(cursor, id, "text", text),
      arguments_ref: extra.arguments === undefined ? null : payload(cursor, id, "arguments", extra.arguments),
      output_ref: extra.output === undefined ? null : payload(cursor, id, "output", extra.output),
    }
  );
}

function command(
  cursor: number,
  id: string,
  operation: string,
  outcome: "pending" | "effected" | "failed" | "noop",
  reason: string | null = null,
  text: string | null = null
): Record<string, unknown> {
  return entity(
    "command",
    id,
    cursor,
    {
      operation,
      outcome,
      outcome_cursor: outcome === "pending" ? null : String(cursor),
      outcome_reason: reason,
      requested_value: null,
    },
    { pending: outcome === "pending", input_ref: text === null ? null : payload(cursor, id, "command_input", text) }
  );
}

const LONG_REASONING_BODY = Array.from(
  { length: 18 },
  (_, index) =>
    `Pass ${index + 1}: I compare the stored body with the event projection, check each boundary for lost formatting, and keep the complete note available as ordinary Markdown. This section is deliberately long to exercise scrolling inside one disclosure.`
).join("\n\n");

function standardRows(
  threadId: string,
  longReasoningPreview: boolean,
  longReasoningBody: boolean
): Record<string, unknown>[] {
  const suspended = threadId === SUSPENDED_THREAD;
  const thread = THREADS_WITH_SANDBOXES.find((candidate) => candidate.id === threadId);
  if (!thread) throw new Error(`Unknown fixture Thread: ${threadId}`);
  const rows = [
    viewState(34, suspended ? null : "turn-visual", suspended ? { status: "ended" } : {}, thread.model),
    entity(
      "confirmed_input",
      "user-1",
      4,
      { harness_message_id: "user-1", origin_command_ids: ["input-1"] },
      {
        thread_id: threadId,
        turn_id: "turn-visual",
        input_ref: payload(4, "user-1", "confirmed_input", "List the repository files."),
      }
    ),
    item(10, "tool-0", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Read",
      arguments: '{"path":"README.md"}',
      output: "# ducktape\n\nRepository instructions are available.",
    }),
    item(
      20,
      "r-1",
      ItemKind.REASONING,
      longReasoningBody
        ? LONG_REASONING_BODY
        : longReasoningPreview
          ? "I will compare the projected row with its source payload. **The streamed body must retain its Markdown formatting** while the one-line preview clips what does not fit. I will verify the fetch path and expanded content before changing behavior. ".repeat(
              2
            )
          : "I will inspect the repository structure, compare **the relevant implementation and tests**, then confirm which path preserves the existing behavior before proposing a change.",
      { threadId }
    ),
    item(28, "m-1", ItemKind.ASSISTANT_TEXT, "I found the project files and the relevant tests.", { threadId }),
    lifecycle(
      31,
      "harness_stderr",
      toJson(
        EventSchema,
        create(EventSchema, { observation: { case: "harnessStderr", value: { text: "warning: fixture stderr" } } })
      ) as Record<string, unknown>,
      threadId
    ),
    item(34, "m-2", ItemKind.ASSISTANT_TEXT, "Next I will read the focused implementation.", {
      threadId,
      complete: false,
    }),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** A tool run with a consecutive group of empty reasoning summaries between its calls. */
function consecutiveReasoningRows(threadId: string): Record<string, unknown>[] {
  const rows = [
    viewState(70, null),
    item(10, "reasoning-group-tool-before", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Bash",
      arguments: JSON.stringify({ command: "agentplane actions submit sandbox/exec" }),
    }),
    item(20, "reasoning-group-empty-1", ItemKind.REASONING, "", { threadId }),
    item(30, "reasoning-group-empty-2", ItemKind.REASONING, "", { threadId }),
    item(40, "reasoning-group-empty-3", ItemKind.REASONING, "", { threadId }),
    item(50, "reasoning-group-tool-after", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Bash",
      arguments: JSON.stringify({ command: "agentplane actions submit kubernetes_admin/pods_log" }),
    }),
    item(60, "reasoning-group-answer", ItemKind.ASSISTANT_TEXT, "The log check is complete.", { threadId }),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

function failedRows(
  threadId: string,
  afterContent: boolean,
  error = "Test model request failed: HTTP 429. Quota exhausted for this fixture."
): Record<string, unknown>[] {
  const event = toJson(
    EventSchema,
    create(EventSchema, {
      observation: { case: "turnCompleted", value: { turnId: "failed-turn", status: TurnStatus.FAILED, error } },
    })
  ) as Record<string, unknown>;
  const rows = [
    viewState(afterContent ? 8 : 6, null),
    entity(
      "confirmed_input",
      "failed-input",
      4,
      { harness_message_id: "failed-input", origin_command_ids: ["input-failed"] },
      {
        thread_id: threadId,
        turn_id: "failed-turn",
        input_ref: payload(4, "failed-input", "confirmed_input", "Inspect the test repository."),
      }
    ),
    ...(afterContent
      ? [
          item(
            6,
            "partial",
            ItemKind.ASSISTANT_TEXT,
            "The first test files are present. Checking the remaining files…",
            { threadId }
          ),
        ]
      : []),
    lifecycle(afterContent ? 8 : 6, "turn_completed", event, threadId),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

function interleavedRows(threadId: string): Record<string, unknown>[] {
  const rows = [
    viewState(18, null),
    item(3, "tool-1", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Read",
      arguments: '{"path":"README.md"}',
      output: "README opened",
    }),
    item(
      8,
      "before-input",
      ItemKind.ASSISTANT_TEXT,
      "I checked the current files before processing the queued messages.",
      {
        threadId,
        turn: "interleaved-turn",
      }
    ),
    entity(
      "confirmed_input",
      "coalesced-message",
      10,
      { harness_message_id: "coalesced-message", origin_command_ids: ["input-B", "input-C"] },
      {
        thread_id: threadId,
        turn_id: "interleaved-turn",
        input_ref: payload(10, "coalesced-message", "confirmed_input", "Also inspect tests.\nKeep the patch small."),
      }
    ),
    item(15, "after-input", ItemKind.ASSISTANT_TEXT, "Continuing with the new model…", {
      threadId,
      turn: "interleaved-turn",
    }),
    lifecycle(
      18,
      "turn_completed",
      toJson(
        EventSchema,
        create(EventSchema, {
          observation: {
            case: "turnCompleted",
            value: { turnId: "interleaved-turn", status: TurnStatus.INTERRUPTED, interruptedByCommandId: "interrupt" },
          },
        })
      ) as Record<string, unknown>,
      threadId
    ),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** Close-packed command states so the semaphore and its real labels can be inspected on a phone. */
function commandProgressRows(threadId: string): Record<string, unknown>[] {
  const model = command(2, "progress-model", "change_model", "pending");
  const effort = command(3, "progress-effort", "change_reasoning_effort", "failed", "Not supported by harness");
  const noop = command(4, "progress-noop", "change_model", "noop", "Model already selected");
  const effected = command(5, "progress-effected", "change_model", "effected");
  return [
    { ...viewState(5, "t-progress"), thread_id: threadId },
    { ...model, state: { ...(model.state as object), requested_value: "test-model-next" } },
    { ...effort, state: { ...(effort.state as object), requested_value: "high" } },
    { ...noop, state: { ...(noop.state as object), requested_value: "current-model" } },
    { ...effected, state: { ...(effected.state as object), requested_value: "applied-model" } },
    command(9, "progress-applied-input", "submit_input", "effected", null, "Applied input."),
    command(6, "progress-input", "submit_input", "pending", null, "Please review this change."),
    command(7, "progress-noop-input", "submit_input", "noop", "Nothing to do", "Already handled."),
  ];
}

/** Local and remote pending inputs whose payload references are present but bodies are withheld. */
function pendingInputEchoRows(threadId: string): Record<string, unknown>[] {
  const local = command(
    15,
    "locally-retained",
    "submit_input",
    "pending",
    null,
    "This browser already knows the text it sent."
  );
  const remote = command(16, "remote-pending", "submit_input", "pending", null, "This text is still unknown here.");
  payloadBodies.delete(payloadKey("locally-retained", "command_input", "1"));
  payloadBodies.delete(payloadKey("remote-pending", "command_input", "1"));
  return [{ ...viewState(16, "t-input-echo"), thread_id: threadId }, local, remote];
}

/**
 * The statuses the main thread fixture does not produce on its own: a standalone failed tool call, a
 * run whose reasoning is still streaming beside a tool call, and queued commands. Both runs render
 * folded, which is the point: a run's summary is where its streaming and failed indicators show.
 * The run's first step, at cursor 16, is its anchor. The existing nav/header chrome leaves little
 * vertical room at phone width, so the queued-input dot at the bottom falls off the page; the desktop
 * capture is where every state here is visible.
 */
function statesRows(threadId: string, outcomes = false): Record<string, unknown>[] {
  const suspended = threadId === SUSPENDED_THREAD;
  const rows = [
    viewState(28, suspended ? null : "t2", suspended ? { status: "ended" } : {}),
    item(7, "tool-0", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Bash",
      arguments: '{"command":"git branch -d stale"}',
      output: "fatal: branch 'stale' not found.",
      failed: true,
    }),
    item(10, "m-0", ItemKind.ASSISTANT_TEXT, "That branch does not exist.", { threadId, turn: "t1" }),
    item(16, "r-0", ItemKind.REASONING, "Running the suite twice exposes flaky failures.", {
      threadId,
      complete: false,
      turn: "t2",
    }),
    item(19, "tool-1", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Bash",
      arguments: '{"command":"bazel test //..."}',
      output: "42 passed",
      turn: "t2",
    }),
    command(24, "queued-model", "change_model", outcomes ? "failed" : "pending", outcomes ? "Model unavailable" : null),
    command(
      25,
      "queued-interrupt",
      "interrupt_turn",
      outcomes ? "noop" : "pending",
      outcomes ? "Target turn already ended" : null
    ),
    // Admitted and still pending, so it renders inline as a pending message bubble rather than in
    // the pending-commands box below -- see conversation.tsx's pendingSentMessage.
    command(26, "queued-submit", "submit_input", "pending", null, "Continue past the failing test once it lands."),
    command(
      27,
      "failed-submit",
      "submit_input",
      "failed",
      "Runner could not start this turn.",
      "Run the rejected input."
    ),
    command(
      28,
      "noop-submit",
      "submit_input",
      "noop",
      "The harness was already stopping.",
      "Run the input that was not applied."
    ),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** Mirror the screenshot's order: assistant text, a folded run, then an unfinished assistant-text
 * item whose streaming badge renders before its body. Copy is synthetic; only the row states matter. */
function streamingInterleavedRows(threadId: string): Record<string, unknown>[] {
  const activeTurn = "streaming-turn";
  let cursor = 1;
  const rows: Record<string, unknown>[] = [];

  const appendAssistantText = (id: string, text: string, streaming: boolean): void => {
    rows.push(
      item(cursor++, id, ItemKind.ASSISTANT_TEXT, text, {
        threadId,
        complete: !streaming,
        turn: activeTurn,
      })
    );
  };

  const appendRun = (prefix: string, toolCalls: number, reasoningSteps: number, failedTool?: number): void => {
    for (let index = 0; index < Math.max(toolCalls, reasoningSteps); index++) {
      if (index < toolCalls) {
        const failed = index === failedTool;
        rows.push(
          item(cursor++, `${prefix}-tool-${index}`, ItemKind.TOOL_CALL, null, {
            threadId,
            tool: "Search",
            output: failed ? "The fixture marks this tool call as failed." : undefined,
            failed,
            turn: activeTurn,
          })
        );
      }
      if (index < reasoningSteps)
        rows.push(
          item(cursor++, `${prefix}-reasoning-${index}`, ItemKind.REASONING, "Evaluating the latest results.", {
            threadId,
            turn: activeTurn,
          })
        );
    }
  };

  appendAssistantText(
    "previous-response",
    "Understood — fold it into basic itself, not into default_policies alongside it. That's the better shape: basic is \"what every agent can do\", and the API server is part of that. Let me look at basic's construction and every reference to KUBERNETES_POLICY, because folding it in makes the separate policy a lie unless I delete it too.",
    false
  );
  appendRun("large-run", 32, 17);
  appendAssistantText(
    "streaming-response-1",
    "Understood — folding it into basic rather than defaulting a separate policy. Let me check what asserts on the policy set before I move the rule.",
    true
  );
  appendRun("small-run-1", 2, 1);
  appendAssistantText(
    "streaming-response-2",
    'Understood — that\'s a different and better shape: basic is "the self-identity plumbing every agent needs", and the API server belongs in it. Let me check what merging it would touch, then do it.',
    true
  );
  appendRun("failed-run", 2, 1, 0);
  appendAssistantText(
    "streaming-response-3",
    "Understood — that's a different and better shape: basic is the floor, and Kubernetes reach is part of the floor. Let me check what that implies before editing.",
    true
  );
  appendRun("small-run-2", 3, 2);
  appendAssistantText(
    "streaming-response-4",
    'Understood — the rule belongs in basic, and the separate Kubernetes policy should be removed with it. I\'ll make the change and verify the result.\n\n```python\nanswer = "ready"\nprint(answer)',
    true
  );
  rows.unshift(viewState(cursor - 1, activeTurn));
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** Three mundane observations that collapse into one comma-joined row, then a prominent one
 * (harness lost) that stands alone, then one final mundane one -- lone, so it groups with nothing. */
function lifecycleGroupRows(threadId: string): Record<string, unknown>[] {
  const event = (value: MessageInitShape<typeof EventSchema>["observation"]): Record<string, unknown> =>
    toJson(EventSchema, create(EventSchema, { observation: value })) as Record<string, unknown>;
  const rows = [
    viewState(50, null),
    lifecycle(10, "turn_started", event({ case: "turnStarted", value: { turnId: "turn-visual" } }), threadId),
    lifecycle(20, "harness_started", event({ case: "harnessStarted", value: {} }), threadId),
    lifecycle(
      30,
      "turn_completed",
      event({ case: "turnCompleted", value: { turnId: "turn-visual", status: TurnStatus.COMPLETED } }),
      threadId
    ),
    lifecycle(40, "harness_lost", event({ case: "harnessLost", value: {} }), threadId),
    lifecycle(50, "harness_started", event({ case: "harnessStarted", value: {} }), threadId),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

function setupRows(threadId: string, running = false): Record<string, unknown>[] {
  const event = (value: MessageInitShape<typeof EventSchema>["observation"]): Record<string, unknown> =>
    toJson(EventSchema, create(EventSchema, { observation: value })) as Record<string, unknown>;
  const output = (cursor: number, channel: "stdout" | "stderr", text: string) =>
    lifecycle(
      cursor,
      "setup_output",
      event({
        case: "setupOutput",
        value: { stream: { case: channel, value: new TextEncoder().encode(text) } },
      }),
      threadId
    );
  return [
    { ...viewState(7, null), thread_id: threadId },
    lifecycle(1, "setup_started", event({ case: "setupStarted", value: {} }), threadId),
    output(2, "stderr", "Cloning into project...\nUpdating files: 10%\r"),
    output(3, "stdout", "Workspace ready\n"),
    output(4, "stderr", "Updating files: 80%\r"),
    output(5, "stderr", "Updating files: 100%\n"),
    ...(running
      ? []
      : [
          lifecycle(6, "setup_finished", event({ case: "setupFinished", value: { exitCode: 0 } }), threadId),
          lifecycle(7, "harness_started", event({ case: "harnessStarted", value: {} }), threadId),
        ]),
  ];
}

/** One user turn answered with a fenced Python code block, so Markdown's syntax highlighting of a
 * registered language renders the way tool-call Arguments/Output already do. */
function codeFenceRows(threadId: string): Record<string, unknown>[] {
  const rows = [
    viewState(20, null),
    entity(
      "confirmed_input",
      "user-1",
      4,
      { harness_message_id: "user-1", origin_command_ids: ["input-1"] },
      {
        thread_id: threadId,
        turn_id: "turn-visual",
        input_ref: payload(4, "user-1", "confirmed_input", "Add type hints to the greet function."),
      }
    ),
    item(
      20,
      "m-code",
      ItemKind.ASSISTANT_TEXT,
      'Done. The signature now declares its types explicitly:\n\n```python\ndef greet(name: str) -> str:\n    return f"Hello, {name}!"\n```\n',
      { threadId }
    ),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** A reasoning step with no neighboring tool call, so `historyRows` never folds it into a run and
 * `EntityCard` renders it directly -- the standalone case, distinct from `standardRows`'s reasoning
 * step, which sits right after a tool call and so is always part of a run. */
function standaloneReasoningRows(
  threadId: string,
  longPreview: boolean,
  codeFence: boolean,
  longBody: boolean
): Record<string, unknown>[] {
  const reasoning = codeFence
    ? [
        "> Check",
        "",
        "| State | Count |",
        "| --- | --- |",
        "| Ready | 2 |",
        "",
        "- Parsed",
        "- Complete",
        "",
        "```python",
        "def greet(name: str) -> str:",
        '    return f"Hello, {name}!"',
        "```",
        "",
        "The complete result remains available when expanded.",
      ].join("\n")
    : longBody
      ? LONG_REASONING_BODY
      : longPreview
        ? "The [projection path](https://example.test/projection) may explain this result. I should **verify it** before adding a retry or changing client behavior. The latest observations still disagree, so I need to compare the source payload with its projected row before changing the client."
        : "Weighing which path to try next.";
  const rows = [
    viewState(24, null),
    entity(
      "confirmed_input",
      "user-1",
      4,
      { harness_message_id: "user-1", origin_command_ids: ["input-1"] },
      {
        thread_id: threadId,
        turn_id: "turn-visual",
        input_ref: payload(4, "user-1", "confirmed_input", "What should we try next?"),
      }
    ),
    item(20, "r-solo", ItemKind.REASONING, reasoning, { threadId }),
    item(24, "m-1", ItemKind.ASSISTANT_TEXT, "Let's fix the root cause.", { threadId }),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** The command Codex records for a script it ran through a shell: the argv, joined, with the script
 * double-quoted. */
function codexShellCommand(script: string): string {
  return `/bin/bash -lc "${script.replace(/["\\$`]/g, "\\$&")}"`;
}

const LONG_QUERY_SCRIPT = [
  `curl -sS -u 'test-user:test-credential' --data-urlencode "query=WITH latest AS (SELECT DISTINCT ON (account_id, security_id) account_id, security_id, institution_value, captured_at FROM holding_snapshots ORDER BY account_id, security_id, captured_at DESC, id DESC)`,
  `SELECT l.institution_name, a.name AS account, s.name AS security, round(sum(h.institution_value)::numeric, 2) AS market_value FROM latest h JOIN accounts a ON a.account_id = h.account_id JOIN links l ON l.item_id = a.item_id LEFT JOIN securities s ON s.security_id = h.security_id GROUP BY 1, 2, 3 ORDER BY market_value DESC" http://test-pgweb.example/api/query`,
].join(" ");

const LONG_SCRIPT = [
  "set -euo pipefail",
  ...Array.from({ length: 16 }, (_, index) => `echo "step ${index + 1}: $(date -Is)" >> /tmp/test-progress.log`),
  "tail -n 3 /tmp/test-progress.log",
].join("\n");

/** Shell tool calls in the shapes the two harnesses record them: Claude's `Bash` with the model's
 * description, a script past the input's cap, and Codex's joined `bash -lc` command with output past
 * the output's. Neighbouring reasoning keeps them one run. */
function shellCallRows(threadId: string): Record<string, unknown>[] {
  const rows = [
    viewState(60, null),
    entity(
      "confirmed_input",
      "user-1",
      4,
      { harness_message_id: "user-1", origin_command_ids: ["input-1"] },
      {
        thread_id: threadId,
        turn_id: "turn-visual",
        input_ref: payload(4, "user-1", "confirmed_input", "Check the containers and the holdings."),
      }
    ),
    item(10, "claude-bash", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Bash",
      arguments: JSON.stringify({
        command: "docker ps --all --format 'table {{.Names}}\\t{{.Status}}'",
        description: "List every container and its status",
      }),
      output: "NAMES\tSTATUS\ntest-web\tUp 3 hours\ntest-db\tUp 3 hours (healthy)",
    }),
    item(20, "r-1", ItemKind.REASONING, "The containers are up, so the holdings query is next.", { threadId }),
    item(30, "codex-command", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "commandExecution",
      arguments: JSON.stringify({ command: codexShellCommand(LONG_QUERY_SCRIPT), cwd: "/test-workspace" }),
      output: JSON.stringify(
        Array.from({ length: 24 }, (_, index) => ({
          institution_name: "Test Bank",
          account: `Test Account ${index + 1}`,
          market_value: (1000 - index * 17.5).toFixed(2),
        })),
        null,
        2
      ),
    }),
    item(40, "claude-script", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Bash",
      arguments: JSON.stringify({ command: LONG_SCRIPT, timeout: 120000 }),
      output: "step 14\nstep 15\nstep 16",
      failed: true,
    }),
    item(50, "claude-streaming", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Bash",
      arguments: '{"command": "bazel test //agentplane/...", "descri',
      complete: false,
    }),
    item(60, "m-1", ItemKind.ASSISTANT_TEXT, "The holdings query returned 24 rows.", { threadId }),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** Two reasoning steps with no neighbouring tool call, so each stands alone, both unfinished: one in
 * the turn the harness is running, which is streaming, and one in an earlier turn that ended without
 * finishing it, which is incomplete. */
function unfinishedReasoningRows(threadId: string): Record<string, unknown>[] {
  const rows = [
    viewState(30, "turn-visual"),
    item(10, "r-stalled", ItemKind.REASONING, "A step its turn ended without finishing.", {
      threadId,
      complete: false,
      turn: "turn-earlier",
    }),
    item(20, "m-1", ItemKind.ASSISTANT_TEXT, "Between the two steps.", { threadId, turn: "turn-earlier" }),
    item(30, "r-running", ItemKind.REASONING, "A step the running turn is still working through.", {
      threadId,
      complete: false,
    }),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** What a recovery leaves on most of a finished turn: every item retained and finished, none badged. */
function quietRecoveryRows(threadId: string): Record<string, unknown>[] {
  const retained = { threadId, recovery: RecoveryDisposition.RETAINED };
  const retainedTool = (cursor: number, id: string) =>
    item(cursor, id, ItemKind.TOOL_CALL, null, {
      ...retained,
      tool: "Retained tool",
      arguments: '{"outcome":"succeeded","context":"retained"}',
      output: "Succeeded and retained: no badge",
    });
  return [
    { ...viewState(50, null), thread_id: threadId },
    retainedTool(10, "retained-tool-1"),
    item(20, "retained-reasoning-in-run", ItemKind.REASONING, "Retained reasoning in a run: no badge", retained),
    retainedTool(30, "retained-tool-2"),
    item(40, "retained-text", ItemKind.ASSISTANT_TEXT, "Retained assistant text: no badge row above it", retained),
    item(50, "retained-reasoning-alone", ItemKind.REASONING, "Retained reasoning on its own: no badge", retained),
  ];
}

function recoveryRows(threadId: string, kind: "messages" | "tools" | "quiet"): Record<string, unknown>[] {
  if (kind === "quiet") return quietRecoveryRows(threadId);
  const rows =
    kind === "tools"
      ? [
          item(10, "revised-tool", ItemKind.TOOL_CALL, null, {
            threadId,
            tool: "Bash",
            complete: false,
            recovery: RecoveryDisposition.REVISED,
            arguments: '{"command":"write-report"}',
          }),
          item(20, "discarded-tool", ItemKind.TOOL_CALL, null, {
            threadId,
            tool: "Bash",
            output: "Succeeded, then discarded from context",
            recovery: RecoveryDisposition.ABSENT,
          }),
          item(30, "failed-tool", ItemKind.TOOL_CALL, null, {
            threadId,
            tool: "Retained tool",
            output: "Failed, still in context",
            failed: true,
            recovery: RecoveryDisposition.RETAINED,
          }),
          item(40, "unknown-tool", ItemKind.TOOL_CALL, null, {
            threadId,
            tool: "Bash",
            complete: false,
            recovery: RecoveryDisposition.UNKNOWN,
            recoveryReason: "The harness history could not be inspected.",
          }),
        ]
      : [
          item(10, "retained-text", ItemKind.ASSISTANT_TEXT, "Retained text, interrupted mid-reply", {
            threadId,
            complete: false,
            recovery: RecoveryDisposition.RETAINED,
          }),
          item(20, "discarded-text", ItemKind.ASSISTANT_TEXT, "Remember the name in the margin", {
            threadId,
            complete: false,
            recovery: RecoveryDisposition.ABSENT,
          }),
          item(30, "revised-text", ItemKind.ASSISTANT_TEXT, "This is the text retained for the next turn.", {
            threadId,
            complete: false,
            recovery: RecoveryDisposition.REVISED,
          }),
          item(
            40,
            "unknown-text",
            ItemKind.ASSISTANT_TEXT,
            "Then she heard the bells of her city, ringing under the floor.",
            {
              threadId,
              complete: false,
              recovery: RecoveryDisposition.UNKNOWN,
              recoveryReason: "The harness history could not be inspected.",
            }
          ),
        ];
  if (kind === "tools") rows[0].output_ref = payload(10, "revised-tool", "output", "aborted");
  return [{ ...viewState(40, null), thread_id: threadId }, ...rows];
}

let threadEntityRows = (threadId: string): Record<string, unknown>[] =>
  threadId === THREADS[2].id ? statesRows(threadId) : standardRows(threadId, false, false);

function realisticRolloutRows(
  threadId: string,
  entries: typeof rollout | typeof reportedRollout
): Record<string, unknown>[] {
  const rows = entries.map((entry, index) => {
    const cursor = index + 1;
    const id = `rollout-${cursor}`;
    const turn = `rollout-turn-${entry.turn}`;
    if (entry.kind === "input") {
      if (entry.text === null) throw new Error("Rollout input requires text");
      return entity(
        "confirmed_input",
        id,
        cursor,
        { harness_message_id: id, origin_command_ids: [] },
        { thread_id: threadId, turn_id: turn, input_ref: payload(cursor, id, "confirmed_input", entry.text) }
      );
    }
    let kind: ItemKind;
    switch (entry.kind) {
      case "tool":
        kind = ItemKind.TOOL_CALL;
        break;
      case "reasoning":
        kind = ItemKind.REASONING;
        break;
      case "assistant":
        kind = ItemKind.ASSISTANT_TEXT;
        break;
      default:
        throw new Error(`Unknown rollout item kind: ${entry.kind}`);
    }
    return item(cursor, id, kind, entry.text, {
      threadId,
      turn,
      tool: entry.tool ?? undefined,
      arguments: entry.arguments ?? undefined,
      output: entry.output ?? undefined,
      failed: entry.failed,
    });
  });
  return [{ ...viewState(entries.length, null), thread_id: threadId }, ...rows];
}

function threadScope(threadId: string): Record<string, string> {
  const through = threadEntityRows(threadId).find((row) => row.entity_kind === "view_state")?.revision_cursor ?? "0";
  return { projection_epoch: CONVERSATION_EPOCH, through_cursor: String(through) };
}

function rememberPendingInput(): void {
  const local = new LocalCommands(THREADS[2].id);
  local.remember(
    create(CommandSchema, {
      commandId: "locally-retained",
      operation: {
        case: "submitInput",
        value: { text: "Continue when ready. This message has no saved confirmation yet." },
      },
    })
  );
}

function rememberInputEcho(): void {
  const local = new LocalCommands(THREADS[2].id);
  local.remember(
    create(CommandSchema, {
      commandId: "locally-retained",
      operation: {
        case: "submitInput",
        value: { text: "This browser already knows the text it sent." },
      },
    })
  );
}

function rememberSettledCommands(): void {
  // A settled command shows while the browser that sent it still holds it.
  const local = new LocalCommands(THREADS[2].id);
  local.remember(
    create(CommandSchema, {
      commandId: "queued-model",
      operation: { case: "changeModel", value: { model: "next-model" } },
    })
  );
  local.remember(
    create(CommandSchema, {
      commandId: "queued-interrupt",
      operation: { case: "interruptTurn", value: { turnId: "t2" } },
    })
  );
}

function enableSuspendedSandboxWakeOnMessage(): void {
  const sandbox = SANDBOXES.find((candidate) => candidate.name === "suspended-sandbox");
  if (!sandbox) throw new Error("Missing suspended Sandbox fixture");
  sandbox.wake_on_send = true;
  sandbox.suspended_by = "Requested";
}

function rememberDispatch(threadId = RUNNING_THREAD): void {
  const command = create(CommandSchema, {
    commandId: "queued-dispatch",
    operation: {
      case: "submitInput",
      value: { text: "Continue when the Sandbox is ready." },
    },
  });
  const local = new LocalCommands(threadId);
  local.remember(command);
  const dispatch = dispatchFromJson({
    commandId: command.commandId,
    sessionId: threadId,
    state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
    reason: "sandbox_resuming",
    updatedAt: ago(1_000),
    admission: null,
  });
  local.acknowledgeDispatch(command, dispatch);
}

// What the mock Dispatcher's stream reports for a queued command, where a scene gives it a reason of
// its own.
const queuedReasons = new Map<string, string>();

// Real reason from a staging Dispatcher whose Role lacked a verb, on a message short enough that
// its indicator sits near the right edge of the page.
function rememberShortRefusedWake(): void {
  const command = create(CommandSchema, {
    commandId: "short-wake-refused",
    operation: { case: "submitInput", value: { text: "Short" } },
  });
  const reason =
    'sandbox_wake_failed: sandboxes.agentplane.allegedly.works "box" is forbidden: User "system:serviceaccount:agentplane-staging:runner-gateway" cannot update resource "sandboxes" in API group "agentplane.allegedly.works" in the namespace "agentplane-staging"';
  queuedReasons.set(command.commandId, reason);
  const local = new LocalCommands(RUNNING_THREAD);
  local.remember(command);
  local.acknowledgeDispatch(
    command,
    dispatchFromJson({
      commandId: command.commandId,
      sessionId: RUNNING_THREAD,
      state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
      reason,
      updatedAt: ago(1_000),
      admission: null,
    })
  );
}

function rememberFailedDispatch(): void {
  const command = create(CommandSchema, {
    commandId: "wake-deadline-dispatch",
    operation: {
      case: "submitInput",
      value: { text: "Please start the Sandbox when capacity is available." },
    },
  });
  const local = new LocalCommands(RUNNING_THREAD);
  local.remember(command);
  const dispatch = dispatchFromJson({
    commandId: command.commandId,
    sessionId: RUNNING_THREAD,
    state: "DELIVERY_STATE_FAILED",
    reason: "wake_deadline_expired: Ready=False/Unschedulable: 0/2 nodes are available: PVC is not bound",
    updatedAt: ago(1_000),
    admission: null,
  });
  local.acknowledgeDispatch(command, dispatch);
}

// One MCP server per row state the MCP servers page draws: linked and connected, a link whose token
// lapsed while its refresh keeps failing, a refresh the provider refused, never linked, and
// bearer-only backends that are up or unreachable.
const MCP_LINKAGES: McpLinkageView[] = [
  {
    server_id: "example_docs",
    server_url: "https://docs-mcp.example.test/mcp",
    status: "linked",
    scopes: ["openid", "offline_access"],
    refresh_failure: null,
  },
  {
    server_id: "example_cluster",
    server_url: "https://cluster-mcp.example.test/mcp",
    status: "expired",
    scopes: ["openid", "email", "profile", "offline_access"],
    refresh_failure: {
      action: "retrying",
      error: "the token endpoint answered HTTP 503 Service Unavailable",
      attempts: 6,
      retry_at: new Date(NOW + 4 * 60_000).toISOString(),
    },
  },
  {
    server_id: "example_calendar",
    server_url: "https://calendar-mcp.example.test/mcp",
    status: "degraded",
    scopes: ["openid", "offline_access"],
    refresh_failure: {
      action: "reconnect",
      error: "the OAuth provider refused the token request: invalid_grant: Token is not active",
      attempts: 1,
      retry_at: null,
    },
  },
  {
    server_id: "example_pantry",
    server_url: "https://pantry-mcp.example.test/mcp",
    status: "unlinked",
    scopes: [],
    refresh_failure: null,
  },
];

type McpFailure = Pick<NonNullable<ActionGroupView["health"]>, "reason" | "detail" | "last_discovery_at" | "failures">;

/** An MCP group, available unless `failure` says why it is disconnected. */
function mcpGroup(key: string, executorDescription: string, failure?: McpFailure): ActionGroupView {
  return {
    key,
    title: key,
    description: `Test MCP backend ${key}.`,
    executor_kind: "mcp",
    executor_description: executorDescription,
    available: failure === undefined,
    health: failure
      ? { state: "disconnected", retry_at: new Date(NOW + 20_000).toISOString(), ...failure }
      : { state: "available", reason: null, detail: null, last_discovery_at: ago(60_000), retry_at: null, failures: 0 },
    actions: [],
    direct_tools: [],
  };
}

const LINKED = "Linked operator account.";
const BEARER = "Test MCP backend behind a static bearer.";
const MCP_GROUPS: ActionGroupView[] = [
  mcpGroup("example_docs", LINKED),
  mcpGroup("example_cluster", LINKED, {
    reason: "linkage_unavailable",
    detail: `the access token expired at ${ago(2 * HOUR)
      .replace("T", " ")
      .slice(0, 19)} UTC and has not been refreshed`,
    last_discovery_at: ago(3 * HOUR),
    failures: 12,
  }),
  mcpGroup("example_calendar", LINKED, {
    reason: "linkage_unavailable",
    detail: "refreshing the token failed in a way retrying cannot fix; link the account again",
    last_discovery_at: ago(HOUR),
    failures: 9,
  }),
  mcpGroup("example_pantry", LINKED, {
    reason: "linkage_unavailable",
    detail: "no account is linked",
    last_discovery_at: null,
    failures: 4,
  }),
  mcpGroup("example_mail", BEARER, {
    reason: "connect_failed",
    detail: "RuntimeError: Client failed to connect: All connection attempts failed",
    last_discovery_at: null,
    failures: 7,
  }),
  mcpGroup("gmail", "Test Gmail action group for Agentplane rendering."),
  mcpGroup("example_notes", BEARER),
  mcpGroup("ssh", BEARER),
];
let receiptActionGroup: ActionGroupView | undefined;

/** Switches a test sets on the mock services, before mounting or (`publish`) while a stream is open. */
type Flag =
  | "centralSamplerEnabled"
  | "claudePaused"
  | "watchStale"
  | "threadDatabaseDisconnected"
  | "threadStreamDropped"
  | "inventoryStreamDropped"
  | "entityStreamDisconnected"
  | "entitySegmentsWithheld"
  | "syncScopeUnavailable"
  | "commandAdmissionTimedOut"
  | "pendingActions"
  | "pendingActionOverflow"
  | "compactPodAction"
  | "gmailDraftHistory"
  | "pagedActionHistory"
  | "actionGroupsUnavailable"
  | "notificationStatusUnavailable"
  | "notificationExtraEntry"
  | "pushBrowserAdded"
  | "connectionRenamed"
  | "mcpLinkageChanged"
  | "mcpHealthChanged";
const flags = new Set<Flag>();

function actionGroups(): ActionGroupView[] {
  return MCP_GROUPS.map((row) =>
    row.key === "example_docs" && flags.has("mcpHealthChanged")
      ? {
          ...row,
          available: false,
          health: {
            state: "disconnected" as const,
            reason: "connect_failed" as const,
            detail: "Connection refused",
            last_discovery_at: row.health?.last_discovery_at ?? null,
            retry_at: new Date(NOW + 20_000).toISOString(),
            failures: 1,
          },
        }
      : row
  );
}

function mcpLinkages(): McpLinkageView[] {
  return MCP_LINKAGES.map((row) =>
    row.server_id === "example_docs" && flags.has("mcpLinkageChanged") ? { ...row, status: "expired" as const } : row
  );
}

function pendingActions(): ActionRequestView[] {
  const pending = ACTIONS.filter((request) => request.state === "decision_pending");
  return previewAction !== null
    ? [previewAction]
    : flags.has("compactPodAction")
      ? [COMPACT_POD_ACTION]
      : flags.has("pendingActionOverflow")
        ? [COMPACT_POD_ACTION, COMPACT_RESOURCE_ACTION, ...pending]
        : flags.has("pendingActions")
          ? pending
          : [];
}

// Only what a page still asks for: the sandboxes, their bindings and their threads arrive on the
// live streams above.
routes.push(
  [
    "GET",
    /^\/frontend-config$/,
    () => ({
      trace_search: {
        grafana_base_url: "https://grafana.test.invalid",
        tempo_datasource_uid: "tempo-test",
        deployment_environment: "agentplane-visual-test",
      },
      langfuse_search: {
        base_url: "https://langfuse.test.invalid",
        project_id: "langfuse-visual-test",
        session_metadata_key: "attributes.litellm.metadata.spend_logs_metadata.agentplane.session_id",
        sandbox_namespace_metadata_key: "attributes.agentplane.namespace",
        sandbox_service_account_metadata_key: "attributes.agentplane.service_account",
      },
    }),
  ],
  [
    "GET",
    /^\/models$/,
    () => ({
      central_sampler: flags.has("centralSamplerEnabled") ? { service: "central", provider: "responses" } : null,
      models: [
        {
          model: "harness-claude-model",
          display_name: "Harness Claude Model",
          reasoning_efforts: TEST_REASONING_EFFORTS,
        },
        { model: "next-model", display_name: "Next Model", reasoning_efforts: TEST_REASONING_EFFORTS },
        {
          model: "harness-codex-model",
          display_name: "Harness Codex Model",
          reasoning_efforts: TEST_REASONING_EFFORTS,
        },
      ],
      harnesses: {
        HARNESS_CLAUDE: flags.has("claudePaused") ? [] : ["harness-claude-model", "next-model"],
        HARNESS_CODEX: ["harness-codex-model"],
        ...(flags.has("centralSamplerEnabled") ? { HARNESS_CENTRAL_SAMPLER: ["harness-codex-model"] } : {}),
      },
    }),
  ],
  [
    "GET",
    /^\/system\/status$/,
    () => [
      { name: "Healthy component", state: "ok", detail: null, checked_at: ago(4000) },
      { name: "Down component", state: "down", detail: "UNAVAILABLE: connection refused", checked_at: ago(4000) },
    ],
  ],
  [
    "GET",
    /^\/presets$/,
    () => [
      {
        name: "test-coder",
        title: "Test coder",
        template: "runner",
        policy_sets: [...TEST_CODER_POLICY_SETS],
        inbox_tags: [],
        session_defaults: {
          harness: "HARNESS_CODEX",
          model: "harness-codex-model",
          cwd: "/state/workspaces/{session_id}",
          reasoning_effort: "medium",
          instructions: "Work on public repositories only.",
        },
        bootstrap: "mkdir -p /state/workspaces",
      },
    ],
  ],
  ["GET", /^\/sandboxes\/templates$/, () => ["runner"]],
  ["GET", /^\/sandboxes\/form$/, () => ({ policy_sets: [...TEST_CODER_POLICY_SETS, "pypi", "github-public"] })],
  [
    "GET",
    /^\/upstream\/actions\/v1\/action-requests:watch$/,
    (_match, _query, signal) => {
      const path = "/upstream/actions/v1/action-requests:watch";
      return liveNdjsonWatch(
        path,
        [actionRequestsSnapshotProtoJson(pendingActions()), actionRequestsSnapshotCompleteProtoJson()],
        () => pendingActions().map(actionRequestUpsertProtoJson),
        signal
      );
    },
  ],
  [
    "GET",
    /^\/upstream\/actions\/v1\/action-groups:watch$/,
    (_match, _query, signal) => {
      const path = "/upstream/actions/v1/action-groups:watch";
      return liveNdjsonWatch(
        path,
        [actionGroupsSnapshotProtoJson(actionGroups()), actionGroupsSnapshotCompleteProtoJson()],
        () => actionGroups().map(actionGroupUpsertProtoJson),
        signal
      );
    },
  ],
  [
    "GET",
    /^\/upstream\/actions\/v1\/operator\/mcp-servers:watch$/,
    (_match, _query, signal) => {
      const path = "/upstream/actions/v1/operator/mcp-servers:watch";
      return liveNdjsonWatch(
        path,
        [mcpLinkagesSnapshotProtoJson(mcpLinkages()), mcpLinkagesSnapshotCompleteProtoJson()],
        () =>
          mcpLinkages()
            .filter((linkage) => linkage.server_id === "example_docs")
            .map(mcpLinkageUpsertProtoJson),
        signal
      );
    },
  ],
  [
    "GET",
    /^\/upstream\/actions\/v1\/action-requests$/,
    (_match, query) =>
      listActionRequestsProtoJson(
        ACTIONS.filter((request) => !query.has("state") || actionStateProtoJson(request.state) === query.get("state"))
      ),
  ],
  [
    "GET",
    /^\/upstream\/actions\/v1\/action-requests:history$/,
    (_match, query) => {
      const past = flags.has("gmailDraftHistory")
        ? [GMAIL_DRAFT_ACTION]
        : ACTIONS.filter((request) => request.state !== "decision_pending");
      const filtered =
        query.get("includeAutoAllowed") === "false"
          ? past.filter((request) => !(request.decision?.verdict === "allow" && request.decision.operator === null))
          : past;
      const page =
        flags.has("pagedActionHistory") && !query.has("cursor")
          ? { items: filtered.slice(0, 2), next_cursor: "second-page" }
          : { items: flags.has("pagedActionHistory") ? filtered.slice(2) : filtered, next_cursor: null };
      return listActionHistoryProtoJson(page);
    },
  ],
  [
    "GET",
    /^\/upstream\/actions\/v1\/action-requests\/([^/]+)$/,
    (match) => {
      const request = previewAction?.id === match[1] ? previewAction : ACTIONS.find((item) => item.id === match[1]);
      return request
        ? getActionRequestProtoJson(request)
        : Response.json({ code: 5, message: "Action request not found" }, { status: 404 });
    },
  ],
  [
    "GET",
    /^\/upstream\/connections\/v1\/operator\/connections$/,
    () => ({ connections: [sampleConnection(), ...retiredConnectionRows()].map(connectionJson) }),
  ],
  [
    "GET",
    /^\/caller-grants\/([^/]+)\/([^/]+)$/,
    (match) => ({
      bindings: BINDINGS.map((binding) => ({
        ...binding,
        subjects: [{ namespace: match[1], name: match[2] }],
      })),
      action_policy: ACTION_POLICY,
    }),
  ],
  [
    "GET",
    /^\/upstream\/connections\/v1\/operator\/caller-service-accounts$/,
    () => ({ serviceAccounts: [{ namespace: "agentplane-visual", name: "operator-assistant" }] }),
  ],
  // The Settings modal mounts all three tabs at once (Mantine keepMounted), so MCP servers and
  // Notifications fetch on mount even while the OAuth clients tab is the one shown in the shot.
  [
    "GET",
    /^\/upstream\/actions\/v1\/action-groups$/,
    () => {
      if (flags.has("actionGroupsUnavailable")) {
        return Response.json(
          { code: 14, message: "the Action Service did not answer: connection refused" },
          { status: 503 }
        );
      }
      const groups = receiptActionGroup === undefined ? MCP_GROUPS : [...MCP_GROUPS, receiptActionGroup];
      return listActionGroupsProtoJson(groups);
    },
  ],
  ["GET", /^\/upstream\/actions\/v1\/operator\/push\/config$/, () => ({})],
  ["GET", /^\/upstream\/actions\/v1\/operator\/push\/subscriptions$/, () => ({ pushSubscriptions: [] })],
  [
    "GET",
    /^\/upstream\/actions\/v1\/operator\/push\/subscriptions:watch$/,
    (_match, _query, signal) =>
      liveNdjsonWatch(
        "/upstream/actions/v1/operator/push/subscriptions:watch",
        [{ snapshotComplete: {} }],
        () =>
          flags.has("pushBrowserAdded")
            ? [{ upsert: { endpoint: "https://push.example/browser", userAgent: "Second browser", createdAt: ago(0) } }]
            : [],
        signal
      ),
  ],
  [
    "POST",
    /^\/upstream\/connections\/v1\/operator\/connection-enrollments\/[^/]+\/preview$/,
    () => ({
      clientId: "test-client-registration-5d46c4f2",
      clientName: "Test external client",
      redirectUri: "https://test-client.example/oauth/callback",
      expiresAt: new Date(NOW + 10 * 60_000).toISOString(),
      version: "1",
    }),
  ],
  [
    "GET",
    /^\/upstream\/connections\/v1\/operator\/connections:watch$/,
    (_match, _query, signal) =>
      liveNdjsonWatch(
        CONNECTIONS_WATCH,
        [
          { snapshot: { connections: [sampleConnection(), ...retiredConnectionRows()].map(connectionJson) } },
          { snapshotComplete: {} },
        ],
        () => [{ upsert: connectionJson({ ...sampleConnection(), display_name: "Updated OAuth client" }) }],
        signal
      ),
  ],
  [
    "POST",
    /^\/upstream\/connections\/v1\/operator\/connection-enrollments\/[^/]+\/decision$/,
    () => ({ verdict: "VERDICT_ALLOW", redirectUrl: "https://idp.test/held-authorization" }),
  ],
  [
    "POST",
    /^\/upstream\/actions\/v1\/operator\/action-requests\/([^/]+)\/decision$/,
    (match) => {
      const request = ACTIONS.find((item) => item.id === match[1]);
      return request
        ? decideActionRequestProtoJson({ ...request, state: "allowed", version: 2 })
        : Response.json({ code: 5, message: "Action request not found" }, { status: 404 });
    },
  ],
  [
    "GET",
    /^\/sandboxes\/([^/]+)\/runner-status$/,
    () => ({
      runner_boot_id: "visual-runner-boot-01",
      lifecycle: {
        desired_mode: "RUNNER_LIFECYCLE_MODE_QUIESCING",
        desired_generation: "3",
        progress_generation: "3",
        progress_available: true,
        progress_current: true,
        stage: "RUNNER_LIFECYCLE_STAGE_FLUSHING",
        error: "",
        blocker: {
          kind: "RUNNER_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT",
          error: "",
          resource_type: "IngressAcknowledgements.agentplane",
          resource_id: "ready-session",
          process_id: "",
          resource_present: true,
          admissions: "0",
          journal_seal_present: true,
          journal_seal_uid: "visual-incarnation-01",
          expected_journal_seal_uid: "visual-incarnation-01",
          journal_seal_cursor: "19",
          acknowledgement_present: true,
          acknowledgement_sealed: true,
          acknowledgement_cursor: "18",
          finalizers_pending: false,
        },
      },
      processes: [
        {
          process_id: "visual-claude-process-01",
          pid: 18001,
          harness: "HARNESS_CLAUDE",
          state: "PROCESS_STATE_RUNNING",
          assigned_session_ids: ["ready-session"],
        },
      ],
      sessions: [
        {
          session_id: "dormant-session",
          process_id: null,
          native_session_id: null,
          execution_state: "SESSION_EXECUTION_STATE_DORMANT",
          active_turn_id: null,
          failure_cursor: null,
          busy: false,
        },
        {
          session_id: "ready-session",
          process_id: "visual-claude-process-01",
          native_session_id: "visual-native-session-01",
          execution_state: "SESSION_EXECUTION_STATE_READY",
          active_turn_id: null,
          failure_cursor: null,
          busy: false,
        },
      ],
    }),
  ],
  [
    "POST",
    /^\/sandboxes\/([^/]+)\/sessions$/,
    (match, _query, _signal, body) => {
      if (!queueOpenResponses || body === undefined) throw new Error("No queued Session Open fixture is configured");
      const request = JSON.parse(body) as { session_id?: string };
      if (!request.session_id) throw new Error("Open request has no Session ID");
      queuedOpenSessionId = request.session_id;
      return {
        sessionId: request.session_id,
        sandbox: match[1],
        state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
        reason: "waiting for runner admission",
      };
    },
  ],
  [
    "GET",
    /^\/threads$/,
    (_match, query) => ({
      threads: THREADS.filter((thread) => !query.has("sandbox") || thread.sandbox === query.get("sandbox")),
      next_page_token: null,
    }),
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)$/,
    (match) => {
      const thread = THREADS_WITH_SANDBOXES.find((candidate) => candidate.id === match[1]);
      return thread && mapThread(thread);
    },
  ]
);

/** Encode the database-facing Electric row, including PostgreSQL JSONB and bool columns. */
function electricEntity(row: Record<string, unknown>): Record<string, unknown> {
  const value = { ...row };
  for (const field of ["state", "text_ref", "arguments_ref", "output_ref", "input_ref"] as const) {
    if (value[field] !== null) value[field] = JSON.stringify(value[field]);
  }
  value.pending = String(value.pending);
  return value;
}

interface Subset {
  where?: string;
  params?: Record<string, string>;
  order_by?: string;
  limit?: number;
}

function subsetOf(query: URLSearchParams, body: string | undefined): Subset {
  if (query.get("projection_epoch") !== CONVERSATION_EPOCH) {
    throw new Error("thread shapes must name the resolved thread fold projection epoch");
  }
  if (body === undefined) throw new Error("a subset is POSTed with its parameters in the body");
  return JSON.parse(body) as Subset;
}

/** The proxy's entity subset forms, evaluated over the fixture rows. */
function entitySubset(rows: Record<string, unknown>[], subset: Subset): Record<string, unknown>[] {
  const index = (row: Record<string, unknown>) => BigInt(String(row.entity_index));
  const newestFirst = (selected: Record<string, unknown>[]) =>
    selected.sort((left, right) => (index(right) > index(left) ? 1 : index(right) < index(left) ? -1 : 0));
  const selected =
    subset.where === undefined && subset.order_by === "entity_index DESC"
      ? newestFirst(rows)
      : subset.where === "entity_index < $1" && subset.order_by === "entity_index DESC"
        ? newestFirst(rows.filter((row) => index(row) < BigInt(subset.params?.["1"] ?? "0")))
        : subset.where === "entity_kind = 'view_state'"
          ? rows.filter((row) => row.entity_kind === "view_state")
          : subset.where === "entity_kind = 'command' AND pending = true"
            ? rows.filter((row) => row.entity_kind === "command" && row.pending === "true")
            : subset.where === "entity_kind = 'command' AND entity_id = ANY($1)"
              ? rows.filter(
                  (row) =>
                    row.entity_kind === "command" &&
                    (JSON.parse(`[${(subset.params?.["1"] ?? "{}").slice(1, -1)}]`) as string[]).includes(
                      String(row.entity_id)
                    )
                )
              : undefined;
  if (selected === undefined) throw new Error(`the proxy admits no entity subset ${JSON.stringify(subset)}`);
  return selected.slice(0, subset.limit);
}

function shapeRow(relation: string, value: Record<string, unknown>) {
  const identity =
    relation === "thread_entity"
      ? [value.thread_id, value.projection_epoch, value.entity_kind, value.entity_id]
      : [
          value.thread_id,
          value.projection_epoch,
          value.owner_cursor,
          value.owner_id,
          value.field,
          value.generation,
          value.chunk_index,
        ];
  return {
    headers: { relation: ["public", relation] as ["public", string], operation: "insert" as const },
    key: `"public"."${relation}"/${identity.map((part) => JSON.stringify(String(part))).join("/")}`,
    // PostgreSQL JSON columns arrive as their JSON representation; ShapeStream parses this scalar.
    value: relation === "thread_payload_chunk" ? { ...value, text: JSON.stringify(value.text) } : value,
  };
}

/** Positions in the order the fixture lists its rows, which is the order they were first written. */
function threadRows(threadId: string): Record<string, unknown>[] {
  return threadEntityRows(threadId).map((row, index) => electricEntity({ ...row, entity_index: String(index) }));
}

function archivedStderr(cursor: number): Record<string, unknown> {
  return toJson(
    EventEntrySchema,
    create(EventEntrySchema, {
      cursor: BigInt(cursor),
      origin: { sourceId: CONVERSATION_SOURCE, sequence: BigInt(cursor) },
      event: create(EventSchema, {
        observation: { case: "harnessStderr", value: { text: "warning: fixture stderr" } },
      }),
    })
  ) as Record<string, unknown>;
}

function archivedCompletion(cursor: number): Record<string, unknown> {
  return toJson(
    EventEntrySchema,
    create(EventEntrySchema, {
      cursor: BigInt(cursor),
      origin: { sourceId: CONVERSATION_SOURCE, sequence: BigInt(cursor) },
      event: create(EventSchema, {
        observation: { case: "itemCompleted", value: { itemId: "m-2", outcome: { case: "text", value: "complete" } } },
      }),
    })
  ) as Record<string, unknown>;
}

const OBSERVATION_ENTRIES: Record<string, () => Record<string, unknown>> = {
  "31": () => archivedStderr(31),
  "34": () => archivedCompletion(34),
};

function observationPage(threadId: string) {
  return {
    observations: [
      { cursor: "31", kind: "harness_stderr" },
      { cursor: "34", kind: "item_completed" },
    ],
    next_before_cursor: null,
    next_after_cursor: null,
    thread_id: threadId,
  };
}

const CONNECTIONS_WATCH = "/upstream/connections/v1/operator/connections:watch";

function connectionJson(row: Connection): Record<string, unknown> {
  return {
    id: row.id,
    displayName: row.display_name,
    version: row.version,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    grants: row.grants.map((grant) => ({
      id: grant.id,
      connectionId: grant.connection_id,
      revision: grant.revision,
      caller: grant.caller,
      issuer: grant.issuer,
      clientId: grant.client_id,
      status: `GRANT_STATUS_${grant.status.toUpperCase()}`,
      createdAt: grant.created_at,
      ...(grant.activated_at === null ? {} : { activatedAt: grant.activated_at }),
      ...(grant.revoked_at === null ? {} : { revokedAt: grant.revoked_at }),
    })),
    ...(row.bound_caller === null ? {} : { boundCaller: row.bound_caller }),
  };
}

function retiredConnectionRows(): Connection[] {
  const sample = sampleConnection();
  return [
    {
      ...sample,
      id: "10000000-0000-4000-8000-000000000002",
      display_name: "Retired research client",
      bound_caller: null,
      grants: sample.grants.map((grant) => ({
        ...grant,
        id: "20000000-0000-4000-8000-000000000002",
        connection_id: "10000000-0000-4000-8000-000000000002",
        caller: { namespace: "agentplane-visual", name: "retired" },
        status: "revoked",
        revoked_at: "2026-09-09T12:03:00Z",
      })),
    },
  ];
}

routes.push(
  [
    "GET",
    /^\/upstream\/dispatcher\/v1\/dispatches:watch$/,
    (_match, query, signal) =>
      ndjsonWatch(
        [
          ...query.getAll("filter.commandIds").map((commandId) => ({
            dispatch: {
              commandId,
              sessionId: query.get("filter.sessionId"),
              sandbox: "pending-sandbox",
              state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
              reason: queuedReasons.get(commandId) ?? "sandbox_resuming",
              updatedAt: ago(1_000),
            },
          })),
          { caughtUp: {} },
        ],
        signal
      ),
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/sync\/scope$/,
    (match) =>
      flags.has("syncScopeUnavailable")
        ? // This persistent service failure is distinct from a retired epoch's 410, which the
          // production store resolves by reading the scope again.
          Response.json({ detail: "thread fold is temporarily unavailable" }, { status: 503 })
        : threadScope(match[1]),
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/sync\/entities$/,
    (match, query, signal) =>
      query.get("live") !== "true"
        ? electricShape([], `visual-entities-${match[1]}`)
        : flags.has("entityStreamDisconnected")
          ? Response.json({ detail: "thread shape is temporarily unavailable" }, { status: 503 })
          : electricLive(`visual-entities-${match[1]}`, undefined, signal),
  ],
  [
    "POST",
    /^\/threads\/([0-9a-f-]+)\/sync\/entities$/,
    (match, query, _signal, body) => {
      const rows = threadRows(match[1]).map((row) =>
        flags.has("entitySegmentsWithheld") && row.entity_kind === "view_state" ? { ...row, revision_cursor: "8" } : row
      );
      return electricSubset(
        entitySubset(rows, subsetOf(query, body)).map((row) => shapeRow("thread_entity", row)),
        `visual-entities-${match[1]}`,
        "thread_entity"
      );
    },
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/sync\/chunks\/([a-z_]+)$/,
    (match, query, signal) =>
      query.get("live") === "true"
        ? electricLive(`visual-chunks-${match[1]}-${match[2]}`, "thread_payload_chunk", signal)
        : electricShape([], `visual-chunks-${match[1]}-${match[2]}`, "thread_payload_chunk"),
  ],
  [
    "POST",
    /^\/threads\/([0-9a-f-]+)\/sync\/chunks\/([a-z_]+)$/,
    (match, query, _signal, body) => {
      const params = subsetOf(query, body).params ?? {};
      const rows = Array.from({ length: Object.keys(params).length / 2 }, (_, index) => ({
        ownerId: params[String(2 * index + 1)],
        generation: params[String(2 * index + 2)],
      })).flatMap(({ ownerId, generation }) => {
        const text = payloadBodies.get(payloadKey(ownerId, match[2], generation));
        return text === undefined
          ? []
          : [
              shapeRow("thread_payload_chunk", {
                thread_id: match[1],
                projection_epoch: CONVERSATION_EPOCH,
                owner_cursor: "0",
                owner_id: ownerId,
                field: match[2],
                generation,
                chunk_index: "0",
                text,
              }),
            ];
      });
      return electricSubset(rows, `visual-chunks-${match[1]}-${match[2]}`, "thread_payload_chunk");
    },
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/evidence$/,
    () => ({ observations: [{ observation_cursor: "31", has_native: true }], next_after_cursor: null }),
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/evidence\/([0-9]+)\/frames$/,
    (_match) => ({
      frames: [
        {
          source_sequence: "31",
          availability: "present",
          entry: archivedStderr(31),
        },
      ],
      next_after_sequence: null,
    }),
  ],
  // No runner here admits a command, so one the page delivers on load stays unadmitted.
  [
    "POST",
    /^\/threads\/([0-9a-f-]+)\/(?:submit-input|change-model|change-reasoning-effort|interrupt-turn)$/,
    () =>
      flags.has("commandAdmissionTimedOut")
        ? Response.json({ detail: "runner admission was not confirmed within 15 seconds" }, { status: 504 })
        : UNANSWERED,
  ],
  ["GET", /^\/threads\/([0-9a-f-]+)\/observations$/, (match) => observationPage(match[1])],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)\/observations\/([0-9]+)$/,
    (match) => ({ cursor: match[2], entry: OBSERVATION_ENTRIES[match[2]]() }),
  ]
);

const FRESH: WatchHealth = {
  fresh: true,
  stale_after_seconds: 900,
  refreshed_seconds_ago: {
    sandboxes: 4.2,
    pods: 3.1,
    "policybindings.agentplane.allegedly.works": 11.7,
    "policysets.agentplane.allegedly.works": 8.3,
  },
};

/** A watch that has stopped cycling: what the `_stale` scenarios have to show rather than hide. */
const WEDGED: WatchHealth = {
  fresh: false,
  stale_after_seconds: 900,
  refreshed_seconds_ago: {
    sandboxes: 2417.4,
    pods: 2417.4,
    "policybindings.agentplane.allegedly.works": 11.7,
    "policysets.agentplane.allegedly.works": 8.3,
  },
};

function watch(): WatchHealth {
  return flags.has("watchStale") ? WEDGED : FRESH;
}

/** Live inventory remains EventSource; Action facade watches and projected threads use fetch streams. */
class HarnessEventSource extends EventTarget {
  static readonly sources = new Set<HarnessEventSource>();
  static publish(pathSuffix: string): void {
    for (const source of this.sources) {
      const url = new URL(source.url, "http://harness");
      if (url.pathname.endsWith(pathSuffix) && source.readyState === this.OPEN) source.serve(url);
    }
  }
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readonly url: string;
  readyState = HarnessEventSource.CONNECTING;

  constructor(url: string) {
    super();
    this.url = url;
    HarnessEventSource.sources.add(this);
    // After the view's listeners are attached, which happens right after construction.
    setTimeout(() => this.serve(new URL(url, "http://harness")), 0);
  }

  private serve(url: URL): void {
    this.readyState = HarnessEventSource.OPEN;
    if (url.pathname === "/live/threads") {
      // The listing the server would push: the newest `limit` matches, then the open thread.
      const all = THREADS_WITH_SANDBOXES.map(mapThread);
      const search = url.searchParams.get("search")?.toLowerCase();
      const limit = Number(url.searchParams.get("limit"));
      const matching = all.filter(
        (thread) =>
          (url.searchParams.get("include_archived") === "true" || !thread.archived) &&
          (search === undefined ||
            (thread.name ?? "").toLowerCase().includes(search) ||
            thread.sandbox.toLowerCase().includes(search))
      );
      const listed = matching.slice(0, limit);
      const open = all.find((thread) => thread.id === url.searchParams.get("open_thread"));
      const snapshot: ThreadsSnapshot = {
        sandboxes: SANDBOXES,
        threads: open === undefined || listed.includes(open) ? listed : [...listed, open],
        more: matching.length > limit,
        archived_count: all.filter((thread) => thread.archived).length,
        updates_connected: !flags.has("threadDatabaseDisconnected"),
        watch: watch(),
      };
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(snapshot) }));
      if (flags.has("threadStreamDropped")) this.drop();
      return;
    }
    if (/^\/live\/sandboxes\/[^/]+\/egress\/decisions$/.test(url.pathname)) {
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(egressDecisions()) }));
      return;
    }
    if (url.pathname === "/live/sandboxes") {
      const snapshot: SandboxesSnapshot = { sandboxes: SANDBOXES, watch: watch() };
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(snapshot) }));
      if (flags.has("inventoryStreamDropped")) this.drop();
      return;
    }
    if (url.pathname === "/threads/notices/stream") {
      const states: Record<string, Partial<InboxNoticeState>> = {
        [THREE_UNPROCESSED_THREAD]: {
          queued: 2,
          unacknowledged: 1,
          notice: "NOTICE_PROGRESS_CONFIRMED",
          noticeWaitReason: "NOTICE_WAIT_REASON_DEBOUNCING",
          eligibleAt: ago(-90_000),
        },
        [ONE_UNPROCESSED_THREAD]: { unacknowledged: 1, notice: "NOTICE_PROGRESS_CONFIRMED" },
        [DELIVERY_FAILED_THREAD]: { queued: 1, notice: "NOTICE_PROGRESS_ATTEMPTED", error: "runner unreachable" },
      };
      const inboxes = url.searchParams
        .getAll("thread_id")
        .filter((session) => session in states)
        .map((session): InboxNoticeState => ({
          inboxId: `inbox-${session}`,
          sessionId: session,
          queued: 0,
          unacknowledged: 0,
          ...states[session],
        }));
      const snapshot: SessionNoticeStates = { observedAt: ago(0), inboxes };
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(snapshot) }));
      // A dropped Thread listing takes the notice stream down with it, so the badges show stale too.
      if (flags.has("threadStreamDropped")) this.drop();
      return;
    }
    const sandbox = url.pathname.startsWith("/live/sandboxes/") ? url.pathname.slice("/live/sandboxes/".length) : null;
    const notification = url.pathname.match(/^\/sandboxes\/([^/]+)\/notifications\/stream$/);
    if (notification) {
      if (flags.has("notificationStatusUnavailable")) {
        this.drop();
        return;
      }
      const snapshot = notificationStatus(notification[1]);
      const first = snapshot.inboxes[0];
      if (flags.has("notificationExtraEntry") && first) {
        first.inbox.lastCursor = "10";
        first.unannouncedCount += 1;
        first.pendingAcknowledgementCount += 1;
        first.pendingEntries.push({
          cursor: "10",
          createdAt: ago(0),
          provider: "github",
          summary: "GitHub workflow_run · completed",
        });
      }
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(snapshot) }));
      return;
    }
    if (/^\/sandboxes\/[^/]+\/console\/stream$/.test(url.pathname)) {
      this.dispatchEvent(new MessageEvent("output", { data: JSON.stringify(btoa("guest console output\r\n")) }));
      return;
    }
    if (sandbox !== null) {
      const snapshot: SandboxSnapshot = {
        sandbox: SANDBOXES.find((row) => row.name === decodeURIComponent(sandbox)) ?? null,
        bindings: BINDINGS,
        action_policy: ACTION_POLICY,
        threads: THREADS,
        updates_connected: !flags.has("threadDatabaseDisconnected"),
        watch: watch(),
      };
      this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(snapshot) }));
      return;
    }
    throw new Error(`Unexpected EventSource route: ${url.pathname}`);
  }

  private drop(): void {
    this.readyState = HarnessEventSource.CONNECTING;
    this.dispatchEvent(new Event("error"));
  }

  close(): void {
    HarnessEventSource.sources.delete(this);
    this.readyState = HarnessEventSource.CLOSED;
  }
}

window.EventSource = HarnessEventSource as unknown as typeof EventSource;

// Mutable mock service state; Python fixture methods change it before mounting React.
let previewAction: ActionRequestView | null = null;
let queueOpenResponses = false;
let queuedOpenSessionId: string | null = null;

function projectQueuedSession(): void {
  if (!queuedOpenSessionId) throw new Error("No queued Session Open to project");
  const sessionId = queuedOpenSessionId;
  const source = THREADS[0];
  if (!source) throw new Error("No Session fixture to copy");
  const projected: ThreadView = {
    ...source,
    id: sessionId,
    name: "Queued opening thread",
    created_at: ago(0),
    last_cursor: 0,
    last_event_at: ago(0),
    harness_state: "HARNESS_STATE_STOPPED",
    feed_status: "active",
  };
  THREADS.push(projected);
  THREADS_WITH_SANDBOXES.push(projected);
  HarnessEventSource.publish("/live/sandboxes/ready-sandbox");
  HarnessEventSource.publish("/live/threads");
}

function mount(element: ReactNode): void {
  const container = document.getElementById("app");
  if (!container) throw new Error("missing #app");
  createRoot(container).render(<ThemeProvider>{element}</ThemeProvider>);
}

/** Callable fixture builders, not a name-to-recipe dispatcher. Python owns their composition. */
const visualHarness = {
  mountApp(route: string): void {
    window.location.hash = route;
    mount(<App />);
  },
  asVmSandbox(name: string): void {
    const sandbox = SANDBOXES.find((candidate) => candidate.name === name);
    if (!sandbox?.pod) throw new Error(`Sandbox ${name} has no running Pod fixture`);
    sandbox.pod.owner_references = [
      {
        api_version: "kubevirt.io/v1",
        kind: "VirtualMachineInstance",
        name,
        uid: `${sandbox.uid}-vmi`,
        controller: true,
      },
    ];
  },
  nameThread(threadId: string, name: string): void {
    const thread = THREADS_WITH_SANDBOXES.find((candidate) => candidate.id === threadId);
    if (!thread) throw new Error(`Missing thread fixture ${threadId}`);
    thread.name = name;
  },
  updateThread(threadId: string, changes: Partial<ThreadView>): void {
    const thread = THREADS_WITH_SANDBOXES.find((candidate) => candidate.id === threadId);
    if (!thread) throw new Error(`Missing thread fixture ${threadId}`);
    Object.assign(thread, changes);
  },
  mountDisclosure(props: Parameters<typeof DisclosureVisual>[0]): void {
    mount(<DisclosureVisual {...props} />);
  },
  mountOperationPresentation(): void {
    mount(<OperationPresentationVisual />);
  },
  autosuspendCountdown,
  autosuspendBusy,
  autosuspendStarting,
  addProvisioningSandbox,
  addSuspendedSandboxes,
  queueOpenResponse(): void {
    queueOpenResponses = true;
  },
  projectQueuedSession,
  set(flag: Flag, on = true): void {
    if (on) flags.add(flag);
    else flags.delete(flag);
  },
  /** Sets `flag` and serves the open streams whose path ends in `pathSuffix` again. */
  publish(flag: Flag, pathSuffix: string): void {
    flags.add(flag);
    HarnessEventSource.publish(pathSuffix);
    publishNdjsonWatch(pathSuffix);
  },
  ageOutage(milliseconds: number): void {
    streamRegistry.now = () => Date.now() + milliseconds;
  },
  actionRendererKeys(): string[] {
    return Array.from(ACTION_RENDERERS, ([group, renderers]) =>
      Array.from(renderers.keys(), (name) => `${group}.${name}`)
    ).flat();
  },
  showActionPreview(action: ActionRequestView["action"], args: ActionRequestView["arguments"]): void {
    previewAction = {
      ...COMPACT_POD_ACTION,
      action,
      arguments: args,
      title:
        action.group === "kubernetes_admin" && action.name === "pods_list_in_namespace"
          ? "inspect running demo pods"
          : `review ${action.name.replaceAll("_", " ")}`,
      description: "Check the exact call before approving.",
    };
  },
  showActionResult(
    action: ActionRequestView["action"],
    args: ActionRequestView["arguments"],
    result: NonNullable<ActionRequestView["execution"]>["result"]
  ): void {
    ACTIONS[2]!.action = action;
    ACTIONS[2]!.arguments = args;
    ACTIONS[2]!.title = `Review ${action.name} result`;
    ACTIONS[2]!.execution!.result = result;
  },
  showActionReceipt(
    action: ActionRequestView["action"],
    args: ActionRequestView["arguments"],
    result: NonNullable<ActionRequestView["execution"]>["result"]
  ): void {
    receiptActionGroup =
      action.group === "sandbox" || MCP_GROUPS.some((group) => group.key === action.group)
        ? undefined
        : mcpGroup(action.group, "Test MCP backend for the receipt renderer.");
    const receipt = ACTIONS[2]!;
    ACTIONS.splice(0, ACTIONS.length, {
      ...receipt,
      action,
      arguments: args,
      title: "Long action text",
      description: null,
      execution: { ...receipt.execution!, result },
    });
  },
  longPendingAction(): void {
    ACTIONS[1]!.arguments = {
      host: "test-archive-host",
      user: "test-user",
      timeout_seconds: 60,
      command: Array.from({ length: 55 }, (_, index) => `echo review-step-${index + 1}`).join("\n"),
    };
  },
  hiddenCodepoints(): void {
    ACTIONS[1]!.arguments = {
      host: "test-archive-host",
      user: "test-user",
      timeout_seconds: 60,
      command: 'printf "review \u202Ereversed\u202C zero\u200Bwidth control\u001B"',
    };
    const result = {
      ...SSH_EXEC_RESULT,
      stdout: "review result: visible start \u202Ereversed\u202C, joined\u200Bword, and control\u001Bmarker",
      stderr: "stderr contains a zero-width\u200B separator and U+202E bidi override\u202C\n",
    };
    ACTIONS[2]!.execution!.result = {
      _meta: { "io.modelcontextprotocol/serverInfo": { name: "ssh-mcp", version: "4.0.3" } },
      content: [{ type: "text", text: JSON.stringify(result) }],
      structuredContent: result,
      isError: false,
    };
  },
  recovery(kind: "messages" | "tools" | "quiet"): void {
    threadEntityRows = (id) => recoveryRows(id, kind);
  },
  failedTurn(afterContent: boolean): void {
    mapThread = withFailedTurn;
    threadEntityRows = (id) => failedRows(id, afterContent);
  },
  blockedSession(reason: string): void {
    mapThread = (thread) =>
      thread.id === IDLE_THREAD ? { ...withFailedTurn(thread), name: "Blocked session" } : thread;
    threadEntityRows = (id) => [
      { ...viewState(8, null, {}, "harness-claude-model", reason), thread_id: id },
      ...failedRows(id, false, reason).filter((row) => row.entity_kind !== "view_state"),
    ];
  },
  dormantSession(): void {
    mapThread = withDormantSession;
    threadEntityRows = interleavedRows;
  },
  interleavedEvents(): void {
    threadEntityRows = interleavedRows;
  },
  lifecycleGroup(): void {
    threadEntityRows = lifecycleGroupRows;
  },
  threadSetup(): void {
    threadEntityRows = (id) => setupRows(id);
  },
  threadSetupRunning(): void {
    threadEntityRows = (id) => setupRows(id, true);
  },
  shellCalls(): void {
    threadEntityRows = shellCallRows;
  },
  markdownCodeFence(): void {
    threadEntityRows = codeFenceRows;
  },
  streamingInterleaved(): void {
    threadEntityRows = streamingInterleavedRows;
  },
  unfinishedReasoning(): void {
    threadEntityRows = unfinishedReasoningRows;
  },
  standaloneReasoning(longPreview: boolean, codeFence: boolean, longBody: boolean): void {
    threadEntityRows = (id) => standaloneReasoningRows(id, longPreview, codeFence, longBody);
  },
  standardHistory(longPreview: boolean, longBody: boolean): void {
    threadEntityRows = (id) => (id === THREADS[2].id ? statesRows(id) : standardRows(id, longPreview, longBody));
  },
  consecutiveReasoningHistory(): void {
    threadEntityRows = consecutiveReasoningRows;
  },
  commandProgress(): void {
    threadEntityRows = commandProgressRows;
  },
  pendingCommands(): void {
    threadEntityRows = (id) => statesRows(id);
  },
  pendingInputEcho(): void {
    threadEntityRows = pendingInputEchoRows;
  },
  commandOutcomes(): void {
    threadEntityRows = (id) => statesRows(id, true);
  },
  rememberPendingInput,
  rememberInputEcho,
  rememberSettledCommands,
  enableSuspendedSandboxWakeOnMessage,
  rememberDispatch,
  rememberShortRefusedWake,
  rememberFailedDispatch,
  completedRollout(): void {
    mapThread = (thread) =>
      thread.id === IDLE_THREAD
        ? {
            ...thread,
            name: "Completed diagnostic run",
            harness: "HARNESS_CODEX",
            active_turn_id: null,
            last_turn_status: "TURN_STATUS_COMPLETED",
          }
        : thread;
    threadEntityRows = (id) => realisticRolloutRows(id, rollout);
  },
  reportedRollout(): void {
    mapThread = (thread) =>
      thread.id === IDLE_THREAD
        ? {
            ...thread,
            name: "Reported thread excerpt",
            harness: "HARNESS_CODEX",
            active_turn_id: null,
            last_turn_status: "TURN_STATUS_COMPLETED",
          }
        : thread;
    threadEntityRows = (id) => realisticRolloutRows(id, reportedRollout);
  },
};
Object.assign(window, { agentplaneVisual: visualHarness });
