/**
 * The standalone dashboard page at aiquota.allegedly.works: a title, the snapshot's age beside
 * the control that fetches a newer one, and the board itself.
 *
 * Only the chrome lives here. Everything that renders quota state is `QuotaBoard`, which the
 * Haku Console embeds in its own shell — see board.tsx.
 */

import { type JSX } from "react";

import { QuotaBoard } from "./board";
import { formatAge } from "./format";
import type { QuotasView } from "./quotas";

export function Dashboard({
  quotas,
  now,
  error,
  refreshing,
  onRefresh,
}: {
  quotas: QuotasView | null;
  now: number;
  error: string | null;
  refreshing: boolean;
  onRefresh: () => void;
}): JSX.Element {
  return (
    <main id="app">
      <header className="aiquota-page-heading">
        <h1>AI quota</h1>
        <div className="aiquota-page-status">
          {quotas && (
            <span className="aiquota-snapshot">
              Snapshot {new Date(quotas.fetched_at).toLocaleString()} ·{" "}
              {formatAge((now - Date.parse(quotas.fetched_at)) / 1000)} ago
            </span>
          )}
          <button
            type="button"
            className="aiquota-refresh"
            onClick={onRefresh}
            disabled={refreshing}
            aria-busy={refreshing}
            aria-label="Refresh"
            title="Refresh"
          >
            <RefreshIcon />
          </button>
        </div>
      </header>
      {error && <p className="aiquota-notice">{error}</p>}
      {quotas ? (
        <QuotaBoard quotas={quotas} now={now} />
      ) : (
        <p className="aiquota-empty">{error ? "No snapshot loaded." : "Loading quota data…"}</p>
      )}
    </main>
  );
}

/** The conventional circular arrow; the button is icon-only, so its label lives in aria-label. */
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
