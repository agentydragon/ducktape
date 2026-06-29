import { useEffect, useState } from "react";

import { fetchImprovements } from "./client.ts";
import { renderMarkdown } from "./markdown.ts";
import type { Friction, ImprovementIdea, ImprovementsBoard } from "./types.ts";

// Haku's read-only self-backlog: capability ideas it could grow into, and friction it hits
// during runs (data-access gaps, flaky/limited backends) that the operator might want to fix.
// Source: improvements.yaml, gardened each run (procedures/maintenance_and_synthesis.md).

const VALUE_RANK = { high: 0, medium: 1, low: 2 } as const;
// Open problems first; the rest are FYI / closed-loop.
const FRICTION_RANK = { open: 0, workaround: 1, answered: 2, resolved: 3 } as const;

function ideaCard(idea: ImprovementIdea) {
  return (
    <li key={idea.id} className="board-line">
      <div>
        <strong>{idea.title}</strong>
        <span className={`chip chip-val-${idea.value}`}>{idea.value} value</span>
        <span className={`chip chip-st-${idea.status}`}>{idea.status}</span>
        <div className="dimmed">{idea.summary}</div>
        {idea.detail ? (
          <details className="imp-detail">
            <summary>details</summary>
            <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(idea.detail) }} />
          </details>
        ) : null}
      </div>
    </li>
  );
}

function frictionCard(f: Friction) {
  return (
    <li key={f.id} className="board-line">
      <div>
        <strong>{f.title}</strong>
        <span className={`chip chip-sev-${f.severity}`}>{f.severity}</span>
        <span className={`chip chip-st-${f.status}`}>{f.status}</span>
        {f.detail ? <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(f.detail) }} /> : null}
      </div>
    </li>
  );
}

export function ImprovementsPage() {
  const [data, setData] = useState<ImprovementsBoard | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchImprovements()
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  if (error) return <p className="page-error">Failed to load improvements: {error}</p>;
  if (!data) return <p className="loading">Loading…</p>;

  const ideas = [...data.ideas].sort((a, b) => VALUE_RANK[a.value] - VALUE_RANK[b.value]);
  const friction = [...data.friction].sort((a, b) => FRICTION_RANK[a.status] - FRICTION_RANK[b.status]);
  const openCount = friction.filter((f) => f.status === "open").length;

  return (
    <div className="board">
      <p className="dimmed">
        What would make me more useful, and what's getting in my way — Haku's own backlog, value-ranked.
        Steer it with a note from the Inbox tab. {data.updated ? `Updated ${new Date(data.updated).toLocaleString()}.` : null}
      </p>

      <section className="board-section">
        <h3>💡 Capability ideas ({ideas.length})</h3>
        <ul className="board-list">{ideas.map(ideaCard)}</ul>
      </section>

      <section className="board-section">
        <h3>
          🔧 Friction &amp; breakages ({friction.length}
          {openCount > 0 ? `, ${openCount} open` : ""})
        </h3>
        <ul className="board-list">{friction.map(frictionCard)}</ul>
      </section>
    </div>
  );
}
