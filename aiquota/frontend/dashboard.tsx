/**
 * The standalone dashboard page at aiquota.allegedly.works: a title, the stream status beside
 * the control that reconnects the live subscription, and the board itself.
 *
 * QuotaBoard owns quota rendering; this component supplies the standalone page controls.
 */

import { Button, Title } from "@mantine/core";
import { type JSX } from "react";

import type { AllQuotasView } from "./schema";
import { QuotaBoard } from "./board";

export function Dashboard({
  quotas,
  now,
  error,
  connecting,
  stopped,
  onRefresh,
}: {
  quotas: AllQuotasView | null;
  now: number;
  error: string | null;
  connecting: boolean;
  stopped: boolean;
  onRefresh: () => void;
}): JSX.Element {
  return (
    <main id="app">
      <header className="aiquota-page-heading">
        <Title order={1}>AI quota</Title>
        <div className="aiquota-page-status">
          <span className="aiquota-connection" role="status">
            {connecting ? "Connecting to live updates…" : error ? "Live updates unavailable" : "Live updates connected"}
          </span>
          <Button
            variant="default"
            leftSection={<RefreshIcon />}
            type="button"
            className="aiquota-refresh"
            onClick={onRefresh}
            disabled={connecting}
            aria-busy={connecting}
          >
            Reconnect
          </Button>
        </div>
      </header>
      <p className="aiquota-connection-help">
        {connecting
          ? "Opening the live quota stream."
          : stopped
            ? "The stream has closed. Reconnect will reload the page and start a fresh stream; data freshness is shown on each provider."
            : error
              ? "The last quota view remains visible. The browser is retrying the live stream; use Reconnect to start a new connection. Data freshness is shown on each provider."
              : "Live updates are connected. Data freshness is shown on each provider."}
      </p>
      {error && <p className="aiquota-notice">{error}</p>}
      {quotas ? (
        <QuotaBoard quotas={quotas} now={now} />
      ) : (
        <p className="aiquota-empty">{error ? "No snapshot loaded." : "Loading quota data…"}</p>
      )}
    </main>
  );
}

/** The circular arrow complements the button's visible reconnect label. */
function RefreshIcon(): JSX.Element {
  return (
    <svg
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
      <path d="M21 3v6h-6" />
    </svg>
  );
}
