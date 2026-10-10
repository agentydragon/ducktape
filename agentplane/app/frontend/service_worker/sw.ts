import type { ActionRequestView } from "../actions/types";
import { formatActionNotification } from "../actions/notifications";

interface PushShow {
  kind: "show";
  action_id: string;
  action_group: string;
  action_name: string;
  version: number;
  url: string;
}

interface PushRetract {
  kind: "retract";
  action_id: string;
  outcome: string;
}

type PushMessage = PushShow | PushRetract;

// The worker is checked against WebWorker globals, not the frontend's document globals.
declare const self: ServiceWorkerGlobalScope;

// TypeScript's standard libraries do not declare notification actions yet, despite their use by
// ServiceWorkerRegistration.showNotification(). Augment only the missing options field.
declare global {
  interface NotificationOptions {
    actions?: Array<{ action: string; title: string }>;
  }
}

async function closeActionNotification(actionId: string): Promise<void> {
  const notifications = await self.registration.getNotifications({ tag: actionId });
  for (const notification of notifications) notification.close();
}

self.addEventListener("push", (event) => {
  if (!event.data) return;
  const message = event.data.json() as PushMessage;
  if (message.kind === "retract") {
    if (message.outcome === "allowed") {
      event.waitUntil(closeActionNotification(message.action_id));
    } else {
      event.waitUntil(
        self.registration.showNotification(message.outcome, {
          tag: message.action_id,
          silent: true,
          requireInteraction: false,
          data: message,
        })
      );
    }
    return;
  }
  if (message.kind !== "show") return;
  event.waitUntil(
    (async () => {
      // Push services can deliver an old show after its retraction. Re-read authority before
      // offering decisions; an expired session gets a reauthentication deep link, not buttons.
      let current: ActionRequestView | null = null;
      try {
        const response = await fetch(`/actions/${encodeURIComponent(message.action_id)}`, { credentials: "include" });
        if (response.ok) current = (await response.json()) as ActionRequestView;
      } catch {
        /* Offline delivery still opens the app without offering stale decisions. */
      }
      if (current && current.state !== "decision_pending") {
        if (current.state === "allowed") {
          await closeActionNotification(message.action_id);
        } else {
          await self.registration.showNotification(current.state.replaceAll("_", " "), {
            tag: message.action_id,
            silent: true,
            data: { kind: "retract", action_id: message.action_id },
          });
        }
        return;
      }
      const content = current
        ? formatActionNotification(current)
        : {
            title: `${message.action_group} / ${message.action_name}`,
            text: "Open Agentplane to review this Action",
          };
      await self.registration.showNotification(content.title, {
        body: content.text,
        tag: message.action_id,
        requireInteraction: true,
        actions: current
          ? [
              { action: "approve", title: "Approve" },
              { action: "deny", title: "Deny" },
            ]
          : [],
        data: current ? { ...message, version: current.version } : message,
      });
    })()
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const message = event.notification.data as PushMessage | null;
  if (!message || message.kind !== "show") return;
  if (event.action !== "approve" && event.action !== "deny") {
    event.waitUntil(self.clients.openWindow("/#/actions"));
    return;
  }
  event.waitUntil(
    fetch(`/push/decision/${encodeURIComponent(message.action_id)}`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        verdict: event.action === "approve" ? "allow" : "deny",
        expected_version: message.version,
        idempotency_key: crypto.randomUUID(),
        decision_note: null,
      }),
    }).then((response) => {
      if (!response.ok) return self.clients.openWindow("/#/actions");
    })
  );
});

self.addEventListener("install", (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
