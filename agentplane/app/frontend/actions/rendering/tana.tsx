import { Badge, Code, Stack, Text } from "@mantine/core";
import type { JSX, ReactNode } from "react";
import { z } from "zod";

import { moveNodeArguments } from "../schemas/integration_previews";
import { Markdown } from "../../../../../util/frontend/markdown";
import { ClampedBlock } from "../../../../../util/frontend/clamped_block";
import { TextBlock } from "../../ui/text_block";
import { tanaTitles } from "../schemas/tana_titles";
import { definePreview, type PreviewProps } from "./entry";
import { LiveResourceLink, useIntegrationPreviewNames } from "./integration_previews";
import { type ActionPresentation, type CardDecisionProps, expandsForRejectedArguments } from "./presentation";
import { defineResultPreview, type ResultPreview, type ResultPreviewContext } from "./result_entry";

const calendarNodeArguments = z.strictObject({
  workspaceId: z.string(),
  granularity: z.enum(["day", "week", "month", "year"]),
  date: z.string().optional(),
});

function calendarPeriod(args: z.infer<typeof calendarNodeArguments>): string {
  if (args.date) {
    return args.granularity === "day" ? args.date : `${args.granularity} containing ${args.date}`;
  }
  return args.granularity === "day" ? "today" : `this ${args.granularity}`;
}

function TanaNodeLink({ id, name }: { id: string; name?: string }): JSX.Element {
  return <LiveResourceLink id={id} name={name} href={`https://app.tana.inc?nodeid=${encodeURIComponent(id)}`} />;
}

const calendarNode = definePreview(
  calendarNodeArguments,
  function CalendarNode({ args, requestId }: PreviewProps<z.infer<typeof calendarNodeArguments>>): JSX.Element {
    const workspace = useIntegrationPreviewNames("tana", requestId)?.get(args.workspaceId);
    return (
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        For {calendarPeriod(args)} in {workspace ?? <Code>{args.workspaceId}</Code>}
      </Text>
    );
  }
);

const calendarResult = defineResultPreview(z.strictObject({ nodeId: z.string().min(1) }), ({ result, context }) => {
  const args = calendarNodeArguments.safeParse(context?.arguments);
  const label = args.success
    ? args.data.date
      ? `Calendar node for ${calendarPeriod(args.data)}`
      : `Open ${args.data.granularity} calendar node`
    : "Open calendar node";
  return (
    <Text size="sm">
      <TanaNodeLink id={result.nodeId} name={label} />
    </Text>
  );
});

const calendarNodePresentation: ActionPresentation = {
  fallbackLabel: "Get or create Tana calendar node",
  pane: { collapsed: calendarNode },
  details: { arguments: calendarNode, result: calendarResult },
  offersCardDecisionButtons: (args) => calendarNodeArguments.safeParse(args).success,
  compactCardExpands: expandsForRejectedArguments(calendarNodeArguments),
};

const moveNode = definePreview(
  moveNodeArguments,
  function MoveNode({ args, requestId }: PreviewProps<z.infer<typeof moveNodeArguments>>): JSX.Element {
    const names = useIntegrationPreviewNames("tana", requestId);
    const node = (id: string) => <TanaNodeLink id={id} name={names?.get(id)} />;
    const atEdge = args.position === "start" || args.position === "end";
    const reference = args.referenceNodeId ? node(args.referenceNodeId) : null;
    return (
      <Text size="sm" style={{ overflowWrap: "anywhere" }}>
        Move {node(args.nodeId)}
        {args.sourceParentId && <> from {node(args.sourceParentId)}</>}
        {atEdge ? (
          ` to the ${args.position} of `
        ) : (
          <>
            {" "}
            {args.position} {reference ?? "(reference node missing)"} in{" "}
          </>
        )}
        {node(args.targetNodeId)}
        {atEdge && reference && <>, with reference {reference}</>}
        {args.keepSourceReference ? ", keeping the source reference." : ", removing the source reference."}
      </Text>
    );
  }
);

// Tana Desktop emits the renderer's confirmation followed by labeled IDs and placement.
const moveResultPattern =
  /^Moved "(.*)\n\nNode ID: (\S+)\nPrevious parent: (.*) \((\S+)\)\nNew parent: (.*) \((\S+)\)\nPosition: (start|end|before|after)$/;

const moveResult = z.string().transform((text, ctx) => {
  const match = moveResultPattern.exec(text);
  if (match) {
    const [, message, nodeId, previousName, previousId, targetName, targetId, position] = match;
    const suffix = `" from "${previousName}" to "${targetName}" (${position})`;
    // The first line repeats the parent names and placement. Match them before extracting the node title.
    if (message?.endsWith(suffix) && nodeId && previousId && targetId && position) {
      return {
        nodeId,
        nodeName: message.slice(0, -suffix.length),
        previousId,
        previousName,
        targetId,
        targetName,
        position,
      };
    }
  }
  ctx.addIssue({ code: "custom", message: "Unrecognized Tana move receipt" });
  return z.NEVER;
});

const moveReceipt = defineResultPreview(moveResult, ({ result }) => (
  <Text size="sm" style={{ overflowWrap: "anywhere" }}>
    Moved <TanaNodeLink id={result.nodeId} name={result.nodeName} /> from{" "}
    <TanaNodeLink id={result.previousId} name={result.previousName} /> to{" "}
    <TanaNodeLink id={result.targetId} name={result.targetName} /> ({result.position}).
  </Text>
));

const moveNodePresentation: ActionPresentation = {
  fallbackLabel: "Move Tana node",
  pane: { collapsed: moveNode, opened: moveNode },
  details: { arguments: moveNode, result: moveReceipt },
};

const id = z.string().min(1);
const noArguments = z.strictObject({});
const nodeArguments = z.strictObject({ nodeId: id });
const readArguments = nodeArguments.extend({ maxDepth: z.number().min(0).max(10).default(1) });
const childrenArguments = nodeArguments.extend({
  limit: z.number().min(1).max(200).default(100),
  offset: z.number().min(0).default(0),
});
const openArguments = nodeArguments.extend({ openType: z.enum(["current", "panel", "tab"]).default("current") });
const tagsArguments = z.strictObject({ workspaceId: id, limit: z.number().min(1).max(200).default(200) });
const tagSchemaArguments = z.strictObject({
  tagId: id,
  includeEditInstructions: z.boolean().optional(),
  includeInheritedFields: z.boolean().optional(),
});
const edit = z.strictObject({ old_string: z.string(), new_string: z.string(), replace_all: z.boolean().optional() });
const editArguments = nodeArguments
  .extend({ name: edit.optional(), description: edit.optional() })
  .refine((args) => args.name !== undefined || args.description !== undefined);
const tagArguments = nodeArguments.extend({ tagIds: z.array(id).min(1), action: z.enum(["add", "remove"]) });
const checkboxArguments = z.strictObject({
  tagId: id,
  showCheckbox: z.boolean(),
  doneStateMapping: z
    .strictObject({ fieldId: id, checkedValues: z.array(id).min(1), uncheckedValues: z.array(id).optional() })
    .optional(),
});
const createTagArguments = z.strictObject({
  workspaceId: id,
  name: z.string().min(1),
  description: z.string().optional(),
  extendsTagIds: z.array(id).optional(),
  showCheckbox: z.boolean().optional(),
});
const fieldType = z.enum(["plain", "number", "date", "url", "email", "checkbox", "user", "instance", "options"]);
const addFieldArguments = z.strictObject({
  tagId: id,
  name: z.string().min(1),
  dataType: fieldType,
  description: z.string().optional(),
  sourceTagId: id.optional(),
  options: z.array(z.string()).optional(),
  defaultValue: z.union([z.string(), z.boolean(), z.number()]).optional(),
  isMultiValue: z.boolean().optional(),
});
const fieldArguments = nodeArguments.extend({
  attributeId: id,
  mode: z.enum(["replace", "append"]).default("replace"),
});
const fieldContentArguments = fieldArguments
  .extend({ content: z.string().nullable() })
  .refine((args) => args.mode === "replace" || args.content !== null);
const fieldOptionArguments = fieldArguments.extend({ optionId: id });
const pasteArguments = z.strictObject({ parentNodeId: id, content: z.string() });

const openPlace: Record<z.infer<typeof openArguments>["openType"], string> = {
  current: "in the current panel",
  panel: "in a new side panel",
  tab: "in a new tab",
};

function Line({ children }: { children: ReactNode }): JSX.Element {
  return (
    <Text size="sm" style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>
      {children}
    </Text>
  );
}

function requestView<S extends z.ZodType>(
  name: string,
  schema: S,
  view: (args: z.infer<S>, names: ReadonlyMap<string, string> | undefined) => ReactNode,
  result: ResultPreview,
  summary: (args: z.infer<S>, names: ReadonlyMap<string, string> | undefined) => ReactNode = view
): ActionPresentation {
  const preview = definePreview(schema, function TanaRequest({ args, requestId }) {
    const names = useIntegrationPreviewNames("tana", requestId);
    return view(args, names);
  });
  const collapsed = definePreview(schema, function TanaSummary({ args, requestId }) {
    const names = useIntegrationPreviewNames("tana", requestId);
    return summary(args, names);
  });
  return {
    fallbackLabel: tanaTitles.get(name),
    pane: { collapsed, opened: preview },
    details: { arguments: preview, result },
  };
}

function nodeLink(nodeId: string, names: ReadonlyMap<string, string> | undefined): JSX.Element {
  return <TanaNodeLink id={nodeId} name={names?.get(nodeId)?.trim()} />;
}

function nodeLinks(ids: string[], names: ReadonlyMap<string, string> | undefined): ReactNode {
  return ids.map((nodeId, index) => (
    <span key={index}>
      {index > 0 && ", "}
      {nodeLink(nodeId, names)}
    </span>
  ));
}

function tanaDecision<S extends z.ZodType>(schema: S, targets: (args: z.infer<S>) => string[] | undefined) {
  return function TanaDecision({ args, requestId, buttons, fallback }: CardDecisionProps): ReactNode {
    const parsed = schema.safeParse(args);
    const ids = parsed.success ? targets(parsed.data) : undefined;
    const names = useIntegrationPreviewNames("tana", ids ? requestId : undefined);
    return ids?.every((id) => names?.get(id)?.trim()) ? buttons : fallback;
  };
}

const checkDecision = tanaDecision(nodeArguments, ({ nodeId }) => [nodeId]);
const tagDecision = tanaDecision(tagArguments, ({ nodeId, tagIds }) => {
  const [tagId] = tagIds;
  return tagIds.length === 1 && tagId !== undefined && tagId !== nodeId ? [nodeId, tagId] : undefined;
});

function checkPresentation(name: "check_node" | "uncheck_node", result: ResultPreview): ActionPresentation {
  return {
    ...requestView(
      name,
      nodeArguments,
      (args, names) => <Line>{nodeLink(args.nodeId, names)} · Syncs the mapped field if configured</Line>,
      result
    ),
    liveCardDecision: { component: checkDecision, collapsed: true },
  };
}

function tagChange(
  args: z.infer<typeof tagArguments>,
  names: ReadonlyMap<string, string> | undefined,
  compact = false
): JSX.Element {
  const tags = compact ? args.tagIds.slice(0, 2) : args.tagIds;
  return (
    <Line>
      {args.action === "add" ? "Add" : "Remove"} {nodeLinks(tags, names)}
      {tags.length < args.tagIds.length && ` and ${args.tagIds.length - tags.length} more`}{" "}
      {args.action === "add" ? "to" : "from"} {nodeLink(args.nodeId, names)}
    </Line>
  );
}

function workspaceName(workspaceId: string, names: ReadonlyMap<string, string> | undefined): ReactNode {
  return names?.get(workspaceId) ?? <Code>{workspaceId}</Code>;
}

function changes(label: string, operation: z.infer<typeof edit>): JSX.Element {
  return (
    <Stack gap={2}>
      <Text size="sm" fw={600}>
        {label} · {operation.replace_all ? "All matches" : "One match"}
      </Text>
      <Line>Find {operation.old_string === "" ? "(empty)" : <Code>{operation.old_string}</Code>}</Line>
      <Line>Replace with {operation.new_string === "" ? "(clear)" : <Code>{operation.new_string}</Code>}</Line>
    </Stack>
  );
}

function fieldValueTarget(
  args: z.infer<typeof fieldArguments>,
  names: ReadonlyMap<string, string> | undefined
): JSX.Element {
  return (
    <Line>
      {nodeLink(args.attributeId, names)} on {nodeLink(args.nodeId, names)}
    </Line>
  );
}

function PastePreview({ content }: { content: string }): JSX.Element {
  // Ordinary Tana Paste bullets use indentation for structure. Other grammar stays verbatim.
  const lines = content
    .replace(/^%%tana%%(?:\r?\n)?/, "")
    .trimEnd()
    .split(/\r?\n/);
  if (!lines.every((line) => /^[ \t]*-[ \t]/.test(line))) {
    return <TextBlock text={content} />;
  }
  return (
    <ClampedBlock maxHeightRem={24} lines={lines.length} label="Tana Paste tree">
      <Stack gap={2}>
        {lines.map((line, index) => {
          const indent = line.length - line.trimStart().length;
          return (
            <Text
              size="sm"
              key={index}
              style={{ paddingInlineStart: `${Math.min(indent, 16) / 2}rem`, overflowWrap: "anywhere" }}
            >
              • {line.trimStart().slice(2)}
            </Text>
          );
        })}
      </Stack>
    </ClampedBlock>
  );
}

/** Desktop 1.515.0 returns these text blocks and JSON shapes from the renderer Local API. */
function textResult(
  pattern: RegExp,
  view: (match: RegExpExecArray, context?: ResultPreviewContext) => ReactNode
): ResultPreview {
  return defineResultPreview(
    z.string().transform((result, ctx) => {
      const match = pattern.exec(result);
      if (match) return match;
      ctx.addIssue({ code: "custom", message: "Unrecognized Tana receipt" });
      return z.NEVER;
    }),
    ({ result, context }) => view(result, context)
  );
}

const readReceipt = textResult(
  /^[ \t]*(?:-[ \t]+)?(?:\[[ Xx]\][ \t]+)?(.*?)[ \t]*<!-- node-id: (\S+) -->[^\S\r\n]*(?:\r?\n([\s\S]*))?$/,
  ([, name, nodeId, rest]) => (
    <Stack gap="xs">
      <Line>
        <TanaNodeLink id={nodeId} name={name || "Open node"} />
      </Line>
      {rest && <Markdown source={rest} size="sm" />}
    </Stack>
  )
);

const tagSchemaReceipt = textResult(/^# Tag definition: (.*?) \(id:(\S+)\)\r?\n([\s\S]*)$/, ([, name, tagId, body]) => {
  const [schema, ...instructions] = body.split(/\r?\n## Tag-Level Edit Instructions\r?\n/);
  // The provider formats identifiers in the same line as each field and choice. Keep the row
  // label, type, defaults and choice text. Raw retains the identifiers and exact source Markdown.
  const fields = schema.replace(/^((?:Extends |[ \t]*- ).*)$/gm, (line) => line.replace(/ \(id:[^\s()]+\)/g, ""));
  return (
    <Stack gap="xs">
      <Line>
        <TanaNodeLink id={tagId} name={name || "Open tag"} />
      </Line>
      <Markdown source={fields} size="sm" />
      {instructions.length > 0 && (
        <details>
          <summary>Edit instructions</summary>
          <Markdown source={instructions.join("\n## Tag-Level Edit Instructions\n")} size="sm" />
        </details>
      )}
    </Stack>
  );
});

const childrenReceipt = defineResultPreview(
  z.strictObject({
    children: z.array(
      z.strictObject({
        id,
        name: z.string(),
        tags: z.array(z.strictObject({ id, name: z.string() })),
        tagIds: z.array(id),
        childCount: z.number().int().nonnegative(),
        docType: z.string(),
        description: z.string().optional(),
        created: z.string(),
        inTrash: z.boolean(),
      })
    ),
    total: z.number().int().nonnegative(),
    hasMore: z.boolean(),
  }),
  ({ result }) => (
    <Stack gap="xs">
      <Line>
        {result.children.length === 0
          ? result.total === 0
            ? "No child nodes"
            : "No child nodes on this page"
          : `${result.children.length} of ${result.total} child nodes`}
      </Line>
      {result.children.map((child, index) => (
        <Stack key={index} gap={2}>
          <Line>
            <TanaNodeLink id={child.id} name={child.name || "Unnamed node"} />
            {child.inTrash && " · In trash"}
          </Line>
          {child.description && <Line>{child.description}</Line>}
          {child.tags.length > 0 && (
            <Line>
              {child.tags.map((tag, tagIndex) => (
                <span key={tagIndex}>
                  {tagIndex > 0 && ", "}
                  <TanaNodeLink id={tag.id} name={tag.name} />
                </span>
              ))}
            </Line>
          )}
        </Stack>
      ))}
      {result.hasMore && (
        <Text size="xs" c="dimmed">
          More child nodes available
        </Text>
      )}
    </Stack>
  )
);

const workspacesReceipt = defineResultPreview(
  z.array(z.strictObject({ id, name: z.string(), homeNodeId: id })),
  ({ result }) => (
    <Stack gap={4}>
      <Line>
        {result.length === 0
          ? "No loaded workspaces"
          : `${result.length} loaded ${result.length === 1 ? "workspace" : "workspaces"}`}
      </Line>
      {result.map((workspace) => (
        <Line key={workspace.id}>
          <TanaNodeLink id={workspace.homeNodeId} name={workspace.name || "Unnamed workspace"} />
        </Line>
      ))}
    </Stack>
  )
);

const tagsReceipt = defineResultPreview(
  z.array(z.strictObject({ id, name: z.string(), color: z.string().optional() })),
  ({ result }) => (
    <Stack gap={4}>
      <Line>
        {result.length === 0 ? "No tags returned" : `${result.length} ${result.length === 1 ? "tag" : "tags"} returned`}
      </Line>
      {result.map((tag) => (
        <Line key={tag.id}>
          <TanaNodeLink id={tag.id} name={tag.name || "Unnamed tag"} />
        </Line>
      ))}
    </Stack>
  )
);

function openedNode(name: string, context?: ResultPreviewContext): ReactNode {
  const parsed = z.object({ nodeId: id }).safeParse(context?.arguments);
  return parsed.success ? <TanaNodeLink id={parsed.data.nodeId} name={name || "Unnamed node"} /> : name;
}

const openReceipt = textResult(/^Opened "([^\r\n]*)" in Tana \((current|panel|tab)\)$/, ([, name, place], context) => {
  const placement = openArguments.shape.openType.safeParse(place);
  return (
    <Line>
      Opened {openedNode(name, context)} {placement.success && openPlace[placement.data]}
    </Line>
  );
});

const editReceipt = textResult(
  /^Successfully edited node (\S+)\n(?:Name: ([^\r\n]*)(?:\n|$))?(?:Description: ([\s\S]*))?$/,
  ([, nodeId, name, description]) => (
    <Stack gap={4}>
      <Line>
        Edited <TanaNodeLink id={nodeId} name={name && name !== "(cleared)" ? name : "node"} />
      </Line>
      {name === "(cleared)" && <Line>Cleared name</Line>}
      {description !== undefined && (
        <Line>{description === "(cleared)" ? "Cleared description" : <>Description · {description}</>}</Line>
      )}
    </Stack>
  )
);

const trashReceipt = textResult(
  /^Moved "([^\r\n]*)" to trash\n\nNode ID: (\S+)\nTrash ID: (\S+)$/,
  ([, name, nodeId, trashId]) => (
    <Line>
      Moved <TanaNodeLink id={nodeId} name={name || "node"} /> to <TanaNodeLink id={trashId} name="trash" />
    </Line>
  )
);

const checkedReceipt = textResult(/^Checked "([^\r\n]*)"$/, ([, name], context) => (
  <Line>Completed {openedNode(name, context)}</Line>
));
const uncheckedReceipt = textResult(/^Unchecked "([^\r\n]*)"$/, ([, name], context) => (
  <Line>Reopened {openedNode(name, context)}</Line>
));

const taggingResult = z.string().transform((text, ctx) => {
  const [heading, ...lines] = text.split("\n");
  const match = /^(Adding|Removing) tags on "(.*)" \((\S+)\):$/.exec(heading);
  const entries = lines.map((line) => /^([✓✗]) (.+?): (.+)$/.exec(line));
  if (match && entries.length > 0 && entries.every((entry) => entry !== null)) {
    return {
      name: match[2],
      nodeId: match[3],
      entries: entries.map((entry) => ({ success: entry[1] === "✓", message: entry[3] })),
    };
  }
  ctx.addIssue({ code: "custom", message: "Unrecognized Tana tag receipt" });
  return z.NEVER;
});

const taggingReceipt = defineResultPreview(taggingResult, ({ result }) => (
  <Stack gap={4}>
    <Line>
      <TanaNodeLink id={result.nodeId} name={result.name || "Unnamed node"} />
    </Line>
    {result.entries.map((entry, index) => (
      <Line key={index}>
        {!entry.success && (
          <>
            <Badge color="red" size="xs">
              Failed
            </Badge>{" "}
          </>
        )}
        {entry.message}
      </Line>
    ))}
  </Stack>
));

const checkboxReceipt = textResult(
  /^✅ Configured checkbox for tag "(.*)"\nCheckbox: (enabled|disabled)(\nDone state mapping: configured)?$/,
  ([, name, state, mapping], context) => {
    const args = checkboxArguments.safeParse(context?.arguments);
    return (
      <Stack gap={4}>
        <Line>
          Checkbox {state} for {args.success ? <TanaNodeLink id={args.data.tagId} name={name || "tag"} /> : name}
        </Line>
        {mapping && <Line>Done state mapping configured</Line>}
      </Stack>
    );
  }
);

const createTagReceipt = textResult(
  /^✅ Created tag "([^\r\n]*)"(?: extending [^\r\n]+)?\nTag ID: (\S+)(?:\nExtends: ([^\r\n]+))?$/,
  ([, name, tagId, extendsNames]) => (
    <Stack gap={4}>
      <Line>
        Created tag <TanaNodeLink id={tagId} name={name || "Open tag"} />
      </Line>
      {extendsNames && <Line>Extends {extendsNames}</Line>}
    </Stack>
  )
);

const addFieldReceipt = textResult(
  /^✅ Added field "(.*)" to tag "(.*)"\nField ID: (\S+)\nData Type: (plain|number|date|url|email|checkbox|user|instance|options)$/,
  ([, name, tag, fieldId, type], context) => {
    const args = addFieldArguments.safeParse(context?.arguments);
    return (
      <Line>
        Added <TanaNodeLink id={fieldId} name={name || "field"} /> ({type}) to{" "}
        {args.success ? <TanaNodeLink id={args.data.tagId} name={tag || "tag"} /> : tag}
      </Line>
    );
  }
);

const fieldContentReceipt = textResult(
  /^(Field cleared|(Set|Appended) field content to "([\s\S]*)")$/,
  ([, message, verb, value]) =>
    verb === undefined ? (
      <Line>{message}</Line>
    ) : (
      <Line>
        {verb === "Set" ? "Set field to" : "Appended to field"} <Code>{value === "" ? "(empty text)" : value}</Code>
      </Line>
    )
);

const fieldOptionReceipt = textResult(
  /^(?:(Set|Appended) field to option "([\s\S]*)"|Option "([\s\S]*)" is already set on this field)$/,
  ([, verb, option, existing], context) => {
    const args = fieldOptionArguments.safeParse(context?.arguments);
    const name = option ?? existing;
    const link = args.success ? <TanaNodeLink id={args.data.optionId} name={name || "option"} /> : name;
    return verb === undefined ? (
      <Line>{link} was already set on this field</Line>
    ) : (
      <Line>
        {verb === "Set" ? "Set field to" : "Appended to field"} {link}
      </Line>
    );
  }
);

const importReceipt = textResult(
  /^Successfully imported (\d+) node\(s\) under "([^\r\n]*)"\n\nTarget container: (\S+)\nCreated nodes:\n((?:- \S+ \("[^\r\n]*"\)(?:\n|$))*)$/,
  ([, count, name, containerId, lines], context) => {
    const args = pasteArguments.safeParse(context?.arguments);
    const entries = [...lines.matchAll(/^- (\S+) \("(.*)"\)$/gm)];
    return (
      <Stack gap={4}>
        <Line>
          Imported {count} {count === "1" ? "node" : "nodes"} under{" "}
          {args.success ? <TanaNodeLink id={args.data.parentNodeId} name={name || "parent node"} /> : name}
        </Line>
        <Line>
          <TanaNodeLink id={containerId} name="Open import container" />
        </Line>
        {entries.map(([, id, title], index) => (
          <Line key={index}>
            <TanaNodeLink id={id} name={title || "Unnamed node"} />
          </Line>
        ))}
      </Stack>
    );
  }
);

export const tanaRenderers: ReadonlyMap<string, ActionPresentation> = new Map([
  ["get_or_create_calendar_node", calendarNodePresentation],
  ["move_node", moveNodePresentation],
  [
    "read_node",
    requestView(
      "read_node",
      readArguments,
      (args, names) => (
        <Line>
          {nodeLink(args.nodeId, names)} ·{" "}
          {args.maxDepth === 0
            ? "Only this node"
            : `Up to ${args.maxDepth} ${args.maxDepth === 1 ? "level" : "levels"} of descendants`}
        </Line>
      ),
      readReceipt
    ),
  ],
  [
    "get_children",
    requestView(
      "get_children",
      childrenArguments,
      (args, names) => (
        <Line>
          {nodeLink(args.nodeId, names)} · Up to {args.limit} direct children
          {args.offset > 0 && `, after the first ${args.offset}`}
        </Line>
      ),
      childrenReceipt
    ),
  ],
  [
    "open_node",
    requestView(
      "open_node",
      openArguments,
      (args, names) => (
        <Line>
          {nodeLink(args.nodeId, names)} {openPlace[args.openType]}
        </Line>
      ),
      openReceipt
    ),
  ],
  [
    "list_workspaces",
    {
      fallbackLabel: tanaTitles.get("list_workspaces"),
      pane: { opened: definePreview(noArguments, () => null) },
      details: { arguments: definePreview(noArguments, () => null), result: workspacesReceipt },
      compactCardExpands: expandsForRejectedArguments(noArguments),
    },
  ],
  [
    "list_tags",
    requestView(
      "list_tags",
      tagsArguments,
      (args, names) => (
        <Line>
          Up to {args.limit} tags in {workspaceName(args.workspaceId, names)} and linked workspaces
        </Line>
      ),
      tagsReceipt
    ),
  ],
  [
    "get_tag_schema",
    requestView(
      "get_tag_schema",
      tagSchemaArguments,
      (args, names) => (
        <Stack gap={4}>
          <Line>{nodeLink(args.tagId, names)}</Line>
          {args.includeInheritedFields && <Line>Include inherited fields</Line>}
          {args.includeEditInstructions && <Line>Include edit instructions</Line>}
        </Stack>
      ),
      tagSchemaReceipt,
      (args, names) => <Line>{nodeLink(args.tagId, names)}</Line>
    ),
  ],
  [
    "edit_node",
    requestView(
      "edit_node",
      editArguments,
      (args, names) => (
        <Stack gap="xs">
          <Line>{nodeLink(args.nodeId, names)}</Line>
          {args.name && changes("Name", args.name)}
          {args.description && changes("Description", args.description)}
        </Stack>
      ),
      editReceipt,
      (args, names) => (
        <Stack gap={4}>
          <Line>{nodeLink(args.nodeId, names)}</Line>
          {args.name && <Line>{args.name.replace_all ? "Replace all matches in the name" : "Edit name"}</Line>}
          {args.description && (
            <Line>
              {args.description.replace_all
                ? "Replace all matches in the description"
                : "Replace one match in the description"}
            </Line>
          )}
        </Stack>
      )
    ),
  ],
  [
    "trash_node",
    requestView(
      "trash_node",
      nodeArguments,
      (args, names) => <Line>{nodeLink(args.nodeId, names)} can be restored from the workspace trash.</Line>,
      trashReceipt
    ),
  ],
  ["check_node", checkPresentation("check_node", checkedReceipt)],
  ["uncheck_node", checkPresentation("uncheck_node", uncheckedReceipt)],
  [
    "tag",
    {
      ...requestView("tag", tagArguments, tagChange, taggingReceipt, (args, names) => tagChange(args, names, true)),
      liveCardDecision: { component: tagDecision, collapsed: true },
      outcome: (value) => {
        const parsed = taggingResult.safeParse(value);
        if (!parsed.success) return undefined;
        const failed = parsed.data.entries.filter((entry) => !entry.success).length;
        return failed === 0
          ? undefined
          : { label: failed === parsed.data.entries.length ? "Tags failed" : "Partially applied", failed: true };
      },
    },
  ],
  [
    "set_tag_checkbox",
    requestView(
      "set_tag_checkbox",
      checkboxArguments,
      (args, names) => {
        const mapping = args.doneStateMapping;
        return (
          <Stack gap={4}>
            <Line>
              {args.showCheckbox ? "Show" : "Hide"} the done checkbox for {nodeLink(args.tagId, names)}
            </Line>
            {mapping && (
              <>
                <Line>Map to {nodeLink(mapping.fieldId, names)}</Line>
                <Line>Checked · {nodeLinks(mapping.checkedValues, names)}</Line>
                {mapping.uncheckedValues && mapping.uncheckedValues.length > 0 && (
                  <Line>Unchecked · {nodeLinks(mapping.uncheckedValues, names)}</Line>
                )}
              </>
            )}
          </Stack>
        );
      },
      checkboxReceipt,
      (args, names) => (
        <Stack gap={4}>
          <Line>
            {args.showCheckbox ? "Show" : "Hide"} the done checkbox for {nodeLink(args.tagId, names)}
          </Line>
          {args.doneStateMapping && <Line>Map to {nodeLink(args.doneStateMapping.fieldId, names)}</Line>}
        </Stack>
      )
    ),
  ],
  [
    "create_tag",
    requestView(
      "create_tag",
      createTagArguments,
      (args, names) => (
        <Stack gap={4}>
          <Line>
            <strong>{args.name}</strong> in {workspaceName(args.workspaceId, names)}
          </Line>
          {args.description !== undefined && <Line>{args.description}</Line>}
          {args.extendsTagIds && args.extendsTagIds.length > 0 && (
            <Line>Extends {nodeLinks(args.extendsTagIds, names)}</Line>
          )}
          {args.showCheckbox && <Line>Show a done checkbox</Line>}
        </Stack>
      ),
      createTagReceipt,
      (args, names) => (
        <Stack gap={4}>
          <Line>
            <strong>{args.name}</strong> in {workspaceName(args.workspaceId, names)}
          </Line>
          {(!!args.extendsTagIds?.length || args.showCheckbox) && (
            <Line>
              {!!args.extendsTagIds?.length && <>Extends {nodeLinks(args.extendsTagIds, names)}</>}
              {!!args.extendsTagIds?.length && args.showCheckbox && " · "}
              {args.showCheckbox && "Show a done checkbox"}
            </Line>
          )}
        </Stack>
      )
    ),
  ],
  [
    "add_field_to_tag",
    requestView(
      "add_field_to_tag",
      addFieldArguments,
      (args, names) => (
        <Stack gap={4}>
          <Line>
            <strong>{args.name}</strong> ({args.dataType}) on {nodeLink(args.tagId, names)}
          </Line>
          {args.description !== undefined && <Line>{args.description}</Line>}
          {args.sourceTagId && <Line>References {nodeLink(args.sourceTagId, names)}</Line>}
          {args.options && <Line>Choices · {args.options.join(", ")}</Line>}
          {args.defaultValue !== undefined && (
            <Line>
              Default · <Code>{JSON.stringify(args.defaultValue)}</Code>
            </Line>
          )}
          {args.isMultiValue && <Line>Allows multiple values</Line>}
        </Stack>
      ),
      addFieldReceipt,
      (args, names) => (
        <Stack gap={4}>
          <Line>
            <strong>{args.name}</strong> ({args.dataType}) on {nodeLink(args.tagId, names)}
          </Line>
          {args.sourceTagId && <Line>References {nodeLink(args.sourceTagId, names)}</Line>}
          {!!args.options?.length && <Line>{args.options.length} choices</Line>}
        </Stack>
      )
    ),
  ],
  [
    "set_field_content",
    requestView(
      "set_field_content",
      fieldContentArguments,
      (args, names) => (
        <Stack gap={4}>
          {fieldValueTarget(args, names)}
          {args.content === null ? (
            <Line>Clear the field</Line>
          ) : (
            <Line>
              {args.mode === "append" ? "Append" : "Replace with"}{" "}
              <Code>{args.content === "" ? "(empty text)" : args.content}</Code>
            </Line>
          )}
        </Stack>
      ),
      fieldContentReceipt,
      (args, names) => (
        <Stack gap={4}>
          {fieldValueTarget(args, names)}
          <Line>
            {args.content === null
              ? "Clear the field"
              : args.mode === "append"
                ? "Append text"
                : "Replace the field with text"}
          </Line>
        </Stack>
      )
    ),
  ],
  [
    "set_field_option",
    requestView(
      "set_field_option",
      fieldOptionArguments,
      (args, names) => (
        <Stack gap={4}>
          {fieldValueTarget(args, names)}
          <Line>
            {args.mode === "append" ? "Append" : "Replace with"} {nodeLink(args.optionId, names)}
          </Line>
        </Stack>
      ),
      fieldOptionReceipt
    ),
  ],
  [
    "import_tana_paste",
    requestView(
      "import_tana_paste",
      pasteArguments,
      (args, names) => (
        <Stack gap={4}>
          <Line>Under {nodeLink(args.parentNodeId, names)}</Line>
          <PastePreview content={args.content} />
        </Stack>
      ),
      importReceipt,
      (args, names) => <Line>Import pasted content under {nodeLink(args.parentNodeId, names)}</Line>
    ),
  ],
]);
