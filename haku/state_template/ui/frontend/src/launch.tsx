import { Anchor, Button, Group, Modal, Text, Textarea } from "@mantine/core";
import { useState } from "react";

import { requestLaunch } from "./bridge.ts";

// Title-bar control for starting a Haku run. This is agent-authored UI, so it can only
// *ask*: clicking Launch posts `requestLaunch` to the trusted shell, which shows its OWN
// confirm (rendering the prompt for the operator to review) before firing the capability.
// We render the prompt dialog; the shell owns the authority. The result (a session link,
// or a cancellation/failure reason) comes back over the bridge.
export function LaunchButton() {
  const [opened, setOpened] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; sessionUrl?: string; reason?: string } | null>(null);

  function fire() {
    setBusy(true);
    setResult(null);
    void requestLaunch(prompt.trim()).then((r) => {
      setBusy(false);
      setResult({ ok: r.ok, sessionUrl: r.sessionUrl, reason: r.reason });
      if (r.ok) setPrompt("");
    });
  }

  function close() {
    if (busy) return; // don't let the operator dismiss a launch mid-flight
    setOpened(false);
    setResult(null);
  }

  return (
    <>
      <Button
        variant="light"
        size="xs"
        onClick={() => {
          setResult(null);
          setOpened(true);
        }}
      >
        Launch run
      </Button>

      <Modal opened={opened} onClose={close} title="Launch a Haku run?" centered>
        <Text size="sm" mb="md">
          This asks the console to start a new Claude Code web session running Haku now. You'll confirm once more in the
          console before it starts.
        </Text>
        <Textarea
          label="Custom prompt"
          placeholder="Optional instructions for this run"
          autosize
          minRows={4}
          mb="md"
          value={prompt}
          onChange={(event) => setPrompt(event.currentTarget.value)}
          disabled={busy}
        />
        {result?.ok && (
          <Text size="sm" c="green" mb="md">
            Launched ✓{" "}
            {result.sessionUrl && (
              <Anchor href={result.sessionUrl} target="_blank" rel="noreferrer">
                Open session
              </Anchor>
            )}
          </Text>
        )}
        {result && !result.ok && (
          <Text size="sm" c="red" mb="md">
            {result.reason ?? "Launch was not confirmed."}
          </Text>
        )}
        <Group justify="flex-end">
          <Button variant="default" disabled={busy} onClick={close}>
            {result?.ok ? "Close" : "Cancel"}
          </Button>
          {!result?.ok && (
            <Button loading={busy} onClick={fire}>
              Launch
            </Button>
          )}
        </Group>
      </Modal>
    </>
  );
}
