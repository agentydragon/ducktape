import "@mantine/core/styles.css";

import { Button, MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";
import { type JSX, useState } from "react";

import { VirtualizedHistory, type HistoryItem } from "../virtualized_history";

import "./virtualized_history_scroll.css";

interface FixtureRow {
  readonly kind: "message" | "tool-group";
  readonly label: string;
  readonly anchorKey: string;
}

const MESSAGE_COUNT = 24;
const TOOL_STEP_COUNT = 6;
const toolStepKeys = Array.from({ length: TOOL_STEP_COUNT }, (_, index) => `tool-step:${index}`);
const toolGroupHeaderKey = "tool-group-header";

function Harness(): JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const rows: HistoryItem<FixtureRow>[] = [
    ...Array.from({ length: MESSAGE_COUNT }, (_, index) => ({
      key: `message:${index}`,
      revision: "stable",
      value: {
        kind: "message" as const,
        label: `Conversation message ${index + 1}`,
        anchorKey: `message-anchor:${index}`,
      },
      anchorKeys: [`message-anchor:${index}`],
    })),
    {
      key: "near-bottom-tool-group",
      revision: expanded ? "expanded" : "collapsed",
      value: {
        kind: "tool-group",
        label: "Tool group near the end of the conversation",
        anchorKey: toolGroupHeaderKey,
      },
      anchorKeys: [toolGroupHeaderKey, ...toolStepKeys],
      presentation: { kind: "tool-group", entityCount: TOOL_STEP_COUNT },
    },
  ];

  return (
    <MantineProvider defaultColorScheme="light">
      <main className="history-fixture">
        <div className="history-fixture-controls">
          <span>Shared virtualized conversation history</span>
        </div>
        <VirtualizedHistory
          scopeId="shared-scroll-regression"
          rows={rows}
          label="Conversation history"
          renderRow={(row) => {
            if (row.kind === "message") {
              return (
                <div className="history-fixture-message" data-history-anchor-key={row.anchorKey}>
                  {row.label}
                </div>
              );
            }
            return (
              <div className="history-fixture-tool-group">
                <Button
                  className="history-fixture-group-title"
                  variant="subtle"
                  aria-expanded={expanded}
                  aria-label={expanded ? "Collapse near-bottom tool group" : "Expand near-bottom tool group"}
                  data-history-anchor-key={row.anchorKey}
                  onClick={() => setExpanded((current) => !current)}
                >
                  {row.label}
                </Button>
                {expanded && (
                  <div className="history-fixture-tool-steps">
                    {toolStepKeys.map((key, index) => (
                      <section className="history-fixture-tool-step" data-history-anchor-key={key} key={key}>
                        <strong>Tool step {index + 1}</strong>
                        <p>Rendered output and nested activity remain part of this virtualized row.</p>
                        <code>tool.call({index + 1})</code>
                      </section>
                    ))}
                  </div>
                )}
              </div>
            );
          }}
          showJumpToLatest
        />
      </main>
    </MantineProvider>
  );
}

createRoot(document.getElementById("root")!).render(<Harness />);
