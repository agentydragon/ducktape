import { fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";
import type { JsonFrame } from "./live_stream";

import {
  NoticeProgress as NoticeProgressValue,
  NoticeWaitReason as NoticeWaitReasonValue,
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
import {
  SourceFailureKind as SourceFailureKindValue,
  type InboxJson,
  type GitHubAccessStatusJson,
  type GitHubEventFilterJson,
  GitHubEventName as GitHubEventNameValue,
  type GitHubEventNameJson,
  type GitHubSourceJson,
  type GitHubStatusJson,
  type GitHubSubjectJson,
  type GitHubSubjectStatusJson,
  type SourceJson,
  type SourceFailureKindJson,
} from "../../proto/notification_operator_proto_generated/agentplane/notification_service/notifications_pb";

type RequiredJson<T, K extends keyof T> = Omit<T, K> & Required<Pick<T, K>>;
type Override<T, R> = Omit<T, keyof R> & R;
type UnknownProtoEnum = { unknown: number };
type ForwardCompatibleEnum<T extends string> = T | UnknownProtoEnum;

function enumNumbers(values: object): Set<number> {
  return new Set(Object.values(values).filter((value): value is number => typeof value === "number"));
}

const enumFieldNumbers = new Map<string, Set<number>>([
  ["errorKind", enumNumbers(SourceFailureKindValue)],
  ["event", enumNumbers(GitHubEventNameValue)],
  ["notice", enumNumbers(NoticeProgressValue)],
  ["noticeWaitReason", enumNumbers(NoticeWaitReasonValue)],
]);

/** Preserve numeric values assigned by a newer protobuf writer as an explicit unknown variant.
 * Generated Protobuf-ES accepts these numeric ProtoJSON enum values while its generated JSON type
 * describes only the enum names, so normalize them before returning that JSON-shaped API. */
function preserveUnknownEnums(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map(preserveUnknownEnums);
  if (typeof value !== "object" || value === null) return value;
  const normalized: Record<string, JsonValue> = {};
  for (const [key, field] of Object.entries(value)) {
    const knownValues = enumFieldNumbers.get(key);
    if (knownValues && typeof field === "number" && !knownValues.has(field)) {
      normalized[key] = { unknown: field };
    } else {
      normalized[key] = preserveUnknownEnums(field);
    }
  }
  return normalized;
}

type InboxView = RequiredJson<
  InboxJson,
  "id" | "sessionId" | "lastCursor" | "acknowledged" | "covered" | "expiredThrough" | "retired"
>;
type EntrySummaryView = RequiredJson<InboxEntrySummaryJson, "cursor" | "createdAt" | "provider" | "summary">;
type GitHubAccessView = Override<
  RequiredJson<GitHubAccessStatusJson, "appId" | "installationId" | "repositoryId" | "currentlyValid">,
  { errorKind?: SourceFailureKind }
>;
type GitHubSubjectStatusView = Override<
  RequiredJson<GitHubSubjectStatusJson, "repositoryId" | "subject">,
  { errorKind?: SourceFailureKind }
>;
type GitHubStatusView = Override<
  RequiredJson<GitHubStatusJson, "access" | "subject">,
  {
    access: GitHubAccessView[];
    subject: GitHubSubjectStatusView;
  }
>;
type GitHubEventFilterView = Override<GitHubEventFilterJson, { event?: ForwardCompatibleEnum<GitHubEventNameJson> }>;
type GitHubSourceView = Override<GitHubSourceJson, { events?: GitHubEventFilterView[] }>;

export type SourceFailureKind = ForwardCompatibleEnum<SourceFailureKindJson>;
export type NoticeWaitReason = ForwardCompatibleEnum<NoticeWaitReasonJson>;
export type NoticeProgress = ForwardCompatibleEnum<NoticeProgressJson>;
export type GitHubSubject = GitHubSubjectJson;
export type Source = Override<SourceJson, { github?: GitHubSourceView }>;
export type GitHubAccessStatus = GitHubAccessView;
export type GitHubSubjectStatus = GitHubSubjectStatusView;
export type SourceObservation = Override<
  Pick<
    GitHubAccessStatusJson,
    "lastSuccessAt" | "errorKind" | "error" | "errorSince" | "errorObservedAt" | "retryAt" | "refreshingUntil"
  >,
  { errorKind?: SourceFailureKind }
>;
export type SubscriptionStatus = Override<
  RequiredJson<SubscriptionStatusJson, "id" | "source" | "cancelled" | "expiresAt">,
  {
    source: Source;
    github?: GitHubStatusView;
    errorKind?: SourceFailureKind;
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
    noticeWaitReason?: NoticeWaitReason;
  }
>;
export type SandboxNotificationStatus = Override<
  RequiredJson<SandboxNotificationStatusJson, "observedAt" | "inboxes">,
  {
    inboxes: InboxStatus[];
  }
>;
export type InboxNoticeState = Override<
  RequiredJson<InboxNoticeStateJson, "inboxId" | "sessionId" | "queued" | "unacknowledged">,
  { notice?: NoticeProgress; noticeWaitReason?: NoticeWaitReason }
>;
/** Generated wire shape, before ProtoJSON numeric unknown enums become named UI variants. */
export type InboxNoticeStateProtoJson = InboxNoticeStateJson;
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
  // alwaysEmitImplicit widens to JsonValue; the generated schema above controls its encoding, and
  // numeric enum values outside this version's generated enum tables are retained as named variants.
  const json = preserveUnknownEnums(
    toJson(SandboxNotificationStatusSchema, message, {
      alwaysEmitImplicit: true,
    })
  );
  return json as SandboxNotificationStatus;
}

/** Decodes the thread-list ProtoJSON snapshot and requires the observation timestamp the service always writes. */
export function decodeSessionNoticeStates(data: string): SessionNoticeStates {
  const message = fromJson(SessionNoticeStatesSchema, parseJson(data));
  requireObservedAt(message);
  const json = preserveUnknownEnums(toJson(SessionNoticeStatesSchema, message, { alwaysEmitImplicit: true }));
  return json as SessionNoticeStates;
}

/** Convert generated ProtoJSON validation failures into the stream's explicit invalid-frame result. */
export function decodeSessionNoticeStatesFrame(data: string): JsonFrame<SessionNoticeStates> {
  try {
    return { success: true, data: decodeSessionNoticeStates(data) };
  } catch (reason) {
    return {
      success: false,
      error: `Invalid thread notification snapshot: ${reason instanceof Error ? reason.message : String(reason)}`,
    };
  }
}
