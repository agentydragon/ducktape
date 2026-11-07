import { AutosuspendBlockerSchema } from "../../../proto/environment_lifecycle_pb";
/**
 * Visual-test harness: Python configures canned data and explicitly mounts the app. No scene registry.
 * `fetch` (stubbed by network.ts, imported first so the
 * app's client captures the stub) answers API, Electric Shape, event stream and watch routes.
 */
import {
  electricLive,
  electricShape,
  electricSubset,
  liveEventStream,
  liveNdjsonWatch,
  ndjsonWatch,
  publishStreams,
  routes,
  type ServerSentEvent,
  UNANSWERED,
} from "./network";
import {
  ActionGroupSchema,
  ActionIdentitySchema,
  ActionRequestSchema,
  ListActionRequestHistoryResponseSchema,
  ActionState,
  ExecutorKind,
  McpLifecycle,
  McpHealthSchema,
  McpUnavailableReason,
  GetActionPolicyResponseSchema,
  Verdict,
  type ActionGroup,
  type ActionRequest,
} from "../../../action_service/action_service_pb";
import {
  PolicyBindingSchema,
  PolicySetSchema,
  WatchPolicyBindingsResponseSchema,
  WatchPolicySetsResponseSchema,
} from "../../../policy/v1/policy_service_pb";
import {
  Decision_Outcome,
  WatchCredentialsResponseSchema,
  WatchDecisionsResponseSchema,
  type Decision,
} from "../../../egress/egress_admin_pb";
import { decisionFixture, type DecisionFixture } from "../sandboxes/decision_fixture";
import { DurationSchema, TimestampSchema, ValueSchema, timestampFromDate } from "@bufbuild/protobuf/wkt";
import { ServiceAccountIdentitySchema } from "../../../proto/identity_pb";
import { OwnerReferenceSchema } from "../../../proto/kubernetes_pb";
import {
  SandboxPhase,
  EnvironmentDescriptorSchema,
  OperatingMode,
  ReadinessSchema,
  ReadinessState,
  type EnvironmentDescriptor,
} from "../../../environments/v1/environment_service_pb";

import { TEST_REASONING_EFFORTS } from "../testing/model_catalog";
import { testSandbox } from "../testing/sandbox";
import { acknowledgedQuiescingLifecycleFixture, runnerStatusFixture } from "../testing/runner_status";
import "@mantine/core/styles.css";

import { clone, create, fromJson, toJson, type JsonValue, type MessageInitShape } from "@bufbuild/protobuf";
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

import { type ThreadView, dispatchFromJson } from "../threads/client";
import { ThreadMetadataSchema } from "../../app_service_pb";
import { ACTION_RENDERERS } from "../actions/rendering/index";
import type { McpLinkageView } from "../mcp_linkage_client";
import {
  WatchInboxResponseSchema,
  WatchInboxEntriesResponseSchema,
  WatchSubscriptionsResponseSchema,
} from "../../../notification_service/notifications_pb";
import { EventSchema, ItemKind, RecoveryDisposition, TurnStatus, type Event } from "../../../proto/event_pb";
import { CommandSchema } from "../../../proto/command_pb";
import { EventEntrySchema } from "../../../proto/event_log_pb";
import {
  AttachedSchema,
  CentralSamplerV1PlacementSchema,
  EnvironmentHostedPlacementSchema,
  Harness,
  HarnessState,
  LaunchOutcome,
  ProcessState,
  SessionPlacementSchema,
  SessionSpecSchema,
  SessionExecutionState,
  SetupState,
} from "../../../proto/session_pb";
import {
  ReadThreadEntitiesRequestSchema,
  ReadThreadEntitiesResponseSchema,
  ThreadCommandOutcome,
  ThreadCommandSchema,
  ThreadConfirmedInputSchema,
  ThreadFeedStatus,
  ThreadItemSchema,
  ThreadItemToolCompletionSchema,
  ThreadItemTextCompletionSchema,
  ThreadLifecycleSchema,
  ThreadPayloadField,
  ThreadPayloadReferenceSchema,
  ThreadViewStateSchema,
  FeedStatus,
  SandboxRefSchema,
  SessionSummarySchema,
  WatchSessionSummariesResponseSchema,
  type ThreadEntity,
  type ThreadPayloadReference,
} from "../../../history_service/history_service_pb";
import { DisclosureVisual } from "./disclosure_visual";
import { OperationPresentationVisual } from "./operation_presentation_visual";
import { LocalCommands } from "../threads/local_commands";
import { electricEntityVariant } from "../threads/testing/electric_entity";
import { rememberRowHeight } from "../../../../util/frontend/transcript/history_sizes";
import { streamRegistry } from "../stream_status";
import { ThemeProvider } from "../theme";
import rollout from "./realistic_rollout.json";
import reportedRollout from "./reported_rollout.json";

const COMMAND_TARGET = { sandbox: "ready-sandbox", sandboxUid: "0f9c1d2e-0000-4000-8000-00000000a1b2" };

// Each test owns its local-command fixtures.
localStorage.clear();

// The visual harness freezes the wall clock before this bundle runs, so relative ages stay put.
const NOW = Date.now();
const HOUR = 3_600_000;

function ago(ms: number): string {
  return new Date(NOW - ms).toISOString();
}

function threadView(
  metadata: MessageInitShape<typeof ThreadMetadataSchema>,
  summary: MessageInitShape<typeof SessionSummarySchema>
): ThreadView {
  return {
    metadata: create(ThreadMetadataSchema, metadata),
    summary: create(SessionSummarySchema, {
      sessionId: metadata.threadId,
      foldedThrough: 0n,
      feedStatus: FeedStatus.UNSPECIFIED,
      ...summary,
    }),
  };
}

function visualSandbox(
  init: MessageInitShape<typeof EnvironmentDescriptorSchema>,
  ready = false
): EnvironmentDescriptor {
  const result = create(EnvironmentDescriptorSchema, init);
  const target = result.target;
  if (!target?.namespace || !target.name || !target.uid) throw new Error("Visual Environment needs an exact target");
  const base = testSandbox(target.name, target.uid, target.namespace);
  if (result.executionAccount && !result.executionAccount.uid) result.executionAccount.uid = base.executionAccount!.uid;
  result.observedState!.readiness = create(ReadinessSchema, {
    state: ready ? ReadinessState.READY : ReadinessState.NOT_READY,
    reason: ready ? "PodReady" : "NotReady",
  });
  return result;
}

const SANDBOXES: EnvironmentDescriptor[] = [
  visualSandbox(
    {
      target: { name: "ready-sandbox", uid: "0f9c1d2e-0000-4000-8000-00000000a1b2", namespace: "agentplane-visual" },
      observedState: {
        createdAt: timestampFromDate(new Date(ago(3 * HOUR))),
        operatingMode: OperatingMode.RUNNING,
        phase: SandboxPhase.RUNNING,
      },
      executionAccount: { account: { namespace: "agentplane-visual", name: "ready-sandbox" } },
      configuration: { wakeOnSend: true, idleSuspendAfter: { seconds: 3600n } },
      kubernetesDiagnostics: {
        sandboxStatus: {
          conditions: [{ type: "Ready", status: "True", reason: "PodReady", lastTransitionTime: ago(HOUR) }],
        },
        pod: {
          name: "ready-sandbox",
          namespace: "agentplane-visual",
          uid: "visual-pod-a1b2",
          deleting: false,
          ownerReferences: [
            {
              apiVersion: "agentplane.allegedly.works/v1alpha1",
              kind: "Sandbox",
              name: "ready-sandbox",
              uid: "0f9c1d2e-0000-4000-8000-00000000a1b2",
              controller: true,
            },
          ],
          nodeName: "harness-node",
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
    },
    true
  ),
  visualSandbox({
    target: { name: "pending-sandbox", uid: "0f9c1d2e-0000-4000-8000-00000000c3d4", namespace: "agentplane-visual" },
    observedState: {
      createdAt: timestampFromDate(new Date(ago(2 * 60_000))),
      operatingMode: OperatingMode.RUNNING,
      phase: SandboxPhase.RUNNING,
    },
    executionAccount: { account: { namespace: "agentplane-visual", name: "pending-sandbox" } },
    configuration: { wakeOnSend: false },
    kubernetesDiagnostics: {
      sandboxStatus: { conditions: [{ type: "Ready", status: "False", reason: "PodPending" }] },
      pod: {
        name: "pending-sandbox",
        namespace: "agentplane-visual",
        uid: "visual-pod-c3d4",
        deleting: false,
        ownerReferences: [
          {
            apiVersion: "agentplane.allegedly.works/v1alpha1",
            kind: "Sandbox",
            name: "pending-sandbox",
            uid: "0f9c1d2e-0000-4000-8000-00000000c3d4",
            controller: true,
          },
        ],
        nodeName: "harness-node",
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
  }),
  visualSandbox({
    target: { name: "suspended-sandbox", uid: "0f9c1d2e-0000-4000-8000-00000000e5f6", namespace: "agentplane-visual" },
    observedState: {
      createdAt: timestampFromDate(new Date(ago(48 * HOUR))),
      operatingMode: OperatingMode.SUSPENDED,
      phase: SandboxPhase.SUSPENDED,
    },
    executionAccount: { account: { namespace: "agentplane-visual", name: "suspended-sandbox" } },
    configuration: { wakeOnSend: false },
    kubernetesDiagnostics: {
      sandboxStatus: { conditions: [{ type: "Ready", status: "False", reason: "Suspended" }] },
    },
  }),
];

/** One Sandbox suspended because its runner was idle, and one because a requester suspended it. */
function addSuspendedSandboxes(): void {
  SANDBOXES.push(
    visualSandbox({
      target: {
        name: "idle-suspended-sandbox",
        uid: "0f9c1d2e-0000-4000-8000-00000000b7c8",
        namespace: "agentplane-visual",
      },
      observedState: {
        createdAt: timestampFromDate(new Date(ago(30 * HOUR))),
        operatingMode: OperatingMode.SUSPENDED,
        suspendedBy: "Idle",
        phase: SandboxPhase.SUSPENDED,
      },
      executionAccount: { account: { namespace: "agentplane-visual", name: "idle-suspended-sandbox" } },
      configuration: { wakeOnSend: true, idleSuspendAfter: { seconds: 1800n } },
      kubernetesDiagnostics: { sandboxStatus: { conditions: [{ type: "Ready", status: "False", reason: "Stopped" }] } },
    }),
    visualSandbox({
      target: {
        name: "requested-suspended-sandbox",
        uid: "0f9c1d2e-0000-4000-8000-00000000d9e0",
        namespace: "agentplane-visual",
      },
      observedState: {
        createdAt: timestampFromDate(new Date(ago(26 * HOUR))),
        operatingMode: OperatingMode.SUSPENDED,
        suspendedBy: "Requested",
        phase: SandboxPhase.SUSPENDED,
      },
      executionAccount: { account: { namespace: "agentplane-visual", name: "requested-suspended-sandbox" } },
      configuration: { wakeOnSend: false, idleSuspendAfter: { seconds: 7200n } },
      kubernetesDiagnostics: { sandboxStatus: { conditions: [{ type: "Ready", status: "False", reason: "Stopped" }] } },
    })
  );
}

function addProvisioningSandbox(): void {
  SANDBOXES.push(
    visualSandbox({
      target: {
        name: "test-provisioning",
        uid: "0f9c1d2e-0000-4000-8000-000000000007",
        namespace: "agentplane-visual",
      },
      observedState: {
        createdAt: timestampFromDate(new Date(ago(30_000))),
        operatingMode: OperatingMode.RUNNING,
        phase: SandboxPhase.CREATING,
      },
      executionAccount: { account: { namespace: "agentplane-visual", name: "test-provisioning" } },
      configuration: { wakeOnSend: false },
      kubernetesDiagnostics: { sandboxStatus: {} },
    })
  );
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

const POLICY_SETS = [
  create(PolicySetSchema, {
    identity: { namespace: "agentplane-visual", name: "github-public", uid: "set-github" },
    generation: 1n,
    spec: {
      rules: [
        {
          type: "egress",
          hosts: ["api.github.com", "github.com"],
          methods: ["GET", "POST"],
          credentialRef: { name: "harness-github-pat" },
        },
      ],
    },
  }),
  create(PolicySetSchema, {
    identity: { namespace: "agentplane-visual", name: "pypi", uid: "set-pypi" },
    generation: 1n,
    spec: {
      rules: [
        { type: "egress", hosts: ["pypi.org", "files.pythonhosted.org"], methods: ["GET"], paths: ["/simple/**"] },
      ],
    },
  }),
  ...["test-coder", "harness-reviews", "harness-push", "harness-edited"].map((name) =>
    fromJson(PolicySetSchema, {
      identity: { namespace: "agentplane-visual", name, uid: `set-${name}` },
      generation: "1",
      spec: {
        rules: [
          {
            type: "action_auto_approve",
            policy: { type: "exact_actions", actions: { github: ["get_file_contents"] } },
          },
        ],
      },
    })
  ),
];

const BINDINGS = [
  { name: "ready-sandbox-k2m9x", source: "sandbox", sets: ["test-coder", "harness-reviews"], state: "True" },
  { name: "ready-sandbox-github-public", source: "git", sets: ["github-public"], state: "True" },
  { name: "ready-sandbox-7q4xk", source: "operator", sets: ["pypi"], state: "True", expires: ago(2 * HOUR) },
  {
    name: "ready-sandbox-unjudged-grant",
    source: "operator",
    sets: ["harness-reviews"],
    state: null,
    expires: ago(-HOUR),
  },
  {
    name: "ready-sandbox-push-afternoon",
    source: "operator",
    sets: ["harness-push", "harness-edited", "harness-release"],
    state: "False",
    expires: ago(-3 * HOUR),
  },
].map(({ name, source, sets, state, expires }) =>
  create(PolicyBindingSchema, {
    identity: { namespace: "agentplane-visual", name, uid: `binding-${name}` },
    generation: 1n,
    labels: source === "git" ? { "kustomize.toolkit.fluxcd.io/name": "harness" } : {},
    ownerReferences:
      source === "sandbox"
        ? [
            {
              apiVersion: "agentplane.allegedly.works/v1alpha1",
              kind: "Sandbox",
              name: "ready-sandbox",
              uid: "sandbox-uid",
              controller: true,
            },
          ]
        : [],
    spec: {
      subject: { namespace: "agentplane-visual", name: "ready-sandbox" },
      policySets: sets,
      ...(expires ? { expiresAt: timestampFromDate(new Date(expires)) } : {}),
    },
    status: {
      conditions: state
        ? [{ type: "ActionsReady", status: state, reason: "Valid", message: "spec accepted", observedGeneration: 1n }]
        : [],
    },
  })
);

/** What the Action Service auto-decides for ready-sandbox, from the sets those bindings name. */
function actionPolicy(namespace: string, name: string) {
  const policy = (binding: string, policySet: string, index: number, actionNames: string[]) => ({
    binding,
    policySet,
    index,
    policy: { kind: { case: "exactActions" as const, value: { actions: { github: { values: actionNames } } } } },
  });
  return create(GetActionPolicyResponseSchema, {
    actionPolicy: {
      subject: { namespace, name },
      synced: true,
      autoApproveIf: [
        policy("ready-sandbox-k2m9x", "test-coder", 0, ["get_file_contents", "list_commits", "search_code"]),
        {
          binding: "ready-sandbox-k2m9x",
          policySet: "test-coder",
          index: 1,
          policy: {
            kind: {
              case: "argumentSchema",
              value: {
                actions: { github: { values: ["create_issue"] } },
                argumentSchema: {
                  properties: { owner: { const: "harness-owner" }, repo: { const: "harness-repo" } },
                  required: ["owner", "repo"],
                },
              },
            },
          },
        },
        policy("ready-sandbox-k2m9x", "harness-reviews", 0, ["pull_request_read", "list_pull_requests"]),
        {
          binding: "ready-sandbox-push-afternoon",
          policySet: "harness-push",
          index: 0,
          policy: {
            kind: {
              case: "argumentSchema",
              value: {
                actions: { github: { values: ["push_files"] } },
                argumentSchema: { properties: { branch: { pattern: "^harness/" } }, required: ["branch"] },
              },
            },
          },
        },
      ],
    },
  });
}

const GITHUB_ADMITTED = {
  host: "api.github.com",
  outcome: Decision_Outcome.ALLOW,
  address: "140.82.116.5",
  binding: "ready-sandbox-github-public",
  policy: "github-public",
  rule: 0,
} satisfies Omit<DecisionFixture, "at">;

const PYPI_REFUSED = {
  host: "pypi.org",
  outcome: Decision_Outcome.DENY,
  reason: "no-rule",
} satisfies Omit<DecisionFixture, "at">;

// Oldest first, as the proxy sends the whole retained log.
const DECISIONS: Decision[] = [
  decisionFixture({ ...GITHUB_ADMITTED, at: ago(9 * 60_000), method: "CONNECT" }),
  decisionFixture({ ...GITHUB_ADMITTED, at: ago(9 * 60_000 - 200), substituted: true }),
  decisionFixture({ ...GITHUB_ADMITTED, at: ago(7 * 60_000), substituted: true }),
  decisionFixture({ ...GITHUB_ADMITTED, at: ago(6 * 60_000), substituted: true }),
  decisionFixture({ ...PYPI_REFUSED, at: ago(5 * 60_000) }),
  decisionFixture({ ...PYPI_REFUSED, at: ago(4 * 60_000) }),
  decisionFixture({ ...PYPI_REFUSED, at: ago(3 * 60_000) }),
  decisionFixture({
    at: ago(60_000),
    host: "example.invalid",
    method: "CONNECT",
    outcome: Decision_Outcome.DENY,
    reason: "no-binding",
  }),
];

const IDLE_THREAD = "5f1c4a2e-0000-4000-8000-000000000001";
const UNNAMED_STOPPED_THREAD = "5f1c4a2e-0000-4000-8000-000000000000";
const RUNNING_THREAD = "5f1c4a2e-0000-4000-8000-000000000002";
const SUSPENDED_THREAD = "5f1c4a2e-0000-4000-8000-000000000004";
const THREE_UNPROCESSED_THREAD = "5f1c4a2e-0000-4000-8000-000000000007";
const ONE_UNPROCESSED_THREAD = "5f1c4a2e-0000-4000-8000-000000000008";
const DELIVERY_FAILED_THREAD = "5f1c4a2e-0000-4000-8000-000000000009";

const ACTION_REQUEST_ID = "d49b85b5-849f-4e7d-a644-d4a8b8c16127";

// Each fake watch emits the actual generated resource and watch messages.
function notificationFrames(session: string, kind: string, delta: boolean): unknown[] {
  const extra = flags.has("notificationExtraEntry") && session === IDLE_THREAD;
  const badge =
    session === THREE_UNPROCESSED_THREAD
      ? { unannouncedCount: 2, pendingAcknowledgementCount: 3 }
      : session === ONE_UNPROCESSED_THREAD
        ? { pendingAcknowledgementCount: 1 }
        : session === DELIVERY_FAILED_THREAD
          ? { unannouncedCount: 1, pendingAcknowledgementCount: 1 }
          : undefined;
  const first = session === IDLE_THREAD;
  const second = session === UNNAMED_STOPPED_THREAD;
  const third = session === RUNNING_THREAD;
  if (kind === "inbox:watch") {
    const inbox = first
      ? {
          id: "inbox-one",
          sessionId: session,
          lastCursor: extra ? "10" : "9",
          acknowledged: "4",
          covered: "7",
          latestNotice: { throughCursor: "7", attempted: true, admitted: true },
          delivery: {
            unannouncedCount: extra ? 3 : 2,
            pendingAcknowledgementCount: extra ? 6 : 5,
            waitReason: "NOTICE_WAIT_REASON_AWAITING_CONFIRMATION",
            nextWorkAt: ago(-5000),
          },
        }
      : second
        ? {
            id: "inbox-two",
            sessionId: session,
            lastCursor: "3",
            acknowledged: "1",
            covered: "1",
            delivery: {
              unannouncedCount: 2,
              pendingAcknowledgementCount: 2,
              waitReason: "NOTICE_WAIT_REASON_DEBOUNCING",
              noticeDueAt: ago(-60000),
              quietUntil: ago(-60000),
              maxWaitAt: ago(-120000),
            },
          }
        : third
          ? {
              id: "inbox-three",
              sessionId: session,
              lastCursor: "2",
              covered: "2",
              deliveryError: "runner unavailable",
              latestNotice: { throughCursor: "2", attempted: true },
              delivery: { pendingAcknowledgementCount: 2, waitReason: "NOTICE_WAIT_REASON_DELIVERY_RETRY" },
            }
          : badge
            ? {
                id: `inbox-${session}`,
                sessionId: session,
                delivery: badge,
                ...(session === DELIVERY_FAILED_THREAD
                  ? { deliveryError: "runner unreachable" }
                  : { latestNotice: { confirmed: true } }),
              }
            : undefined;
    return [
      toJson(
        WatchInboxResponseSchema,
        fromJson(WatchInboxResponseSchema, JSON.parse(JSON.stringify(inbox ? { inbox } : { noInbox: {} })))
      ),
    ];
  }
  if (kind === "inbox/entries:watch") {
    const action = (cursor: number, state: string) => ({
      cursor: String(cursor),
      createdAt: ago(0),
      event: { actions: { requestId: ACTION_REQUEST_ID, sequence: cursor } },
      actionState: `ACTION_STATE_${state}`,
    });
    const github = (cursor: number, event: string, actionName: string, conclusion?: string) => ({
      cursor: String(cursor),
      createdAt: ago(0),
      event: { github: { event: `GITHUB_EVENT_NAME_${event}`, action: actionName } },
      githubConclusion: conclusion,
    });
    const entries = first
      ? [
          action(5, "DECISION_PENDING"),
          github(6, "CHECK_RUN", "completed", "success"),
          github(7, "ISSUE_COMMENT", "created"),
          action(8, "RUNNING"),
          action(9, "SUCCEEDED"),
          ...(extra ? [github(10, "WORKFLOW_RUN", "completed")] : []),
        ]
      : second
        ? [github(2, "CHECK_RUN", "completed", "failure"), github(3, "PULL_REQUEST", "synchronize")]
        : third
          ? [action(1, "DECISION_PENDING"), action(2, "RUNNING")]
          : [];
    const frame = delta ? { delta: { upserted: entries } } : { snapshot: { entries } };
    return [
      toJson(
        WatchInboxEntriesResponseSchema,
        fromJson(WatchInboxEntriesResponseSchema, JSON.parse(JSON.stringify(frame)))
      ),
    ];
  }
  const githubSubscription = (id: string, repository: string, number: number, kind?: string, cancelled = false) => ({
    id,
    source: { github: { repository, subject: { pullRequest: { number } } } },
    cancelled,
    expiresAt: ago(cancelled ? HOUR : -HOUR),
    lastSuccessAt: ago(60000),
    github: {
      access: [
        {
          appId: "42",
          installationId: "11",
          repositoryId: "102",
          lastSuccessAt: ago(60000),
          ...(kind
            ? {
                errorKind: `SOURCE_FAILURE_KIND_${kind}`,
                error:
                  kind === "RATE_LIMITED"
                    ? "GitHub rate limited (HTTP 429)"
                    : "GitHub App access unavailable (HTTP 403)",
                errorSince: ago(30000),
                errorObservedAt: ago(10000),
                retryAt: ago(-60000),
              }
            : {}),
        },
      ],
      subject: { repositoryId: "102", subject: { pullRequest: { number } }, lastSuccessAt: ago(60000) },
    },
  });
  const subscriptions = first
    ? [
        {
          id: "subscription-one",
          source: { actions: { requestId: ACTION_REQUEST_ID } },
          expiresAt: ago(-HOUR),
          lastSuccessAt: ago(60000),
          nextSourceCheckAt: ago(-30000),
        },
        githubSubscription("subscription-two", "agentydragon/ducktape", 42, undefined, true),
        githubSubscription("subscription-failure-1", "owner/repo", 1, "RATE_LIMITED"),
        githubSubscription("subscription-failure-2", "owner/repo", 2, "ACCESS_DENIED"),
      ]
    : [];
  const frame = delta ? { delta: { upserted: subscriptions } } : { snapshot: { subscriptions } };
  return [
    toJson(
      WatchSubscriptionsResponseSchema,
      fromJson(WatchSubscriptionsResponseSchema, JSON.parse(JSON.stringify(frame)))
    ),
  ];
}

function visualNativeSpec(harness: Harness, sandboxName: string) {
  const target = SANDBOXES.find((candidate) => candidate.target?.name === sandboxName)?.target;
  if (!target) throw new Error(`Missing Sandbox fixture ${sandboxName}`);
  return create(SessionSpecSchema, {
    placement: {
      kind: {
        case: "environmentHosted",
        value: { environment: { name: target.name, uid: target.uid }, harness },
      },
    },
    model: harness === Harness.CODEX ? "harness-codex-model" : "harness-claude-model",
    cwd: "/state/work",
  });
}

/** The store's copy of the sessions: the idle one named, the stopped one not, so both renderings are on the page. */
const THREADS: ThreadView[] = [
  threadView(
    {
      threadId: IDLE_THREAD,
      createdAt: timestampFromDate(new Date(ago(HOUR))),
      name: "Idle thread",
      archived: false,
    },
    {
      foldedThrough: 14n,
      firstEventAt: timestampFromDate(new Date(ago(HOUR))),
      lastEventAt: timestampFromDate(new Date(ago(60_000))),
      feedStatus: FeedStatus.ACTIVE,
      sandbox: { namespace: "agentplane-visual", name: "ready-sandbox" },
      attached: {
        sessionId: IDLE_THREAD,
        spec: visualNativeSpec(Harness.CLAUDE, "ready-sandbox"),
        lastCursor: 14n,
        harnessState: HarnessState.RUNNING,
      },
    }
  ),
  threadView(
    {
      threadId: UNNAMED_STOPPED_THREAD,
      createdAt: timestampFromDate(new Date(ago(2 * HOUR))),
      archived: false,
    },
    {
      foldedThrough: 31n,
      firstEventAt: timestampFromDate(new Date(ago(2 * HOUR))),
      lastEventAt: timestampFromDate(new Date(ago(90 * 60_000))),
      feedStatus: FeedStatus.ACTIVE,
      sandbox: { namespace: "agentplane-visual", name: "ready-sandbox" },
      attached: {
        sessionId: UNNAMED_STOPPED_THREAD,
        spec: visualNativeSpec(Harness.CLAUDE, "ready-sandbox"),
        lastCursor: 31n,
        harnessState: HarnessState.STOPPED,
      },
    }
  ),
  threadView(
    {
      threadId: RUNNING_THREAD,
      createdAt: timestampFromDate(new Date(ago(30 * 60_000))),
      name: "Running thread",
      archived: false,
    },
    {
      foldedThrough: 23n,
      firstEventAt: timestampFromDate(new Date(ago(30 * 60_000))),
      lastEventAt: timestampFromDate(new Date(ago(10_000))),
      feedStatus: FeedStatus.ACTIVE,
      sandbox: { namespace: "agentplane-visual", name: "ready-sandbox" },
      attached: {
        sessionId: RUNNING_THREAD,
        spec: visualNativeSpec(Harness.CLAUDE, "ready-sandbox"),
        lastCursor: 23n,
        harnessState: HarnessState.RUNNING,
        activeTurnId: "t2",
      },
    }
  ),
];

/**
 * The sidebar's cross-sandbox fixture: the three THREADS above and one per notice badge state
 * (all `ready-sandbox`), one each for the pending and suspended sandboxes, one archived, and one whose
 * `sandbox` names no live EnvironmentDescriptor at all -- the struck-through, read-only group.
 */
const THREADS_WITH_SANDBOXES: ThreadView[] = [
  ...THREADS,
  threadView(
    {
      threadId: THREE_UNPROCESSED_THREAD,
      createdAt: timestampFromDate(new Date(ago(3 * HOUR))),
      name: "Three unprocessed thread",
      archived: false,
    },
    {
      foldedThrough: 7n,
      firstEventAt: timestampFromDate(new Date(ago(3 * HOUR))),
      lastEventAt: timestampFromDate(new Date(ago(2 * 60_000))),
      feedStatus: FeedStatus.ACTIVE,
      sandbox: { namespace: "agentplane-visual", name: "ready-sandbox" },
      attached: {
        sessionId: THREE_UNPROCESSED_THREAD,
        spec: visualNativeSpec(Harness.CLAUDE, "ready-sandbox"),
        lastCursor: 7n,
        harnessState: HarnessState.RUNNING,
      },
    }
  ),
  threadView(
    {
      threadId: ONE_UNPROCESSED_THREAD,
      createdAt: timestampFromDate(new Date(ago(3 * HOUR))),
      name: "One unprocessed thread",
      archived: false,
    },
    {
      foldedThrough: 7n,
      firstEventAt: timestampFromDate(new Date(ago(3 * HOUR))),
      lastEventAt: timestampFromDate(new Date(ago(3 * 60_000))),
      feedStatus: FeedStatus.ACTIVE,
      sandbox: { namespace: "agentplane-visual", name: "ready-sandbox" },
      attached: {
        sessionId: ONE_UNPROCESSED_THREAD,
        spec: visualNativeSpec(Harness.CLAUDE, "ready-sandbox"),
        lastCursor: 7n,
        harnessState: HarnessState.RUNNING,
      },
    }
  ),
  threadView(
    {
      threadId: DELIVERY_FAILED_THREAD,
      createdAt: timestampFromDate(new Date(ago(3 * HOUR))),
      name: "Delivery-failed thread",
      archived: false,
    },
    {
      foldedThrough: 7n,
      firstEventAt: timestampFromDate(new Date(ago(3 * HOUR))),
      lastEventAt: timestampFromDate(new Date(ago(4 * 60_000))),
      feedStatus: FeedStatus.ACTIVE,
      sandbox: { namespace: "agentplane-visual", name: "ready-sandbox" },
      attached: {
        sessionId: DELIVERY_FAILED_THREAD,
        spec: visualNativeSpec(Harness.CLAUDE, "ready-sandbox"),
        lastCursor: 7n,
        harnessState: HarnessState.STOPPED,
      },
    }
  ),
  threadView(
    {
      threadId: "5f1c4a2e-0000-4000-8000-000000000003",
      createdAt: timestampFromDate(new Date(ago(5 * 60_000))),
      name: "Pending thread",
      archived: false,
    },
    {
      foldedThrough: 2n,
      sandbox: { namespace: "agentplane-visual", name: "pending-sandbox" },
      attached: {
        sessionId: "5f1c4a2e-0000-4000-8000-000000000003",
        spec: visualNativeSpec(Harness.CODEX, "pending-sandbox"),
        lastCursor: 2n,
        harnessState: HarnessState.STOPPED,
      },
    }
  ),
  threadView(
    {
      threadId: SUSPENDED_THREAD,
      createdAt: timestampFromDate(new Date(ago(47 * HOUR))),
      name: "Suspended thread",
      archived: false,
    },
    {
      foldedThrough: 9n,
      firstEventAt: timestampFromDate(new Date(ago(47 * HOUR))),
      lastEventAt: timestampFromDate(new Date(ago(46 * HOUR))),
      feedStatus: FeedStatus.ACTIVE,
      sandbox: { namespace: "agentplane-visual", name: "suspended-sandbox" },
      attached: {
        sessionId: SUSPENDED_THREAD,
        spec: visualNativeSpec(Harness.CLAUDE, "suspended-sandbox"),
        lastCursor: 9n,
        harnessState: HarnessState.STOPPED,
        setupState: SetupState.SUCCEEDED,
        launchOutcome: LaunchOutcome.STARTED,
      },
    }
  ),
  threadView(
    {
      threadId: "5f1c4a2e-0000-4000-8000-000000000005",
      createdAt: timestampFromDate(new Date(ago(72 * HOUR))),
      name: "Read-only thread",
      archived: false,
    },
    {
      foldedThrough: 18n,
      firstEventAt: timestampFromDate(new Date(ago(72 * HOUR))),
      lastEventAt: timestampFromDate(new Date(ago(70 * HOUR))),
      feedStatus: FeedStatus.ACTIVE,
      sandbox: { namespace: "agentplane-visual", name: "deleted-sandbox" },
      attached: {
        sessionId: "5f1c4a2e-0000-4000-8000-000000000005",
        historicalHarness: Harness.CLAUDE,
        lastCursor: 18n,
        harnessState: HarnessState.STOPPED,
      },
    }
  ),
  threadView(
    {
      threadId: "5f1c4a2e-0000-4000-8000-000000000006",
      createdAt: timestampFromDate(new Date(ago(96 * HOUR))),
      name: "Archived thread",
      archived: true,
    },
    {
      foldedThrough: 3n,
      firstEventAt: timestampFromDate(new Date(ago(96 * HOUR))),
      lastEventAt: timestampFromDate(new Date(ago(95 * HOUR))),
      feedStatus: FeedStatus.ACTIVE,
      sandbox: { namespace: "agentplane-visual", name: "ready-sandbox" },
      attached: {
        sessionId: "5f1c4a2e-0000-4000-8000-000000000006",
        spec: visualNativeSpec(Harness.CLAUDE, "ready-sandbox"),
        lastCursor: 3n,
        harnessState: HarnessState.STOPPED,
      },
    }
  ),
];

for (const thread of THREADS_WITH_SANDBOXES) {
  const sandbox = SANDBOXES.find((candidate) => candidate.target?.name === thread.summary.sandbox?.name);
  if (sandbox?.target?.uid && thread.summary.sandbox) thread.summary.sandbox.uid = sandbox.target.uid;
}

function withDormantSession(thread: ThreadView): ThreadView {
  return thread.metadata.threadId === RUNNING_THREAD
    ? {
        ...thread,
        metadata: { ...thread.metadata, name: "Dormant harness thread" },
        summary: {
          ...thread.summary,
          feedStatus: FeedStatus.ACTIVE,
          attached: { ...thread.summary.attached!, harnessState: HarnessState.STOPPED, activeTurnId: "" },
        },
      }
    : thread;
}

/** The `failedTurn` scenarios: the idle thread's history ends in a failed turn, which is also its last completed one. */
function withFailedTurn(thread: ThreadView): ThreadView {
  return thread.metadata.threadId === IDLE_THREAD
    ? {
        ...thread,
        metadata: { ...thread.metadata, name: "Failed-turn thread" },
        summary: { ...thread.summary, lastTurnStatus: TurnStatus.FAILED },
      }
    : thread;
}

function autosuspendCountdown(): void {
  const sandbox = SANDBOXES.find((candidate) => candidate.target?.name === "ready-sandbox");
  if (!sandbox) throw new Error("Missing ready Sandbox fixture");
  sandbox.configuration!.idleSuspendAfter = create(DurationSchema, { seconds: 900n });
  sandbox.observedState!.idleSuspendAt = timestampFromDate(new Date(ago(-12 * 60_000)));
  threadEntityRows = interleavedRows;
  mapThread = (thread) =>
    thread.metadata.threadId === IDLE_THREAD
      ? {
          ...thread,
          metadata: { ...thread.metadata, name: "Idle thread awaiting autosuspend" },
          summary: {
            ...thread.summary,
            lastModelActivityAt: timestampFromDate(new Date(ago(13 * 60_000 + 25_000))),
            idleSince: timestampFromDate(new Date(ago(13 * 60_000 + 5_000))),
            attached: { ...thread.summary.attached!, activeTurnId: "" },
          },
        }
      : thread;
}

function autosuspendBusy(): void {
  autosuspendCountdown();
  const sandbox = SANDBOXES.find((candidate) => candidate.target?.name === "ready-sandbox");
  if (!sandbox) throw new Error("Missing ready Sandbox fixture");
  // Exercise busy facts taking precedence over a retained idle deadline.
  sandbox.observedState!.autosuspendBlockers = [
    create(AutosuspendBlockerSchema, { reason: "ActiveTurn", sessionId: RUNNING_THREAD }),
    create(AutosuspendBlockerSchema, { reason: "Setup", sessionId: UNNAMED_STOPPED_THREAD }),
    create(AutosuspendBlockerSchema, { reason: "Bootstrap" }),
  ];
  const countdownThread = mapThread;
  mapThread = (thread) => {
    const mapped = countdownThread(thread);
    if (thread.metadata.threadId === IDLE_THREAD)
      return { ...mapped, metadata: { ...mapped.metadata, name: "Idle thread sharing a busy Sandbox" } };
    if (thread.metadata.threadId === RUNNING_THREAD)
      return { ...mapped, metadata: { ...mapped.metadata, name: "Session running a turn" } };
    if (thread.metadata.threadId === UNNAMED_STOPPED_THREAD) {
      return {
        ...mapped,
        metadata: { ...mapped.metadata, name: "Session running setup" },
        summary: { ...mapped.summary, attached: { ...mapped.summary.attached!, setupState: SetupState.RUNNING } },
      };
    }
    return mapped;
  };
}

function autosuspendStarting(): void {
  autosuspendCountdown();
  const ready = SANDBOXES.find((candidate) => candidate.target?.name === "ready-sandbox");
  if (!ready) throw new Error("Missing ready Sandbox fixture");
  SANDBOXES.push(
    Object.assign(clone(EnvironmentDescriptorSchema, ready), {
      target: { ...ready.target!, name: "starting-sandbox", uid: "0f9c1d2e-0000-4000-8000-000000000077" },
      executionAccount: create(ServiceAccountIdentitySchema, {
        account: { namespace: ready.target!.namespace, name: "starting-sandbox" },
        uid: "starting-account",
      }),
      observedState: {
        ...ready.observedState,
        phase: SandboxPhase.CREATING,
        idleSuspendAt: undefined,
        autosuspendBlockers: [],
      },
      kubernetesDiagnostics: { ...ready.kubernetesDiagnostics, pod: undefined, sandboxStatus: {} },
    })
  );
  const countdownThread = mapThread;
  mapThread = (thread) => {
    const mapped = countdownThread(thread);
    return thread.metadata.threadId === IDLE_THREAD
      ? {
          ...mapped,
          metadata: { ...mapped.metadata, name: "Thread waiting for Sandbox startup" },
          summary: {
            ...mapped.summary,
            sandbox: {
              ...mapped.summary.sandbox!,
              name: "starting-sandbox",
              uid: "0f9c1d2e-0000-4000-8000-000000000077",
            },
            feedStatus: FeedStatus.ENDED,
            attached: { ...mapped.summary.attached!, harnessState: HarnessState.STOPPED },
          },
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

const ACTIONS: ActionRequest[] = [
  fromJson(ActionRequestSchema, {
    id: "70000000-0000-4000-8000-000000000001",
    action: { group: "everything", name: "echo" },
    arguments: { repository: "test-owner/test-repository", token: "test-token" },
    title: "echo the test repository handle back",
    description: "Confirms the fixture connection still reaches the echo Action before the demo run.",
    idempotencyKey: "visual-pending",
    caller: { namespace: "agentplane-visual", name: "test-coder" },
    callerUid: null,
    state: "ACTION_STATE_DECISION_PENDING",
    version: "1",
    createdAt: ago(3 * 60_000),
    updatedAt: ago(3 * 60_000),
    decision: null,
    execution: null,
  }),
  fromJson(ActionRequestSchema, {
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
    idempotencyKey: "visual-ssh-pending",
    caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
    callerUid: null,
    state: "ACTION_STATE_DECISION_PENDING",
    version: "1",
    createdAt: ago(2 * 60_000),
    updatedAt: ago(2 * 60_000),
    decision: null,
    execution: null,
  }),
  fromJson(ActionRequestSchema, {
    id: "70000000-0000-4000-8000-000000000007",
    action: { group: "ssh", name: "exec" },
    arguments: {
      host: "test-archive-host",
      user: "test-user",
      command: 'ls -l "$HOME/test-archive" "$HOME/test-archive/test-missing"',
    },
    title: "list the test backup archive",
    description: null,
    idempotencyKey: "visual-ssh-completed",
    caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
    callerUid: null,
    state: "ACTION_STATE_SUCCEEDED",
    version: "4",
    createdAt: ago(5 * 60_000),
    updatedAt: ago(4 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000007",
      verdict: "VERDICT_ALLOW",
      provider: "human_operator",
      operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
      decisionNote: null,
      reasonCode: null,
      reasonDescription: null,
      policyEvidence: null,
      idempotencyKey: "visual-allow-ssh",
      decidedAt: ago(4 * 60_000),
    },
    execution: {
      id: "72000000-0000-4000-8000-000000000007",
      state: "EXECUTION_STATE_SUCCEEDED",
      // The whole CallToolResult, as FastMCP answers with the ExecResult: the value as structured
      // content and again as one JSON text block, and the server's info in `_meta`.
      result: {
        _meta: { "io.modelcontextprotocol/serverInfo": { name: "ssh-mcp", version: "4.0.3" } },
        content: [{ type: "text", text: JSON.stringify(SSH_EXEC_RESULT) }],
        structuredContent: SSH_EXEC_RESULT,
        isError: false,
      },
      error: null,
      createdAt: ago(4 * 60_000),
      startedAt: ago(4 * 60_000 - 500),
      completedAt: ago(4 * 60_000 - 1_900),
      reconciledAt: null,
    },
  }),
  fromJson(ActionRequestSchema, {
    id: "70000000-0000-4000-8000-000000000002",
    // An MCP group in MCP_GROUPS below, so its stored result is the tool's whole CallToolResult.
    action: { group: "example_docs", name: "render_diagram" },
    arguments: { path: "docs/test-diagram.mmd" },
    title: "render the test diagram",
    description: null,
    idempotencyKey: "visual-completed",
    caller: { namespace: "test-agentplane", name: "test-workload" },
    callerUid: null,
    state: "ACTION_STATE_SUCCEEDED",
    version: "4",
    createdAt: ago(40 * 60_000),
    updatedAt: ago(39 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000002",
      verdict: "VERDICT_ALLOW",
      provider: "human_operator",
      operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
      decisionNote: null,
      reasonCode: null,
      reasonDescription: null,
      policyEvidence: null,
      idempotencyKey: "visual-allow",
      decidedAt: ago(39 * 60_000),
    },
    execution: {
      id: "72000000-0000-4000-8000-000000000002",
      state: "EXECUTION_STATE_SUCCEEDED",
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
      createdAt: ago(39 * 60_000),
      startedAt: ago(39 * 60_000 - 500),
      completedAt: ago(39 * 60_000 - 900),
      reconciledAt: null,
    },
  }),
  fromJson(ActionRequestSchema, {
    id: "70000000-0000-4000-8000-000000000003",
    action: { group: "everything", name: "echo" },
    arguments: { repository: "test-owner/other-repository" },
    title: "echo a repository outside the binding",
    description: "Reads test-owner/other-repository, which this workload has no binding for.",
    idempotencyKey: "visual-denied",
    caller: { namespace: "agentplane-visual", name: "test-coder" },
    callerUid: null,
    state: "ACTION_STATE_DENIED",
    version: "2",
    createdAt: ago(80 * 60_000),
    updatedAt: ago(79 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000003",
      verdict: "VERDICT_DENY",
      provider: "human_operator",
      operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
      decisionNote: "Out of scope for this workload's binding.",
      reasonCode: null,
      reasonDescription: null,
      policyEvidence: null,
      idempotencyKey: "visual-deny",
      decidedAt: ago(79 * 60_000),
    },
    execution: null,
  }),
  fromJson(ActionRequestSchema, {
    id: "70000000-0000-4000-8000-000000000004",
    action: { group: "github", name: "search_code" },
    arguments: { repository: "agentydragon/ducktape", query: "auto_allow" },
    title: "search ducktape for auto_allow",
    description: null,
    idempotencyKey: "visual-auto-approved",
    caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
    callerUid: null,
    state: "ACTION_STATE_SUCCEEDED",
    version: "3",
    createdAt: ago(15 * 60_000),
    updatedAt: ago(14 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000004",
      verdict: "VERDICT_ALLOW",
      provider: "policy_engine",
      operator: null,
      decisionNote: null,
      reasonCode: null,
      reasonDescription: null,
      idempotencyKey: "visual-policy-allow",
      decidedAt: ago(14 * 60_000),
      policyEvidence: {
        bindings: [{ namespace: "agentplane-visual", name: "ready-sandbox-github-public", resourceVersion: "12345" }],
        policySets: [{ namespace: "agentplane-visual", name: "fixture_auto_allow", generation: 1 }],
        matched: {
          namespace: "agentplane-visual",
          policySet: "fixture_auto_allow",
          source: "POLICY_SOURCE_RULES",
          index: 0,
          type: "POLICY_KIND_EXACT_ACTIONS",
          repository: null,
        },
      },
    },
    execution: {
      id: "72000000-0000-4000-8000-000000000004",
      state: "EXECUTION_STATE_SUCCEEDED",
      result: { matches: 3 },
      error: null,
      createdAt: ago(14 * 60_000),
      startedAt: ago(14 * 60_000 - 500),
      completedAt: ago(14 * 60_000 - 900),
      reconciledAt: null,
    },
  }),
  fromJson(ActionRequestSchema, {
    id: "70000000-0000-4000-8000-000000000005",
    // A tool's error answer: the Action succeeded, and the CallToolResult says the tool failed.
    action: { group: "example_notes", name: "get_note" },
    arguments: { note_id: "test-missing-note" },
    title: "read the missing test note",
    description: null,
    idempotencyKey: "visual-tool-error",
    caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
    callerUid: null,
    state: "ACTION_STATE_SUCCEEDED",
    version: "4",
    createdAt: ago(10 * 60_000),
    updatedAt: ago(9 * 60_000),
    decision: {
      id: "71000000-0000-4000-8000-000000000005",
      verdict: "VERDICT_ALLOW",
      provider: "human_operator",
      operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
      decisionNote: null,
      reasonCode: null,
      reasonDescription: null,
      policyEvidence: null,
      idempotencyKey: "visual-allow-tool-error",
      decidedAt: ago(9 * 60_000),
    },
    execution: {
      id: "72000000-0000-4000-8000-000000000005",
      state: "EXECUTION_STATE_SUCCEEDED",
      result: { content: [{ type: "text", text: "No note has the id test-missing-note." }], isError: true },
      error: null,
      createdAt: ago(9 * 60_000),
      startedAt: ago(9 * 60_000 - 500),
      completedAt: ago(9 * 60_000 - 900),
      reconciledAt: null,
    },
  }),
];

// A single losslessly rendered request, for compact approval screenshots.
const COMPACT_POD_ACTION = clone(ActionRequestSchema, ACTIONS[1]!);
COMPACT_POD_ACTION.id = "70000000-0000-4000-8000-000000000009";
COMPACT_POD_ACTION.action = create(ActionIdentitySchema, { group: "kubernetes_admin", name: "pods_list_in_namespace" });
COMPACT_POD_ACTION.arguments = {
  namespace: "test-apps",
  labelSelector: "app=demo",
  fieldSelector: "status.phase=Running",
};
COMPACT_POD_ACTION.title = "inspect running demo pods";
COMPACT_POD_ACTION.description = "Read-only inspection of the demo workload.";
COMPACT_POD_ACTION.idempotencyKey = "visual-pods-compact-pending";

const COMPACT_RESOURCE_ACTION = clone(ActionRequestSchema, COMPACT_POD_ACTION);
COMPACT_RESOURCE_ACTION.id = "70000000-0000-4000-8000-000000000011";
COMPACT_RESOURCE_ACTION.action = create(ActionIdentitySchema, { group: "kubernetes_admin", name: "resources_get" });
COMPACT_RESOURCE_ACTION.arguments = {
  apiVersion: "v1",
  kind: "ConfigMap",
  namespace: "test-apps",
  name: "test-demo-config",
};
COMPACT_RESOURCE_ACTION.title = "read the demo config";
COMPACT_RESOURCE_ACTION.description = "Read-only inspection of the demo configuration.";
COMPACT_RESOURCE_ACTION.idempotencyKey = "visual-resource-compact-pending";

const GMAIL_DRAFT_ACTION: ActionRequest = fromJson(ActionRequestSchema, {
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
  idempotencyKey: "visual-gmail-draft-completed",
  caller: { namespace: "agentplane-visual", name: "ready-sandbox" },
  callerUid: null,
  state: "ACTION_STATE_SUCCEEDED",
  version: "4",
  createdAt: ago(7 * 60_000),
  updatedAt: ago(6 * 60_000),
  decision: {
    id: "71000000-0000-4000-8000-000000000010",
    verdict: "VERDICT_ALLOW",
    provider: "human_operator",
    operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
    decisionNote: null,
    reasonCode: null,
    reasonDescription: null,
    policyEvidence: null,
    idempotencyKey: "visual-allow-gmail-draft",
    decidedAt: ago(6 * 60_000),
  },
  execution: {
    id: "72000000-0000-4000-8000-000000000010",
    state: "EXECUTION_STATE_SUCCEEDED",
    result: {
      content: [],
      structuredContent: {
        id: "draft-release-123",
        message: { payload: { headers: [{ name: "Subject", value: "Release notes" }] } },
      },
      isError: false,
    },
    error: null,
    createdAt: ago(6 * 60_000),
    startedAt: ago(6 * 60_000 - 500),
    completedAt: ago(6 * 60_000 - 1_500),
    reconciledAt: null,
  },
});

const CONVERSATION_SOURCE = "visual-runner";
const CONVERSATION_EPOCH = "20260921";
const payloadBodies = new Map<string, string>();

function payloadKey(ownerId: string, field: ThreadPayloadField, generation: string): string {
  return `${ownerId}:${ThreadPayloadField[field].toLowerCase()}:${generation}`;
}

function payload(
  ownerCursor: number,
  ownerId: string,
  field: ThreadPayloadField,
  body: string,
  revisionCursor = ownerCursor
): ThreadPayloadReference {
  const reference = create(ThreadPayloadReferenceSchema, {
    projectionEpoch: CONVERSATION_EPOCH,
    ownerCursor: BigInt(ownerCursor),
    ownerId,
    field,
    generation: BigInt(ownerCursor),
    revisionCursor: BigInt(revisionCursor),
    chunkCount: 1n,
  });
  payloadBodies.set(payloadKey(ownerId, field, ownerCursor.toString()), body);
  return reference;
}

function entity(
  id: string,
  cursor: number,
  entity: ThreadEntity["entity"],
  extra: { threadId?: string } = {}
): Record<string, unknown> {
  const { kind, pending, state } = electricEntityVariant(entity);
  return {
    thread_id: extra.threadId ?? THREADS[0].metadata.threadId,
    projection_epoch: CONVERSATION_EPOCH,
    entity_kind: kind,
    entity_id: id,
    cursor: String(cursor),
    revision_cursor: String(cursor),
    pending,
    state,
  };
}

function viewState(
  throughCursor: number,
  activeTurn: string | null,
  status: ThreadFeedStatus = ThreadFeedStatus.ACTIVE,
  appliedModel: string | null = "harness-claude-model"
): Record<string, unknown> {
  return entity("current", throughCursor, {
    case: "viewState",
    value: create(ThreadViewStateSchema, {
      controls: {
        appliedModel: appliedModel ?? undefined,
        activeTurnId: activeTurn ?? undefined,
        harnessState: activeTurn === null ? "stopped" : "running",
      },
      operational: {
        status,
        lastVerifiedCursor: BigInt(throughCursor),
      },
    }),
  });
}

function fixtureEvent(observation: MessageInitShape<typeof EventSchema>["observation"]): Event {
  return create(EventSchema, { observation });
}

function lifecycle(cursor: number, event: Event, threadId?: string): Record<string, unknown> {
  return entity(
    String(cursor),
    cursor,
    { case: "lifecycle", value: create(ThreadLifecycleSchema, { event }) },
    { threadId }
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
  const completion =
    extra.complete === false
      ? undefined
      : kind === ItemKind.TOOL_CALL
        ? {
            case: "tool" as const,
            value: create(ThreadItemToolCompletionSchema, { succeeded: extra.failed !== true }),
          }
        : { case: "text" as const, value: create(ThreadItemTextCompletionSchema, {}) };
  return entity(
    id,
    cursor,
    {
      case: "item",
      value: create(ThreadItemSchema, {
        kind,
        toolName: extra.tool ?? "",
        completion,
        recovery: extra.recovery,
        recoveryReason: extra.recoveryReason,
        turnId: extra.turn,
        textRef: text === null ? undefined : payload(cursor, id, ThreadPayloadField.TEXT, text),
        argumentsRef:
          extra.arguments === undefined
            ? undefined
            : payload(cursor, id, ThreadPayloadField.ARGUMENTS, extra.arguments),
        outputRef:
          extra.output === undefined ? undefined : payload(cursor, id, ThreadPayloadField.OUTPUT, extra.output),
      }),
    },
    {
      threadId: extra.threadId,
    }
  );
}

function command(
  cursor: number,
  id: string,
  operation: string,
  outcome: ThreadCommandOutcome,
  options: { reason?: string; text?: string; requestedValue?: string } = {}
): Record<string, unknown> {
  return entity(id, cursor, {
    case: "command",
    value: create(ThreadCommandSchema, {
      operation,
      outcome,
      outcomeCursor: outcome === ThreadCommandOutcome.PENDING ? undefined : BigInt(cursor),
      outcomeReason: options.reason,
      requestedValue: options.requestedValue,
      inputRef:
        options.text === undefined ? undefined : payload(cursor, id, ThreadPayloadField.COMMAND_INPUT, options.text),
    }),
  });
}

function confirmedInput(
  cursor: number,
  entityId: string,
  harnessMessageId: string,
  originCommandIds: string[],
  text: string,
  { threadId, turnId }: { threadId?: string; turnId?: string } = {}
): Record<string, unknown> {
  const ownerId = harnessMessageId || `confirmed:${cursor}`;
  return entity(
    entityId,
    cursor,
    {
      case: "confirmedInput",
      value: create(ThreadConfirmedInputSchema, {
        harnessMessageId,
        originCommandIds,
        turnId,
        inputRef: payload(cursor, ownerId, ThreadPayloadField.CONFIRMED_INPUT, text),
      }),
    },
    { threadId }
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
  const thread = THREADS_WITH_SANDBOXES.find((candidate) => candidate.metadata.threadId === threadId);
  if (!thread) throw new Error(`Unknown fixture Thread: ${threadId}`);
  const rows = [
    viewState(
      34,
      suspended ? null : "turn-visual",
      suspended ? ThreadFeedStatus.ENDED : ThreadFeedStatus.ACTIVE,
      thread.summary.attached?.spec?.model
    ),
    confirmedInput(4, "user-1", "user-1", ["input-1"], "List the repository files.", {
      threadId,
      turnId: "turn-visual",
    }),
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
    lifecycle(31, fixtureEvent({ case: "harnessStderr", value: { text: "warning: fixture stderr" } }), threadId),
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

function failedRows(threadId: string, afterContent: boolean): Record<string, unknown>[] {
  const error = "Test model request failed: HTTP 429. Quota exhausted for this fixture.";
  const event = fixtureEvent({
    case: "turnCompleted",
    value: { turnId: "failed-turn", status: TurnStatus.FAILED, error },
  });
  const rows = [
    viewState(afterContent ? 8 : 6, null),
    confirmedInput(4, "failed-input", "failed-input", ["input-failed"], "Inspect the test repository.", {
      threadId,
      turnId: "failed-turn",
    }),
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
    lifecycle(afterContent ? 8 : 6, event, threadId),
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
    confirmedInput(
      10,
      "coalesced-message",
      "coalesced-message",
      ["input-B", "input-C"],
      "Also inspect tests.\nKeep the patch small.",
      { threadId, turnId: "interleaved-turn" }
    ),
    item(15, "after-input", ItemKind.ASSISTANT_TEXT, "Continuing with the new model…", {
      threadId,
      turn: "interleaved-turn",
    }),
    lifecycle(
      18,
      fixtureEvent({
        case: "turnCompleted",
        value: { turnId: "interleaved-turn", status: TurnStatus.INTERRUPTED, interruptedByCommandId: "interrupt" },
      }),
      threadId
    ),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

/** Close-packed command states so the semaphore and its real labels can be inspected on a phone. */
function commandProgressRows(threadId: string): Record<string, unknown>[] {
  const model = command(2, "progress-model", "change_model", ThreadCommandOutcome.PENDING, {
    requestedValue: "test-model-next",
  });
  const effort = command(3, "progress-effort", "change_reasoning_effort", ThreadCommandOutcome.FAILED, {
    reason: "Not supported by harness",
    requestedValue: "high",
  });
  const noop = command(4, "progress-noop", "change_model", ThreadCommandOutcome.NOOP, {
    reason: "Model already selected",
    requestedValue: "current-model",
  });
  const effected = command(5, "progress-effected", "change_model", ThreadCommandOutcome.EFFECTED, {
    requestedValue: "applied-model",
  });
  return [
    { ...viewState(5, "t-progress"), thread_id: threadId },
    model,
    effort,
    noop,
    effected,
    command(9, "progress-applied-input", "submit_input", ThreadCommandOutcome.EFFECTED, { text: "Applied input." }),
    command(6, "progress-input", "submit_input", ThreadCommandOutcome.PENDING, { text: "Please review this change." }),
    command(7, "progress-noop-input", "submit_input", ThreadCommandOutcome.NOOP, {
      reason: "Nothing to do",
      text: "Already handled.",
    }),
  ];
}

/** Local and remote pending inputs whose payload references are present but bodies are withheld. */
function pendingInputEchoRows(threadId: string): Record<string, unknown>[] {
  const local = command(15, "locally-retained", "submit_input", ThreadCommandOutcome.PENDING, {
    text: "This browser already knows the text it sent.",
  });
  const remote = command(16, "remote-pending", "submit_input", ThreadCommandOutcome.PENDING, {
    text: "This text is still unknown here.",
  });
  payloadBodies.delete(payloadKey("locally-retained", ThreadPayloadField.COMMAND_INPUT, "15"));
  payloadBodies.delete(payloadKey("remote-pending", ThreadPayloadField.COMMAND_INPUT, "16"));
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
    viewState(28, suspended ? null : "t2", suspended ? ThreadFeedStatus.ENDED : ThreadFeedStatus.ACTIVE),
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
    command(24, "queued-model", "change_model", outcomes ? ThreadCommandOutcome.FAILED : ThreadCommandOutcome.PENDING, {
      reason: outcomes ? "Model unavailable" : undefined,
    }),
    command(
      25,
      "queued-interrupt",
      "interrupt_turn",
      outcomes ? ThreadCommandOutcome.NOOP : ThreadCommandOutcome.PENDING,
      { reason: outcomes ? "Target turn already ended" : undefined }
    ),
    // Admitted and still pending, so it renders inline as a pending message bubble rather than in
    // the pending-commands box below -- see conversation.tsx's pendingSentMessage.
    command(26, "queued-submit", "submit_input", ThreadCommandOutcome.PENDING, {
      text: "Continue past the failing test once it lands.",
    }),
    command(27, "failed-submit", "submit_input", ThreadCommandOutcome.FAILED, {
      reason: "Runner could not start this turn.",
      text: "Run the rejected input.",
    }),
    command(28, "noop-submit", "submit_input", ThreadCommandOutcome.NOOP, {
      reason: "The harness was already stopping.",
      text: "Run the input that was not applied.",
    }),
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
  const rows = [
    viewState(50, null),
    lifecycle(10, fixtureEvent({ case: "turnStarted", value: { turnId: "turn-visual" } }), threadId),
    lifecycle(20, fixtureEvent({ case: "harnessStarted", value: {} }), threadId),
    lifecycle(
      30,
      fixtureEvent({ case: "turnCompleted", value: { turnId: "turn-visual", status: TurnStatus.COMPLETED } }),
      threadId
    ),
    lifecycle(40, fixtureEvent({ case: "harnessLost", value: {} }), threadId),
    lifecycle(50, fixtureEvent({ case: "harnessStarted", value: {} }), threadId),
  ];
  return rows.map((row) => (row.entity_kind === "view_state" ? { ...row, thread_id: threadId } : row));
}

function shortThreadSwitchRows(threadId: string): Record<string, unknown>[] {
  if (threadId !== IDLE_THREAD && threadId !== UNNAMED_STOPPED_THREAD) {
    return [{ ...viewState(80, null), thread_id: threadId }];
  }
  const rekey = (row: Record<string, unknown>, id: string) => ({ ...row, entity_id: id });
  return [
    { ...viewState(80, null), thread_id: threadId },
    rekey(lifecycle(1, fixtureEvent({ case: "setupStarted", value: {} }), threadId), "visual-setup"),
    rekey(
      lifecycle(
        2,
        fixtureEvent({
          case: "setupOutput",
          value: { stream: { case: "stdout", value: new TextEncoder().encode("Workspace sync complete\n") } },
        }),
        threadId
      ),
      "visual-setup-output"
    ),
    rekey(
      lifecycle(3, fixtureEvent({ case: "setupFinished", value: { exitCode: 0 } }), threadId),
      "visual-setup-finished"
    ),
    rekey(
      lifecycle(10, fixtureEvent({ case: "turnStarted", value: { turnId: "visual-turn" } }), threadId),
      "visual-lifecycle-before"
    ),
    rekey(lifecycle(11, fixtureEvent({ case: "harnessStarted", value: {} }), threadId), "visual-harness-before"),
    confirmedInput(20, "visual-input", "visual-input", [], "Check the short thread after switching.", { threadId }),
    rekey(
      lifecycle(
        30,
        fixtureEvent({ case: "turnCompleted", value: { turnId: "visual-turn", status: TurnStatus.COMPLETED } }),
        threadId
      ),
      "visual-lifecycle-after"
    ),
    rekey(lifecycle(31, fixtureEvent({ case: "harnessStarted", value: {} }), threadId), "visual-harness-after"),
    item(40, "visual-reasoning", ItemKind.REASONING, "The history has seven compact rows.", { threadId }),
    item(50, "visual-answer", ItemKind.ASSISTANT_TEXT, "The short thread is ready.", { threadId }),
    item(60, "visual-tool", ItemKind.TOOL_CALL, null, {
      threadId,
      tool: "Read",
      arguments: '{"path":"notes.txt"}',
      output: "No additional history is needed.",
    }),
  ];
}

/** Finished long Bash calls amid variable-height messages, matching the browser integration
 * history's content and virtualizer load. Tests choose geometry and perform every gesture. */
function collapsibleCommandRows(threadId: string, below: number): Record<string, unknown>[] {
  let cursor = 0;
  const message = (prefix: string, index: number) =>
    item(
      ++cursor,
      `${prefix}-${index}`,
      ItemKind.ASSISTANT_TEXT,
      `Window message ${String(index).padStart(3, "0")}: ` + "measured variable-height text ".repeat((index % 4) + 1),
      { threadId }
    );
  const rows = [
    item(++cursor, "retained-prefix", ItemKind.ASSISTANT_TEXT, "Test retained prefix", { threadId }),
    ...Array.from({ length: 25 }, (_, index) => message("above", index)),
    ...["a", "b", "c"].map((name) =>
      item(++cursor, `resize-tool-${name}`, ItemKind.TOOL_CALL, null, {
        threadId,
        tool: "Bash",
        arguments: JSON.stringify({
          command: Array.from({ length: 24 }, (_, index) => `echo command line ${index}`).join("\n"),
          description: `Run tool ${name}`,
        }),
        output: Array.from({ length: 60 }, (_, index) => `output line ${index}`).join("\n"),
      })
    ),
    ...Array.from({ length: below }, (_, index) => message("below", index)),
  ];
  return [{ ...viewState(cursor, null), thread_id: threadId }, ...rows];
}

function historyProfileRows(threadId: string, rowCount: number): Record<string, unknown>[] {
  if (!Number.isInteger(rowCount) || rowCount < 1 || rowCount > 5000) {
    throw new RangeError(`History profile row count must be between 1 and 5000: ${rowCount}`);
  }
  return [
    { ...viewState(rowCount, null), thread_id: threadId },
    ...Array.from({ length: rowCount }, (_, index) =>
      item(index + 1, `profile-row-${index}`, ItemKind.ASSISTANT_TEXT, `Profile history row ${index}.`, {
        threadId,
        turn: `profile-turn-${index}`,
      })
    ),
  ];
}

function setupRows(threadId: string, running = false): Record<string, unknown>[] {
  const output = (cursor: number, channel: "stdout" | "stderr", text: string) =>
    lifecycle(
      cursor,
      fixtureEvent({
        case: "setupOutput",
        value: { stream: { case: channel, value: new TextEncoder().encode(text) } },
      }),
      threadId
    );
  return [
    { ...viewState(7, null), thread_id: threadId },
    lifecycle(1, fixtureEvent({ case: "setupStarted", value: {} }), threadId),
    output(2, "stderr", "Cloning into project...\nUpdating files: 10%\r"),
    output(3, "stdout", "Workspace ready\n"),
    output(4, "stderr", "Updating files: 80%\r"),
    output(5, "stderr", "Updating files: 100%\n"),
    ...(running
      ? []
      : [
          lifecycle(6, fixtureEvent({ case: "setupFinished", value: { exitCode: 0 } }), threadId),
          lifecycle(7, fixtureEvent({ case: "harnessStarted", value: {} }), threadId),
        ]),
  ];
}

/** One user turn answered with a fenced Python code block, so Markdown's syntax highlighting of a
 * registered language renders the way tool-call Arguments/Output already do. */
function codeFenceRows(threadId: string): Record<string, unknown>[] {
  const rows = [
    viewState(20, null),
    confirmedInput(4, "user-1", "user-1", ["input-1"], "Add type hints to the greet function.", {
      threadId,
      turnId: "turn-visual",
    }),
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
    confirmedInput(4, "user-1", "user-1", ["input-1"], "What should we try next?", {
      threadId,
      turnId: "turn-visual",
    }),
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
    confirmedInput(4, "user-1", "user-1", ["input-1"], "Check the containers and the holdings.", {
      threadId,
      turnId: "turn-visual",
    }),
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
      turn: "turn-visual",
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
  if (kind === "tools") {
    const state = fromJson(ThreadItemSchema, rows[0].state as JsonValue);
    rows[0].state = toJson(
      ThreadItemSchema,
      create(ThreadItemSchema, {
        ...state,
        outputRef: payload(10, "revised-tool", ThreadPayloadField.OUTPUT, "aborted"),
      })
    );
  }
  return [{ ...viewState(40, null), thread_id: threadId }, ...rows];
}

let threadEntityRows = (threadId: string): Record<string, unknown>[] =>
  threadId === THREADS[2].metadata.threadId ? statesRows(threadId) : standardRows(threadId, false, false);

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
      return confirmedInput(cursor, id, id, [], entry.text, { threadId, turnId: turn });
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

function threadScope(threadId: string): Record<string, unknown> {
  const through = threadEntityRows(threadId).find((row) => row.entity_kind === "view_state")?.revision_cursor ?? "0";
  return toJson(
    ReadThreadEntitiesResponseSchema,
    create(ReadThreadEntitiesResponseSchema, {
      projectionEpoch: CONVERSATION_EPOCH,
      throughCursor: BigInt(String(through)),
    })
  ) as Record<string, unknown>;
}

function rememberPendingInput(): void {
  const local = new LocalCommands(THREADS[2].metadata.threadId, COMMAND_TARGET);
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
  const local = new LocalCommands(THREADS[2].metadata.threadId, COMMAND_TARGET);
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
  const local = new LocalCommands(THREADS[2].metadata.threadId, COMMAND_TARGET);
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
  const sandbox = SANDBOXES.find((candidate) => candidate.target?.name === "suspended-sandbox");
  if (!sandbox) throw new Error("Missing suspended Sandbox fixture");
  sandbox.configuration!.wakeOnSend = true;
  sandbox.observedState!.suspendedBy = "Requested";
}

function rememberDispatch(threadId = RUNNING_THREAD): void {
  const command = create(CommandSchema, {
    commandId: "queued-dispatch",
    operation: {
      case: "submitInput",
      value: { text: "Continue when the Sandbox is ready." },
    },
  });
  const local = new LocalCommands(threadId, COMMAND_TARGET);
  local.remember(command);
  const dispatch = dispatchFromJson({
    commandId: command.commandId,
    sessionId: threadId,
    ...COMMAND_TARGET,
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
  const local = new LocalCommands(RUNNING_THREAD, COMMAND_TARGET);
  local.remember(command);
  local.acknowledgeDispatch(
    command,
    dispatchFromJson({
      commandId: command.commandId,
      sessionId: RUNNING_THREAD,
      ...COMMAND_TARGET,
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
  const local = new LocalCommands(RUNNING_THREAD, COMMAND_TARGET);
  local.remember(command);
  const dispatch = dispatchFromJson({
    commandId: command.commandId,
    sessionId: RUNNING_THREAD,
    ...COMMAND_TARGET,
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

type McpFailure = {
  reason: McpUnavailableReason;
  detail: string;
  lastDiscoveryAt: string | undefined;
  failures: number;
};

/** An MCP group, available unless `failure` says why it is disconnected. */
function mcpGroup(key: string, executorDescription: string, failure?: McpFailure): ActionGroup {
  return create(ActionGroupSchema, {
    key,
    title: key,
    description: `Test MCP backend ${key}.`,
    executorKind: ExecutorKind.MCP,
    executorDescription,
    available: failure === undefined,
    health: failure
      ? create(McpHealthSchema, {
          state: McpLifecycle.DISCONNECTED,
          reason: failure.reason,
          detail: failure.detail,
          lastDiscoveryAt:
            failure.lastDiscoveryAt === undefined ? undefined : fromJson(TimestampSchema, failure.lastDiscoveryAt),
          retryAt: fromJson(TimestampSchema, new Date(NOW + 20_000).toISOString()),
          failures: failure.failures,
        })
      : create(McpHealthSchema, {
          state: McpLifecycle.AVAILABLE,
          lastDiscoveryAt: fromJson(TimestampSchema, ago(60_000)),
        }),
    actions: [],
    directTools: [],
  });
}

const LINKED = "Linked operator account.";
const BEARER = "Test MCP backend behind a static bearer.";
const MCP_GROUPS: ActionGroup[] = [
  mcpGroup("example_docs", LINKED),
  mcpGroup("example_cluster", LINKED, {
    reason: McpUnavailableReason.LINKAGE_UNAVAILABLE,
    detail: `the access token expired at ${ago(2 * HOUR)
      .replace("T", " ")
      .slice(0, 19)} UTC and has not been refreshed`,
    lastDiscoveryAt: ago(3 * HOUR),
    failures: 12,
  }),
  mcpGroup("example_calendar", LINKED, {
    reason: McpUnavailableReason.LINKAGE_UNAVAILABLE,
    detail: "refreshing the token failed in a way retrying cannot fix; link the account again",
    lastDiscoveryAt: ago(HOUR),
    failures: 9,
  }),
  mcpGroup("example_pantry", LINKED, {
    reason: McpUnavailableReason.LINKAGE_UNAVAILABLE,
    detail: "no account is linked",
    lastDiscoveryAt: undefined,
    failures: 4,
  }),
  mcpGroup("example_mail", BEARER, {
    reason: McpUnavailableReason.CONNECT_FAILED,
    detail: "RuntimeError: Client failed to connect: All connection attempts failed",
    lastDiscoveryAt: undefined,
    failures: 7,
  }),
  mcpGroup("gmail", "Test Gmail action group for Agentplane rendering."),
  mcpGroup("example_notes", BEARER),
  mcpGroup("ssh", BEARER),
];
let receiptActionGroup: ActionGroup | undefined;

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
  | "threadScopeUnavailable"
  | "commandAdmissionTimedOut"
  | "pendingActions"
  | "pendingActionOverflow"
  | "compactPodAction"
  | "gmailDraftHistory"
  | "pagedActionHistory"
  | "actionGroupsUnavailable"
  | "notificationStatusUnavailable"
  | "notificationStreamDropped"
  | "notificationExtraEntry"
  | "pushBrowserAdded"
  | "connectionRenamed"
  | "mcpLinkageChanged"
  | "mcpHealthChanged";
const flags = new Set<Flag>();

function actionGroups(): ActionGroup[] {
  return MCP_GROUPS.map((row) =>
    row.key === "example_docs" && flags.has("mcpHealthChanged")
      ? create(ActionGroupSchema, {
          ...row,
          available: false,
          health: create(McpHealthSchema, {
            state: McpLifecycle.DISCONNECTED,
            reason: McpUnavailableReason.CONNECT_FAILED,
            detail: "Connection refused",
            lastDiscoveryAt: row.health?.lastDiscoveryAt,
            retryAt: fromJson(TimestampSchema, new Date(NOW + 20_000).toISOString()),
            failures: 1,
          }),
        })
      : row
  );
}

function mcpLinkages(): McpLinkageView[] {
  return MCP_LINKAGES.map((row) =>
    row.server_id === "example_docs" && flags.has("mcpLinkageChanged") ? { ...row, status: "expired" as const } : row
  );
}

function pendingActions(): ActionRequest[] {
  const pending = ACTIONS.filter((request) => request.state === ActionState.DECISION_PENDING);
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
    /^\/upstream\/app-config\/v1\/frontend-config$/,
    () => ({
      frontendConfig: {
        traceSearch: {
          grafanaBaseUrl: "https://grafana.test.invalid",
          tempoDatasourceUid: "tempo-test",
          deploymentEnvironment: "agentplane-visual-test",
        },
        langfuseSearch: {
          baseUrl: "https://langfuse.test.invalid",
          projectId: "langfuse-visual-test",
          sessionMetadataKey: "attributes.litellm.metadata.spend_logs_metadata.agentplane.session_id",
          sandboxNamespaceMetadataKey: "attributes.agentplane.namespace",
          sandboxServiceAccountMetadataKey: "attributes.agentplane.service_account",
        },
      },
    }),
  ],
  [
    "GET",
    /^\/upstream\/app-config\/v1\/models$/,
    () => ({
      models: [
        {
          model: "harness-claude-model",
          displayName: "Harness Claude Model",
          reasoningEfforts: TEST_REASONING_EFFORTS,
        },
        { model: "next-model", displayName: "Next Model", reasoningEfforts: TEST_REASONING_EFFORTS },
        {
          model: "harness-codex-model",
          displayName: "Harness Codex Model",
          reasoningEfforts: TEST_REASONING_EFFORTS,
        },
      ],
      harnesses: [
        {
          harness: "HARNESS_CLAUDE",
          models: flags.has("claudePaused") ? [] : ["harness-claude-model", "next-model"],
        },
        { harness: "HARNESS_CODEX", models: ["harness-codex-model"] },
        ...(flags.has("centralSamplerEnabled")
          ? [{ harness: "HARNESS_CENTRAL_SAMPLER", models: ["harness-codex-model"] }]
          : []),
      ],
    }),
  ],
  [
    "GET",
    /^\/upstream\/app\/v1\/system\/status$/,
    () => ({
      components: [
        { name: "Healthy component", state: "SYSTEM_COMPONENT_STATE_OK", detail: null, checkedAt: ago(4000) },
        {
          name: "Down component",
          state: "SYSTEM_COMPONENT_STATE_DOWN",
          detail: "UNAVAILABLE: connection refused",
          checkedAt: ago(4000),
        },
      ],
    }),
  ],
  [
    "GET",
    /^\/upstream\/app-config\/v1\/presets$/,
    () => ({
      presets: [
        {
          name: "test-coder",
          title: "Test coder",
          template: "runner",
          policySets: [...TEST_CODER_POLICY_SETS],
          inboxTags: [],
          sessionDefaults: {
            harness: "HARNESS_CODEX",
            model: "harness-codex-model",
            cwd: "/state/workspaces/{session_id}",
            reasoningEffort: "medium",
            instructions: "Work on public repositories only.",
            setupScript: "",
          },
          bootstrap: "mkdir -p /state/workspaces",
        },
      ],
    }),
  ],
  ["GET", /^\/upstream\/environments\/v1\/environment-templates$/, () => ({ names: ["runner"] })],
  [
    "GET",
    /^\/upstream\/app-config\/v1\/sandbox-form$/,
    () => ({
      policySets: [...TEST_CODER_POLICY_SETS, "pypi", "github-public"],
      newSandboxDefaults: { wakeOnSend: false },
    }),
  ],
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
        : ACTIONS.filter((request) => request.state !== ActionState.DECISION_PENDING);
      const filtered =
        query.get("includeAutoAllowed") === "false"
          ? past.filter(
              (request) => !(request.decision?.verdict === Verdict.ALLOW && request.decision.operator === undefined)
            )
          : past;
      const page =
        flags.has("pagedActionHistory") && !query.has("cursor")
          ? create(ListActionRequestHistoryResponseSchema, {
              actionRequests: filtered.slice(0, 2),
              nextCursor: "second-page",
            })
          : create(ListActionRequestHistoryResponseSchema, {
              actionRequests: flags.has("pagedActionHistory") ? filtered.slice(2) : filtered,
            });
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
        ? decideActionRequestProtoJson(
            create(ActionRequestSchema, { ...request, state: ActionState.ALLOWED, version: 2n })
          )
        : Response.json({ code: 5, message: "Action request not found" }, { status: 404 });
    },
  ],
  [
    "GET",
    /^\/sandboxes\/([^/]+)\/runner-status$/,
    () =>
      runnerStatusFixture({
        environmentRuntimeBootId: "visual-runner-boot-01",
        lifecycle: acknowledgedQuiescingLifecycleFixture({
          generation: 3n,
          sessionId: "ready-session",
          journalSealUid: "visual-incarnation-01",
          journalSealCursor: 19n,
          acknowledgementCursor: 18n,
        }),
        processes: [
          {
            processId: "visual-claude-process-01",
            pid: 18001n,
            harness: Harness.CLAUDE,
            state: ProcessState.RUNNING,
            assignedSessionIds: ["ready-session"],
          },
        ],
        sessions: [
          {
            sessionId: "dormant-session",
            executionState: SessionExecutionState.DORMANT,
            busy: false,
          },
          {
            sessionId: "ready-session",
            processId: "visual-claude-process-01",
            nativeSessionId: "visual-native-session-01",
            executionState: SessionExecutionState.READY,
            busy: false,
          },
        ],
      }),
  ],
  [
    "POST",
    /^\/sandboxes\/([^/]+)\/sessions$/,
    (_match, _query, _signal, body) => {
      if (!queueCreateResponses || body === undefined)
        throw new Error("No queued Session Create fixture is configured");
      const request = JSON.parse(body) as { session_id?: string; spec?: JsonValue };
      if (!request.session_id || !request.spec) throw new Error("Create request has no Session ID or spec");
      const spec = fromJson(SessionSpecSchema, request.spec);
      if (spec.placement?.kind.case !== "environmentHosted") {
        throw new Error("Queued fixture requires native placement");
      }
      queuedSessionId = request.session_id;
      return {
        sessionId: request.session_id,
        status: { materialization: "SESSION_MATERIALIZATION_WAITING", reason: "waiting for owner" },
        placement: {
          environmentHosted: toJson(EnvironmentHostedPlacementSchema, spec.placement.kind.value),
        },
      };
    },
  ],
  [
    "GET",
    /^\/threads$/,
    (_match, query) => ({
      threads: THREADS.filter(
        (thread) => !query.has("sandbox") || thread.summary.sandbox?.name === query.get("sandbox")
      ).map((thread) => ({
        metadata: toJson(ThreadMetadataSchema, thread.metadata),
        summary: toJson(SessionSummarySchema, thread.summary),
      })),
      next_page_token: null,
    }),
  ],
  [
    "GET",
    /^\/threads\/([0-9a-f-]+)$/,
    (match) => {
      const thread = THREADS_WITH_SANDBOXES.find((candidate) => candidate.metadata.threadId === match[1]);
      if (!thread) return undefined;
      const mapped = mapThread(thread);
      return {
        metadata: toJson(ThreadMetadataSchema, mapped.metadata),
        summary: toJson(SessionSummarySchema, mapped.summary),
      };
    },
  ]
);

/** Encode the database-facing Electric row, including PostgreSQL JSONB and bool columns. */
function electricEntity(row: Record<string, unknown>): Record<string, unknown> {
  const value = { ...row };
  value.state = JSON.stringify(value.state);
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

const OBSERVATIONS = [
  { cursor: "31", kind: "harness_stderr" },
  { cursor: "34", kind: "item_completed" },
] as const;
const SETTLED_RANGES = [
  { first: 1n, last: 30n },
  { first: 32n, last: 33n },
] as const;

function historyObservationPage(query: URLSearchParams) {
  const after = BigInt(query.get("after_cursor") ?? "0");
  const beforeValue = query.get("before_cursor");
  const before = beforeValue === null ? undefined : BigInt(beforeValue);
  const limit = Number(query.get("limit") ?? "30");
  const observations = OBSERVATIONS.filter((observation) => {
    const cursor = BigInt(observation.cursor);
    return cursor > after && (before === undefined || cursor < before);
  })
    .slice(-limit)
    .map((observation) => ({ cursor: observation.cursor, kind: observation.kind }));
  const end = before === undefined || before > 34n ? 34n : before - 1n;
  const settled = SETTLED_RANGES.filter((range) => range.first <= end && range.last > after).map((range) => ({
    first: range.first.toString(),
    last: range.last.toString(),
  }));
  return { lastCursor: "34", observations, settled };
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
              ...COMMAND_TARGET,
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
    "POST",
    /^\/upstream\/history\/v1\/sessions\/([0-9a-f-]+)\/entities$/,
    (match, _query, _signal, body) => {
      if (body === undefined) throw new Error("ReadThreadEntities must use a JSON request body");
      const request = fromJson(ReadThreadEntitiesRequestSchema, JSON.parse(body) as JsonValue);
      if (
        request.projectionEpoch !== "" ||
        request.window.case !== "range" ||
        request.window.value.startEntityIndex !== 0n ||
        request.window.value.limit !== 1
      ) {
        throw new Error("scope read must request range 0 with limit 1 at the current projection");
      }
      return flags.has("threadScopeUnavailable")
        ? // A persistent service failure is distinct from a retired epoch's 410, which the
          // production store resolves by reading the scope again.
          Response.json({ detail: "thread fold is temporarily unavailable" }, { status: 503 })
        : Response.json(threadScope(match[1]));
    },
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
        flags.has("entitySegmentsWithheld") && row.entity_kind === "view_state"
          ? { ...row, cursor: "8", revision_cursor: "8" }
          : row
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
        const text = payloadBodies.get(`${ownerId}:${match[2]}:${generation}`);
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
    /^\/upstream\/app\/v1\/threads\/([0-9a-f-]+)$/,
    (match) => {
      const thread = THREADS_WITH_SANDBOXES.find((candidate) => candidate.metadata.threadId === match[1]);
      return thread === undefined
        ? Response.json({ detail: "thread not found" }, { status: 404 })
        : toJson(ThreadMetadataSchema, mapThread(thread).metadata);
    },
  ],
  [
    "GET",
    /^\/upstream\/app\/v1\/threads$/,
    (_match, query) => {
      const sandboxUID = query.get("sandbox_uid");
      const all = THREADS_WITH_SANDBOXES.map(mapThread).filter(
        (thread) => sandboxUID === null || thread.summary.sandbox?.uid === sandboxUID
      );
      const archivedCount = all.filter((thread) => thread.metadata.archived).length;
      const includeArchived = query.get("include_archived") === "true";
      const listed = all.filter((thread) => includeArchived || !thread.metadata.archived);
      const offset = Number(query.get("page_token") ?? "0");
      const limit = Number(query.get("limit") ?? "50");
      const threads = listed.slice(offset, offset + limit);
      return {
        threads: threads.map((thread) => toJson(ThreadMetadataSchema, thread.metadata)),
        nextPageToken: offset + threads.length < listed.length ? String(offset + threads.length) : "",
        archivedCount,
      };
    },
  ],
  [
    "GET",
    /^\/upstream\/history\/v1\/sessions\/([0-9a-f-]+)\/summary$/,
    (match) => {
      const thread = THREADS_WITH_SANDBOXES.find((candidate) => candidate.metadata.threadId === match[1]);
      if (thread === undefined) return Response.json({ detail: "session not found" }, { status: 404 });
      return toJson(SessionSummarySchema, mapThread(thread).summary);
    },
  ],
  [
    "GET",
    /^\/upstream\/history\/v1\/sessions:find$/,
    (_match, query) => {
      const search = query.get("sandbox_name_contains")?.toLowerCase();
      return {
        sessions: THREADS_WITH_SANDBOXES.map(mapThread)
          .filter((thread) => search === undefined || thread.summary.sandbox?.name.toLowerCase().includes(search))
          .map((thread) => ({
            sessionId: thread.metadata.threadId,
            sandbox: thread.summary.sandbox,
          })),
      };
    },
  ],
  [
    "POST",
    /^\/upstream\/history\/v1\/sessions:batchGetSummaries$/,
    (_match, _query, _signal, body) => {
      const request = body === undefined ? {} : (JSON.parse(body) as { sessionIds?: string[] });
      const wanted = new Set(request.sessionIds ?? []);
      return {
        summaries: THREADS_WITH_SANDBOXES.map(mapThread)
          .filter((thread) => wanted.has(thread.metadata.threadId))
          .map((thread) => toJson(SessionSummarySchema, thread.summary)),
      };
    },
  ],
  [
    "GET",
    /^\/upstream\/history\/v1\/sessions\/([0-9a-f-]+)\/observations$/,
    (_match, query) => historyObservationPage(query),
  ],
  [
    "GET",
    /^\/upstream\/history\/v1\/sessions\/([0-9a-f-]+)\/events$/,
    (_match, query) => {
      const after = BigInt(query.get("after_cursor") ?? "0");
      const limit = Number(query.get("limit") ?? "128");
      const entries = Object.entries(OBSERVATION_ENTRIES)
        .filter(([cursor]) => BigInt(cursor) > after)
        .slice(0, limit)
        .map(([, entry]) => entry());
      return { lastCursor: "34", entries, settlements: [], removed: [] };
    },
  ],
  [
    "POST",
    /^\/upstream\/history\/v1\/sessions\/([0-9a-f-]+)\/entity-evidence$/,
    (_match, _query, _signal, body) => {
      const request = body === undefined ? {} : (JSON.parse(body) as { afterCursor?: string });
      return {
        observations: BigInt(request.afterCursor ?? "0") < 31n ? [{ observationCursor: "31", hasNative: true }] : [],
      };
    },
  ],
  [
    "POST",
    /^\/upstream\/history\/v1\/sessions\/([0-9a-f-]+)\/native-frames$/,
    (_match, _query, _signal, body) => {
      const request =
        body === undefined ? {} : (JSON.parse(body) as { observationCursor?: string; afterSequence?: string });
      return {
        frames:
          request.observationCursor === "31" && BigInt(request.afterSequence ?? "0") < 31n
            ? [{ sourceSequence: "31", entry: archivedStderr(31) }]
            : [],
      };
    },
  ],
  // No runner here admits a command, so one the page delivers on load stays unadmitted.
  [
    "POST",
    /^\/upstream\/dispatcher\/v1\/sessions\/([0-9a-f-]+):(?:submitInput|changeModel|changeReasoningEffort|interruptTurn)$/,
    () =>
      flags.has("commandAdmissionTimedOut")
        ? Response.json({ code: 4, message: "deadline exceeded" }, { status: 504 })
        : UNANSWERED,
  ]
);

/** What the live stream at `url` sends, when it opens and whenever a fixture publishes it again. */
function liveEvents(url: URL): ServerSentEvent[] {
  if (/^\/sandboxes\/[^/]+\/console\/stream$/.test(url.pathname)) {
    return [{ event: "output", data: btoa("guest console output\r\n") }];
  }
  throw new Error(`Unexpected event stream route: ${url.pathname}`);
}

/** Whether the stream at `url` drops once it has sent its events. */
function drops(url: URL): boolean {
  if (url.pathname === "/live/threads") return flags.has("threadStreamDropped");
  if (url.pathname === "/upstream/environments/v1/environments:watch")
    return flags.has("inventoryStreamDropped") || flags.has("watchStale");
  if (/^\/upstream\/notifications\/v1\/sessions\//.test(url.pathname))
    return (
      flags.has("notificationStatusUnavailable") ||
      (url.pathname.endsWith("/inbox:watch") && flags.has("notificationStreamDropped"))
    );
  return false;
}

// A dropped stream is not answered again while its drop flag stays set, as on a network that stays
// down; once the flag is cleared it is served afresh.
const droppedStreams = new Set<string>();

routes.unshift([
  "GET",
  /^\/(live\/.+|upstream\/notifications\/v1\/sessions\/[^/]+\/(?:inbox:watch|inbox\/entries:watch|subscriptions:watch)|sandboxes\/[^/]+\/console\/stream)$/,
  (match, query, signal) => {
    const url = new URL(`${match[0]}?${query.toString()}`, "http://harness");
    const key = `${url.pathname}${url.search}`;
    const drop = drops(url);
    if (!drop) droppedStreams.delete(key);
    else if (droppedStreams.has(key)) return UNANSWERED;
    else droppedStreams.add(key);
    const notice = url.pathname.match(
      /^\/upstream\/notifications\/v1\/sessions\/([^/]+)\/(inbox:watch|inbox\/entries:watch|subscriptions:watch)$/
    );
    if (notice) {
      const session = decodeURIComponent(notice[1]);
      return liveNdjsonWatch(
        url.pathname,
        flags.has("notificationStatusUnavailable") ? [] : notificationFrames(session, notice[2], false),
        () => notificationFrames(session, notice[2], true),
        signal,
        drop
      );
    }
    return liveEventStream(url.pathname, () => liveEvents(url), signal, drop);
  },
]);

routes.unshift([
  "GET",
  /^\/upstream\/environments\/v1\/environments\/([^/]+)\/([^/]+):watch$/,
  (match, query, signal) => {
    const frames = (): unknown[] => {
      const environment = SANDBOXES.find(
        (item) =>
          item.target?.namespace === decodeURIComponent(match[1]) &&
          item.target.name === decodeURIComponent(match[2]) &&
          item.target.uid === query.get("target.uid")
      );
      return environment ? [{ environment: toJson(EnvironmentDescriptorSchema, environment) }] : [];
    };
    return liveNdjsonWatch(match[0], frames(), frames, signal, flags.has("watchStale"));
  },
]);

routes.unshift([
  "GET",
  /^\/upstream\/environments\/v1\/environments:watch$/,
  (_match, _query, signal) => {
    const snapshot = [
      { snapshot: { environments: SANDBOXES.map((sandbox) => toJson(EnvironmentDescriptorSchema, sandbox)) } },
      { snapshotComplete: {} },
    ];
    let previous = new Map(
      SANDBOXES.map((sandbox) => [
        sandbox.target!.name,
        { uid: sandbox.target!.uid, namespace: sandbox.target!.namespace },
      ])
    );
    const updates = (): unknown[] => {
      const current = new Map(
        SANDBOXES.map((sandbox) => [
          sandbox.target!.name,
          { uid: sandbox.target!.uid, namespace: sandbox.target!.namespace },
        ])
      );
      const removals = Array.from(previous, ([name, old]) =>
        current.get(name)?.uid === old.uid ? null : { removed: { namespace: old.namespace, name, uid: old.uid } }
      ).filter((frame) => frame !== null);
      previous = current;
      return [...removals, ...SANDBOXES.map((sandbox) => ({ upsert: toJson(EnvironmentDescriptorSchema, sandbox) }))];
    };
    return liveNdjsonWatch(
      "/upstream/environments/v1/environments:watch",
      snapshot,
      updates,
      signal,
      flags.has("inventoryStreamDropped") || flags.has("watchStale")
    );
  },
]);

routes.unshift(
  [
    "GET",
    /^\/upstream\/egress\/v1\/decisions:watch$/,
    (_match, _query, signal) =>
      window.matchMedia("(max-width: 600px)").matches
        ? Response.json({ code: 14, message: "the egress proxy did not answer: connection refused" }, { status: 503 })
        : ndjsonWatch(
            [
              toJson(
                WatchDecisionsResponseSchema,
                create(WatchDecisionsResponseSchema, {
                  frame: { case: "snapshot", value: { decisions: DECISIONS } },
                })
              ),
            ],
            signal
          ),
  ],
  [
    "GET",
    /^\/upstream\/egress\/v1\/credentials:watch$/,
    (_match, _query, signal) =>
      ndjsonWatch(
        [
          toJson(
            WatchCredentialsResponseSchema,
            create(WatchCredentialsResponseSchema, {
              frame: {
                case: "snapshot",
                value: {
                  credentials: [
                    {
                      name: "harness-github-pat",
                      uid: "credential-uid",
                      description: "GitHub API access",
                      targets: [{ method: 2, header: "Authorization", scheme: "Bearer" }],
                    },
                  ],
                },
              },
            })
          ),
          toJson(
            WatchCredentialsResponseSchema,
            create(WatchCredentialsResponseSchema, { frame: { case: "snapshotComplete", value: {} } })
          ),
        ],
        signal
      ),
  ],
  [
    "GET",
    /^\/upstream\/policy\/v1\/policy-bindings:watch$/,
    (_match, query, signal) => {
      const subject = { namespace: query.get("subject.namespace") ?? "", name: query.get("subject.name") ?? "" };
      const bindings = BINDINGS.map((item) => clone(PolicyBindingSchema, item)).map((item) => {
        item.spec!.subject = create(ServiceAccountIdentitySchema, { account: subject }).account;
        return item;
      });
      return ndjsonWatch(
        [
          toJson(
            WatchPolicyBindingsResponseSchema,
            create(WatchPolicyBindingsResponseSchema, { frame: { case: "snapshot", value: { bindings } } })
          ),
          toJson(
            WatchPolicyBindingsResponseSchema,
            create(WatchPolicyBindingsResponseSchema, { frame: { case: "snapshotComplete", value: {} } })
          ),
        ],
        signal
      );
    },
  ],
  [
    "GET",
    /^\/upstream\/policy\/v1\/policy-sets:watch$/,
    (_match, _query, signal) =>
      ndjsonWatch(
        [
          toJson(
            WatchPolicySetsResponseSchema,
            create(WatchPolicySetsResponseSchema, { frame: { case: "snapshot", value: { policySets: POLICY_SETS } } })
          ),
          toJson(
            WatchPolicySetsResponseSchema,
            create(WatchPolicySetsResponseSchema, { frame: { case: "snapshotComplete", value: {} } })
          ),
        ],
        signal
      ),
  ]
);

routes.unshift(
  [
    "GET",
    /^\/upstream\/actions\/v1\/action-policy:watch$/,
    (_match, query, signal) =>
      ndjsonWatch(
        [
          toJson(
            GetActionPolicyResponseSchema,
            actionPolicy(query.get("subject.namespace") ?? "", query.get("subject.name") ?? "")
          ),
        ],
        signal
      ),
  ],
  [
    "GET",
    /^\/upstream\/app\/v1\/threads:watch$/,
    (_match, _query, signal) =>
      liveNdjsonWatch(
        "/upstream/app/v1/threads:watch",
        [{ changed: {}, updatesConnected: !flags.has("threadDatabaseDisconnected") }],
        () => [{ changed: {}, updatesConnected: !flags.has("threadDatabaseDisconnected") }],
        signal,
        flags.has("threadStreamDropped")
      ),
  ],
  [
    "GET",
    /^\/upstream\/history\/v1\/session-summaries:watch$/,
    (_match, _query, signal) =>
      liveNdjsonWatch(
        "/upstream/history/v1/session-summaries:watch",
        [
          toJson(
            WatchSessionSummariesResponseSchema,
            create(WatchSessionSummariesResponseSchema, { observation: { case: "changed", value: {} } })
          ),
        ],
        () => [
          toJson(
            WatchSessionSummariesResponseSchema,
            create(WatchSessionSummariesResponseSchema, { observation: { case: "changed", value: {} } })
          ),
        ],
        signal,
        flags.has("threadStreamDropped")
      ),
  ]
);

// Mutable mock service state; Python fixture methods change it before mounting React.
let previewAction: ActionRequest | null = null;
let queueCreateResponses = false;
let queuedSessionId: string | null = null;

function projectQueuedSession(): void {
  if (!queuedSessionId) throw new Error("No queued Session Create to project");
  const sessionId = queuedSessionId;
  const source = THREADS[0];
  if (!source) throw new Error("No Session fixture to copy");
  const sourceAttached = source.summary.attached;
  if (!sourceAttached) throw new Error("Queued Session source has no attachment");
  const projected = threadView(
    {
      ...source.metadata,
      threadId: sessionId,
      name: "Waiting Session thread",
      createdAt: timestampFromDate(new Date(ago(0))),
    },
    {
      ...source.summary,
      sessionId,
      foldedThrough: 0n,
      lastEventAt: timestampFromDate(new Date(ago(0))),
      feedStatus: FeedStatus.ACTIVE,
      attached: {
        ...create(AttachedSchema, sourceAttached),
        sessionId,
        lastCursor: 0n,
        harnessState: HarnessState.STOPPED,
        activeTurnId: "",
      },
    }
  );
  THREADS.push(projected);
  THREADS_WITH_SANDBOXES.push(projected);
  publishStreams("/upstream/app/v1/threads:watch");
  publishStreams("/upstream/history/v1/session-summaries:watch");
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
    const sandbox = SANDBOXES.find((candidate) => candidate.target?.name === name);
    if (!sandbox?.kubernetesDiagnostics?.pod) throw new Error(`Sandbox ${name} has no running Pod fixture`);
    sandbox.kubernetesDiagnostics.pod.ownerReferences = [
      create(OwnerReferenceSchema, {
        apiVersion: "kubevirt.io/v1",
        kind: "VirtualMachineInstance",
        name,
        uid: `${sandbox.target!.uid}-vmi`,
        controller: true,
      }),
    ];
  },
  nameThread(threadId: string, name: string): void {
    const thread = THREADS_WITH_SANDBOXES.find((candidate) => candidate.metadata.threadId === threadId);
    if (!thread) throw new Error(`Missing thread fixture ${threadId}`);
    thread.metadata.name = name;
  },
  centralSamplerOnSandbox(threadId: string, sandboxName: string): void {
    const thread = THREADS_WITH_SANDBOXES.find((candidate) => candidate.metadata.threadId === threadId);
    if (!thread) throw new Error(`Missing thread fixture ${threadId}`);
    const target = SANDBOXES.find((candidate) => candidate.target?.name === sandboxName)?.target;
    if (!target) throw new Error(`Missing Sandbox fixture ${sandboxName}`);
    const spec = thread.summary.attached?.spec;
    if (!spec) throw new Error(`Thread fixture ${threadId} has no attached Session spec`);
    thread.summary.sandbox = create(SandboxRefSchema, {
      namespace: target.namespace,
      name: target.name,
      uid: target.uid,
    });
    spec.placement = create(SessionPlacementSchema, {
      kind: {
        case: "centralSamplerV1",
        value: create(CentralSamplerV1PlacementSchema, {
          executorEnvironment: { name: target.name, uid: target.uid },
        }),
      },
    });
    spec.model = "harness-codex-model";
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
  queueCreateResponse(): void {
    queueCreateResponses = true;
  },
  projectQueuedSession,
  set(flag: Flag, on = true): void {
    if (on) flags.add(flag);
    else flags.delete(flag);
  },
  /** Sets `flag` and serves the open streams whose path ends in `pathSuffix` again. */
  publish(flag: Flag, pathSuffix: string): void {
    flags.add(flag);
    publishStreams(pathSuffix);
  },
  ageOutage(milliseconds: number): void {
    streamRegistry.now = () => Date.now() + milliseconds;
  },
  actionRendererKeys(): string[] {
    return Array.from(ACTION_RENDERERS, ([group, renderers]) =>
      Array.from(renderers.keys(), (name) => `${group}.${name}`)
    ).flat();
  },
  showActionPreview(action: { group: string; name: string }, args: NonNullable<ActionRequest["arguments"]>): void {
    const preview = clone(ActionRequestSchema, COMPACT_POD_ACTION);
    preview.action = create(ActionIdentitySchema, action);
    preview.arguments = args;
    preview.title =
      action.group === "kubernetes_admin" && action.name === "pods_list_in_namespace"
        ? "inspect running demo pods"
        : `review ${action.name.replaceAll("_", " ")}`;
    preview.description = "Check the exact call before approving.";
    previewAction = preview;
  },
  showActionResult(
    action: { group: string; name: string },
    args: NonNullable<ActionRequest["arguments"]>,
    result: JsonValue
  ): void {
    ACTIONS[2]!.action = create(ActionIdentitySchema, action);
    ACTIONS[2]!.arguments = args;
    ACTIONS[2]!.title = `Review ${action.name} result`;
    ACTIONS[2]!.execution!.result = fromJson(ValueSchema, result);
  },
  showActionReceipt(
    action: { group: string; name: string },
    args: NonNullable<ActionRequest["arguments"]>,
    result: JsonValue
  ): void {
    receiptActionGroup =
      action.group === "sandbox" || MCP_GROUPS.some((group) => group.key === action.group)
        ? undefined
        : mcpGroup(action.group, "Test MCP backend for the receipt renderer.");
    const receipt = clone(ActionRequestSchema, ACTIONS[2]!);
    receipt.action = create(ActionIdentitySchema, action);
    receipt.arguments = args;
    receipt.title = "Long action text";
    receipt.description = undefined;
    if (receipt.execution === undefined) throw new Error("Receipt fixture has no Execution.");
    receipt.execution.result = fromJson(ValueSchema, result);
    ACTIONS.splice(0, ACTIONS.length, receipt);
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
    ACTIONS[2]!.execution!.result = fromJson(ValueSchema, {
      _meta: { "io.modelcontextprotocol/serverInfo": { name: "ssh-mcp", version: "4.0.3" } },
      content: [{ type: "text", text: JSON.stringify(result) }],
      structuredContent: result,
      isError: false,
    });
  },
  recovery(kind: "messages" | "tools" | "quiet"): void {
    threadEntityRows = (id) => recoveryRows(id, kind);
  },
  failedTurn(afterContent: boolean): void {
    mapThread = withFailedTurn;
    threadEntityRows = (id) => failedRows(id, afterContent);
  },
  dormantSession(): void {
    mapThread = withDormantSession;
    threadEntityRows = interleavedRows;
  },
  interleavedEvents(): void {
    threadEntityRows = interleavedRows;
  },
  shortThreadSwitchHistory(): void {
    mapThread = (thread) =>
      thread.metadata.threadId === IDLE_THREAD
        ? { ...thread, metadata: { ...thread.metadata, name: "Short thread arriving" } }
        : thread;
    threadEntityRows = shortThreadSwitchRows;
  },
  historyProfile(rowCount: number): void {
    const rowsByThread = new Map<string, Record<string, unknown>[]>();
    threadEntityRows = (threadId) => {
      let rows = rowsByThread.get(threadId);
      if (!rows) {
        rows = historyProfileRows(threadId, rowCount);
        rowsByThread.set(threadId, rows);
      }
      return rows;
    };
  },
  rememberHistoryRowHeight(threadId: string, width: number, key: string, height: number): void {
    rememberRowHeight(threadId, width, key, height);
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
  collapsibleCommandHistory(): void {
    threadEntityRows = (id) => collapsibleCommandRows(id, 8);
  },
  midThreadCommandHistory(): void {
    threadEntityRows = (id) => collapsibleCommandRows(id, 40);
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
    threadEntityRows = (id) =>
      id === THREADS[2].metadata.threadId ? statesRows(id) : standardRows(id, longPreview, longBody);
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
      thread.metadata.threadId === IDLE_THREAD
        ? {
            ...thread,
            metadata: { ...thread.metadata, name: "Completed diagnostic run" },
            summary: {
              ...thread.summary,
              lastTurnStatus: TurnStatus.COMPLETED,
              attached: {
                ...thread.summary.attached!,
                spec: visualNativeSpec(Harness.CODEX, "ready-sandbox"),
                activeTurnId: "",
              },
            },
          }
        : thread;
    threadEntityRows = (id) => realisticRolloutRows(id, rollout);
  },
  reportedRollout(): void {
    mapThread = (thread) =>
      thread.metadata.threadId === IDLE_THREAD
        ? {
            ...thread,
            metadata: { ...thread.metadata, name: "Reported thread excerpt" },
            summary: {
              ...thread.summary,
              lastTurnStatus: TurnStatus.COMPLETED,
              attached: {
                ...thread.summary.attached!,
                spec: visualNativeSpec(Harness.CODEX, "ready-sandbox"),
                activeTurnId: "",
              },
            },
          }
        : thread;
    threadEntityRows = (id) => realisticRolloutRows(id, reportedRollout);
  },
};
Object.assign(window, { agentplaneVisual: visualHarness });
