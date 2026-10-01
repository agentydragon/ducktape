import type { SessionEvent } from "./api";

type JsonObject = Record<string, unknown>;

type TranscriptBase = {
  id: string;
  events: SessionEvent[];
};

export type TranscriptMessage = TranscriptBase & {
  kind: "message";
  role: "user" | "assistant";
  text: string;
};

export type TranscriptTool = TranscriptBase & {
  kind: "tool";
  toolUseId: string;
  name: string;
  input: unknown;
  result?: string;
  failed?: boolean;
  status: "running" | "complete" | "error";
};

export type TranscriptActivity = TranscriptBase & {
  kind: "activity";
  taskId: string;
  title: string;
  detail?: string;
  status: string;
};

export type TranscriptThinking = TranscriptBase & {
  kind: "thinking";
  text: string;
};

export type TranscriptSummary = TranscriptBase & {
  kind: "summary";
  title: string;
  details: string[];
};

export type TranscriptNotice = TranscriptBase & {
  kind: "notice";
  title: string;
  detail?: string;
};

export type TranscriptItem =
  TranscriptMessage | TranscriptTool | TranscriptActivity | TranscriptThinking | TranscriptSummary | TranscriptNotice;

function object(value: unknown): JsonObject | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as JsonObject) : null;
}

function string(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function textFrom(value: unknown, depth = 0): string | null {
  if (typeof value === "string") return value.trim() === "" ? null : value;
  if (depth >= 5) return null;
  if (Array.isArray(value)) {
    const parts = value.map((part) => textFrom(part, depth + 1)).filter((part): part is string => part !== null);
    return parts.length === 0 ? null : parts.join("\n");
  }
  const record = object(value);
  if (record === null) return null;
  for (const key of ["text", "content", "message", "output", "summary", "description"]) {
    if (record[key] !== undefined) {
      const result = textFrom(record[key], depth + 1);
      if (result !== null) return result;
    }
  }
  return null;
}

function payloadOf(event: SessionEvent): JsonObject {
  return object(event.payload) ?? {};
}

function eventKind(event: SessionEvent, payload: JsonObject): string {
  const type = string(payload.type) ?? event.event_type;
  if (type === "user_message") return "user";
  if (type === "assistant_message") return "assistant";
  return type;
}

function eventId(event: SessionEvent): string {
  return event.event_id || event.sequence_num;
}

function sequenceOrder(left: SessionEvent, right: SessionEvent): number {
  try {
    const a = BigInt(left.sequence_num);
    const b = BigInt(right.sequence_num);
    return a < b ? -1 : a > b ? 1 : 0;
  } catch {
    return left.sequence_num.localeCompare(right.sequence_num);
  }
}

function contentBlocks(payload: JsonObject): unknown[] {
  const message = object(payload.message);
  const content = message?.content ?? payload.content;
  if (Array.isArray(content)) return content;
  return typeof content === "string" ? [{ type: "text", text: content }] : [];
}

function statusText(value: unknown, fallback: string): string {
  const status = string(value);
  if (status === null) return fallback;
  return status.replaceAll("_", " ");
}

function usageDetails(payload: JsonObject): string[] {
  const usage = object(payload.usage) ?? {};
  const details: string[] = [];
  const totalTokens = usage.total_tokens ?? payload.total_tokens;
  if (typeof totalTokens === "number") details.push(`${totalTokens.toLocaleString()} tokens`);
  const cost = payload.total_cost_usd ?? payload.cost_usd;
  if (typeof cost === "number") details.push(`$${cost.toFixed(4)}`);
  const duration = payload.duration_ms;
  if (typeof duration === "number") details.push(`${(duration / 1000).toFixed(1)} seconds`);
  return details;
}

/**
 * Turns the lossless event log into a compact conversation view. Message text is
 * emitted as message rows; tool results update the matching tool call; task lifecycle
 * events update one activity row. Unknown event kinds remain visible as notices,
 * with their original payload available from the item for an expandable detail.
 */
export function foldSessionEvents(events: SessionEvent[]): TranscriptItem[] {
  const ordered = [...events].sort(sequenceOrder);
  const items: TranscriptItem[] = [];
  const toolIndex = new Map<string, number>();
  const taskIndex = new Map<string, number>();

  const addMessage = (role: "user" | "assistant", text: string, event: SessionEvent): void => {
    const previous = items.at(-1);
    if (previous?.kind === "message" && previous.role === role && previous.events.at(-1)?.event_id === event.event_id) {
      previous.text = `${previous.text}\n${text}`;
      previous.events.push(event);
      return;
    }
    items.push({ kind: "message", id: `${eventId(event)}-${items.length}`, role, text, events: [event] });
  };

  const addToolResult = (
    toolUseId: string | null,
    result: string | null,
    failed: boolean,
    event: SessionEvent
  ): void => {
    const index = toolUseId === null ? undefined : toolIndex.get(toolUseId);
    const previous = index === undefined ? undefined : items[index];
    if (previous?.kind === "tool") {
      previous.result = result ?? previous.result;
      previous.failed = failed;
      previous.status = failed ? "error" : "complete";
      previous.events.push(event);
      return;
    }
    items.push({
      kind: "tool",
      id: `${eventId(event)}-${items.length}`,
      toolUseId: toolUseId ?? eventId(event),
      name: "Tool result",
      input: null,
      result: result ?? undefined,
      failed,
      status: failed ? "error" : "complete",
      events: [event],
    });
  };

  const addActivity = (event: SessionEvent, payload: JsonObject, subtype: string): void => {
    const taskId = string(payload.task_id) ?? eventId(event);
    const status = statusText(payload.status, subtype === "task_started" ? "running" : subtype.replace("task_", ""));
    const title = string(payload.description) ?? string(payload.task_type) ?? "Background task";
    const detail = textFrom(payload.summary) ?? textFrom(payload.message) ?? textFrom(payload.progress);
    const index = taskIndex.get(taskId);
    const previous = index === undefined ? undefined : items[index];
    if (previous?.kind === "activity") {
      previous.status = status;
      previous.detail = detail ?? previous.detail;
      previous.events.push(event);
      return;
    }
    taskIndex.set(taskId, items.length);
    items.push({
      kind: "activity",
      id: `task-${taskId}`,
      taskId,
      title,
      detail: detail ?? undefined,
      status,
      events: [event],
    });
  };

  for (const event of ordered) {
    const payload = payloadOf(event);
    const type = eventKind(event, payload);

    if (type === "user" || type === "assistant") {
      const role = type;
      let text = "";
      const flushText = (): void => {
        if (text !== "") {
          addMessage(role, text, event);
          text = "";
        }
      };
      for (const value of contentBlocks(payload)) {
        const block = object(value);
        if (block === null) continue;
        const blockType = string(block.type) ?? "";
        if (blockType === "text") {
          const blockText = textFrom(block.text);
          if (blockText !== null) text = text === "" ? blockText : `${text}\n${blockText}`;
        } else if (blockType === "thinking") {
          flushText();
          const thinking = textFrom(block.thinking) ?? textFrom(block.text);
          if (thinking !== null)
            items.push({
              kind: "thinking",
              id: `${eventId(event)}-thinking-${items.length}`,
              text: thinking,
              events: [event],
            });
        } else if (blockType === "tool_use") {
          flushText();
          const id = string(block.id) ?? `${eventId(event)}-tool-${items.length}`;
          const tool: TranscriptTool = {
            kind: "tool",
            id,
            toolUseId: id,
            name: string(block.name) ?? "Tool",
            input: block.input ?? null,
            status: "running",
            events: [event],
          };
          toolIndex.set(id, items.length);
          items.push(tool);
        } else if (blockType === "tool_result") {
          flushText();
          addToolResult(string(block.tool_use_id), textFrom(block.content), block.is_error === true, event);
        } else {
          flushText();
          const description = textFrom(block);
          if (description !== null)
            items.push({
              kind: "notice",
              id: `${eventId(event)}-block-${items.length}`,
              title: statusText(blockType, "Content"),
              detail: description,
              events: [event],
            });
        }
      }
      flushText();
      const toolResult = object(payload.tool_use_result);
      if (toolResult !== null) {
        const linkedId = string(payload.tool_use_id) ?? string(toolResult.tool_use_id);
        if (linkedId !== null) addToolResult(linkedId, textFrom(toolResult), toolResult.is_error === true, event);
      }
      continue;
    }

    if (type === "system") {
      const subtype = string(payload.subtype) ?? "system event";
      if (subtype.startsWith("task_")) {
        addActivity(event, payload, subtype);
      } else if (subtype === "init" || subtype === "thinking_tokens") {
        continue;
      } else {
        const title =
          subtype === "compact_boundary" ? "Conversation compacted" : `System · ${subtype.replaceAll("_", " ")}`;
        items.push({
          kind: "notice",
          id: eventId(event),
          title,
          detail: textFrom(payload.message) ?? textFrom(payload.description) ?? undefined,
          events: [event],
        });
      }
      continue;
    }

    if (type === "tool_progress" || type === "tool_use_summary") {
      const name = string(payload.tool_name) ?? string(payload.name) ?? "Tool activity";
      const id = string(payload.tool_use_id) ?? eventId(event);
      const detail = textFrom(payload.message) ?? textFrom(payload.summary) ?? textFrom(payload.content);
      const status = type === "tool_progress" ? "running" : statusText(payload.status, "complete");
      items.push({
        kind: "activity",
        id: `tool-activity-${id}`,
        taskId: id,
        title: name,
        detail: detail ?? undefined,
        status,
        events: [event],
      });
      continue;
    }

    if (type === "result") {
      const details = usageDetails(payload);
      const resultText = textFrom(payload.result);
      if (resultText !== null) details.unshift(resultText);
      items.push({ kind: "summary", id: eventId(event), title: "Turn complete", details, events: [event] });
      continue;
    }

    const subtype = string(payload.subtype) ?? string(object(payload.request)?.subtype);
    const title =
      type === "control_request" || type === "control_response"
        ? `Permission · ${subtype?.replaceAll("_", " ") ?? type.replaceAll("_", " ")}`
        : type.replaceAll("_", " ");
    const detail = textFrom(payload.message) ?? textFrom(payload.content) ?? textFrom(payload.summary);
    items.push({ kind: "notice", id: eventId(event), title, detail: detail ?? undefined, events: [event] });
  }

  return items;
}

export function transcriptEventTime(item: TranscriptItem): string | null {
  const timestamp = item.events.at(-1)?.created_at;
  if (timestamp === undefined) return null;
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? timestamp : date.toLocaleString();
}
