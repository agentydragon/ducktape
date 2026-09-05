import { useEffect, useState } from "react";

type Window = { name: string | null; used_percent: number; remaining_percent: number; reset_seconds: number; window_seconds: number };
type ProviderResult = { kind: "success"; windows?: Window[] } | { kind: "error"; error: string };
type Provider = { provider: string; last_output: { result: ProviderResult }; last_success: { result: { windows: Window[] } } | null };
type Quotas = { providers: Provider[]; fetched_at: string };

const duration = (seconds: number) => {
  const minutes = Math.max(0, Math.ceil(seconds / 60));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
};

const label = (window: Window) => window.name || duration(window.window_seconds);

function ProviderCard({ provider }: { provider: Provider }) {
  const result = provider.last_output.result;
  const windows = result.kind === "success" ? result.windows ?? [] : provider.last_success?.result.windows ?? [];
  return <article className="provider-card">
    <header><h2>{provider.provider}</h2>{result.kind === "error" && <span className="status error">Stale</span>}</header>
    {result.kind === "error" && <p className="error-copy">{result.error}</p>}
    {windows.length === 0 ? <p className="empty">No quota data available.</p> : windows.map((window) => {
      const used = Math.max(0, Math.min(100, window.used_percent));
      const severity = used >= 95 ? "critical" : used >= 80 ? "warning" : "normal";
      return <section className="window" key={`${window.name}-${window.window_seconds}`}>
        <div className="window-heading"><strong>{label(window)}</strong><strong>{Math.round(used)}%</strong></div>
        <div className={`meter ${severity}`} aria-label={`${label(window)} ${Math.round(used)} percent used`}><span style={{ width: `${used}%` }} /></div>
        <div className="window-meta"><span>{Math.round(window.remaining_percent)}% remaining</span><span>Resets in {duration(window.reset_seconds)}</span></div>
      </section>;
    })}
  </article>;
}

export function App() {
  const [quotas, setQuotas] = useState<Quotas | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = async () => {
    setError(null);
    try {
      const response = await fetch("/api/v1/quotas", { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) throw new Error(response.status === 401 ? "Your OAuth session has expired. Refresh to sign in again." : `Quota API returned ${response.status}.`);
      setQuotas(await response.json() as Quotas);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load quota data.");
    }
  };
  useEffect(() => { void refresh(); }, []);
  return <main>
    <header className="page-heading"><div><p className="eyebrow">AIQUOTA</p><h1>Subscription headroom</h1><p>Live limits across your AI subscriptions.</p></div><button onClick={() => void refresh()}>Refresh</button></header>
    {error && <p className="notice">{error}</p>}
    <section className="providers" aria-live="polite">{quotas?.providers.map((provider) => <ProviderCard key={provider.provider} provider={provider} />) ?? <p className="empty">Loading quota data…</p>}</section>
    {quotas && <footer>Snapshot {new Date(quotas.fetched_at).toLocaleString()}</footer>}
  </main>;
}
