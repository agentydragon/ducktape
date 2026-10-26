import { Button, Checkbox, Group, Paper, Select, Stack, Textarea, TextInput } from "@mantine/core";
import { type JSX, useEffect, useEffectEvent, useMemo, useState } from "react";

import { fromJson, type JsonValue } from "@bufbuild/protobuf";

import {
  api,
  displayableError,
  harnessOptions,
  modelsForHarness,
  type Harness,
  type ModelCatalog,
  type SessionDefaults,
} from "../client";
import { Disclosure, DisclosureBody } from "../disclosure";
import { SessionSpecSchema, type SessionSpec } from "../../../proto/runner_pb";

type LoadedPresetDefaults = { preset: string | null; defaults: Partial<SessionDefaults> | null };

function isHarness(value: string): value is Harness {
  return value === "HARNESS_CLAUDE" || value === "HARNESS_CODEX";
}

/**
 * What a new Session is launched with. It starts from the Sandbox's preset and the operator edits it
 * for this one Session, so its state outlives the panel it is shown in being closed.
 */
export function NewSessionForm({
  presetName,
  startable,
  creating,
  onStart,
  onClose,
  onError,
}: {
  presetName: string | null;
  /** Whether the Sandbox can take a Session now and no earlier Open is still unconfirmed. */
  startable: boolean;
  creating: boolean;
  onStart: (spec: SessionSpec, setupScript: string, autoTitleOnFirstMessage: boolean) => void;
  onClose: () => void;
  onError: (message: string) => void;
}): JSX.Element {
  const [effort, setEffort] = useState("low");
  const [instructions, setInstructions] = useState("");
  const [cwdTemplate, setCwdTemplate] = useState("/state/workspaces/{session_id}");
  const [setupScript, setSetupScript] = useState("");
  const [autoTitleOnFirstMessage, setAutoTitleOnFirstMessage] = useState(false);
  const [titleGenerationAvailable, setTitleGenerationAvailable] = useState(false);
  const [titleSettingsLoaded, setTitleSettingsLoaded] = useState(false);
  const [inheritsDefaults, setInheritsDefaults] = useState(false);
  const [presetDefaults, setPresetDefaults] = useState<LoadedPresetDefaults | null>(null);
  // Launch-form offerings; existing threads retain their harness and model.
  const [harness, setHarness] = useState<Harness>("HARNESS_CLAUDE");
  const [modelCatalog, setModelCatalog] = useState<ModelCatalog | null>(null);
  const models = useMemo(() => (modelCatalog ? modelsForHarness(modelCatalog, harness) : []), [modelCatalog, harness]);
  const [model, setModel] = useState<string | null>(null);
  const reportError = useEffectEvent(onError);

  useEffect(() => {
    void (async () => {
      const { data, error: failure } = await api.GET("/models");
      if (failure) {
        reportError(displayableError(failure));
        return;
      }
      if (data) setModelCatalog(data);
    })();
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const { data, error: failure } = await api.GET("/threads/form");
        if (cancelled) return;
        if (failure) {
          reportError(displayableError(failure));
          return;
        }
        if (data) {
          setTitleGenerationAvailable(data.title_generation_available);
          setAutoTitleOnFirstMessage(data.auto_title_on_first_message_default);
        }
      } catch (reason: unknown) {
        if (!cancelled) reportError(displayableError(reason));
      } finally {
        if (!cancelled) setTitleSettingsLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (presetName === null) {
      setPresetDefaults({ preset: null, defaults: {} });
      return () => {
        cancelled = true;
      };
    }
    setPresetDefaults(null);
    void (async () => {
      const { data, error: failure } = await api.GET("/presets");
      if (cancelled) return;
      if (failure) {
        reportError(displayableError(failure));
        return;
      }
      const preset = data?.find((candidate) => candidate.name === presetName);
      if (!preset) {
        reportError(`Sandbox preset ${presetName} is missing from the app configuration.`);
        setPresetDefaults({ preset: presetName, defaults: null });
        return;
      }
      setPresetDefaults({ preset: presetName, defaults: preset.session_defaults });
    })();
    return () => {
      cancelled = true;
    };
  }, [presetName]);

  useEffect(() => {
    const offered = harnessOptions(modelCatalog).filter((option) => !option.disabled);
    if (offered.length && !offered.some((option) => option.value === harness)) {
      setHarness(offered[0].value);
    }
    if (modelCatalog) {
      setModel((current) =>
        current && models.some((option) => option.model === current) ? current : (models[0]?.model ?? null)
      );
    }
  }, [modelCatalog, harness, models]);

  useEffect(() => {
    const option = models.find((entry) => entry.model === model);
    // A Sandbox binding can arrive before the model catalog; do not erase its default
    // while the selected model is not yet known to this catalog.
    const reasoningEfforts = option?.reasoning_efforts ?? [];
    if (option && !reasoningEfforts.includes(effort)) setEffort(reasoningEfforts[0] ?? "");
  }, [effort, model, models]);

  const applyPresetDefaults = useEffectEvent(() => {
    if (!presetDefaults || presetDefaults.preset !== presetName || presetDefaults.defaults === null) return;
    const defaults = presetDefaults.defaults;
    const defaultHarness = defaults.harness ?? "";
    setInheritsDefaults(presetName !== null);
    setHarness(isHarness(defaultHarness) ? defaultHarness : "HARNESS_CLAUDE");
    setModel(defaults.model ?? null);
    setEffort(defaults.reasoning_effort ?? "low");
    setInstructions(defaults.instructions ?? "");
    setCwdTemplate(defaults.cwd ?? "/state/workspaces/{session_id}");
    setSetupScript(defaults.setup_script ?? "");
  });
  useEffect(() => applyPresetDefaults(), [presetName, presetDefaults]);

  const efforts = models.find((option) => option.model === model)?.reasoning_efforts ?? [];

  return (
    <Paper withBorder p="md" radius="md" role="group" aria-label="New session">
      <Stack>
        <Group align="flex-end">
          <Select
            label="Harness"
            data={harnessOptions(modelCatalog)}
            value={harness}
            onChange={(value) => value && setHarness(value as Harness)}
          />
          <Select
            label="Model"
            data={models.map((option) => ({ value: option.model, label: option.display_name }))}
            value={model}
            onChange={setModel}
          />
          {efforts.length > 0 && (
            <Select label="Reasoning effort" data={efforts} value={effort} onChange={(v) => v && setEffort(v)} />
          )}
        </Group>
        <Disclosure summary="Standing instructions" keepMounted>
          <DisclosureBody>
            <Textarea
              aria-label="Standing instructions"
              description={
                inheritsDefaults ? "From this Sandbox's configured preset; editable for this thread" : undefined
              }
              autosize
              minRows={2}
              value={instructions}
              onChange={(event) => setInstructions(event.currentTarget.value)}
            />
          </DisclosureBody>
        </Disclosure>
        <TextInput
          label="Working directory"
          description="{session_id} is replaced with this Thread's ID"
          value={cwdTemplate}
          onChange={(event) => setCwdTemplate(event.currentTarget.value)}
        />
        <Disclosure summary="Thread setup script" keepMounted>
          <DisclosureBody>
            <Textarea
              aria-label="Thread setup script"
              description="Runs once in the resolved working directory before the harness starts"
              autosize
              minRows={2}
              value={setupScript}
              onChange={(event) => setSetupScript(event.currentTarget.value)}
            />
          </DisclosureBody>
        </Disclosure>
        {titleGenerationAvailable && (
          <Checkbox
            label="Auto-title after the first message"
            checked={autoTitleOnFirstMessage}
            onChange={(event) => setAutoTitleOnFirstMessage(event.currentTarget.checked)}
          />
        )}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Close
          </Button>
          <Button
            loading={creating}
            disabled={!startable || !model || !titleSettingsLoaded}
            onClick={() =>
              model &&
              onStart(
                fromJson(SessionSpecSchema, {
                  harness,
                  cwd: cwdTemplate, // The app resolves {session_id} before it opens the Session.
                  model,
                  reasoningEffort: effort,
                  instructions,
                } as JsonValue),
                setupScript,
                autoTitleOnFirstMessage
              )
            }
          >
            {creating ? "Creating session…" : "Start session"}
          </Button>
        </Group>
      </Stack>
    </Paper>
  );
}
