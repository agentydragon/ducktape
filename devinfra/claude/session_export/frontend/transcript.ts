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
  originToolUseId?: string;
};

export type TranscriptToolStatus = "running" | "complete" | "error" | "denied" | "interrupted";

export type TranscriptToolCall = {
  id: string;
  toolUseId: string;
  name: string;
  input: unknown;
  result?: string;
  progress?: string;
  summary?: string;
  failed?: boolean;
  policyDenied?: boolean;
  status: TranscriptToolStatus;
  tasks: TranscriptActivity[];
  events: SessionEvent[];
};

export type TranscriptToolRun = TranscriptBase & {
  kind: "tool-run";
  tools: TranscriptToolCall[];
  status: TranscriptToolStatus;
  standalone: boolean;
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
  | TranscriptMessage
  | TranscriptToolRun
  | TranscriptActivity
  | TranscriptThinking
  | TranscriptSummary
  | TranscriptNotice;

/** Claude Web consumes message-shaped records; Ducktape persists an API envelope. */
export type AdaptedSessionEvent = {
  sourceEvent: SessionEvent;
  type: string;
  payload: JsonObject;
};

export function adaptSessionEvent(event: SessionEvent): AdaptedSessionEvent {
  const payload = object(event.payload) ?? {};
  const sourceType = string(payload.type) ?? event.event_type;
  const type = sourceType === "user_message" ? "user" : sourceType === "assistant_message" ? "assistant" : sourceType;
  return { sourceEvent: event, type, payload };
}

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

function toolResultText(value: unknown): string | null {
  const record = object(value);
  if (record === null) return textFrom(value);
  const output: string[] = [];
  for (const key of ["stdout", "stderr", "output", "content", "message", "result", "summary"]) {
    const text = textFrom(record[key]);
    if (text !== null && !output.includes(text)) output.push(text);
  }
  return output.length === 0 ? textFrom(value) : output.join("\n");
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

const STANDALONE_TOOLS = new Set([
  "AskUserQuestion",
  "ExitPlanMode",
  "Workflow",
  "Artifact",
  "ReportFindings",
  "ClaudeDesign",
  "SendUserMessage",
  "SendUserFile",
]);

function addEvent(events: SessionEvent[], event: SessionEvent): void {
  if (!events.some((previous) => previous.event_id === event.event_id)) events.push(event);
}

function aggregateStatus(tools: TranscriptToolCall[]): TranscriptToolStatus {
  if (tools.some((tool) => tool.status === "running")) return "running";
  if (tools.some((tool) => tool.status === "error")) return "error";
  if (tools.some((tool) => tool.status === "denied")) return "denied";
  if (tools.some((tool) => tool.status === "interrupted")) return "interrupted";
  return "complete";
}

function sendToolMessage(tool: TranscriptToolCall): string | null {
  if ((tool.name !== "SendUserMessage" && tool.name !== "SendUserFile") || tool.status !== "complete") return null;
  if (tool.failed || tool.policyDenied) return null;
  const input = object(tool.input) ?? {};
  return string(input.message) ?? string(input.caption) ?? null;
}

/**
 * Adapt Ducktape's lossless event envelope into transcript rows. Tool calls are
 * grouped when adjacent in one assistant content list; results, progress, task
 * updates and policy denials are correlated back to those calls. This follows
 * the extracted Claude Web fold boundary while keeping Ducktape's original
 * events attached for inspection.
 */
export function foldSessionEvents(events: SessionEvent[]): TranscriptItem[] {
  const ordered = [...events].sort(sequenceOrder).map(adaptSessionEvent);
  const items: TranscriptItem[] = [];
  const toolsById = new Map<string, { call: TranscriptToolCall; run: TranscriptToolRun }>();
  const tasksById = new Map<
    string,
    { activity: TranscriptActivity; tool?: TranscriptToolCall; run?: TranscriptToolRun }
  >();
  const deniedTools = new Map<string, SessionEvent[]>();

  const addMessage = (
    role: "user" | "assistant",
    text: string,
    event: SessionEvent,
    originToolUseId?: string
  ): void => {
    const previous = items.at(-1);
    if (
      originToolUseId === undefined &&
      previous?.kind === "message" &&
      previous.role === role &&
      previous.originToolUseId === undefined &&
      previous.events.at(-1)?.event_id === event.event_id
    ) {
      previous.text = `${previous.text}\n${text}`;
      addEvent(previous.events, event);
      return;
    }
    items.push({
      kind: "message",
      id: originToolUseId === undefined ? `${eventId(event)}-${items.length}` : `tool-message-${originToolUseId}`,
      role,
      text,
      ...(originToolUseId === undefined ? {} : { originToolUseId }),
      events: [event],
    });
  };

  const addToolResult = (
    toolUseId: string | null,
    result: string | null,
    failed: boolean,
    event: SessionEvent
  ): void => {
    const match = toolUseId === null ? undefined : toolsById.get(toolUseId);
    if (match !== undefined) {
      match.call.result = result ?? match.call.result;
      match.call.failed = failed;
      match.call.status = failed ? "error" : match.call.policyDenied ? "denied" : "complete";
      addEvent(match.call.events, event);
      addEvent(match.run.events, event);
      match.run.status = aggregateStatus(match.run.tools);
      return;
    }
    const callId = toolUseId ?? eventId(event);
    const call: TranscriptToolCall = {
      id: callId,
      toolUseId: callId,
      name: "Tool result",
      input: null,
      result: result ?? undefined,
      failed,
      status: failed ? "error" : "complete",
      tasks: [],
      events: [event],
    };
    const run: TranscriptToolRun = {
      kind: "tool-run",
      id: `tool-run-result-${callId}`,
      tools: [call],
      status: call.status,
      standalone: true,
      events: [event],
    };
    items.push(run);
    if (toolUseId !== null) toolsById.set(toolUseId, { call, run });
  };

  const addActivity = (event: SessionEvent, payload: JsonObject, subtype: string): void => {
    const taskId = string(payload.task_id) ?? eventId(event);
    const parentToolUseId = string(payload.parent_tool_use_id);
    const parentMatch = parentToolUseId === null ? undefined : toolsById.get(parentToolUseId);
    const parentTool = parentMatch?.call;
    const status = statusText(payload.status, subtype === "task_started" ? "running" : subtype.replace("task_", ""));
    const title = string(payload.description) ?? string(payload.task_type) ?? "Background task";
    const detail = textFrom(payload.summary) ?? textFrom(payload.message) ?? textFrom(payload.progress);
    const previous = tasksById.get(taskId);
    if (previous !== undefined) {
      previous.activity.status = status;
      previous.activity.detail = detail ?? previous.activity.detail;
      addEvent(previous.activity.events, event);
      if (previous.tool !== undefined) addEvent(previous.tool.events, event);
      if (previous.run !== undefined) addEvent(previous.run.events, event);
      return;
    }
    const activity: TranscriptActivity = {
      kind: "activity",
      id: `task-${taskId}`,
      taskId,
      title,
      detail: detail ?? undefined,
      status,
      events: [event],
    };
    tasksById.set(taskId, { activity, tool: parentTool, run: parentMatch?.run });
    if (parentTool !== undefined) {
      parentTool.tasks.push(activity);
      addEvent(parentTool.events, event);
      if (parentMatch !== undefined) addEvent(parentMatch.run.events, event);
    } else {
      items.push(activity);
    }
  };

  for (const adapted of ordered) {
    const { sourceEvent: event, payload, type } = adapted;

    if (type === "user" || type === "assistant") {
      const role = type;
      let text = "";
      let currentRun: TranscriptToolRun | null = null;
      const flushText = (): void => {
        if (text !== "") {
          addMessage(role, text, event);
          text = "";
        }
      };
      const flushRun = (): void => {
        currentRun = null;
      };
      for (const value of contentBlocks(payload)) {
        const block = object(value);
        if (block === null) {
          flushRun();
          continue;
        }
        const blockType = string(block.type) ?? "";
        if (blockType === "tool_use") {
          flushText();
          const id = string(block.id) ?? `${eventId(event)}-tool-${items.length}`;
          const name = string(block.name) ?? "Tool";
          const standalone = STANDALONE_TOOLS.has(name);
          const previous = currentRun;
          if (standalone || previous === null || previous.standalone) {
            currentRun = {
              kind: "tool-run",
              id: `tool-run-${eventId(event)}-${id}`,
              tools: [],
              status: "running",
              standalone,
              events: [...(deniedTools.get(id) ?? []), event],
            };
            items.push(currentRun);
          }
          const run = currentRun;
          if (run === null) continue;
          const deniedEvents = deniedTools.get(id) ?? [];
          const call: TranscriptToolCall = {
            id,
            toolUseId: id,
            name,
            input: block.input ?? null,
            status: deniedEvents.length > 0 ? "denied" : "running",
            policyDenied: deniedEvents.length > 0,
            tasks: [],
            events: [...deniedEvents, event],
          };
          deniedTools.delete(id);
          run.tools.push(call);
          run.status = aggregateStatus(run.tools);
          for (const deniedEvent of deniedEvents) addEvent(run.events, deniedEvent);
          toolsById.set(id, { call, run });
          continue;
        }

        flushRun();
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
        } else if (blockType === "tool_result") {
          flushText();
          addToolResult(string(block.tool_use_id), toolResultText(block.content), block.is_error === true, event);
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
        if (linkedId !== null) addToolResult(linkedId, toolResultText(toolResult), toolResult.is_error === true, event);
      }
      continue;
    }

    if (type === "system") {
      const subtype = string(payload.subtype) ?? "system event";
      if (subtype.startsWith("task_")) {
        addActivity(event, payload, subtype);
      } else if (subtype === "permission_denied") {
        const toolUseId = string(payload.tool_use_id) ?? string(object(payload.tool_use)?.id);
        if (toolUseId !== null) {
          const match = toolsById.get(toolUseId);
          if (match !== undefined) {
            match.call.policyDenied = true;
            match.call.status = "denied";
            addEvent(match.call.events, event);
            addEvent(match.run.events, event);
            match.run.status = aggregateStatus(match.run.tools);
          } else {
            deniedTools.set(toolUseId, [...(deniedTools.get(toolUseId) ?? []), event]);
          }
        }
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
      const id = string(payload.tool_use_id);
      const match = id === null ? undefined : toolsById.get(id);
      const detail = textFrom(payload.message) ?? textFrom(payload.summary) ?? textFrom(payload.content);
      if (match !== undefined) {
        if (type === "tool_progress") match.call.progress = detail ?? match.call.progress;
        else match.call.summary = detail ?? match.call.summary;
        addEvent(match.call.events, event);
        addEvent(match.run.events, event);
      } else {
        const name = string(payload.tool_name) ?? string(payload.name) ?? "Tool activity";
        const activityId = id ?? eventId(event);
        items.push({
          kind: "activity",
          id: `tool-activity-${activityId}`,
          taskId: activityId,
          title: name,
          detail: detail ?? undefined,
          status: type === "tool_progress" ? "running" : statusText(payload.status, "complete"),
          events: [event],
        });
      }
      continue;
    }

    if (type === "result") {
      for (const { call, run } of toolsById.values()) {
        if (call.status === "running") {
          call.status = "interrupted";
          addEvent(call.events, event);
          addEvent(run.events, event);
          run.status = aggregateStatus(run.tools);
        }
      }
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

  return items.flatMap((item): TranscriptItem[] => {
    if (item.kind !== "tool-run" || !item.standalone || item.tools.length !== 1) return [item];
    const [tool] = item.tools;
    if (tool === undefined) return [item];
    const text = sendToolMessage(tool);
    return text === null
      ? [item]
      : [
          {
            kind: "message",
            id: `tool-message-${tool.toolUseId}`,
            role: "assistant",
            text,
            originToolUseId: tool.toolUseId,
            events: item.events,
          },
        ];
  });
}

export function transcriptEventTime(item: TranscriptItem): string | null {
  const timestamp = item.events.at(-1)?.created_at;
  if (timestamp === undefined) return null;
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? timestamp : date.toLocaleString();
}
