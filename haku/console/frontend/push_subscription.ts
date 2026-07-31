import { useCallback, useEffect, useState } from "react";

import { api, errorDetail } from "./client";

// Browser-side half of Web Push (server half: ../web_push.py). Registers the console's service
// worker, asks for notification permission, and hands the resulting subscription to the backend
// so it can reach this browser when no console tab is open.

// Served from the docroot root, not /_console/assets/, for two reasons: a service worker's
// default scope is its own directory (this needs the whole origin, so `clients.matchAll` finds
// console tabs), and its URL must be stable across deploys or the browser treats each build as a
// different worker. Kept in sync with haku/console/BUILD.bazel's :service_worker_files.
const SERVICE_WORKER_URL = "/sw.js";

export type PushState =
  | { status: "unsupported" }
  | { status: "disabled" }
  | { status: "denied" }
  | { status: "off" }
  // The endpoint identifies *this* browser among the operator's enrolled devices, so the device
  // list can mark which row is the one you are looking at.
  | { status: "on"; endpoint: string }
  | { status: "busy" }
  | { status: "failed"; message: string };

export interface PushDevice {
  endpoint: string;
  userAgent: string | null;
  createdAt: string;
}

function pushSupported(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

/** base64url → bytes. The only hand-rolled codec left here: `subscribe` takes the server key as
 * a base64url string and `toJSON()` hands back base64url keys, so this is needed solely to
 * compare a stored subscription's key against the current one (see `subscribedWithCurrentKey`). */
function decodeBase64Url(value: string): Uint8Array {
  const binary = atob(value.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function applicationServerKey(): Promise<string | null> {
  const { data, error } = await api.GET("/api/push/config");
  if (error) throw new Error(errorDetail(error, "Failed to read the console's push configuration"));
  return data.application_server_key;
}

export async function enablePush(): Promise<PushState> {
  if (!pushSupported()) return { status: "unsupported" };

  const serverKey = await applicationServerKey();
  if (!serverKey) return { status: "disabled" };

  // Permission must be requested from a user gesture, which is why this is called from the
  // Settings toggle rather than on load. A browser that has already denied never re-prompts.
  if ((await Notification.requestPermission()) !== "granted") return { status: "denied" };

  const registration = await navigator.serviceWorker.register(SERVICE_WORKER_URL);
  await navigator.serviceWorker.ready;

  // An existing subscription may predate a VAPID key rotation, and one signed by a key the push
  // service no longer associates with us can never be delivered to. Re-subscribing is the only
  // repair, so drop a stale one rather than reporting success for a dead channel.
  const existing = await registration.pushManager.getSubscription();
  if (existing && !subscribedWithCurrentKey(existing, serverKey)) await existing.unsubscribe();

  const subscription =
    (await registration.pushManager.getSubscription()) ??
    (await registration.pushManager.subscribe({
      // Required by Chrome, and honest: every push this console sends shows a notification.
      userVisibleOnly: true,
      // `subscribe` accepts the application server key as a base64url string, so it goes across
      // exactly as the backend published it.
      applicationServerKey: serverKey,
    }));

  // `toJSON()` already yields the subscription's keys base64url-encoded, in the shape the Web
  // Push spec defines and pywebpush consumes — no need to pull ArrayBuffers out of `getKey()`
  // and re-encode them.
  const { keys } = subscription.toJSON();
  if (!keys?.p256dh || !keys.auth) throw new Error("push subscription is missing its encryption keys");

  const { error } = await api.POST("/api/push/subscriptions", {
    body: { endpoint: subscription.endpoint, p256dh: keys.p256dh, auth: keys.auth },
  });
  if (error) throw new Error(errorDetail(error, "Failed to register this browser for notifications"));
  return { status: "on", endpoint: subscription.endpoint };
}

export async function listPushDevices(): Promise<PushDevice[]> {
  const { data, error } = await api.GET("/api/push/subscriptions");
  if (error) throw new Error(errorDetail(error, "Failed to list the browsers registered for notifications"));
  return data.map((entry) => ({
    endpoint: entry.endpoint,
    userAgent: entry.user_agent,
    createdAt: entry.created_at,
  }));
}

/** Forget another device's subscription from here — it stops being pushed to immediately. */
export async function forgetPushDevice(endpoint: string): Promise<void> {
  const { error } = await api.DELETE("/api/push/subscriptions", { params: { query: { endpoint } } });
  if (error) throw new Error(errorDetail(error, "Failed to remove that browser"));
}

/** Whether an existing subscription was signed up under the console's current VAPID key. The
 * browser hands the key back as bytes regardless of how it was passed in, so this is the one
 * place a decode is unavoidable. */
function subscribedWithCurrentKey(subscription: PushSubscription, serverKey: string): boolean {
  const subscribed = subscription.options.applicationServerKey;
  if (!subscribed) return false;
  const expected = decodeBase64Url(serverKey);
  const actual = new Uint8Array(subscribed);
  return actual.length === expected.length && actual.every((byte, index) => byte === expected[index]);
}

export async function disablePush(): Promise<PushState> {
  if (!pushSupported()) return { status: "unsupported" };
  const registration = await navigator.serviceWorker.getRegistration(SERVICE_WORKER_URL);
  const subscription = await registration?.pushManager.getSubscription();
  if (!subscription) return { status: "off" };

  // Tell the console first: a browser that unsubscribed locally but stayed in the database would
  // leave the console pushing into an endpoint nobody reads until a 410 eventually prunes it.
  await api.DELETE("/api/push/subscriptions", { params: { query: { endpoint: subscription.endpoint } } });
  await subscription.unsubscribe();
  return { status: "off" };
}

async function currentPushState(): Promise<PushState> {
  if (!pushSupported()) return { status: "unsupported" };
  if (!(await applicationServerKey())) return { status: "disabled" };
  if (Notification.permission === "denied") return { status: "denied" };
  const registration = await navigator.serviceWorker.getRegistration(SERVICE_WORKER_URL);
  const subscription = await registration?.pushManager.getSubscription();
  return subscription ? { status: "on", endpoint: subscription.endpoint } : { status: "off" };
}

/** Drives the Settings section: this browser's state, the operator's other enrolled devices,
 * and the transitions the panel can request. Devices are refetched after every transition, since
 * enabling and disabling both change the list. */
export function usePushNotifications(): {
  state: PushState;
  devices: PushDevice[];
  enable: () => Promise<void>;
  disable: () => Promise<void>;
  forget: (endpoint: string) => Promise<void>;
} {
  const [state, setState] = useState<PushState>({ status: "busy" });
  const [devices, setDevices] = useState<PushDevice[]>([]);

  const refreshDevices = useCallback(async (next: PushState) => {
    // Only an operator on a push-capable console has devices to list; asking otherwise would 401
    // or 503 for no reason.
    if (next.status === "unsupported" || next.status === "disabled") return;
    setDevices(await listPushDevices());
  }, []);

  const settle = useCallback(
    async (transition: () => Promise<PushState>) => {
      setState({ status: "busy" });
      try {
        const next = await transition();
        await refreshDevices(next);
        setState(next);
      } catch (error) {
        // Surfaced in the panel rather than swallowed: a toggle that silently does nothing is how
        // an operator ends up believing they are covered when they are not.
        console.warn("Push notification transition failed", error);
        setState({ status: "failed", message: error instanceof Error ? error.message : String(error) });
      }
    },
    [refreshDevices]
  );

  useEffect(() => {
    let cancelled = false;
    currentPushState()
      .then(async (next) => {
        if (cancelled) return;
        await refreshDevices(next);
        if (!cancelled) setState(next);
      })
      .catch((error: unknown) => {
        if (!cancelled)
          setState({ status: "failed", message: errorDetail(error, "Could not read notification state") });
      });
    return () => {
      cancelled = true;
    };
  }, [refreshDevices]);

  return {
    state,
    devices,
    enable: useCallback(() => settle(enablePush), [settle]),
    disable: useCallback(() => settle(disablePush), [settle]),
    forget: useCallback(
      (endpoint: string) => settle(async () => (await forgetPushDevice(endpoint), currentPushState())),
      [settle]
    ),
  };
}
