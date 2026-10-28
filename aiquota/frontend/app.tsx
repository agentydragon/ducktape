import { useEffect, useState, type JSX } from "react";

import type { AllQuotasView } from "./api/schema";
import { Dashboard } from "./dashboard";

const TICK_MS = 1_000;

export function App(): JSX.Element {
  const [quotas, setQuotas] = useState<AllQuotasView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(true);
  const [stopped, setStopped] = useState(false);
  const [subscription, setSubscription] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let active = true;
    setConnecting(true);
    setStopped(false);
    const events = new EventSource("/v1/events");
    events.addEventListener("view", (event) => {
      if (!active) return;
      try {
        setQuotas(JSON.parse(event.data) as AllQuotasView);
        setError(null);
        setConnecting(false);
      } catch {
        setError("Could not read the live quota update.");
      }
    });
    events.onerror = () => {
      if (!active) return;
      const closed = events.readyState === EventSource.CLOSED;
      setStopped(closed);
      setConnecting(false);
      setError(
        closed
          ? "Live updates stopped. Reload this page to sign in again."
          : "Connection interrupted. Reconnecting to live updates…"
      );
    };
    return () => {
      active = false;
      events.close();
    };
  }, [subscription]);

  useEffect(() => {
    const tick = globalThis.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => globalThis.clearInterval(tick);
  }, []);

  return (
    <Dashboard
      quotas={quotas}
      now={now}
      error={error}
      connecting={connecting}
      onRefresh={() => {
        if (stopped) globalThis.location.reload();
        else setSubscription((value) => value + 1);
      }}
    />
  );
}
