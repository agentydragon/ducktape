import { create, fromJson, toJson, type JsonValue } from "@bufbuild/protobuf";
import {
  CreatePushSubscriptionRequestSchema,
  GetPushConfigResponseSchema,
  ListPushSubscriptionsResponseSchema,
  WatchPushSubscriptionsResponseSchema,
  type PushSubscription,
} from "../../../action_service/web_push_service_pb";
import { Button, Code, Group, Paper, Stack, Text } from "@mantine/core";
import { type JSX, useCallback, useEffect, useState } from "react";
import { displayableError, fetchWithLogin } from "../client";
import { type StreamConnection } from "../stream_reader";
import { followWatch } from "../watch_stream";

const PUSH_PATH = "/upstream/actions/v1/operator/push";

function keyBytes(value: string): Uint8Array {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((value.length + 3) % 4);
  return Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
}

async function json(url: string, init?: RequestInit): Promise<JsonValue> {
  const response = await fetchWithLogin(url, { credentials: "same-origin", ...init });
  if (!response.ok) {
    const path = new URL(url, window.location.href).pathname;
    const status = `HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}`;
    throw new Error(`${init?.method ?? "GET"} ${path}: ${status}: ${await response.text()}`);
  }
  return response.json() as Promise<JsonValue>;
}

export function PushSettings(): JSX.Element {
  const [devices, setDevices] = useState<PushSubscription[] | null>(null);
  const [connection, setConnection] = useState<StreamConnection>({ phase: "connecting", since: Date.now() });
  useEffect(() => {
    let snapshot: PushSubscription[] = [];
    return followWatch(`${PUSH_PATH}/subscriptions:watch`, {
      onOpen: () => {
        snapshot = [];
      },
      onConnection: setConnection,
      onFrame: (result) => {
        const message = fromJson(WatchPushSubscriptionsResponseSchema, result as JsonValue);
        switch (message.frame.case) {
          case "snapshot":
            snapshot.push(...message.frame.value.pushSubscriptions);
            break;
          case "snapshotComplete":
            setDevices(snapshot);
            break;
          case "upsert": {
            const changed = message.frame.value;
            setDevices((current) => [
              ...(current ?? []).filter((value) => value.endpoint !== changed.endpoint),
              changed,
            ]);
            break;
          }
          case "removedEndpoint":
            setDevices((current) => (current ?? []).filter((value) => value.endpoint !== message.frame.value));
            break;
          case "reconnectRequired":
            break;
          case undefined:
            throw new Error("Push subscription watch returned an empty frame.");
        }
      },
    });
  }, []);
  const [key, setKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [current, setCurrent] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [config, registered] = await Promise.all([
        json(`${PUSH_PATH}/config`).then((value) => fromJson(GetPushConfigResponseSchema, value)),
        json(`${PUSH_PATH}/subscriptions`).then((value) => fromJson(ListPushSubscriptionsResponseSchema, value)),
      ]);
      setKey(config.applicationServerKey ?? null);
      setDevices(registered.pushSubscriptions);
      if ("serviceWorker" in navigator) {
        const registration = await navigator.serviceWorker.getRegistration("/sw.js");
        setCurrent((await registration?.pushManager.getSubscription())?.endpoint ?? null);
      }
      setError(null);
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setLoading(false);
    }
  }, [setDevices]);
  useEffect(() => void refresh(), [refresh]);

  async function enable(): Promise<void> {
    setBusy(true);
    try {
      if (!key || !("serviceWorker" in navigator) || !("PushManager" in window))
        throw new Error("Web Push is not configured or supported by this browser.");
      if ((await Notification.requestPermission()) !== "granted")
        throw new Error("Notification permission was not granted.");
      await navigator.serviceWorker.register("/sw.js");
      const registration = await navigator.serviceWorker.ready;
      let subscription = await registration.pushManager.getSubscription();
      if (subscription) {
        const oldKey = subscription.options.applicationServerKey;
        const expected = keyBytes(key);
        if (
          !oldKey ||
          oldKey.byteLength !== expected.length ||
          !new Uint8Array(oldKey).every((byte, i) => byte === expected[i])
        ) {
          await subscription.unsubscribe();
          subscription = null;
        }
      }
      subscription ??= await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      const body = subscription.toJSON();
      if (!body.endpoint || !body.keys?.p256dh || !body.keys.auth)
        throw new Error("Browser returned an incomplete push subscription.");
      await json(`${PUSH_PATH}/subscriptions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          toJson(
            CreatePushSubscriptionRequestSchema,
            create(CreatePushSubscriptionRequestSchema, {
              endpoint: body.endpoint,
              p256dh: body.keys.p256dh,
              auth: body.keys.auth,
            })
          )
        ),
      });
      await refresh();
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setBusy(false);
    }
  }

  async function forget(endpoint: string): Promise<void> {
    setBusy(true);
    try {
      await json(`${PUSH_PATH}/subscriptions?endpoint=${encodeURIComponent(endpoint)}`, { method: "DELETE" });
      if (endpoint === current) {
        const registration = await navigator.serviceWorker.getRegistration("/sw.js");
        await (await registration?.pushManager.getSubscription())?.unsubscribe();
        setCurrent(null);
      }
      await refresh();
    } catch (failure) {
      setError(displayableError(failure));
    } finally {
      setBusy(false);
    }
  }

  const thisBrowserRegistered = current !== null && devices?.some((device) => device.endpoint === current) === true;

  const unavailable = loading
    ? "Loading notification settings…"
    : error && !key
      ? "Could not load notification settings. Retry to check whether registration is available."
      : !key
        ? "Browser notifications are not configured on this server (no Web Push application server key)."
        : !window.isSecureContext
          ? "Browser notifications require a secure HTTPS connection."
          : !("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)
            ? "This browser does not support Web Push notifications."
            : Notification.permission === "denied"
              ? "Notifications are blocked for this site. Allow them in your browser settings, then retry."
              : null;

  return (
    <Stack>
      <Text c="dimmed">
        Register this browser for Action approval notifications and manage the operator&apos;s registered browsers.
      </Text>
      {error && <Text c="red">{error}</Text>}
      {unavailable && (
        <Text id="push-registration-status" role="status">
          {unavailable}
        </Text>
      )}
      {!thisBrowserRegistered && (
        <Button
          onClick={() => void enable()}
          loading={busy}
          disabled={unavailable !== null}
          aria-describedby={unavailable ? "push-registration-status" : undefined}
        >
          Register this browser
        </Button>
      )}
      {connection.phase === "reconnecting" && (
        <Text role="status" c="dimmed" size="sm">
          Connection lost; showing last registered browsers. Reconnecting…
        </Text>
      )}
      {devices?.map((device) => (
        <Paper withBorder p="sm" key={device.endpoint}>
          <Group justify="space-between">
            <Stack gap={2}>
              <Text size="sm">
                {device.userAgent || "Unknown browser"}
                {device.endpoint === current ? " (this browser)" : ""}
              </Text>
              <Code style={{ maxWidth: 500, overflow: "hidden", textOverflow: "ellipsis" }}>{device.endpoint}</Code>
            </Stack>
            <Button color="red" variant="light" onClick={() => void forget(device.endpoint)} loading={busy}>
              Forget
            </Button>
          </Group>
        </Paper>
      ))}
    </Stack>
  );
}
