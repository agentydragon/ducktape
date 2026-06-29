import { useEffect, useState } from "react";

import { clickAction, fetchDashboard, unclickAction } from "./client.ts";
import { UP_NEXT } from "./constants.ts";
import { countdown } from "./deadline.ts";
import { TaskCard, clickKey } from "./task.tsx";
import { ImprovementsPage } from "./improvements.tsx";
import type { DashboardResponse, Item } from "./types.ts";

// haku-ui is a multi-surface app Haku owns and evolves — NOT a fixed dashboard. The starter
// ships two person-agnostic surfaces: the **Inbox** (the items board) and **Improvements**
// (Haku's self-backlog). Haku adds more tabs as bespoke surfaces for its operator's life
// (e.g. a Kitchen/shopping board, a one-off decision page) by writing a new `*.tsx`, a
// backend endpoint, and a `View` entry here. Those operator-specific surfaces live in that
// operator's haku-state, not in this generic starter.
type View = "inbox" | "improvements";

function statusCounts(items: Item[]): string {
  const counts: Record<string, number> = {};
  for (const item of items) counts[item.status] = (counts[item.status] ?? 0) + 1;
  return Object.keys(counts)
    .sort()
    .map((status) => `${status}: ${counts[status]}`)
    .join(" · ");
}

interface InboxProps {
  data: DashboardResponse | null;
  error: string | null;
  clicked: ReadonlySet<string>;
  onToggle: (itemId: string, actionId: string) => void;
  actionError: string | null;
  now: number;
}

// The triage board: a value-ranked list of items with a time-critical "Due soon" section.
// One surface among several (see App's tabs) — not the whole of haku-ui.
function InboxView({ data, error, clicked, onToggle, actionError, now }: InboxProps) {
  if (error) return <p className="page-error">Failed to load: {error}</p>;
  if (!data) return <p className="loading">Loading…</p>;

  const open = data.items.filter((item) => item.status === "open");
  // Time-critical first: anything overdue or within DUE_SOON_DAYS, soonest deadline on top.
  const dueSoon = open
    .filter((item) => item.deadline && countdown(item.deadline, now).urgency !== "later")
    .sort((a, b) => new Date(a.deadline!).getTime() - new Date(b.deadline!).getTime());
  const dueSoonIds = new Set(dueSoon.map((item) => item.id));
  // Everything else stays value-ranked; due-soon items are pulled out so they appear once.
  const ranked = open.filter((item) => !dueSoonIds.has(item.id)).sort((a, b) => b.value - a.value);
  const upNext = ranked.slice(0, UP_NEXT);
  const backlog = ranked.slice(UP_NEXT);

  return (
    <>
      {actionError && <p className="action-error">Action failed: {actionError}</p>}

      {dueSoon.length > 0 && (
        <section className="due-soon">
          <h2>⏰ Due soon</h2>
          {dueSoon.map((item) => (
            <TaskCard key={item.id} item={item} clicked={clicked} onToggle={onToggle} now={now} />
          ))}
        </section>
      )}

      <h2>Up next</h2>
      {upNext.length > 0 ? (
        upNext.map((item) => <TaskCard key={item.id} item={item} clicked={clicked} onToggle={onToggle} now={now} />)
      ) : (
        <p>No open items.</p>
      )}

      {backlog.length > 0 && (
        <details className="backlog">
          <summary>Backlog — {backlog.length} more open item(s)</summary>
          {backlog.map((item) => (
            <TaskCard key={item.id} item={item} clicked={clicked} onToggle={onToggle} now={now} />
          ))}
        </details>
      )}

      <footer className="dimmed">
        {open.length} open · {statusCounts(data.items)}
        <br />
        Last scan:{" "}
        <time dateTime={data.scan_time} title={data.scan_time}>
          {new Date(data.scan_time).toLocaleString()}
        </time>
        {data.deployed_commit && data.deployed_commit_url && (
          <>
            {" · deployed "}
            <a href={data.deployed_commit_url}>
              <code>{data.deployed_commit}</code>
            </a>
          </>
        )}
      </footer>
    </>
  );
}

export default function App() {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [clicked, setClicked] = useState<ReadonlySet<string>>(new Set());
  const [actionError, setActionError] = useState<string | null>(null);
  const [view, setView] = useState<View>("inbox");
  // Ticks the live deadline countdowns; 30s is fine at minute granularity.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    let alive = true;
    fetchDashboard()
      .then((dashboard) => {
        if (!alive) return;
        setData(dashboard);
        setClicked(new Set(dashboard.clicks.map((c) => clickKey(c.item_id, c.action_id))));
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  function onToggle(itemId: string, actionId: string) {
    const key = clickKey(itemId, actionId);
    const wasClicked = clicked.has(key);
    const next = new Set(clicked);
    if (wasClicked) next.delete(key);
    else next.add(key);
    setClicked(next); // optimistic; reverted below on failure
    setActionError(null);
    void (wasClicked ? unclickAction(itemId, actionId) : clickAction(itemId, actionId)).catch((e: unknown) => {
      const reverted = new Set(next);
      if (wasClicked) reverted.add(key);
      else reverted.delete(key);
      setClicked(reverted);
      setActionError(e instanceof Error ? e.message : String(e));
    });
  }

  const tabs: [View, string][] = [
    ["inbox", "Inbox"],
    ["improvements", "💡 Improvements"],
  ];

  return (
    <div className="app">
      <header>
        <h1>Haku</h1>
        <nav className="tabs">
          {tabs.map(([id, label]) => (
            <button key={id} className={view === id ? "tab tab-on" : "tab"} onClick={() => setView(id)}>
              {label}
            </button>
          ))}
        </nav>
      </header>

      {view === "improvements" ? (
        <ImprovementsPage />
      ) : (
        <InboxView data={data} error={error} clicked={clicked} onToggle={onToggle} actionError={actionError} now={now} />
      )}
    </div>
  );
}
