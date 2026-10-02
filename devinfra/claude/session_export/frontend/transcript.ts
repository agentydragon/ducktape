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
  parentToolUseId?: string;
};

export type TranscriptPeerMessage = TranscriptBase & {
  kind: "peer-message";
  messageUuid: string;
  from: string;
  name?: string;
  handback?: boolean;
  handbackNote?: string;
  text: string;
};

export type TranscriptPeerHold = TranscriptBase & {
  kind: "peer-hold";
  messageUuid: string;
  from: string;
  name?: string;
  state: "held" | "dropped";
  cause?: string;
  outcome?: string;
};

export type TranscriptToolStatus = "running" | "complete" | "error" | "denied" | "interrupted";

export type TranscriptToolImage = {
  data: string;
  mimeType: string;
};

export type TranscriptToolFilePreview = {
  path: string;
  contents: string;
};

export type TranscriptSubagentActivity = {
  latestToolName: string;
  toolCallCount: number;
  model?: string;
};

export type TranscriptToolCall = {
  id: string;
  toolUseId: string;
  name: string;
  input: unknown;
  parentToolUseId?: string;
  subagentActivity?: TranscriptSubagentActivity;
  result?: string;
  filePreview?: TranscriptToolFilePreview;
  outputImages?: TranscriptToolImage[];
  toolUseResult?: JsonObject;
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
  parentToolUseId?: string;
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

export type TranscriptContextUsage = TranscriptBase & {
  kind: "context";
  model: string;
  totalTokens: number;
  rawMaxTokens: number;
  percentage: number;
  categories: Array<{ name: string; tokens: number }>;
  mcpTools: Array<{ name: string; serverName: string; tokens: number }>;
  memoryFiles: Array<{ type: string; path: string; tokens: number }>;
  agents: Array<{ agentType: string; tokens: number }>;
};

export type TranscriptCodeStats = TranscriptBase & {
  kind: "stats";
  stats: JsonObject | null;
};

export type TranscriptPlanUsage = TranscriptBase & { kind: "usage" };
export type TranscriptSessionStatus = TranscriptBase & { kind: "status" };

export type TranscriptNotice = TranscriptBase & {
  kind: "notice";
  title: string;
  detail?: string;
};

export type TranscriptItem =
  | TranscriptMessage
  | TranscriptPeerMessage
  | TranscriptPeerHold
  | TranscriptToolRun
  | TranscriptActivity
  | TranscriptThinking
  | TranscriptSummary
  | TranscriptContextUsage
  | TranscriptCodeStats
  | TranscriptPlanUsage
  | TranscriptSessionStatus
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

function safePeerLabel(value: unknown): string | null {
  const label = string(value);
  if (label === null) return null;
  const normalized = label.replace(/[\p{C}\p{Z}]+/gu, " ").trim();
  if (!/[\p{L}\p{N}\p{P}\p{S}]/u.test(normalized)) return null;
  const characters = Array.from(normalized);
  return characters.length > 64 ? `${characters.slice(0, 63).join("")}…` : normalized;
}

type PeerHoldState = "held" | "released" | "dropped";

type DecodedPeerHold = {
  messageUuid: string;
  from: string;
  name?: string;
  state: PeerHoldState;
  cause?: string;
  outcome?: string;
};

function decodePeerHold(payload: JsonObject): DecodedPeerHold | null {
  if (payload.type !== "system" || payload.subtype !== "peer_message_hold") return null;
  const messageUuid = string(payload.message_uuid);
  const state = string(payload.state);
  if (messageUuid === null || (state !== "held" && state !== "released" && state !== "dropped")) return null;
  const from = safePeerLabel(payload.from);
  if (state === "held" && from === null) return null;
  const cause = string(payload.cause);
  const outcome = string(payload.outcome);
  const validCode = (code: string | null): string | undefined =>
    code !== null && /^[a-z][a-z-]{0,40}$/.test(code) ? code : undefined;
  const name = safePeerLabel(payload.from_name);
  const causeCode = validCode(cause);
  const outcomeCode = validCode(outcome);
  return {
    messageUuid,
    from: from ?? "",
    ...(name === null ? {} : { name }),
    state,
    ...(causeCode === undefined ? {} : { cause: causeCode }),
    ...(outcomeCode === undefined ? {} : { outcome: outcomeCode }),
  };
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

type TranscriptToolResultContent = {
  text: string | null;
  images: TranscriptToolImage[];
};

function toolResultContent(value: unknown): TranscriptToolResultContent {
  if (typeof value === "string") return { text: string(value), images: [] };
  if (!Array.isArray(value)) return { text: null, images: [] };

  const text: string[] = [];
  const images: TranscriptToolImage[] = [];
  for (const part of value) {
    const block = object(part);
    if (block?.type === "text") {
      const blockText = string(block.text);
      if (blockText !== null) text.push(blockText);
    } else if (block?.type === "image") {
      const source = object(block.source);
      const data = string(source?.data) ?? string(block.data);
      const mimeType = string(source?.media_type) ?? string(block.mimeType) ?? "image/png";
      if (data !== null && /^image\/[a-z0-9.+-]+$/i.test(mimeType)) images.push({ data, mimeType });
    }
  }
  return { text: text.length === 0 ? null : text.join("\n"), images };
}

const READ_LINE_NUMBER_PREFIX = /^ *\d+(?:[:|] ?|\u2192|\t)/;

function readFilePreview(
  tool: TranscriptToolCall,
  output: string | null,
  failed: boolean
): TranscriptToolFilePreview | null {
  if (tool.name !== "Read" || output === null || output === "" || failed) return null;
  const path = string(object(tool.input)?.file_path) ?? "file";
  const withoutReminder = output.replace(/\n<system-reminder>[\s\S]*$/, "");
  const withoutTrailingNewlines = withoutReminder.replace(/\n+$/, "");
  const lines = withoutTrailingNewlines.split("\n");
  const hasOnlyNumberedLines = lines.every((line) => line === "" || READ_LINE_NUMBER_PREFIX.test(line));
  const contents = hasOnlyNumberedLines
    ? lines.map((line) => line.replace(READ_LINE_NUMBER_PREFIX, "")).join("\n")
    : withoutTrailingNewlines;
  return { path, contents };
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

function parseTokenCount(value: string | undefined): number | null {
  if (value === undefined) return null;
  const match = /^([\d.]+)\s*([km])?$/i.exec(value.trim());
  if (match === null) return null;
  const amount = Number.parseFloat(match[1] ?? "");
  const unit = match[2]?.toLowerCase();
  if (!Number.isFinite(amount)) return null;
  return Math.round(amount * (unit === "m" ? 1_000_000 : unit === "k" ? 1_000 : 1));
}

function contextTable(text: string, heading: string): string[][] {
  const escapedHeading = heading.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const section = new RegExp(`### ${escapedHeading}\\s*([\\s\\S]*?)(?=\\n### |\\n## |$)`).exec(text)?.[1];
  if (section === undefined) return [];
  return section
    .split(/\r?\n/)
    .filter((line) => line.trimStart().startsWith("|") && !/^\|[-\s|:]+\|$/.test(line.trim()))
    .slice(1)
    .map((line) =>
      line
        .split("|")
        .slice(1, -1)
        .map((cell) => cell.trim())
    );
}

function parseContextUsage(text: string): Omit<TranscriptContextUsage, "id" | "events" | "kind"> | null {
  if (!text.startsWith("## Context Usage")) return null;
  const model = /\*\*Model:\*\*\s*(.+?)\s*$/m.exec(text)?.[1]?.trim();
  const tokenMatch = /\*\*Tokens:\*\*\s*(\S+)\s*\/\s*(\S+)\s*\((\d+)%\)/.exec(text);
  const totalTokens = parseTokenCount(tokenMatch?.[1]);
  const rawMaxTokens = parseTokenCount(tokenMatch?.[2]);
  const percentage = Number.parseInt(tokenMatch?.[3] ?? "", 10);
  if (
    model === undefined ||
    totalTokens === null ||
    rawMaxTokens === null ||
    rawMaxTokens === 0 ||
    !Number.isFinite(percentage)
  )
    return null;

  const rows = (heading: string): Array<{ name: string; tokens: number }> =>
    contextTable(text, heading).flatMap((cells) => {
      const name = cells[0];
      const tokens = parseTokenCount(cells.at(-1));
      return name === undefined || tokens === null ? [] : [{ name, tokens }];
    });
  const mcpTools = contextTable(text, "MCP Tools").flatMap((cells) => {
    const [name, serverName] = cells;
    const tokens = parseTokenCount(cells.at(-1));
    return name === undefined || serverName === undefined || tokens === null ? [] : [{ name, serverName, tokens }];
  });
  const memoryFiles = contextTable(text, "Memory Files").flatMap((cells) => {
    const [type, path] = cells;
    const tokens = parseTokenCount(cells.at(-1));
    return type === undefined || path === undefined || tokens === null ? [] : [{ type, path, tokens }];
  });
  const agents = contextTable(text, "Custom Agents").flatMap((cells) => {
    const [agentType] = cells;
    const tokens = parseTokenCount(cells.at(-1));
    return agentType === undefined || tokens === null ? [] : [{ agentType, tokens }];
  });
  return {
    model,
    totalTokens,
    rawMaxTokens,
    percentage,
    categories: rows("Estimated usage by category"),
    mcpTools,
    memoryFiles,
    agents,
  };
}

type ParsedCodeStats = { kind: "none" } | { kind: "loading" } | { kind: "data"; stats: JsonObject };

function parseCodeStats(text: string): ParsedCodeStats {
  const raw = /<code-stats>([\s\S]*?)<\/code-stats>/.exec(text)?.[1]?.trim();
  if (raw === undefined) return { kind: "none" };
  if (raw === "") return { kind: "loading" };
  try {
    const stats = object(JSON.parse(raw));
    return stats === null || !Array.isArray(stats.dailyActivity) ? { kind: "none" } : { kind: "data", stats };
  } catch {
    return { kind: "none" };
  }
}

function localCommandOutput(payload: JsonObject): string | null {
  const content = string(payload.content);
  if (content === null) return null;
  const stdout = /<local-command-stdout>([\s\S]*?)<\/local-command-stdout>/.exec(content)?.[1]?.trim() ?? "";
  const stderr = /<local-command-stderr>([\s\S]*?)<\/local-command-stderr>/.exec(content)?.[1]?.trim() ?? "";
  return stderr || stdout || null;
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
 * updates and policy denials are correlated back to those calls. Peer-origin
 * messages and hold transitions are joined by message UUID. This follows the
 * extracted Claude Web fold boundary while keeping Ducktape's original events
 * attached for inspection.
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
  const pendingSubagentActivity = new Map<string, TranscriptSubagentActivity>();
  const countedSubagentEvents = new Set<string>();
  const suppressedTaskIds = new Set<string>();
  const peerHoldsByMessageUuid = new Map<string, TranscriptPeerHold>();
  const peerMessagesByUuid = new Map<string, TranscriptPeerMessage>();
  const pendingPeerLifecycleEvents = new Map<string, SessionEvent[]>();
  const eventItemIndices = new Map<string, number>();
  const nextEventItemId = (event: SessionEvent, kind: string): string => {
    const key = eventId(event);
    const index = eventItemIndices.get(key) ?? 0;
    eventItemIndices.set(key, index + 1);
    return `${key}-${kind}-${index}`;
  };

  const addMessage = (
    role: "user" | "assistant",
    text: string,
    event: SessionEvent,
    originToolUseId?: string,
    parentToolUseId?: string
  ): void => {
    const previous = items.at(-1);
    if (
      originToolUseId === undefined &&
      previous?.kind === "message" &&
      previous.role === role &&
      previous.originToolUseId === undefined &&
      previous.parentToolUseId === parentToolUseId &&
      previous.events.at(-1)?.event_id === event.event_id
    ) {
      previous.text = `${previous.text}\n${text}`;
      addEvent(previous.events, event);
      return;
    }
    items.push({
      kind: "message",
      id: originToolUseId === undefined ? nextEventItemId(event, "message") : `tool-message-${originToolUseId}`,
      role,
      text,
      ...(originToolUseId === undefined ? {} : { originToolUseId }),
      ...(parentToolUseId === undefined ? {} : { parentToolUseId }),
      events: [event],
    });
  };

  const addPeerMessage = (payload: JsonObject, event: SessionEvent): boolean => {
    const origin = object(payload.origin);
    if (origin?.kind !== "peer") return false;
    const from = safePeerLabel(origin.from);
    if (from === null) return false;
    const text = contentBlocks(payload)
      .map((value) => {
        const block = object(value);
        return block?.type === "text" || block?.type === "connector_text" ? textFrom(block.text) : null;
      })
      .filter((part): part is string => part !== null)
      .join("\n");
    if (text === "") return false;
    const messageUuid = string(payload.uuid) ?? eventId(event);
    const name = safePeerLabel(origin.name) ?? safePeerLabel(origin.from_name);
    const handbackNote = string(origin.handbackNote) ?? string(origin.handback_note);
    const peerMessage: TranscriptPeerMessage = {
      kind: "peer-message",
      id: `peer-message-${messageUuid}`,
      messageUuid,
      from,
      ...(name === null ? {} : { name }),
      ...(origin.handback === true || handbackNote !== null ? { handback: true } : {}),
      ...(handbackNote === null ? {} : { handbackNote }),
      text,
      events: [event],
    };
    const pendingLifecycle = pendingPeerLifecycleEvents.get(messageUuid);
    if (pendingLifecycle !== undefined) {
      for (const pendingEvent of pendingLifecycle) addEvent(peerMessage.events, pendingEvent);
      pendingPeerLifecycleEvents.delete(messageUuid);
    }
    peerMessage.events.sort(sequenceOrder);
    peerMessagesByUuid.set(messageUuid, peerMessage);
    items.push(peerMessage);
    return true;
  };

  const foldPeerHold = (payload: JsonObject, event: SessionEvent): boolean => {
    if (payload.type !== "system" || payload.subtype !== "peer_message_hold") return false;
    const hold = decodePeerHold(payload);
    if (hold === null) return true;
    const previous = peerHoldsByMessageUuid.get(hold.messageUuid);
    if (hold.state === "released") {
      if (previous !== undefined) {
        addEvent(previous.events, event);
        peerHoldsByMessageUuid.delete(hold.messageUuid);
        const index = items.indexOf(previous);
        if (index !== -1) items.splice(index, 1);
        const peerMessage = peerMessagesByUuid.get(hold.messageUuid);
        if (peerMessage !== undefined) {
          for (const holdEvent of previous.events) addEvent(peerMessage.events, holdEvent);
          peerMessage.events.sort(sequenceOrder);
        } else {
          pendingPeerLifecycleEvents.set(hold.messageUuid, [...previous.events]);
        }
      }
      return true;
    }
    const from = hold.from || previous?.from;
    if (from === undefined || from === "") return true;
    if (previous !== undefined) {
      previous.from = from;
      previous.name = hold.name ?? previous.name;
      previous.state = hold.state;
      previous.cause = hold.cause ?? previous.cause;
      previous.outcome = hold.outcome;
      addEvent(previous.events, event);
      const peerMessage = peerMessagesByUuid.get(hold.messageUuid);
      if (peerMessage !== undefined) {
        addEvent(peerMessage.events, event);
        peerMessage.events.sort(sequenceOrder);
      }
      return true;
    }
    const foldedHold: TranscriptPeerHold = {
      kind: "peer-hold",
      id: `peer-hold-${eventId(event)}`,
      messageUuid: hold.messageUuid,
      from,
      ...(hold.name === undefined ? {} : { name: hold.name }),
      state: hold.state,
      ...(hold.cause === undefined ? {} : { cause: hold.cause }),
      ...(hold.outcome === undefined ? {} : { outcome: hold.outcome }),
      events: [event],
    };
    peerHoldsByMessageUuid.set(hold.messageUuid, foldedHold);
    const peerMessage = peerMessagesByUuid.get(hold.messageUuid);
    if (peerMessage !== undefined) {
      addEvent(peerMessage.events, event);
      peerMessage.events.sort(sequenceOrder);
    }
    items.push(foldedHold);
    return true;
  };

  const accumulateSubagentActivity = (parentToolUseId: string, payload: JsonObject, event: SessionEvent): void => {
    const eventKey = eventId(event);
    if (countedSubagentEvents.has(eventKey)) return;
    const message = object(payload.message);
    const model = string(message?.model);
    const existing =
      toolsById.get(parentToolUseId)?.call.subagentActivity ?? pendingSubagentActivity.get(parentToolUseId);
    let activity = existing;
    for (const value of contentBlocks(payload)) {
      const block = object(value);
      const name = block?.type === "tool_use" ? string(block.name) : null;
      if (name !== null) {
        activity = {
          latestToolName: name,
          toolCallCount: (activity?.toolCallCount ?? 0) + 1,
          ...((model ?? activity?.model) === undefined ? {} : { model: model ?? activity?.model }),
        };
      }
    }
    countedSubagentEvents.add(eventKey);
    if (activity === existing || activity === undefined) return;
    const parent = toolsById.get(parentToolUseId);
    if (parent !== undefined) parent.call.subagentActivity = activity;
    else pendingSubagentActivity.set(parentToolUseId, activity);
  };

  const addToolResult = (
    toolUseId: string | null,
    result: TranscriptToolResultContent,
    failed: boolean,
    event: SessionEvent
  ): void => {
    const match = toolUseId === null ? undefined : toolsById.get(toolUseId);
    if (match !== undefined) {
      const output = result.text ?? match.call.result ?? null;
      match.call.result = output ?? undefined;
      delete match.call.filePreview;
      const filePreview = readFilePreview(match.call, output, failed);
      if (filePreview !== null) match.call.filePreview = filePreview;
      if (result.images.length > 0) match.call.outputImages = result.images;
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
      result: result.text ?? undefined,
      ...(result.images.length === 0 ? {} : { outputImages: result.images }),
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

  const attachToolUseResult = (toolUseId: string, result: JsonObject, event: SessionEvent): void => {
    const match = toolsById.get(toolUseId);
    if (match === undefined) return;
    match.call.toolUseResult = result;
    addEvent(match.call.events, event);
    addEvent(match.run.events, event);
  };

  const addActivity = (event: SessionEvent, payload: JsonObject, subtype: string): void => {
    const taskId = string(payload.task_id) ?? eventId(event);
    if (payload.skip_transcript === true || payload.ambient === true || suppressedTaskIds.has(taskId)) {
      suppressedTaskIds.add(taskId);
      return;
    }
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
    const parentToolUseId = string(payload.parent_tool_use_id);

    if (type === "user" || type === "assistant") {
      const role = type;
      if (role === "assistant" && parentToolUseId !== null) accumulateSubagentActivity(parentToolUseId, payload, event);
      const isSidechain = payload.isSidechain === true;
      const sidechainRowLeftToParent = payload.subagentRowLeftToParent === true;
      if (parentToolUseId !== null || (isSidechain && !(sidechainRowLeftToParent && parentToolUseId === null))) {
        continue;
      }
      if (role === "user" && addPeerMessage(payload, event)) continue;
      let text = "";
      let currentRun: TranscriptToolRun | null = null;
      const flushText = (): void => {
        if (text !== "") {
          addMessage(role, text, event, undefined, parentToolUseId ?? undefined);
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
          const id = string(block.id) ?? nextEventItemId(event, "tool");
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
              ...(parentToolUseId === null ? {} : { parentToolUseId }),
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
            ...(parentToolUseId === null ? {} : { parentToolUseId }),
            ...(pendingSubagentActivity.get(id) === undefined
              ? {}
              : { subagentActivity: pendingSubagentActivity.get(id) }),
            status: deniedEvents.length > 0 ? "denied" : "running",
            policyDenied: deniedEvents.length > 0,
            tasks: [],
            events: [...deniedEvents, event],
          };
          deniedTools.delete(id);
          pendingSubagentActivity.delete(id);
          run.tools.push(call);
          run.status = aggregateStatus(run.tools);
          for (const deniedEvent of deniedEvents) addEvent(run.events, deniedEvent);
          toolsById.set(id, { call, run });
          continue;
        }

        flushRun();
        if (blockType === "text" || blockType === "connector_text") {
          const blockText = textFrom(block.text);
          if (blockText !== null && blockText !== "No response requested.") {
            text = text === "" ? blockText : `${text}\n${blockText}`;
          }
        } else if (blockType === "thinking") {
          flushText();
          const thinking = textFrom(block.thinking) ?? textFrom(block.text);
          if (thinking !== null)
            items.push({
              kind: "thinking",
              id: nextEventItemId(event, "thinking"),
              text: thinking,
              events: [event],
            });
        } else if (blockType === "tool_result") {
          flushText();
          addToolResult(string(block.tool_use_id), toolResultContent(block.content), block.is_error === true, event);
        }
      }
      flushText();
      const toolResult = object(payload.tool_use_result);
      if (toolResult !== null) {
        const linkedId = string(payload.tool_use_id) ?? string(toolResult.tool_use_id);
        if (linkedId !== null) attachToolUseResult(linkedId, toolResult, event);
      }
      continue;
    }

    if (type === "system") {
      const subtype = string(payload.subtype) ?? "system event";
      if (subtype === "local_command" || subtype === "local_command_output") {
        const output = localCommandOutput(payload);
        if (output === null) continue;
        const id = string(payload.uuid) ?? eventId(event);
        const context = parseContextUsage(output);
        if (context !== null) {
          items.push({ kind: "context", id: `${id}-ctx`, ...context, events: [event] });
        } else {
          const stats = parseCodeStats(output);
          if (stats.kind === "loading" || stats.kind === "data") {
            items.push({
              kind: "stats",
              id: `${id}-stats`,
              stats: stats.kind === "data" ? stats.stats : null,
              events: [event],
            });
          } else if (output === "<plan-usage/>") {
            items.push({ kind: "usage", id: `${id}-usage`, events: [event] });
          } else if (output === "<session-status/>") {
            items.push({ kind: "status", id: `${id}-status`, events: [event] });
          } else if (output === "No response requested.") {
            continue;
          } else {
            items.push({ kind: "message", id, role: "assistant", text: output, events: [event] });
          }
        }
      } else if (subtype === "peer_message_hold") {
        foldPeerHold(payload, event);
      } else if (subtype === "task_started" || subtype === "task_notification") {
        addActivity(event, payload, subtype);
      } else if (subtype === "permission_denied") {
        const toolUseId = string(payload.tool_use_id) ?? string(object(payload.tool_use)?.id);
        if (toolUseId !== null && payload.agent_id === undefined) {
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
      } else if (
        subtype === "hook_started" ||
        subtype === "hook_response" ||
        subtype === "init" ||
        subtype === "thinking_tokens"
      ) {
        continue;
      } else if (
        subtype === "compact_boundary" ||
        subtype === "model_refusal_fallback" ||
        subtype === "model_fallback" ||
        subtype === "gap_fill_failed" ||
        subtype === "scheduled_task_fire" ||
        subtype === "informational" ||
        subtype === "ui_log" ||
        subtype === "away_summary" ||
        subtype === "memory_saved" ||
        subtype === "model_consent_fallback" ||
        subtype === "agents_killed"
      ) {
        if (subtype === "informational") {
          if (parentToolUseId !== null || string(payload.level)?.toLowerCase() === "info") continue;
          if (string(payload.content) === null) continue;
        }
        if (
          parentToolUseId !== null &&
          subtype !== "compact_boundary" &&
          subtype !== "model_refusal_fallback" &&
          subtype !== "model_fallback" &&
          subtype !== "gap_fill_failed" &&
          subtype !== "scheduled_task_fire"
        ) {
          continue;
        }
        const title =
          subtype === "compact_boundary" ? "Conversation compacted" : `System · ${subtype.replaceAll("_", " ")}`;
        items.push({
          kind: "notice",
          id: eventId(event),
          title,
          detail: textFrom(payload.message) ?? textFrom(payload.description) ?? undefined,
          events: [event],
        });
      } else {
        continue;
      }
      continue;
    }

    if (type === "env_manager_log") {
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

    // Unsupported event kinds remain available through the raw event stream.
    continue;
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
