import { useEffect, useState } from "react";
import { Anchor, Group, Loader, Text, Title } from "@mantine/core";

import { type DashboardResponse, type Item, clickAction, fetchDashboard, unclickAction } from "./client.ts";
import { INTAKE_NEW, UP_NEXT } from "./constants.ts";
import { FeedbackForm } from "./feedback.tsx";
import { LaunchRoutineButton } from "./launch.tsx";
import { TaskCard, clickKey } from "./task.tsx";
import { toastError } from "./toast.ts";

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
    void (wasClicked ? unclickAction(itemId, actionId) : clickAction(itemId, actionId)).catch((e: unknown) => {
      const reverted = new Set(next);
      if (wasClicked) reverted.add(key);
      else reverted.delete(key);
      setClicked(reverted);
      toastError("Action failed", e);
    });
  }

  // Error reporting standard: action failures (launch, feedback, a click) surface as
  // toasts (see toast.ts). The initial dashboard load is the one exception — a failure
  // leaves nothing to render, so it gets a persistent page-level message rather than a
  // toast that could auto-dismiss over a blank screen.
  if (error)
    return (
      <Text c="red" className="mx-auto max-w-3xl p-4">
        Failed to load: {error}
      </Text>
    );
  if (!data)
    return (
      <div className="flex justify-center p-8">
        <Loader />
      </div>
    );

  const open = data.items.filter((item) => item.status === "open").sort((a, b) => b.value - a.value);
  const upNext = open.slice(0, UP_NEXT);
  const backlog = open.slice(UP_NEXT);

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <Group justify="space-between" align="center">
        <Title order={1}>Haku</Title>
        <LaunchRoutineButton />
      </Group>
      <Text c="dimmed" mb="lg">
        Your value-ranked backlog ·{" "}
        <Anchor href={INTAKE_NEW} c="dimmed" underline="always">
          + Add intake note
        </Anchor>
      </Text>

      <Title order={2} mt="xl" mb="sm">
        Up next
      </Title>
      {upNext.length > 0 ? (
        upNext.map((item) => <TaskCard key={item.id} item={item} clicked={clicked} onToggle={onToggle} />)
      ) : (
        <Text>No open items.</Text>
      )}
      {backlog.length > 0 && (
        <details className="my-4">
          <summary className="cursor-pointer font-semibold">Backlog — {backlog.length} more open item(s)</summary>
          {backlog.map((item) => (
            <TaskCard key={item.id} item={item} clicked={clicked} onToggle={onToggle} />
          ))}
        </details>
      )}

      <section className="mt-10">
        <Title order={2} mb="sm">
          Note to Haku
        </Title>
        <FeedbackForm
          minRows={3}
          placeholder="Anything for Haku to fold into its next run…"
          submitLabel="Send to Haku"
        />
      </section>

      <Text
        component="footer"
        c="dimmed"
        size="sm"
        mt="xl"
        className="border-t border-slate-200 pt-4 dark:border-slate-700"
      >
        {open.length} open · {statusCounts(data.items)}
        <br />
        Last scan: {data.scan_time}
      </Text>
    </div>
  );
}
