import { fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";

import {
  SandboxNotificationStatusSchema,
  SessionNoticeStatesSchema,
  type InboxNoticeStateJson,
  type InboxEntrySummaryJson,
  type InboxStatusJson,
  type NoticeProgressJson,
  type NoticeWaitReasonJson,
  type SandboxNotificationStatus as SandboxNotificationStatusMessage,
  type SandboxNotificationStatusJson,
  type SessionNoticeStatesJson,
  type SubscriptionStatusJson,
} from "../../proto/notification_operator_proto_generated/agentplane/notification_service/notifications_operator_pb";
import type {
  InboxJson,
  GitHubAccessStatusJson,
  GitHubStatusJson,
  GitHubSubjectJson,
  GitHubSubjectStatusJson,
  SourceJson,
  SourceFailureKindJson,
} from "../../proto/notification_operator_proto_generated/agentplane/notification_service/notifications_pb";

type RequiredJson<T, K extends keyof T> = Omit<T, K> & Required<Pick<T, K>>;
type Override<T, R> = Omit<T, keyof R> & R;

type InboxView = RequiredJson<
  InboxJson,
  "id" | "sessionId" | "lastCursor" | "acknowledged" | "covered" | "expiredThrough" | "retired"
>;
type EntrySummaryView = RequiredJson<InboxEntrySummaryJson, "cursor" | "createdAt" | "provider" | "summary">;
type GitHubAccessView = RequiredJson<
  GitHubAccessStatusJson,
  "appId" | "installationId" | "repositoryId" | "currentlyValid"
>;
type GitHubSubjectStatusView = RequiredJson<GitHubSubjectStatusJson, "repositoryId" | "subject">;
type GitHubStatusView = Override<
  RequiredJson<GitHubStatusJson, "access" | "subject">,
  {
    access: GitHubAccessView[];
    subject: GitHubSubjectStatusView;
  }
>;

export type SourceFailureKind = SourceFailureKindJson;
export type NoticeWaitReason = NoticeWaitReasonJson;
export type NoticeProgress = NoticeProgressJson;
export type GitHubSubject = GitHubSubjectJson;
export type Source = SourceJson;
export type GitHubAccessStatus = GitHubAccessView;
export type GitHubSubjectStatus = GitHubSubjectStatusView;
export type SourceObservation = Pick<
  GitHubAccessStatusJson,
  "lastSuccessAt" | "errorKind" | "error" | "errorSince" | "errorObservedAt" | "retryAt" | "refreshingUntil"
>;
export type SubscriptionStatus = Override<
  RequiredJson<SubscriptionStatusJson, "id" | "source" | "cancelled" | "expiresAt">,
  {
    github?: GitHubStatusView;
  }
>;
export type InboxStatus = Override<
  RequiredJson<
    InboxStatusJson,
    | "inbox"
    | "subscriptions"
    | "unannouncedCount"
    | "pendingAcknowledgementCount"
    | "pendingEntries"
    | "pendingEntriesMore"
  >,
  {
    inbox: InboxView;
    subscriptions: SubscriptionStatus[];
    pendingEntries: EntrySummaryView[];
  }
>;
export type SandboxNotificationStatus = Override<
  RequiredJson<SandboxNotificationStatusJson, "observedAt" | "inboxes">,
  {
    inboxes: InboxStatus[];
  }
>;
export type InboxNoticeState = RequiredJson<
  InboxNoticeStateJson,
  "inboxId" | "sessionId" | "queued" | "unacknowledged"
>;
export type SessionNoticeStates = Override<
  RequiredJson<SessionNoticeStatesJson, "observedAt" | "inboxes">,
  {
    inboxes: InboxNoticeState[];
  }
>;

function isJsonValue(value: unknown): value is JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isJsonValue);
  return typeof value === "object" && Object.values(value).every(isJsonValue);
}

function parseJson(data: string): JsonValue {
  const value: unknown = JSON.parse(data);
  if (!isJsonValue(value)) throw new TypeError("notification snapshot must be JSON");
  return value;
}

function requireObservedAt(value: { observedAt?: unknown }): void {
  if (value.observedAt === undefined) throw new TypeError("notification snapshot is missing observedAt");
}

function requireSandboxMessageFields(message: SandboxNotificationStatusMessage): void {
  requireObservedAt(message);
  for (const status of message.inboxes) {
    if (status.inbox === undefined) throw new TypeError("notification inbox status is missing inbox");
    for (const subscription of status.subscriptions) {
      if (subscription.source === undefined || subscription.expiresAt === undefined)
        throw new TypeError("notification subscription is missing source or expiry");
      if (subscription.source.provider.case === undefined)
        throw new TypeError("notification subscription source is missing its provider");
      if (
        subscription.source.provider.case === "github" &&
        subscription.source.provider.value.subject?.subject.case === undefined
      )
        throw new TypeError("GitHub subscription source is missing its subject");
      if (subscription.github !== undefined && subscription.github.subject?.subject?.subject.case === undefined)
        throw new TypeError("GitHub subscription status is missing subject");
    }
    for (const entry of status.pendingEntries)
      if (entry.createdAt === undefined) throw new TypeError("notification entry summary is missing createdAt");
  }
}

/** Decodes the operator endpoint's ProtoJSON and requires the observation timestamp the service always writes. */
export function decodeSandboxNotificationStatus(data: string): SandboxNotificationStatus {
  const message = fromJson(SandboxNotificationStatusSchema, parseJson(data));
  requireSandboxMessageFields(message);
  // alwaysEmitImplicit widens to JsonValue, but the generated schema above validated this message and controls its encoding.
  const json = toJson(SandboxNotificationStatusSchema, message, {
    alwaysEmitImplicit: true,
  }) as SandboxNotificationStatusJson;
  return json as SandboxNotificationStatus;
}

/** Decodes the thread-list ProtoJSON snapshot and requires the observation timestamp the service always writes. */
export function decodeSessionNoticeStates(data: string): SessionNoticeStates {
  const message = fromJson(SessionNoticeStatesSchema, parseJson(data));
  requireObservedAt(message);
  const json = toJson(SessionNoticeStatesSchema, message, { alwaysEmitImplicit: true }) as SessionNoticeStatesJson;
  return json as SessionNoticeStates;
}
