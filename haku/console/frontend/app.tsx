import { useEffect, useState } from "react";
import { Anchor, Group, Loader, Text, Title } from "@mantine/core";

import { type ConfigResponse, fetchConfig } from "./client.ts";
import { INTAKE_NEW } from "./constants.ts";
import { FeedbackForm } from "./feedback.tsx";
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
    <div className="mx-auto max-w-3xl px-4 py-8">
      <Group justify="space-between" align="center" mb="xs">
        <Title order={1}>Haku</Title>
        <LaunchRoutineButton routineUrl={config.launch_routine_url} />
      </Group>
      <Text c="dimmed" mb="xl">
        <Anchor href={INTAKE_NEW} c="dimmed" underline="always">
          + Add intake note
        </Anchor>
      </Text>

      <section>
        <Title order={2} mb="sm">
          Note to Haku
        </Title>
        <FeedbackForm
          minRows={3}
          placeholder="Anything for Haku to fold into its next run…"
          submitLabel="Send to Haku"
        />
      </section>

      {config.haku_ui_url && (
        <section className="mt-10">
          <Title order={2} mb="sm">
            Free-form UI
          </Title>
          <HakuUiEmbed uiUrl={config.haku_ui_url} />
        </section>
      )}
    </div>
  );
}
