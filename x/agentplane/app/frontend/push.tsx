import { Button, Code, Group, Paper, Stack, Text, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

type Device = { endpoint: string; user_agent: string | null; created_at: string };

function keyBytes(value: string): Uint8Array {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((value.length + 3) % 4);
  return Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { credentials: "same-origin", ...init });
  if (!response.ok) throw new Error((await response.text()) || `Request failed: ${response.status}`);
  return response.status === 204 ? (undefined as T) : response.json();
}

export function PushSettings(): JSX.Element {
  const [devices, setDevices] = useState<Device[]>([]);
  const [key, setKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [current, setCurrent] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [config, registered] = await Promise.all([
        json<{ application_server_key: string | null }>("/push/config"),
        json<Device[]>("/push/subscriptions"),
      ]);
      setKey(config.application_server_key);
      setDevices(registered);
      if ("serviceWorker" in navigator) {
        const registration = await navigator.serviceWorker.getRegistration("/sw.js");
        setCurrent((await registration?.pushManager.getSubscription())?.endpoint ?? null);
      }
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }, []);
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
      await json<void>("/push/subscriptions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: body.endpoint, p256dh: body.keys.p256dh, auth: body.keys.auth }),
      });
      await refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }

  async function forget(endpoint: string): Promise<void> {
    setBusy(true);
    try {
      await json<void>(`/push/subscriptions?endpoint=${encodeURIComponent(endpoint)}`, { method: "DELETE" });
      if (endpoint === current) {
        const registration = await navigator.serviceWorker.getRegistration("/sw.js");
        await (await registration?.pushManager.getSubscription())?.unsubscribe();
        setCurrent(null);
      }
      await refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Stack>
      <Title order={2}>Notifications</Title>
      <Text c="dimmed">
        Register this browser for Action approval notifications and manage the operator&apos;s registered browsers.
      </Text>
      {error && <Text c="red">{error}</Text>}
      <Button onClick={() => void enable()} loading={busy} disabled={!key}>
        Register this browser
      </Button>
      {devices.map((device) => (
        <Paper withBorder p="sm" key={device.endpoint}>
          <Group justify="space-between">
            <Stack gap={2}>
              <Text size="sm">
                {device.user_agent || "Unknown browser"}
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
