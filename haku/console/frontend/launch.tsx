import { useState } from "react";
import { Button, Group, Modal, Text } from "@mantine/core";

import { launchRoutine } from "./client.ts";

// Lifecycle as a closed union so the confirm/launching/done/error states can't be
// combined into something nonsensical.
type State =
  | { status: "idle" }
  | { status: "confirming" }
  | { status: "launching" }
  | { status: "launched" }
  | { status: "error"; message: string };

// Shell-owned launch control for the `launch-routine` capability. This button and its
// confirm copy live in the trusted bundle (never agent-authored), so a genuine operator
// gesture against trusted-rendered text is what fires the capability — agent UI can at
// most ask for it, never script or spoof it. The actual CSRF + server-side bearer live
// in the backend (see haku/console/capabilities.py).
export function LaunchRoutineButton() {
  const [state, setState] = useState<State>({ status: "idle" });
  const launching = state.status === "launching";

  function confirm() {
    setState({ status: "launching" });
    void launchRoutine()
      .then(() => setState({ status: "launched" }))
      .catch((e: unknown) => setState({ status: "error", message: e instanceof Error ? e.message : String(e) }));
  }

  return (
    <>
      <Group gap="sm">
        <Button
          variant="light"
          color="indigo"
          size="xs"
          loading={launching}
          onClick={() => setState({ status: "confirming" })}
        >
          {state.status === "launched" ? "Launched ✓" : "Launch Haku run"}
        </Button>
        {state.status === "error" && (
          <Text size="sm" c="red">
            Failed: {state.message}
          </Text>
        )}
      </Group>

      <Modal
        opened={state.status === "confirming" || launching}
        onClose={() => !launching && setState({ status: "idle" })}
        title="Launch a Haku run?"
        centered
      >
        <Text size="sm" mb="md">
          This starts a new Claude Code web session running Haku now.
        </Text>
        <Group justify="flex-end">
          <Button variant="default" disabled={launching} onClick={() => setState({ status: "idle" })}>
            Cancel
          </Button>
          <Button color="indigo" loading={launching} onClick={confirm}>
            Launch
          </Button>
        </Group>
      </Modal>
    </>
  );
}
