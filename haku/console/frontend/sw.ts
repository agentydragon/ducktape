/// <reference lib="webworker" />

// The console's service worker. Its only job is Web Push: render an OS notification for a tool
// call waiting on the operator, and let them decide from it without opening the console.
//
// This is the one console surface that runs with no tab open, so it is deliberately tiny and
// holds no state. Everything it needs arrives in the push payload, and every decision it makes
// goes back through the same `/api/tool-calls/{id}/decision` endpoint the approvals drawer uses,
// as a same-origin credentialed fetch. That is what keeps the trust story unchanged: the buttons
// are defined here in reviewed console code, and the authority behind them is the operator's own
// Authentik session cookie, not anything carried in the message.
//
// Payload shapes mirror `PushShow`/`PushRetract` in ../web_push.py.

import { toolActionDescription } from "./tool_rendering/actions.ts";

// The project's tsconfig is DOM-typed (it is a React SPA), so redeclaring `self` as a
// ServiceWorkerGlobalScope would collide with the DOM lib's own declaration. Alias instead: the
// `webworker` lib reference above supplies the worker event types, and every worker-global call
// below goes through `sw` so it resolves against the right global.
const sw = self as unknown as ServiceWorkerGlobalScope;

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

// Action buttons are only supported by the service-worker `showNotification`, and this project's
// DOM-typed `NotificationOptions` therefore has no `actions` field. Widen it here rather than
// switching the whole tsconfig to the webworker lib for one file.
type ServiceWorkerNotificationOptions = NotificationOptions & {
  actions?: { action: string; title: string }[];
};

/** The same one-line description the approvals card shows, from the same registry — so a
 * notification reads "Gmail: Draft email", not "gmail.drafts_create". Falls back to the bare
 * identity when a tool has no entry or its (not-yet-validated) arguments do not parse. */
function notificationTitle(message: PushShow): string {
  const action = toolActionDescription(message.server_id, message.tool_name, message.arguments);
  return action ? action.text : `${message.server_id}.${message.tool_name}`;
}

/** The buttons this platform will actually render.
 *
 * Browsers cap notification actions and silently drop the rest (`Notification.maxActions` —
 * Chrome reports 2), so the array's tail cannot be treated as guaranteed. Deciding is what the
 * notification is *for*, so Approve and Deny come first and Details is offered only where there
 * is room for it. Nothing is lost when it is dropped: tapping the notification body opens the
 * call, which is the same thing Details does. */
function notificationActions(): { action: string; title: string }[] {
  const decisions = [
    { action: APPROVE_ACTION, title: "Approve" },
    { action: DENY_ACTION, title: "Deny" },
  ];
  const details = { action: DETAILS_ACTION, title: "Details" };
  // Read defensively: `maxActions` is not in every lib.dom, and a browser that omits it is one
  // whose limit we do not know — offer the full set and let it drop what it cannot show.
  const max = (Notification as { maxActions?: number }).maxActions;
  return max === undefined || max > decisions.length ? [...decisions, details] : decisions;
}

async function showPending(message: PushShow): Promise<void> {
  await sw.registration.showNotification(notificationTitle(message), {
    body: message.rationale,
    // The call id as tag means a re-sent push replaces rather than stacks, and gives
    // `retract` a handle on exactly this notification.
    tag: message.tool_call_id,
    // A pending call is a question. Leaving it up until answered is the point.
    requireInteraction: true,
    actions: notificationActions(),
    data: message,
  } satisfies ServiceWorkerNotificationOptions as NotificationOptions);
}

async function showResolved(message: PushRetract): Promise<void> {
  // Replace in place rather than just closing. Chrome requires a `userVisibleOnly` subscription
  // to show something per push and spends the origin's push budget when a handler shows nothing
  // — exhaust it and the browser substitutes its own "site updated in the background" notice,
  // which is worse than the stale notification this is clearing. Showing the outcome is also
  // the better answer for the operator: they learn the thing they were pinged about is settled.
  await sw.registration.showNotification(message.outcome, {
    tag: message.tool_call_id,
    silent: true,
    requireInteraction: false,
    data: message,
  });
  await new Promise((resolve) => setTimeout(resolve, RESOLVED_LINGER_MS));
  for (const notification of await sw.registration.getNotifications({ tag: message.tool_call_id })) {
    notification.close();
  }
}

sw.addEventListener("push", (event) => {
  const message = parsePush(event);
  if (!message) return;
  // waitUntil keeps this worker alive across the await chain; without it the browser may kill it
  // mid-notification (and, for `retract`, before the linger completes).
  event.waitUntil(message.kind === "show" ? showPending(message) : showResolved(message));
});

async function openToolCall(url: string): Promise<void> {
  // Prefer a console tab that already exists: focusing the operator's open session is both faster
  // and less disruptive than spawning a duplicate window.
  const clients = await sw.clients.matchAll({ type: "window", includeUncontrolled: true });
  const existing = clients.find((client) => new URL(client.url).origin === sw.location.origin);
  if (existing) {
    await existing.focus();
    await existing.navigate(url).catch(() => undefined);
    return;
  }
  await sw.clients.openWindow(url);
}

async function decide(message: PushShow, action: string): Promise<void> {
  const response = await fetch(`/api/tool-calls/${encodeURIComponent(message.tool_call_id)}/decision`, {
    method: "POST",
    // Same-origin, so the operator's session cookie rides along and the browser sends the exact
    // Origin the backend's mutation guard requires. No credential is stored in the worker.
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(action === APPROVE_ACTION ? { decision: "approve" } : { decision: "deny", reason: null }),
  });

  if (response.ok) return;

  // 401 is the expected failure, not an exceptional one: operator sessions are short, so a
  // notification acted on hours later routinely outlives the session that would have authorized
  // it. Hand the operator to the console, which re-authenticates and lands on this call.
  //
  // 409 means the call was already decided or withdrawn elsewhere — the backend resolves that
  // race under the tool-call row lock and tells the loser the winner's status. Also not an
  // error worth alarming about; opening the call shows what actually happened.
  if (response.status !== 401 && response.status !== 409) {
    console.warn("Tool call decision failed", response.status);
  }
  await openToolCall(message.url);
}

sw.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const message = event.notification.data as PushMessage | undefined;
  if (!message || message.kind !== "show") return;
  const decided = event.action === APPROVE_ACTION || event.action === DENY_ACTION;
  event.waitUntil(decided ? decide(message, event.action) : openToolCall(message.url));
});

// Take over from a previous worker immediately. A console that is showing approval notifications
// from stale worker code is worse than a brief reload, and there is no in-flight state to lose.
sw.addEventListener("install", () => {
  void sw.skipWaiting();
});

sw.addEventListener("activate", (event) => {
  event.waitUntil(sw.clients.claim());
});
