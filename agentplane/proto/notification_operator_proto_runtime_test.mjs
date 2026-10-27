import assert from "node:assert/strict";
import { fromJson, getOption, toJson } from "@bufbuild/protobuf";

import { http } from "./notification_operator_proto_generated/google/api/annotations_pb.js";
import {
  InboxStatusSchema,
  NotificationsOperator,
  SandboxNotificationStatusSchema,
} from "./notification_operator_proto_generated/agentplane/notification_service/notifications_operator_pb.js";

const watch = getOption(NotificationsOperator.method.watchSandboxNotifications, http);
assert.equal(watch.pattern.case, "get");
assert.equal(watch.pattern.value, "/v1/operator/sandboxes/{namespace}/{name}/notifications:watch");

const snapshot = fromJson(SandboxNotificationStatusSchema, {
  observedAt: "2026-10-27T18:00:00Z",
  inboxes: [
    {
      inbox: {
        id: "inbox-test",
        sessionId: "session-test",
        lastCursor: "9007199254740993",
        acknowledged: "9007199254740990",
        covered: "9007199254740992",
        expiredThrough: "0",
        retired: false,
      },
      subscriptions: [],
      unannouncedCount: 3,
      pendingAcknowledgementCount: 2,
      pendingEntries: [
        {
          cursor: "9007199254740993",
          createdAt: "2026-10-27T17:59:00Z",
          provider: "actions",
          summary: "A completed Action request",
        },
      ],
      pendingEntriesMore: false,
      noticeWaitReason: "NOTICE_WAIT_REASON_DEBOUNCING",
    },
  ],
});
const json = toJson(SandboxNotificationStatusSchema, snapshot);
assert.equal(json.observedAt, "2026-10-27T18:00:00Z");
assert.equal(json.inboxes?.[0]?.inbox?.lastCursor, "9007199254740993");
assert.equal(json.inboxes?.[0]?.pendingEntries?.[0]?.cursor, "9007199254740993");
assert.equal(json.inboxes?.[0]?.noticeWaitReason, "NOTICE_WAIT_REASON_DEBOUNCING");

const defaults = fromJson(InboxStatusSchema, {});
assert.equal(defaults.unannouncedCount, 0);
assert.deepEqual(defaults.pendingEntries, []);
assert.equal(defaults.noticeWaitReason, undefined);

assert.throws(
  () => fromJson(InboxStatusSchema, { unannouncedCount: "three" }),
  "a malformed proto3 integer is rejected"
);
assert.throws(
  () => fromJson(InboxStatusSchema, { noticeWaitReason: "NOTICE_WAIT_REASON_FUTURE_VALUE" }),
  "an unknown notice state is rejected rather than misreported as a known state"
);
