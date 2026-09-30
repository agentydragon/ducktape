import type { JSX } from "react";

import type { SyncStatus } from "./api";
import { ago, every, lastEvent, nextPoll, plural, untilExpiry } from "./status";

type Props = {
  status: SyncStatus;
  now: number;
  onSyncNow: () => void;
};

export function Overview({ status, now, onSyncNow }: Props): JSX.Element {
  const { credential, last_cycle: cycle, last_failure: failure, live } = status;
  return (
    <section aria-labelledby="overview-heading">
      <h2 id="overview-heading">Sync</h2>
      <p>
        {plural(status.sessions, "session")} stored.{" "}
        {credential === null
          ? "Not paired, so nothing is syncing."
          : "Live following keeps the sessions in use current as events happen; polling catches up on everything else."}
      </p>
      {credential !== null && (
        <>
          <h3>Live following</h3>
          <p className="hint">
            {live.following
              ? "Streams the events of sessions in use as they happen; a session watch finds new sessions at once."
              : "Off: only polling runs."}
          </p>
          {live.following && (
            <dl>
              <dt>Streaming</dt>
              <dd>{plural(live.streams, "session")}</dd>
              <dt>Session watch</dt>
              <dd>
                {live.watching ? "connected" : "not connected; new sessions are found by a frequent list check instead"}
              </dd>
              <dt>Last event stored</dt>
              <dd>{lastEvent(live.last_event_at, now)}</dd>
            </dl>
          )}
          <h3>Polling</h3>
          <p className="hint">
            Every {every(status.poll_interval_seconds)} it lists every session and reads the events of any that moved
            on. It needs no live following, and catches whatever that missed.
          </p>
          <dl>
            <dt>State</dt>
            <dd>{status.state === "syncing" ? "Running now" : "Idle"}</dd>
            <dt>Last poll</dt>
            <dd>
              {cycle === null
                ? "none yet"
                : `${ago(cycle.finished_at, now)}: ${plural(cycle.behind, "session")} had moved on, ${plural(cycle.events_read, "event")} read`}
            </dd>
            <dt>Next poll</dt>
            <dd>
              {status.state === "syncing" || cycle === null
                ? "when this one ends"
                : nextPoll(cycle.finished_at, status.poll_interval_seconds, now)}
            </dd>
            <dt>Behind</dt>
            <dd>{plural(status.sessions_behind, "session")} (sessions with a live stream are not counted)</dd>
          </dl>
          <button type="button" onClick={onSyncNow} disabled={status.state === "syncing"}>
            Poll now
          </button>
          <h3>Grant</h3>
          <dl>
            <dt>Organization</dt>
            <dd>
              <code>{credential.organization_uuid}</code>
            </dd>
            <dt>Scopes</dt>
            <dd>
              <code>{credential.scopes.join(" ")}</code>
            </dd>
            <dt>Access token</dt>
            <dd>refreshes {untilExpiry(credential.access_token_expires_at, now)}</dd>
          </dl>
        </>
      )}
      {failure !== null && (
        <p role="alert" className="error">
          The last poll failed {ago(failure.at, now)}: {failure.message}
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
    </section>
  );
}
