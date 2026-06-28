import { useEffect, useState } from "react";
import { Group, Loader, Text, Title } from "@mantine/core";

import { LOGO_URL } from "./assets.ts";
import { type ConfigResponse, fetchConfig } from "./client.ts";
import { FeedbackFab } from "./feedback.tsx";
import { HakuUiEmbed } from "./haku_ui_embed.tsx";
import { LaunchRoutineButton } from "./launch.tsx";
import { toastError } from "./toast.ts";

export default function App() {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchConfig()
      .then((c) => {
        if (alive) setConfig(c);
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  // Error reporting standard: action failures (launch, trace) surface as toasts
  // (see toast.ts). The initial config load is the one exception — a failure leaves
  // nothing to render, so it gets a persistent page-level message.
  if (error)
    return (
      <Text c="red" className="mx-auto max-w-3xl p-4">
        Failed to load: {error}
      </Text>
    );
  if (!config)
    return (
      <div className="flex justify-center p-8">
        <Loader />
      </div>
    );

  return (
    <>
      <div className="mx-auto max-w-3xl px-4 py-8">
        <Group justify="space-between" align="center" mb="xs">
          <Group gap="sm" align="center">
            <img src={LOGO_URL} alt="" aria-hidden="true" className="h-10 w-10 shrink-0" />
            <Title order={1}>Haku</Title>
          </Group>
          <LaunchRoutineButton routineUrl={config.launch_routine_url} />
        </Group>
        {/* The Free-form UI is the main surface; the note-to-haku form opens from the corner button. */}
        {config.haku_ui_url && <HakuUiEmbed uiUrl={config.haku_ui_url} />}
      </div>

      {/* Viewport-pinned (not inside the centered content column); see FeedbackFab. */}
      <FeedbackFab />
    </>
  );
}
