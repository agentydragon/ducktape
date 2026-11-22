import { Button, Stack, Text } from "@mantine/core";
import { useEffect, useRef, useState, type JSX } from "react";

import type { Dispatch } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { displayableError } from "../transport/client";
import { getDispatch } from "./client";

/** Render the original selected command without loading all authored payloads in a receipt page. */
export function StoredCommandDetails({ receipt }: { receipt: Dispatch }): JSX.Element {
  const [text, setText] = useState<string | undefined>();
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  async function read(): Promise<void> {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    try {
      const row = await getDispatch(receipt.sessionId, receipt.commandId, controller.signal);
      const command = row?.command;
      if (
        !command ||
        command.commandId !== receipt.commandId ||
        !receipt.sandboxUid ||
        row?.sandbox !== receipt.sandbox ||
        row.sandboxUid !== receipt.sandboxUid ||
        row.operation !== receipt.operation
      )
        throw new Error("The stored command is unavailable for this receipt.");
      const operation = command.operation;
      let value: string;
      switch (operation.case) {
        case "submitInput":
          value = operation.value.text;
          break;
        case "changeModel":
          value = operation.value.model;
          break;
        case "changeReasoningEffort":
          value = operation.value.effort;
          break;
        case "interruptTurn":
          value = operation.value.turnId;
          break;
        case "compact":
          value = "Compact";
          break;
        default:
          throw new Error("The stored command operation is unavailable.");
      }
      if (!controller.signal.aborted) setText(value);
    } catch (reason) {
      if (!controller.signal.aborted) setError(displayableError(reason));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }
  return (
    <Stack gap={2} align="flex-start">
      {text === undefined ? (
        <Button size="compact-xs" variant="subtle" loading={loading} onClick={() => void read()}>
          Show original command
        </Button>
      ) : (
        <Text size="sm" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {text}
        </Text>
      )}
      {error && (
        <Text role="status" size="xs" c="dimmed">
          {error}
        </Text>
      )}
    </Stack>
  );
}
