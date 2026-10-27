/**
 * The Notification Service's operator snapshots as the app relays them to the browser: the proto3 JSON of
 * `agentplane/notification_service/notifications_operator.proto`. Names are camelCase, enums are their value
 * names, int64 is a string, a Timestamp is RFC 3339, and a message or `optional` field the service did not set
 * has no key. A field is required here when the service always sets it.
 *
 * Hand-written: the generated TypeScript modules import `google/api/annotations_pb`, which no target provides
 * where they look for it.
 */

type Timestamp = string;
type Int64 = string;

export type SourceFailureKind =
  | "SOURCE_FAILURE_KIND_RATE_LIMITED"
  | "SOURCE_FAILURE_KIND_UNAVAILABLE"
  | "SOURCE_FAILURE_KIND_ACCESS_DENIED"
  | "SOURCE_FAILURE_KIND_SOURCE_CHANGED"
  | "SOURCE_FAILURE_KIND_PROCESSING_ERROR";

export type NoticeWaitReason =
  | "NOTICE_WAIT_REASON_RETIRED"
  | "NOTICE_WAIT_REASON_NOTICE_ERROR"
  | "NOTICE_WAIT_REASON_DELIVERY_RETRY"
  | "NOTICE_WAIT_REASON_AWAITING_CONFIRMATION"
  | "NOTICE_WAIT_REASON_DEBOUNCING"
  | "NOTICE_WAIT_REASON_READY";

export type NoticeProgress =
  "NOTICE_PROGRESS_PREPARED" | "NOTICE_PROGRESS_ATTEMPTED" | "NOTICE_PROGRESS_ADMITTED" | "NOTICE_PROGRESS_CONFIRMED";

/** The one key present names the kind of subject. */
export type GitHubSubject =
  | { pullRequest: { number: number } }
  | { issue: { number: number } }
  | { branch: { name: string } }
  | { commit: { sha: string } };

/** The one key present names the provider. */
export type Source = { actions: { requestId: string } } | { github: { repository: string; subject?: GitHubSubject } };

/** What a GitHub access grant, a GitHub subject and a subscription each observe of their own source. */
export type SourceObservation = {
  lastSuccessAt?: Timestamp;
  errorKind?: SourceFailureKind;
  error?: string;
  errorSince?: Timestamp;
  errorObservedAt?: Timestamp;
  retryAt?: Timestamp;
  refreshingUntil?: Timestamp;
};

export type GitHubAccessStatus = SourceObservation & {
  appId: Int64;
  installationId: Int64;
  repositoryId: Int64;
  checkedAt?: Timestamp;
  validUntil?: Timestamp;
  currentlyValid: boolean;
};

export type GitHubSubjectStatus = SourceObservation & { repositoryId: Int64; subject: GitHubSubject };

export type SubscriptionStatus = SourceObservation & {
  id: string;
  source: Source;
  cancelled: boolean;
  expiresAt: Timestamp;
  nextSourceCheckAt?: Timestamp;
  github?: { access: GitHubAccessStatus[]; subject: GitHubSubjectStatus };
};

export type InboxStatus = {
  inbox: {
    id: string;
    sessionId: string;
    lastCursor: Int64;
    acknowledged: Int64;
    covered: Int64;
    expiredThrough: Int64;
    retired: boolean;
    deliveryError?: string;
  };
  notice?: { throughCursor: Int64; attempted: boolean; admitted: boolean; confirmed: boolean; error?: string };
  subscriptions: SubscriptionStatus[];
  unannouncedCount: number;
  pendingAcknowledgementCount: number;
  pendingEntries: { cursor: Int64; createdAt: Timestamp; provider: string; summary: string }[];
  pendingEntriesMore: boolean;
  noticeDueAt?: Timestamp;
  quietUntil?: Timestamp;
  maxWaitAt?: Timestamp;
  noticeWaitReason?: NoticeWaitReason;
  nextWorkAt?: Timestamp;
};

export type SandboxNotificationStatus = { observedAt: Timestamp; inboxes: InboxStatus[] };

export type InboxNoticeState = {
  inboxId: string;
  sessionId: string;
  queued: number;
  unacknowledged: number;
  notice?: NoticeProgress;
  noticeWaitReason?: NoticeWaitReason;
  eligibleAt?: Timestamp;
  error?: string;
};

export type SessionNoticeStates = { observedAt: Timestamp; inboxes: InboxNoticeState[] };
