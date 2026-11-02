/** Decode the generated Sandbox watch while retaining the drawer's current display projection. */
import { fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";

import type { SandboxNotificationStatus } from "./notification_types";
import {
  SandboxNotificationStatusSchema,
  WatchSandboxNotificationsResponseSchema,
} from "../../notification_service/notifications_operator_pb";

export type NotificationWatchFrame = { kind: "snapshot"; status: SandboxNotificationStatus } | { kind: "reconnect" };

export function decodeNotificationWatchFrame(json: unknown, sawSnapshot: boolean): NotificationWatchFrame {
  const response = fromJson(WatchSandboxNotificationsResponseSchema, json as JsonValue);
  if (response.frame.case === "snapshot") {
    return {
      kind: "snapshot",
      status: toJson(SandboxNotificationStatusSchema, response.frame.value, {
        alwaysEmitImplicit: true,
      }) as unknown as SandboxNotificationStatus,
    };
  }
  if (response.frame.case === "reconnectRequired") {
    if (!sawSnapshot) throw new Error("Notification watch renewed before its initial snapshot");
    return { kind: "reconnect" };
  }
  throw new Error("Notification watch returned no frame");
}
