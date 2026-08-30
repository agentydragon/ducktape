// The console's service worker. Its only job is Web Push: render an OS notification for a tool
// call waiting on the operator, and let them decide from it without opening the console.
//
// This is the one console surface that runs with no tab open, so it holds no state: everything it
// needs arrives in the push payload, and every decision goes back through the same
// `/api/tool-calls/{id}/decision` endpoint the approvals drawer uses, as a same-origin credentialed
// fetch. The buttons are defined here in reviewed console code and the authority behind them is the
// operator's own Authentik session cookie, not anything carried in the message.
//
// Payload shapes mirror `PushShow`/`PushRetract` in ../notifications/push.py as a versioned wire contract,
// not as two views of one type. This file updates on the browser's schedule — a navigation to the
// console, or a push handled once the registration has gone stale (>24h since its last check) —
// while the backend deploys atomically, so a worker running this code can be a day or more behind
// the server sending to it. Read every field below defensively; an older payload may not carry it.

import { toolActionDescription } from "./tool_rendering/actions";

// Checked against the WebWorker lib (tsconfig.sw.json), so this narrows the worker global to
// the service-worker scope rather than casting around a DOM-typed one.
declare const self: ServiceWorkerGlobalScope;

// TypeScript models no notification actions at all — neither lib.dom nor lib.webworker declares
// `NotificationAction` or `NotificationOptions.actions`, though the service-worker
// `showNotification` has taken them for years. Declare the slice this worker uses, scoped to the
// sw type-check program (tsconfig.json excludes this file), so the call site stays a plain typed
// object rather than a cast.
interface NotificationAction {
  action: string;
  title: string;
}

declare global {
  interface NotificationOptions {
    actions?: NotificationAction[];
  }
}

interface PushShow {
  kind: "show";
  tool_call_id: string;
  server_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  rationale: string;
  url: string;
}

interface PushRetract {
  kind: "retract";
  tool_call_id: string;
  outcome: string;
}

type PushMessage = PushShow | PushRetract;

const APPROVE_ACTION = "approve";
const DENY_ACTION = "deny";
const DETAILS_ACTION = "details";
const PENDING_NOTIFICATION_ACTIONS: NotificationAction[] = [
  { action: APPROVE_ACTION, title: "Approve" },
  { action: DENY_ACTION, title: "Deny" },
  { action: DETAILS_ACTION, title: "Details" },
];
// How long a resolved notification lingers before closing itself. Long enough to read on a lock
// screen, short enough that settled calls do not pile up in the shade.
const RESOLVED_LINGER_MS = 6000;

function parsePush(event: PushEvent): PushMessage | null {
  if (!event.data) return null;
  try {
    return event.data.json() as PushMessage;
  } catch (error) {
    console.warn("Ignoring push with an unreadable payload", error);
    return null;
  }
}

/** The same one-line description the approvals card shows, from the same registry — so a
 * notification reads "Gmail: Draft email", not "gmail.drafts_create". Falls back to the bare
 * identity when a tool has no entry or its (not-yet-validated) arguments do not parse. */
function notificationTitle(message: PushShow): string {
  const action = toolActionDescription(message.server_id, message.tool_name, message.arguments);
  if (!action) return `${message.server_id}.${message.tool_name}`;
  // The approvals card renders a destructive action's line in red (tool_action_line.tsx). An OS
  // notification has no equivalent, so the same cue has to be carried in the text — and this is
  // the one surface where such a call can be approved without its arguments ever being seen.
  return action.destructive ? `⚠ ${action.text}` : action.text;
}

/** The buttons this platform will actually render.
 *
 * Browsers cap notification actions and silently drop the rest (`Notification.maxActions` — Chrome
 * reports 2), so the array's tail is not guaranteed. Deciding is what the notification is *for*, so
 * Approve and Deny come first and Details only where there is room; dropping it loses nothing,
 * since tapping the notification body opens the call too. */
function browserMaxNotificationActions(): number | undefined {
  if (typeof Notification === "undefined") return undefined;
  return (Notification as typeof Notification & { maxActions?: number }).maxActions;
}

export function notificationActions(
  maxActions: number | undefined = browserMaxNotificationActions()
): NotificationAction[] {
  // Read defensively: `maxActions` is not in every lib.dom, and a browser that omits it is one
  // whose limit we do not know — offer the full set and let it drop what it cannot show. Invalid
  // values are treated the same way; browsers expose this as a finite, non-negative integer.
  const limit =
    maxActions === undefined || !Number.isFinite(maxActions) || maxActions < 0
      ? PENDING_NOTIFICATION_ACTIONS.length
      : Math.floor(maxActions);
  return PENDING_NOTIFICATION_ACTIONS.slice(0, limit);
}

async function showPending(message: PushShow): Promise<void> {
  await self.registration.showNotification(notificationTitle(message), {
    body: message.rationale,
    // The call id as tag means a re-sent push replaces rather than stacks, and gives
    // `retract` a handle on exactly this notification.
    tag: message.tool_call_id,
    // A pending call is a question. Leaving it up until answered is the point.
    requireInteraction: true,
    actions: notificationActions(),
    data: message,
  });
}

async function showResolved(message: PushRetract): Promise<void> {
  // Replace in place rather than just closing. Chrome requires a `userVisibleOnly` subscription to
  // show something per push and spends the origin's push budget when a handler shows nothing;
  // exhaust it and the browser substitutes its own "site updated in the background" notice. It also
  // tells the operator that the call they were pinged about is settled.
  await self.registration.showNotification(message.outcome, {
    tag: message.tool_call_id,
    silent: true,
    requireInteraction: false,
    data: message,
  });
  await new Promise((resolve) => setTimeout(resolve, RESOLVED_LINGER_MS));
  for (const notification of await self.registration.getNotifications({ tag: message.tool_call_id })) {
    notification.close();
  }
}

self.addEventListener("push", (event) => {
  const message = parsePush(event);
  if (!message) return;
  // waitUntil keeps this worker alive across the await chain; without it the browser may kill it
  // mid-notification (and, for `retract`, before the linger completes).
  event.waitUntil(message.kind === "show" ? showPending(message) : showResolved(message));
});

async function openToolCall(url: string): Promise<void> {
  // Prefer a console tab that already exists: focusing the operator's open session is both faster
  // and less disruptive than spawning a duplicate window.
  const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  const existing = clients.find((client) => new URL(client.url).origin === self.location.origin);
  if (existing) {
    await existing.focus();
    await existing.navigate(url).catch(() => undefined);
    return;
  }
  await self.clients.openWindow(url);
}

async function decide(message: PushShow, action: string): Promise<void> {
  const response = await fetch(`/api/tool-calls/${encodeURIComponent(message.tool_call_id)}/decision`, {
    method: "POST",
    // Same-origin, so the operator's session cookie rides along and the browser sends the exact
    // Origin the backend's mutation guard requires. No credential is stored in the worker.
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(
      action === APPROVE_ACTION ? { decision: "approve" } : { decision: "deny", decision_note: null }
    ),
  });

  if (response.ok) return;

  // 401 is expected, not exceptional: operator sessions are short, so a notification acted on hours
  // later routinely outlives the session that would have authorized it. Opening the console
  // re-authenticates and lands on this call.
  //
  // 409 means the call was already decided or withdrawn elsewhere — the backend resolves that race
  // under the tool-call row lock. Also not worth alarming about; opening the call shows what
  // happened.
  if (response.status !== 401 && response.status !== 409) {
    console.warn("Tool call decision failed", response.status);
  }
  await openToolCall(message.url);
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const message = event.notification.data as PushMessage | undefined;
  if (!message || message.kind !== "show") return;
  const decided = event.action === APPROVE_ACTION || event.action === DENY_ACTION;
  event.waitUntil(decided ? decide(message, event.action) : openToolCall(message.url));
});

// Take over from a previous worker immediately rather than waiting until every controlled tab
// closes, which is how a fixed worker sits undeployed for days. Safe because this worker holds no
// state and intercepts no fetches, so a mid-session handover loses nothing.
self.addEventListener("install", () => {
  void self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});
