import type {
  InboxEntrySummaryJson,
  NoticeWaitReasonJson,
} from "./notification_operator_proto_generated/agentplane/notification_service/notifications_operator_pb.js";

const cursor: InboxEntrySummaryJson["cursor"] = "9007199254740993";
const waitReason: NoticeWaitReasonJson = "NOTICE_WAIT_REASON_DEBOUNCING";

// int64 values in ProtoJSON use decimal strings, so a JS number is not safe here.
// @ts-expect-error
const numericCursor: InboxEntrySummaryJson["cursor"] = 9007199254740993;

void [cursor, waitReason, numericCursor];
