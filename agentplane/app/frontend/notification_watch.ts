/** Decode the generated Sandbox watch. */
import { fromJson, type JsonValue } from "@bufbuild/protobuf";

import {
  WatchSandboxNotificationsResponseSchema,
  type SandboxNotificationStatus,
} from "../../notification_service/notifications_operator_pb";

export type NotificationWatchFrame = { kind: "snapshot"; status: SandboxNotificationStatus } | { kind: "reconnect" };

export function decodeNotificationWatchFrame(json: unknown, sawSnapshot: boolean): NotificationWatchFrame {
  const response = fromJson(WatchSandboxNotificationsResponseSchema, json as JsonValue);
  if (response.frame.case === "snapshot") return { kind: "snapshot", status: response.frame.value };
  if (response.frame.case === "reconnectRequired") {
    if (!sawSnapshot) throw new Error("Notification watch renewed before its initial snapshot");
    return { kind: "reconnect" };
  }
  throw new Error("Notification watch returned no frame");
}
