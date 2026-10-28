import { Button, Group, Paper, Select, Stack, Textarea, TextInput, Tooltip } from "@mantine/core";
import { type JSX, useEffect, useEffectEvent, useMemo, useState } from "react";

import { fromJson, type JsonValue } from "@bufbuild/protobuf";

import type { Harness, ModelCatalog, SessionDefaults } from "../api/schema";
import { api, displayableError } from "../client";
import { harnessOptions, modelsForHarness } from "../model_catalog";
import { Disclosure, DisclosureBody } from "../disclosure";
import { SessionSpecSchema, type SessionSpec } from "../../../proto/runner_pb";

type LoadedPresetDefaults = { preset: string | null; defaults: SessionDefaults | null };

/**
 * What a new Thread is created with. It starts from the Sandbox's preset and the operator edits it
 * for this one Thread, so its state outlives the panel it is shown in being closed.
 */
export function NewThreadForm({
  presetName,
  unavailableReason,
  creating,
  onCreate,
  onClose,
  onError,
}: {
  presetName: string | null;
  unavailableReason: string | null;
  creating: boolean;
  onCreate: (spec: SessionSpec, setupScript: string) => void;
  onClose: () => void;
  onError: (message: string) => void;
}): JSX.Element {
  const [effort, setEffort] = useState("low");
  const [instructions, setInstructions] = useState("");
  const [cwdTemplate, setCwdTemplate] = useState("/state/workspaces/{session_id}");
  const [setupScript, setSetupScript] = useState("");
  const [inheritsDefaults, setInheritsDefaults] = useState(false);
  const [presetDefaults, setPresetDefaults] = useState<LoadedPresetDefaults | null>(null);
  // Create-form offerings; existing threads retain their harness and model.
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
    if (option && !option.reasoning_efforts.includes(effort)) setEffort(option.reasoning_efforts[0] ?? "");
  }, [effort, model, models]);

  const applyPresetDefaults = useEffectEvent(() => {
    if (!presetDefaults || presetDefaults.preset !== presetName || presetDefaults.defaults === null) return;
    const defaults = presetDefaults.defaults;
    setInheritsDefaults(presetName !== null);
    setHarness(defaults.harness ?? "HARNESS_CLAUDE");
    setModel(defaults.model ?? null);
    setEffort(defaults.reasoning_effort ?? "low");
    setInstructions(defaults.instructions ?? "");
    setCwdTemplate(defaults.cwd ?? "/state/workspaces/{session_id}");
    setSetupScript(defaults.setup_script ?? "");
  });
  useEffect(() => applyPresetDefaults(), [presetName, presetDefaults]);

  const efforts = models.find((option) => option.model === model)?.reasoning_efforts ?? [];
  const disabledReason = creating
    ? "A thread is being created. Wait for this attempt to finish."
    : (unavailableReason ??
      (!model
        ? modelCatalog === null
          ? "Waiting for the model catalog to load."
          : models.length === 0
            ? "No models are available for the selected harness."
            : "Select a model before creating a thread."
        : null));

  return (
    <Paper withBorder p="md" radius="md" role="group" aria-label="New thread">
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
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Close
          </Button>
          <Tooltip
            label={disabledReason}
            disabled={disabledReason === null}
            events={{ hover: true, focus: true, touch: true }}
            multiline
            maw="calc(100vw - 32px)"
            w={340}
            style={{ whiteSpace: "pre-line" }}
            withArrow
          >
            {/* Disabled buttons do not receive pointer events; the wrapper owns hover and focus. */}
            <span style={{ display: "inline-flex" }} tabIndex={disabledReason ? 0 : undefined}>
              <Button
                loading={creating}
                disabled={disabledReason !== null}
                onClick={() =>
                  model &&
                  onCreate(
                    fromJson(SessionSpecSchema, {
                      harness,
                      cwd: cwdTemplate, // The app resolves {session_id} before it opens the Session.
                      model,
                      reasoningEffort: effort,
                      instructions,
                    } as JsonValue),
                    setupScript
                  )
                }
              >
                {creating ? "Creating thread…" : "Create thread"}
              </Button>
            </span>
          </Tooltip>
        </Group>
      </Stack>
    </Paper>
  );
}
