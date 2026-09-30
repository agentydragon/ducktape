import type { JSX } from "react";

import type { SyncStatus } from "./api";
import { ago, liveSummary, untilExpiry } from "./status";

type Props = {
  status: SyncStatus;
  now: number;
  onSyncNow: () => void;
};

const STATE_LABEL: Record<SyncStatus["state"], string> = {
  unpaired: "Not paired",
  syncing: "Syncing",
  idle: "Idle until the next cycle",
};

export function Overview({ status, now, onSyncNow }: Props): JSX.Element {
  const { credential, last_cycle: cycle, last_failure: failure, live } = status;
  return (
    <section aria-labelledby="overview-heading">
      <h2 id="overview-heading">Sync</h2>
      <dl>
        <dt>State</dt>
        <dd>{STATE_LABEL[status.state]}</dd>
        <dt>Sessions</dt>
        <dd>
          {status.sessions} stored, {status.sessions_behind} behind
        </dd>
        {credential !== null && (
          <>
            <dt>Grant</dt>
            <dd>
              organization <code>{credential.organization_uuid}</code>, scopes{" "}
              <code>{credential.scopes.join(" ")}</code>
            </dd>
            <dt>Access token</dt>
            <dd>refreshes {untilExpiry(credential.access_token_expires_at, now)}</dd>
            <dt>Live</dt>
            <dd>{liveSummary(live, now)}</dd>
          </>
        )}
        {cycle !== null && (
          <>
            <dt>Last cycle</dt>
            <dd>
              {ago(cycle.finished_at, now)}: {cycle.behind} sessions were behind, {cycle.events_read} events read
            </dd>
          </>
        )}
      </dl>
      {failure !== null && (
        <p role="alert" className="error">
          The last cycle failed {ago(failure.at, now)}: {failure.message}
        </p>
      )}
      {live.problems.map((problem) => (
        <p key={problem.source} role="alert" className="error">
          {problem.source} last failed {ago(problem.at, now)} and is retrying: {problem.message}
        </p>
      ))}
      {live.failure !== null && (
        <p role="alert" className="error">
          Live following stopped {ago(live.failure.at, now)}, and polling carries on: {live.failure.message}
        </p>
      )}
      {credential !== null && (
        <button type="button" onClick={onSyncNow} disabled={status.state === "syncing"}>
          Sync now
        </button>
      )}
    </section>
  );
}
