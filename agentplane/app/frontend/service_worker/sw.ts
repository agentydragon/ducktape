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

// TypeScript's standard libraries do not declare these notification options yet, despite their use by
// ServiceWorkerRegistration.showNotification(). Augment only the missing options fields.
declare global {
  interface NotificationOptions {
    actions?: Array<{ action: string; title: string }>;
    renotify?: boolean;
  }
}

// Chrome (Android especially) revokes notification permission from origins whose notifications it
// judges spammy: many shown, few engaged with. So there is exactly one Agentplane notification,
// rebuilt from the Actions currently waiting, rather than one per Action plus a notice per outcome.
const PENDING_TAG = "agentplane-pending";
const LISTED_TITLES = 3;

interface PendingSummary {
  kind: "summary";
}

async function fetchPending(): Promise<ActionRequestView[] | null> {
  try {
    const response = await fetch("/actions?state=decision_pending", { credentials: "include" });
    if (response.ok) return (await response.json()) as ActionRequestView[];
  } catch {
    /* Offline delivery still opens the app without offering stale decisions. */
  }
  return null;
}

async function showPending(message: PushMessage): Promise<void> {
  // Push services can deliver pushes late, out of order, or collapsed, so the message only says
  // that something changed. Current authority decides what is shown; an expired session gets a
  // generic notification that deep-links to reauthentication, never decision buttons.
  const pending = await fetchPending();
  const options: NotificationOptions = { tag: PENDING_TAG, requireInteraction: true };
  if (pending === null) {
    await self.registration.showNotification("Agentplane", {
      ...options,
      body: "Open Agentplane to review waiting Actions",
      renotify: message.kind === "show",
      data: { kind: "summary" } satisfies PendingSummary,
    });
  } else if (pending.length === 0) {
    // Every push must show something, or Chrome shows its own "updated in the background" notice.
    await self.registration.showNotification("Agentplane", { tag: PENDING_TAG, silent: true });
    for (const notification of await self.registration.getNotifications({ tag: PENDING_TAG })) notification.close();
  } else if (pending.length === 1) {
    const [request] = pending;
    const content = formatActionNotification(request);
    await self.registration.showNotification(content.title, {
      ...options,
      body: content.text,
      renotify: message.kind === "show",
      silent: message.kind === "retract",
      actions: [
        { action: "approve", title: "Approve" },
        { action: "deny", title: "Deny" },
      ],
      data: {
        kind: "show",
        action_id: request.id,
        action_group: request.action.group,
        action_name: request.action.name,
        version: request.version,
        url: `/#/actions/${request.id}`,
      } satisfies PushShow,
    });
  } else {
    await self.registration.showNotification(`${pending.length} Actions waiting`, {
      ...options,
      body: pending
        .slice(0, LISTED_TITLES)
        .map((request) => formatActionNotification(request).title)
        .join("\n"),
      renotify: message.kind === "show",
      silent: message.kind === "retract",
      data: { kind: "summary" } satisfies PendingSummary,
    });
  }
}

self.addEventListener("push", (event) => {
  if (!event.data) return;
  event.waitUntil(showPending(event.data.json() as PushMessage));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const message = event.notification.data as PushShow | PendingSummary | null;
  if (!message || message.kind !== "show" || (event.action !== "approve" && event.action !== "deny")) {
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
