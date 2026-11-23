import {
  ActionState,
  Verdict,
  type ActionRequest,
} from "../../../../../action_service/api/contracts/action_service_pb";
import { decideActionById, getIntegrationPreviewNames, listActionRequests } from "../../client";
import { actionIdentity } from "../../presentation";
import { formatActionNotification, notificationPreviewKind } from "../../notifications";
import { notificationPreviewsEnabled } from "../preview_preference";

interface PushShow {
  kind: "show";
  action_id: string;
  action_group: string;
  action_name: string;
  version: string;
  url: string;
}

interface PushRetract {
  kind: "retract";
  action_id: string;
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
let pendingRevision = 0;

interface PendingSummary {
  kind: "summary";
}

async function fetchPending(): Promise<ActionRequest[] | null> {
  try {
    return await listActionRequests(ActionState.DECISION_PENDING, fetch);
  } catch {
    /* Offline delivery still opens the app without offering stale decisions. */
  }
  return null;
}

async function showPending(message: PushMessage): Promise<void> {
  // Push services can deliver pushes late, out of order, or collapsed, so the message only says
  // that something changed. Current authority decides what is shown; an expired session gets a
  // generic notification that deep-links to reauthentication, never decision buttons.
  const revision = ++pendingRevision;
  const pending = await fetchPending();
  if (revision !== pendingRevision) return;
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
    const notices = await self.registration.getNotifications({ tag: PENDING_TAG });
    if (revision !== pendingRevision) return;
    for (const notification of notices) notification.close();
  } else if (pending.length === 1) {
    const [request] = pending;
    const action = actionIdentity(request);
    const kind = notificationPreviewKind(request);
    const names =
      kind && (await notificationPreviewsEnabled())
        ? await getIntegrationPreviewNames(kind, request.id).catch(() => undefined)
        : undefined;
    const includeNames = names && (await notificationPreviewsEnabled());
    if (revision !== pendingRevision) return;
    const content = formatActionNotification(request, includeNames ? names : undefined);
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
        action_group: action.group,
        action_name: action.name,
        version: request.version.toString(),
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
    Promise.resolve()
      .then(() =>
        decideActionById(
          message.action_id,
          BigInt(message.version),
          event.action === "approve" ? Verdict.ALLOW : Verdict.DENY,
          fetch
        )
      )
      .catch(() => self.clients.openWindow("/#/actions"))
  );
});

self.addEventListener("install", (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
