import { type JSX, useEffect, useState } from "react";
import { formatDuration, intervalToDuration, secondsToMilliseconds } from "date-fns";

type Window = {
  name: string | null;
  used_percent: number;
  remaining_percent: number;
  reset_seconds: number;
  window_seconds: number;
};
type ProviderSuccess = {
  kind: "success";
  windows?: Window[];
  available_reset_credits?: number | null;
  available_reset_credit_expiries?: string[];
};
type ProviderResult = ProviderSuccess | { kind: "error"; error: string };
type Provider = {
  provider: string;
  last_output: { result: ProviderResult };
  last_success: { result: ProviderSuccess } | null;
};
type Quotas = { providers: Provider[]; fetched_at: string };

const duration = (seconds: number): string => {
  const value = formatDuration(
    intervalToDuration({ start: 0, end: secondsToMilliseconds(Math.max(0, Math.round(seconds))) }),
    { format: ["days", "hours", "minutes"], delimiter: " " }
  );
  return value || "0 minutes";
};

const label = (window: Window): string => window.name || duration(window.window_seconds);

function ProviderCard({ provider }: { provider: Provider }): JSX.Element {
  const result = provider.last_output.result;
  const success = result.kind === "success" ? result : provider.last_success?.result;
  const windows = success?.windows ?? [];
  const resetCredits = success?.available_reset_credits ?? null;
  const resetExpiries = success?.available_reset_credit_expiries ?? [];
  return (
    <article className="provider-card">
      <header>
        <h2>{provider.provider}</h2>
        <div className="badges">
          {resetCredits !== null && (
            <span className="status reset">
              {resetCredits} banked reset{resetCredits === 1 ? "" : "s"}
            </span>
          )}
          {result.kind === "error" && <span className="status error">Stale</span>}
        </div>
      </header>
      {result.kind === "error" && <p className="error-copy">{result.error}</p>}
      {resetExpiries.length > 0 && (
        <p className="known-expiries">
          Known expiries:{" "}
          {resetExpiries
            .map((value) =>
              new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
            )
            .join(", ")}
        </p>
      )}
      {windows.length === 0 ? (
        <p className="empty">No quota data available.</p>
      ) : (
        windows.map((window) => {
          const used = Math.max(0, Math.min(100, window.used_percent));
          const severity = used >= 95 ? "critical" : used >= 80 ? "warning" : "normal";
          return (
            <section className="window" key={`${window.name}-${window.window_seconds}`}>
              <div className="window-heading">
                <strong>{label(window)}</strong>
                <strong>{Math.round(used)}%</strong>
              </div>
              <div className={`meter ${severity}`} aria-label={`${label(window)} ${Math.round(used)} percent used`}>
                <span style={{ width: `${used}%` }} />
              </div>
              <div className="window-meta">
                <span>{Math.round(window.remaining_percent)}% remaining</span>
                <span>Resets in {duration(window.reset_seconds)}</span>
              </div>
            </section>
          );
        })
      )}
    </article>
  );
}

export function App(): JSX.Element {
  const [quotas, setQuotas] = useState<Quotas | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = async (): Promise<void> => {
    setError(null);
    try {
      const response = await fetch("/v1/quotas", { credentials: "same-origin", cache: "no-store" });
      if (!response.ok)
        throw new Error(
          response.status === 401
            ? "Your OAuth session has expired. Refresh to sign in again."
            : `Quota API returned ${response.status}.`
        );
      setQuotas((await response.json()) as Quotas);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load quota data.");
    }
  };
  useEffect(() => {
    void refresh();
  }, []);
  return (
    <main>
      <header className="page-heading">
        <div>
          <p className="eyebrow">AIQUOTA</p>
          <h1>Subscription headroom</h1>
          <p>Live limits across your AI subscriptions.</p>
        </div>
        <button onClick={() => void refresh()}>Refresh</button>
      </header>
      {error && <p className="notice">{error}</p>}
      <section className="providers" aria-live="polite">
        {quotas?.providers.map((provider) => <ProviderCard key={provider.provider} provider={provider} />) ?? (
          <p className="empty">Loading quota data…</p>
        )}
      </section>
      {quotas && <footer>Snapshot {new Date(quotas.fetched_at).toLocaleString()}</footer>}
    </main>
  );
}
