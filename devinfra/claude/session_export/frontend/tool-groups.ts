import type { SessionEvent } from "./api";
import type { TranscriptItem, TranscriptThinking, TranscriptToolRun } from "./transcript";

export type ToolGroup = {
  kind: "tool-group";
  id: string;
  items: Array<TranscriptToolRun | TranscriptThinking>;
  events: SessionEvent[];
};

/** Group adjacent tool work without crossing prose, notices, or subagent boundaries. */
export function groupToolActivity(items: TranscriptItem[]): Array<TranscriptItem | ToolGroup> {
  const rows: Array<TranscriptItem | ToolGroup> = [];
  let pending: Array<TranscriptToolRun | TranscriptThinking> = [];
  const flush = (): void => {
    // Thinking before the next tool belongs to that work; trailing thinking
    // remains a separate disclosure when prose or another boundary intervenes.
    let end = pending.length;
    while (end > 0 && pending[end - 1]!.kind === "thinking") end -= 1;
    const members = pending.slice(0, end);
    const runs = members.filter((item) => item.kind === "tool-run");
    if (runs.length > 1) {
      const events = new Map(members.flatMap((item) => item.events).map((event) => [event.event_id, event]));
      rows.push({ kind: "tool-group", id: runs[0]!.id, items: members, events: [...events.values()] });
    } else {
      rows.push(...members);
    }
    rows.push(...pending.slice(end));
    pending = [];
  };
  for (const item of items) {
    if (item.kind === "thinking") {
      pending.push(item);
    } else if (
      item.kind === "tool-run" &&
      !item.standalone &&
      !item.tools.some((tool) => tool.name === "Task" || tool.name === "Agent")
    ) {
      const previous = pending.find((candidate) => candidate.kind === "tool-run");
      if (previous?.parentToolUseId !== item.parentToolUseId) flush();
      pending.push(item);
    } else {
      flush();
      rows.push(item);
    }
  }
  flush();
  return rows;
}

export function toolGroupSummary(group: ToolGroup): string {
  const counts = new Map<string, number>();
  for (const item of group.items) {
    if (item.kind !== "tool-run") continue;
    for (const tool of item.tools) counts.set(tool.name, (counts.get(tool.name) ?? 0) + 1);
  }
  return [...counts].map(([name, count]) => `${name} × ${count}`).join(" · ");
}
