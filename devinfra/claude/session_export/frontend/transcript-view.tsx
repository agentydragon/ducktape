import { createContext, useContext, useMemo, useState, type JSX, type ReactNode } from "react";
import {
  Accordion,
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Code,
  Group,
  Image,
  Loader,
  Paper,
  Progress,
  ScrollArea,
  SimpleGrid,
  Stack,
  Text,
  Title,
  UnstyledButton,
} from "@mantine/core";

import type { Event, SessionSummary } from "./schema";
import { Markdown } from "../../../../util/frontend/markdown";
import { EventInspector } from "./event-inspector";
import { groupToolActivity, toolGroupSummary, type ToolGroup } from "./tool-groups";
import { buildEventTimeline, EventTimelineStrip } from "./event-timeline";
import { isRoutineSession, sessionSubtitle, statusColor } from "./session-info";
import {
  foldSessionEvents,
  transcriptEventTime,
  type TranscriptItem,
  type TranscriptToolCall,
  type TranscriptToolRun,
} from "./transcript";

function inputRecord(input: unknown): Record<string, unknown> | null {
  return typeof input === "object" && input !== null && !Array.isArray(input)
    ? (input as Record<string, unknown>)
    : null;
}

function toolAction(tool: TranscriptToolCall): { label: string; value: string; code: boolean } | null {
  const input = inputRecord(tool.input);
  if (typeof tool.input === "string") return { label: "Input", value: tool.input, code: true };
  if (input === null) return null;

  const field = (key: string): string | null => (typeof input[key] === "string" ? (input[key] as string) : null);
  if (tool.name === "Bash" || tool.name === "bash") {
    const command = field("command");
    if (command !== null) return { label: "Command", value: command, code: true };
  }
  if (["Read", "Write", "Edit", "MultiEdit", "NotebookEdit"].includes(tool.name)) {
    const path = field("file_path") ?? field("path");
    if (path !== null) return { label: "File", value: path, code: true };
  }
  if (tool.name === "Grep" || tool.name === "Glob") {
    const pattern = field("pattern");
    const path = field("path") ?? field("glob");
    if (pattern !== null || path !== null) {
      return {
        label: tool.name === "Grep" ? "Search" : "Match",
        value: [pattern, path].filter(Boolean).join(" · "),
        code: true,
      };
    }
  }
  if (tool.name === "WebSearch") {
    const query = field("query");
    if (query !== null) return { label: "Search", value: query, code: false };
  }
  if (tool.name === "WebFetch") {
    const url = field("url");
    if (url !== null) return { label: "URL", value: url, code: true };
  }
  if (tool.name === "Task" || tool.name === "Agent") {
    const description = field("description") ?? field("name") ?? field("prompt");
    if (description !== null) return { label: "Task", value: description, code: false };
  }
  for (const [key, label] of [
    ["command", "Command"],
    ["file_path", "File"],
    ["path", "Path"],
    ["query", "Query"],
    ["pattern", "Pattern"],
    ["url", "URL"],
    ["description", "Description"],
  ]) {
    const value = field(key);
    if (value !== null)
      return { label, value, code: label === "Command" || label === "File" || label === "Path" || label === "URL" };
  }
  return null;
}

function toolRunPreview(item: TranscriptToolRun): string {
  const previews = item.tools.slice(0, 3).map((tool) => {
    const action = toolAction(tool);
    const firstLine = action?.value.split("\n")[0]?.trim();
    const summary = firstLine === undefined || firstLine === "" ? tool.name : firstLine;
    const shortSummary = summary.length > 120 ? `${summary.slice(0, 117)}…` : summary;
    return item.tools.length === 1 || firstLine === undefined || firstLine === ""
      ? shortSummary
      : `${tool.name}: ${shortSummary}`;
  });
  if (item.tools.length > 3) previews.push(`+${item.tools.length - 3} more`);
  return previews.join(" · ");
}

function toolArguments(tool: TranscriptToolCall): string | null {
  if (tool.input === null || tool.input === undefined) return null;
  return typeof tool.input === "string" ? tool.input : JSON.stringify(tool.input, null, 2);
}

function toolStatusColor(status: string): "green" | "yellow" | "red" | "gray" {
  if (status === "complete" || status === "completed") return "green";
  if (status === "running") return "yellow";
  if (status === "error" || status === "denied" || status === "failed") return "red";
  return "gray";
}

function ToolRun({ item }: { item: TranscriptToolRun }): JSX.Element {
  return (
    <Stack gap="sm">
      {item.tools.map((tool, index) => {
        const action = toolAction(tool);
        const args = toolArguments(tool);
        return (
          <Paper
            key={tool.toolUseId}
            withBorder
            radius="sm"
            p="sm"
            data-tool-name={tool.name}
            data-parent-tool-use-id={tool.parentToolUseId}
          >
            <Stack gap="xs">
              <Group justify="space-between" align="center" gap="xs">
                <Group gap="xs">
                  <Text size="sm" fw={600}>
                    {tool.name}
                  </Text>
                  <Badge variant="dot" color={toolStatusColor(tool.status)}>
                    {tool.status}
                  </Badge>
                  {tool.policyDenied && (
                    <Badge color="red" variant="light">
                      Permission denied
                    </Badge>
                  )}
                </Group>
                <Text size="xs" c="dimmed">
                  {index + 1} of {item.tools.length}
                </Text>
              </Group>
              {action !== null && (
                <Stack gap={4}>
                  <Text size="xs" c="dimmed">
                    {action.label}
                  </Text>
                  {action.code ? (
                    <Code block>{action.value}</Code>
                  ) : (
                    <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                      {action.value}
                    </Text>
                  )}
                </Stack>
              )}
              {tool.progress !== undefined && (
                <Text size="sm" c="dimmed">
                  {tool.progress}
                </Text>
              )}
              {tool.summary !== undefined && <Text size="sm">{tool.summary}</Text>}
              {(tool.name === "Task" || tool.name === "Agent") &&
                tool.status === "running" &&
                tool.subagentActivity !== undefined && (
                  <Paper
                    withBorder
                    radius="xs"
                    p="xs"
                    data-subagent-activity
                    data-subagent-tool-count={tool.subagentActivity.toolCallCount}
                  >
                    <Group gap="xs">
                      <Text size="xs" fw={500}>
                        Agent activity
                      </Text>
                      <Badge
                        size="xs"
                        variant="light"
                        color="blue"
                        data-subagent-latest-tool={tool.subagentActivity.latestToolName}
                      >
                        {tool.subagentActivity.latestToolName}
                      </Badge>
                      <Badge size="xs" variant="light" color="gray">
                        {tool.subagentActivity.toolCallCount} calls
                      </Badge>
                    </Group>
                    {tool.subagentActivity.model !== undefined && (
                      <Text size="xs" c="dimmed" mt={4}>
                        {tool.subagentActivity.model}
                      </Text>
                    )}
                  </Paper>
                )}
              {(tool.result !== undefined || (tool.outputImages?.length ?? 0) > 0) && (
                <Stack gap={4}>
                  <Text size="xs" c="dimmed">
                    {tool.filePreview !== undefined
                      ? "File contents"
                      : tool.failed
                        ? "Error output"
                        : tool.name === "Bash"
                          ? "Command output"
                          : "Output"}
                  </Text>
                  {tool.filePreview !== undefined ? (
                    <Stack gap={4} data-tool-file-preview>
                      <Text size="xs" ff="monospace" c="dimmed" data-tool-file-path={tool.filePreview.path}>
                        {tool.filePreview.path}
                      </Text>
                      <Code
                        block
                        style={{ maxHeight: 240, overflow: "auto", whiteSpace: "pre-wrap" }}
                        data-tool-file-content
                      >
                        {tool.filePreview.contents || "(empty file)"}
                      </Code>
                    </Stack>
                  ) : tool.result !== undefined ? (
                    <Code block style={{ maxHeight: 240, overflow: "auto", whiteSpace: "pre-wrap" }}>
                      {tool.result}
                    </Code>
                  ) : null}
                  {tool.outputImages?.map((outputImage, imageIndex) => (
                    <Image
                      key={`${outputImage.mimeType}-${imageIndex}`}
                      src={`data:${outputImage.mimeType};base64,${outputImage.data}`}
                      alt={`Tool output ${imageIndex + 1}`}
                      w={240}
                      h={180}
                      fit="contain"
                      radius="sm"
                      data-tool-output-image
                    />
                  ))}
                </Stack>
              )}
              {tool.tasks.map((task) => (
                <Paper key={task.taskId} withBorder radius="xs" p="xs" bg="var(--mantine-color-default-hover)">
                  <Group justify="space-between" align="center" gap="xs">
                    <Text size="sm" fw={500}>
                      {task.title}
                    </Text>
                    <Badge size="xs" variant="light" color={toolStatusColor(task.status)}>
                      {task.status}
                    </Badge>
                  </Group>
                  {task.detail !== undefined && (
                    <Text size="xs" c="dimmed" mt={4}>
                      {task.detail}
                    </Text>
                  )}
                </Paper>
              ))}
              {args !== null && (
                <Accordion variant="default" radius="sm">
                  <Accordion.Item value="arguments">
                    <Accordion.Control>Tool input</Accordion.Control>
                    <Accordion.Panel>
                      <ScrollArea type="auto" mah={240}>
                        <Code block>{args}</Code>
                      </ScrollArea>
                    </Accordion.Panel>
                  </Accordion.Item>
                </Accordion>
              )}
            </Stack>
          </Paper>
        );
      })}
    </Stack>
  );
}

function FoldableDetail({ label, detail }: { label: string; detail: string }): JSX.Element {
  const [expanded, setExpanded] = useState(false);
  return (
    <Stack gap={4}>
      <UnstyledButton
        w="100%"
        mih={22}
        px={4}
        aria-label={expanded ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`}
        aria-expanded={expanded}
        data-detail-toggle
        onClick={() => setExpanded((value) => !value)}
        style={{ display: "flex", alignItems: "center", gap: 4, minWidth: 0 }}
      >
        <Text component="span" size="xs" fw={600} aria-hidden="true">
          {expanded ? "−" : "+"}
        </Text>
        <Text component="span" size="xs" c="dimmed">
          {label}
        </Text>
      </UnstyledButton>
      {expanded && (
        <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {detail}
        </Text>
      )}
    </Stack>
  );
}

type ToolDisclosures = {
  tools: Set<string>;
  groups: Set<string>;
  toggleTool: (id: string) => void;
  toggleGroup: (ids: string[], expanded: boolean) => void;
};
const ToolDisclosureContext = createContext<ToolDisclosures | null>(null);

function ToolDisclosureProvider({ children }: { children: ReactNode }): JSX.Element {
  const [tools, setTools] = useState(new Set<string>());
  const [groups, setGroups] = useState(new Set<string>());
  return (
    <ToolDisclosureContext.Provider
      value={{
        tools,
        groups,
        toggleTool: (id) =>
          setTools((previous) => {
            const next = new Set(previous);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
          }),
        toggleGroup: (ids, expanded) => {
          setGroups((previous) => {
            const next = new Set(previous);
            for (const id of ids) {
              if (expanded) next.delete(id);
              else next.add(id);
            }
            return next;
          });
          if (expanded) setTools((previous) => new Set([...previous].filter((id) => !ids.includes(id))));
        },
      }}
    >
      {children}
    </ToolDisclosureContext.Provider>
  );
}

function useToolDisclosures(): ToolDisclosures {
  const state = useContext(ToolDisclosureContext);
  if (state === null) throw new Error("Tool disclosures require a provider");
  return state;
}

function CompactToolRun({ item }: { item: TranscriptToolRun }): JSX.Element {
  const disclosures = useToolDisclosures();
  const expanded = disclosures.tools.has(item.id);
  return (
    <Paper
      component="article"
      title={transcriptEventTime(item) ?? undefined}
      aria-label={item.tools.length === 1 ? `${item.tools[0]?.name ?? "Tool"} activity` : "Tool activity"}
      data-fold-kind="tool-run"
      data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
      data-tool-count={item.tools.length}
      data-parent-tool-use-id={item.parentToolUseId}
      radius="sm"
      py={2}
    >
      <UnstyledButton
        w="100%"
        mih={22}
        px={4}
        aria-label={expanded ? "Hide tool details" : "Show tool details"}
        aria-expanded={expanded}
        data-tool-run-toggle
        onClick={() => disclosures.toggleTool(item.id)}
        style={{ display: "flex", alignItems: "center", gap: 4, minWidth: 0 }}
      >
        <Text component="span" size="xs" fw={600} c="blue" aria-hidden="true">
          {expanded ? "−" : "+"}
        </Text>
        <Badge component="span" size="xs" variant="light" color="cyan" style={{ flexShrink: 0 }}>
          {item.tools.length}
        </Badge>
        <Text
          component="span"
          size="xs"
          c="dimmed"
          lineClamp={1}
          style={{ minWidth: 0, flex: 1, overflowWrap: "anywhere" }}
        >
          {toolRunPreview(item)}
        </Text>
        {item.status !== "complete" && (
          <Badge
            component="span"
            size="xs"
            variant="dot"
            color={toolStatusColor(item.status)}
            style={{ flexShrink: 0 }}
          >
            {item.status}
          </Badge>
        )}
      </UnstyledButton>
      {expanded && (
        <Stack gap="sm" mt="xs">
          <ToolRun item={item} />
        </Stack>
      )}
    </Paper>
  );
}

function ToolActivityGroup({ item, session }: { item: ToolGroup; session: SessionSummary }): JSX.Element {
  const disclosures = useToolDisclosures();
  const runIds = item.items.filter((row) => row.kind === "tool-run").map((row) => row.id);
  const expanded = runIds.some((id) => disclosures.groups.has(id) || disclosures.tools.has(id));
  const tools = item.items.flatMap((row) => (row.kind === "tool-run" ? row.tools : []));
  const statuses = [...new Set(tools.map((tool) => tool.status))].filter((status) => status !== "complete");
  return (
    <Box
      data-fold-kind="tool-group"
      data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
      data-tool-count={tools.length}
    >
      <Box py={2}>
        <UnstyledButton
          w="100%"
          mih={22}
          px={4}
          aria-label={expanded ? "Hide activity group" : "Show activity group"}
          aria-expanded={expanded}
          data-tool-group-toggle
          onClick={() => disclosures.toggleGroup(runIds, expanded)}
          style={{ display: "flex", alignItems: "center", gap: 4, minWidth: 0 }}
          title={toolGroupSummary(item)}
        >
          <Text component="span" size="xs" aria-hidden="true">
            {expanded ? "−" : "+"}
          </Text>
          <Text component="span" size="xs" truncate style={{ flex: 1, minWidth: 0 }}>
            {toolGroupSummary(item)}
          </Text>
          {statuses.map((status) => (
            <Badge
              component="span"
              key={status}
              size="xs"
              variant="dot"
              color={toolStatusColor(status)}
              style={{ flexShrink: 0 }}
            >
              {status}
            </Badge>
          ))}
        </UnstyledButton>
      </Box>
      {expanded && (
        <Stack gap={4} pl="xs" style={{ borderLeft: "1px solid var(--mantine-color-default-border)" }}>
          {item.items.map((row) => (
            <TranscriptCard key={row.id} item={row} session={session} />
          ))}
        </Stack>
      )}
    </Box>
  );
}

function TranscriptCard({ item, session }: { item: TranscriptItem; session: SessionSummary }): JSX.Element {
  if (item.kind === "tool-run") return <CompactToolRun item={item} />;
  if (item.kind === "narration") {
    return (
      <Box
        data-fold-kind="narration"
        data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
        my={4}
      >
        <Markdown source={item.text} size="sm" />
      </Box>
    );
  }
  const time = transcriptEventTime(item);
  if (item.kind === "message") {
    return (
      <Paper
        component="article"
        aria-label={item.role === "user" ? "You" : "Claude"}
        title={time ?? undefined}
        data-fold-kind="message"
        data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
        data-message-role={item.role}
        data-parent-tool-use-id={item.parentToolUseId}
        radius="sm"
        bg={item.role === "user" ? "var(--mantine-color-default-hover)" : undefined}
        p={item.role === "user" ? "xs" : 0}
        my={4}
      >
        {item.parentToolUseId !== undefined && (
          <Badge size="xs" variant="light">
            Subagent
          </Badge>
        )}
        <Markdown source={item.text} size="sm" />
      </Paper>
    );
  }
  if (item.kind === "activity" && (item.status === "complete" || item.status === "completed")) {
    return (
      <Box
        component="details"
        data-fold-kind="activity"
        data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
        title={time ?? undefined}
        py={2}
      >
        <Box
          component="summary"
          fz="xs"
          c="dimmed"
          data-activity-summary
          title={item.title}
          style={{ cursor: "pointer", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
        >
          {item.title} · completed
        </Box>
        <Text size="sm" py="xs" data-activity-title style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {item.title}
        </Text>
        {item.detail !== undefined && (
          <Text size="sm" py="xs" data-activity-detail style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
            {item.detail}
          </Text>
        )}
      </Box>
    );
  }
  if (
    item.kind === "summary" &&
    item.events.every((event) => event.payload.subtype === "success" && event.payload.is_error !== true)
  ) {
    return (
      <Box
        component="details"
        data-fold-kind="summary"
        data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
        title={time ?? undefined}
        py={2}
      >
        <Box component="summary" fz="xs" c="dimmed" style={{ cursor: "pointer" }}>
          {item.title}
        </Box>
        <Stack gap={4} py="xs">
          {item.details.map((detail, index) => (
            <Text key={index} size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {detail}
            </Text>
          ))}
        </Stack>
      </Box>
    );
  }
  if (item.kind === "thinking") {
    return (
      <Paper
        component="details"
        aria-label="Thinking"
        data-fold-kind="thinking"
        data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
        radius="sm"
        py={2}
        title={time ?? undefined}
      >
        <Box component="summary" fz="xs" c="dimmed" style={{ cursor: "pointer" }}>
          <Text component="span" size="xs" fw={500}>
            Thinking
          </Text>
        </Box>
        <Box mt="xs">
          <Markdown source={item.text} size="sm" />
        </Box>
      </Paper>
    );
  }
  const title =
    item.kind === "peer-message"
      ? `Message from ${item.name ?? item.from}`
      : item.kind === "peer-hold"
        ? item.state === "held"
          ? "Peer message held"
          : "Peer message dropped"
        : item.kind === "activity"
          ? item.title
          : item.kind === "context"
            ? "Context usage"
            : item.kind === "stats"
              ? "Code statistics"
              : item.kind === "usage"
                ? "Plan usage"
                : item.kind === "status"
                  ? "Session status"
                  : item.kind === "summary" || item.kind === "notice"
                    ? item.title
                    : "Transcript item";
  const color =
    item.kind === "peer-message"
      ? "blue"
      : item.kind === "peer-hold"
        ? item.state === "held"
          ? "yellow"
          : "gray"
        : "gray";
  return (
    <Paper
      component="article"
      aria-label={title}
      data-fold-kind={item.kind}
      data-history-sequences={item.events.map((event) => event.sequence_num).join(" ")}
      data-peer-from={item.kind === "peer-message" || item.kind === "peer-hold" ? item.from : undefined}
      data-peer-handback={item.kind === "peer-message" && item.handback ? "true" : undefined}
      data-peer-state={item.kind === "peer-hold" ? item.state : undefined}
      data-context-model={item.kind === "context" ? item.model : undefined}
      data-stats-state={item.kind === "stats" ? (item.stats === null ? "loading" : "data") : undefined}
      withBorder
      radius="sm"
      p="md"
    >
      <Stack gap="sm">
        <Group justify="space-between" align="center" gap="xs">
          <Group gap="xs">
            <Badge variant="light" color={color}>
              {title}
            </Badge>
            {item.kind === "activity" && (
              <Badge variant="dot" color={toolStatusColor(item.status)}>
                {item.status}
              </Badge>
            )}
            {item.kind === "peer-message" && item.handback && (
              <Badge size="xs" variant="light" color="yellow">
                Subagent hand-back
              </Badge>
            )}
          </Group>
          {time !== null && (
            <Text component="time" size="xs" c="dimmed" dateTime={item.events.at(-1)?.created_at}>
              {time}
            </Text>
          )}
        </Group>
        {item.kind === "peer-message" && (
          <Stack gap="xs">
            {item.handbackNote !== undefined && (
              <Alert variant="light" color="yellow" title="Hand-back note">
                <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                  {item.handbackNote}
                </Text>
              </Alert>
            )}
            <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {item.text}
            </Text>
          </Stack>
        )}
        {item.kind === "peer-hold" && (
          <Stack gap="xs">
            <Text size="sm" c="dimmed">
              From {item.name ?? item.from}
            </Text>
            <Group gap="xs">
              <Badge variant="light" color={item.state === "held" ? "yellow" : "gray"}>
                {item.state === "held" ? "Held" : "Dropped"}
              </Badge>
              {item.cause !== undefined && <Badge variant="light">{item.cause.replaceAll("-", " ")}</Badge>}
              {item.outcome !== undefined && <Badge variant="light">{item.outcome.replaceAll("-", " ")}</Badge>}
            </Group>
          </Stack>
        )}
        {item.kind === "activity" && item.detail !== undefined && (
          <FoldableDetail label="Task details" detail={item.detail} />
        )}
        {item.kind === "summary" &&
          (item.details.length === 0 ? (
            <Text size="sm">Turn complete</Text>
          ) : (
            <Group gap="xs">
              {item.details.map((detail) => (
                <Badge key={detail} variant="light" color="gray">
                  {detail}
                </Badge>
              ))}
            </Group>
          ))}
        {item.kind === "context" && (
          <Stack gap="sm">
            <Group justify="space-between" gap="xs">
              <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
                {item.model}
              </Text>
              <Badge variant="light" color={item.percentage >= 90 ? "red" : item.percentage >= 70 ? "yellow" : "blue"}>
                {item.percentage}% used
              </Badge>
            </Group>
            <Progress
              aria-label="Context window used"
              value={Math.max(0, Math.min(100, item.percentage))}
              color={item.percentage >= 90 ? "red" : item.percentage >= 70 ? "yellow" : "blue"}
            />
            <Text size="sm" c="dimmed">
              {item.totalTokens.toLocaleString()} of {item.rawMaxTokens.toLocaleString()} tokens
            </Text>
            {(item.categories.length > 0 ||
              item.mcpTools.length > 0 ||
              item.memoryFiles.length > 0 ||
              item.agents.length > 0) && (
              <Accordion variant="contained" radius="sm">
                <Accordion.Item value="context-breakdown">
                  <Accordion.Control>Context breakdown</Accordion.Control>
                  <Accordion.Panel>
                    <Stack gap="xs">
                      {item.categories.map((row) => (
                        <Group key={`category-${row.name}`} justify="space-between" gap="xs">
                          <Text size="sm">{row.name}</Text>
                          <Text size="sm" c="dimmed">
                            {row.tokens.toLocaleString()} tokens
                          </Text>
                        </Group>
                      ))}
                      {item.mcpTools.map((row) => (
                        <Group key={`mcp-${row.serverName}-${row.name}`} justify="space-between" gap="xs">
                          <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                            {row.name} · {row.serverName}
                          </Text>
                          <Text size="sm" c="dimmed">
                            {row.tokens.toLocaleString()} tokens
                          </Text>
                        </Group>
                      ))}
                      {item.memoryFiles.map((row) => (
                        <Group key={`memory-${row.type}-${row.path}`} justify="space-between" gap="xs">
                          <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                            {row.type}: {row.path}
                          </Text>
                          <Text size="sm" c="dimmed">
                            {row.tokens.toLocaleString()} tokens
                          </Text>
                        </Group>
                      ))}
                      {item.agents.map((row) => (
                        <Group key={`agent-${row.agentType}`} justify="space-between" gap="xs">
                          <Text size="sm">{row.agentType}</Text>
                          <Text size="sm" c="dimmed">
                            {row.tokens.toLocaleString()} tokens
                          </Text>
                        </Group>
                      ))}
                    </Stack>
                  </Accordion.Panel>
                </Accordion.Item>
              </Accordion>
            )}
          </Stack>
        )}
        {item.kind === "stats" &&
          (item.stats === null ? (
            <Group gap="xs" role="status">
              <Loader size="xs" aria-label="Loading code statistics" />
              <Text size="sm" c="dimmed">
                Loading code statistics…
              </Text>
            </Group>
          ) : (
            (() => {
              const activity = Array.isArray(item.stats.dailyActivity)
                ? item.stats.dailyActivity
                    .map(inputRecord)
                    .filter((value): value is Record<string, unknown> => value !== null)
                : [];
              const total = (field: string): number =>
                activity.reduce((sum, row) => sum + (typeof row[field] === "number" ? (row[field] as number) : 0), 0);
              return (
                <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="xs">
                  {[
                    ["Active days", activity.length],
                    ["Sessions", total("sessionCount")],
                    ["Messages", total("messageCount")],
                    ["Tool calls", total("toolCallCount")],
                  ].map(([label, value]) => (
                    <Paper key={label} withBorder radius="xs" p="xs">
                      <Text size="xs" c="dimmed">
                        {label}
                      </Text>
                      <Text fw={600}>{value.toLocaleString()}</Text>
                    </Paper>
                  ))}
                </SimpleGrid>
              );
            })()
          ))}
        {item.kind === "usage" && (
          <Text size="sm" c="dimmed">
            Claude Code reported plan usage for this session.
          </Text>
        )}
        {item.kind === "status" && (
          <Stack gap="xs">
            <Group gap="xs">
              <Text size="sm" c="dimmed">
                Synced session state
              </Text>
              <Badge variant="light" color={statusColor(session.status)}>
                {session.status}
              </Badge>
            </Group>
            <Text size="sm" style={{ overflowWrap: "anywhere" }}>
              {sessionSubtitle(session)}
            </Text>
          </Stack>
        )}
        {item.kind === "notice" && item.detail !== undefined && <FoldableDetail label="Details" detail={item.detail} />}
      </Stack>
    </Paper>
  );
}

export function TranscriptView({
  selectedSession,
  selectedId,
  events,
  loadingEvents,
  eventError,
  hasMoreEvents,
  loadingMoreEvents,
  showRawEvents,
  onToggleRawEvents,
  setEventViewport,
  loadMoreEvents,
}: {
  selectedSession: SessionSummary;
  selectedId: string;
  events: Event[];
  loadingEvents: boolean;
  eventError: string | null;
  hasMoreEvents: boolean;
  loadingMoreEvents: boolean;
  showRawEvents: boolean;
  onToggleRawEvents: () => void;
  setEventViewport: (viewport: HTMLDivElement | null) => void;
  loadMoreEvents: () => void;
}): JSX.Element {
  const transcript = useMemo(() => foldSessionEvents(events), [events]);
  const rows = useMemo(() => buildEventTimeline(groupToolActivity(transcript), events), [transcript, events]);

  return (
    <Stack gap="md" aria-busy={loadingEvents} style={{ height: "100%", minHeight: 0 }}>
      <Group justify="space-between" align="flex-start" gap="xs">
        <Stack gap={4}>
          <Title order={4}>{selectedSession.title || "Untitled session"}</Title>
          <Text size="xs" c="dimmed" ff="monospace" style={{ overflowWrap: "anywhere" }}>
            {sessionSubtitle(selectedSession)}
          </Text>
        </Stack>
        <Group gap={4}>
          <Badge variant="light" color={statusColor(selectedSession.status)}>
            {selectedSession.status}
          </Badge>
          {isRoutineSession(selectedSession) && (
            <Badge variant="light" color="violet">
              Routine run
            </Badge>
          )}
          <Button
            variant={showRawEvents ? "light" : "subtle"}
            size="compact-xs"
            aria-label={showRawEvents ? "Show folded transcript" : "Show raw event stream"}
            aria-pressed={showRawEvents}
            onClick={onToggleRawEvents}
          >
            {showRawEvents ? "Transcript" : "Events"}
          </Button>
        </Group>
      </Group>

      {eventError !== null && (
        <Alert color="red" title="Could not load transcript">
          {eventError}
        </Alert>
      )}

      {loadingEvents ? (
        <Center h={180}>
          <Loader size="sm" aria-label="Loading transcript" />
        </Center>
      ) : events.length === 0 ? (
        <Center h={180}>
          <Text c="dimmed" ta="center">
            No events are stored for this session yet.
          </Text>
        </Center>
      ) : (
        <ScrollArea style={{ flex: 1, minHeight: 0 }} type="auto" viewportRef={setEventViewport}>
          <Stack gap={4} pr="sm">
            {hasMoreEvents && (
              <Button variant="default" loading={loadingMoreEvents} onClick={() => void loadMoreEvents()}>
                Load older events
              </Button>
            )}
            {showRawEvents ? (
              <EventInspector key={selectedId} events={events} />
            ) : (
              <ToolDisclosureProvider key={selectedId}>
                {rows.map((row) =>
                  row.kind === "event-strip" ? (
                    <EventTimelineStrip key={row.id} events={row.events} />
                  ) : row.kind === "tool-group" ? (
                    <ToolActivityGroup key={row.id} item={row} session={selectedSession} />
                  ) : (
                    <TranscriptCard key={`${row.kind}-${row.id}`} item={row} session={selectedSession} />
                  )
                )}
              </ToolDisclosureProvider>
            )}
          </Stack>
        </ScrollArea>
      )}
    </Stack>
  );
}
