import { useEffect, useState } from "react";

import { clickAction, fetchDashboard, unclickAction } from "./client.ts";
import { UP_NEXT } from "./constants.ts";
import { FeedbackForm } from "./feedback.tsx";
import { TaskCard, clickKey } from "./task.tsx";
import type { DashboardResponse, Item } from "./types.ts";

function statusCounts(items: Item[]): string {
  const counts: Record<string, number> = {};
  for (const item of items) counts[item.status] = (counts[item.status] ?? 0) + 1;
  return Object.keys(counts)
    .sort()
    .map((status) => `${status}: ${counts[status]}`)
    .join(" · ");
}

export default function App() {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [clicked, setClicked] = useState<ReadonlySet<string>>(new Set());
  const [actionError, setActionError] = useState<string | null>(null);

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

  // A failed initial load leaves nothing to render, so it gets a persistent page-level
  // message rather than an inline one.
  if (error) return <p className="page-error">Failed to load: {error}</p>;
  if (!data) return <p className="loading">Loading…</p>;

  const open = data.items.filter((item) => item.status === "open").sort((a, b) => b.value - a.value);
  const upNext = open.slice(0, UP_NEXT);
  const backlog = open.slice(UP_NEXT);

  return (
    <div className="app">
      <header>
        <h1>Haku</h1>
        <p className="dimmed">Your value-ranked backlog</p>
      </header>

      {actionError && <p className="action-error">Action failed: {actionError}</p>}

      <h2>Up next</h2>
      {upNext.length > 0 ? (
        upNext.map((item) => <TaskCard key={item.id} item={item} clicked={clicked} onToggle={onToggle} />)
      ) : (
        <p>No open items.</p>
      )}

      {backlog.length > 0 && (
        <details className="backlog">
          <summary>Backlog — {backlog.length} more open item(s)</summary>
          {backlog.map((item) => (
            <TaskCard key={item.id} item={item} clicked={clicked} onToggle={onToggle} />
          ))}
        </details>
      )}

      <section className="note">
        <h2>Note to Haku</h2>
        <FeedbackForm
          minRows={3}
          placeholder="Anything for Haku to fold into its next run…"
          submitLabel="Send to Haku"
        />
      </section>

      <footer className="dimmed">
        {open.length} open · {statusCounts(data.items)}
        <br />
        Last scan: {data.scan_time}
      </footer>
    </div>
  );
}
