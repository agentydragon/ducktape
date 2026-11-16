import type { AgentMessageContent } from "./codex-record";
import type { ObjectValue } from "./json-values";
import type { EventRecord } from "./schema";

export type RecordPresentation = "user" | "assistant" | "activity" | "metadata" | "compaction" | "record";

export type ActivityStep =
  | {
      key: string;
      kind: "tool";
      records: EventRecord[];
      name: string;
      input: string | null;
      output: string | null;
      failed: boolean;
      missingCall: boolean;
    }
  | { key: string; kind: "reasoning"; records: EventRecord[]; text: string }
  | {
      key: string;
      kind: "subagent";
      records: EventRecord[];
      author: string;
      recipient: string | null;
      content: AgentMessageContent;
    }
  | { key: string; kind: "attachment"; records: EventRecord[]; title: string; preview: string }
  | { key: string; kind: "record"; records: EventRecord[] };

export type TranscriptRow =
  | { key: string; kind: "record"; records: EventRecord[] }
  | { key: string; kind: "metadata"; records: EventRecord[] }
  | { key: string; kind: "activity"; records: EventRecord[]; steps: ActivityStep[] };

export type ToolEvent = {
  key: string;
  namespace: "claude" | "codex";
  kind: "call" | "output";
  payload: ObjectValue;
  callId: string | null;
  record: EventRecord;
  blockIndex: number;
};

export type ToolPairs = {
  callByOutputKey: Map<string, ToolEvent>;
  outputByCallKey: Map<string, ToolEvent>;
};

export type StepCandidate = { step: ActivityStep; ordinal: number; blockIndex: number };
