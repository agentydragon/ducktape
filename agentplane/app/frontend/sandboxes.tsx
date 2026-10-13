import {
  ActionIcon,
  Badge,
  Button,
  Group,
  Menu,
  MultiSelect,
  Select,
  Stack,
  Table,
  Text,
  Textarea,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
// Per-icon subpaths, never the barrel: see tabler_icons.d.ts.
import IconDotsVertical from "@tabler/icons-react/dist/esm/icons/IconDotsVertical.mjs";
import { type JSX, useEffect, useState } from "react";
import { useSearchParams } from "react-router";

import {
  api,
  displayableError,
  models,
  modelsForHarness,
  harnessOptions,
  type ModelCatalog,
  type NewSandbox,
  type SandboxPresetView,
  type SandboxView,
  type SessionDefaults,
} from "./client";
import { Disclosure } from "./disclosure";
import { ConfirmDelete, deletable, SuspendResume } from "./lifecycle";
import { liveSandboxesUrl, useLive, type SandboxesSnapshot } from "./live";
import { placeholderSandboxName, sandboxNameError } from "./sandbox_name";
import { podIp, sandboxStatusDetail, sandboxSummary } from "./sandbox_status";
import { SANDBOX_STATUS_MARKS } from "./status_mark";
import { TopbarTitle } from "./topbar";

const EMPTY_FORM: NewSandbox = {
  name: "",
  template: "",
  policy_sets: [],
  bootstrap: "",
};
const EMPTY_THREAD: SessionDefaults = {};
// The picked preset, in the URL like the sandbox page's tab, so a launch form can be linked to.
const PRESET_PARAM = "preset";

function hasSessionDefaults(defaults: SessionDefaults): boolean {
  return Object.values(defaults).some((value) => value !== undefined && value !== null && value !== "");
}

function StateBadge({ row }: { row: SandboxView }): JSX.Element {
  const summary = sandboxSummary(row);
  return (
    <Tooltip label={sandboxStatusDetail(row)} multiline style={{ whiteSpace: "pre-line" }} withArrow>
      <Badge color={SANDBOX_STATUS_MARKS[summary.kind].color}>{summary.label}</Badge>
    </Tooltip>
  );
}

export function SandboxList({ onOpen }: { onOpen: (name: string) => void }): JSX.Element {
  // The list is pushed; an action's own failure is what this holds.
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<NewSandbox>(EMPTY_FORM);
  const [presets, setPresets] = useState<SandboxPresetView[]>([]);
  const [selectedPreset, setSelectedPreset] = useState<string | null>(null);
  const [thread, setThread] = useState<SessionDefaults>(EMPTY_THREAD);
  const [modelCatalog, setModelCatalog] = useState<ModelCatalog | null>(null);
  const modelOptions = thread.harness && modelCatalog ? modelsForHarness(modelCatalog, thread.harness) : [];
  const reasoningEfforts = modelOptions.find((option) => option.model === thread.model)?.reasoning_efforts ?? [];
  const [templates, setTemplates] = useState<string[]>([]);
  // The PolicySets a new Sandbox may name; a preset pre-fills the pick and the operator edits it.
  const [policySets, setPolicySets] = useState<string[]>([]);
  // The sandbox whose deletion is being confirmed, by name; deleting takes its volume with it.
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);
  const live = useLive<SandboxesSnapshot>(liveSandboxesUrl(), "Sandboxes");
  const rows: SandboxView[] = live.snapshot?.sandboxes ?? [];
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedPreset = searchParams.get(PRESET_PARAM);
  // An empty Name field creates the Sandbox under this free name, derived from the preset's.
  const placeholderName = placeholderSandboxName(selectedPreset ?? "sandbox", new Set(rows.map((row) => row.name)));
  const nameError = sandboxNameError(form.name || placeholderName);

  /** Fill the form from a preset, or clear what one filled; every launch field stays editable. */
  function pickPreset(preset: SandboxPresetView | null): void {
    setSelectedPreset(preset?.name ?? null);
    if (!preset) {
      setForm((current) => ({
        ...current,
        template: "",
        policy_sets: [],
        bootstrap: "",
      }));
      setThread(EMPTY_THREAD);
      return;
    }
    setForm((current) => ({
      ...current,
      template: preset.template,
      policy_sets: preset.policy_sets,
      bootstrap: preset.bootstrap,
    }));
    setThread(preset.session_defaults);
  }

  // The URL names a preset the form has not taken yet: once the catalog is here, take it.
  useEffect(() => {
    if (requestedPreset === null || requestedPreset === selectedPreset) return;
    const preset = presets.find((candidate) => candidate.name === requestedPreset);
    if (preset) pickPreset(preset);
  }, [requestedPreset, selectedPreset, presets]);

  useEffect(() => {
    void (async () => {
      const { data: sandboxForm, error: formFailure } = await api.GET("/sandboxes/form");
      if (formFailure) setError(displayableError(formFailure));
      else setPolicySets(sandboxForm.policy_sets);
      const { data: presetViews } = await api.GET("/presets");
      setPresets(presetViews ?? []);
      const { data: templateNames, error: templateFailure } = await api.GET("/sandboxes/templates");
      if (templateFailure) setError(displayableError(templateFailure));
      else setTemplates(templateNames);
    })();
  }, []);

  useEffect(() => {
    void models().then(setModelCatalog, (reason: unknown) => setError(displayableError(reason)));
  }, []);

  useEffect(() => {
    if (!modelCatalog) return;
    setThread((current) => {
      if (!current.harness) return current;
      const available = harnessOptions(modelCatalog).filter((option) => !option.disabled);
      const harness = available.find((option) => option.value === current.harness)?.value ?? available[0]?.value;
      if (!harness) return current;
      const offered = modelsForHarness(modelCatalog, harness);
      if (harness === current.harness && current.model && offered.some((option) => option.model === current.model))
        return current;
      return { ...current, harness, model: offered[0]?.model ?? null, reasoning_effort: undefined };
    });
  }, [modelCatalog, thread.harness, thread.model]);

  // No refresh after an action: the change reaches the API server, and the watch behind the
  // stream brings the new row back on its own.
  async function act(name: string, action: "suspend" | "resume" | "delete"): Promise<void> {
    const params = { params: { path: { name } } };
    const { error: failure } =
      action === "delete"
        ? await api.DELETE("/sandboxes/{name}", params)
        : await api.POST(`/sandboxes/{name}/${action}`, params);
    setError(failure ? displayableError(failure) : null);
  }

  async function create(): Promise<void> {
    const body = {
      ...form,
      name: form.name || placeholderName,
      session_defaults: hasSessionDefaults(thread) ? thread : undefined,
    };
    const { data, error: failure } = await api.POST("/sandboxes", {
      body,
    });
    if (failure) setError(displayableError(failure));
    else {
      setForm(EMPTY_FORM);
      setSelectedPreset(null);
      setThread(EMPTY_THREAD);
      const params = new URLSearchParams(searchParams);
      params.delete(PRESET_PARAM);
      setSearchParams(params, { replace: true });
      setError(null);
      onOpen(data.name);
    }
  }

  return (
    <Stack>
      <TopbarTitle>
        <Title order={1} size="h4">
          Sandboxes
        </Title>
      </TopbarTitle>
      {confirmingDelete !== null && (
        <ConfirmDelete
          name={confirmingDelete}
          onCancel={() => setConfirmingDelete(null)}
          onConfirm={() => {
            void act(confirmingDelete, "delete");
            setConfirmingDelete(null);
          }}
        />
      )}
      {error && <Text c="red">{error}</Text>}
      <Group align="flex-end">
        <Select
          clearable
          label="Preset"
          description="Fills the template, policies and session defaults below"
          data={presets.map((preset) => ({ value: preset.name, label: preset.title }))}
          value={selectedPreset}
          onChange={(name) => {
            const preset = presets.find((candidate) => candidate.name === name) ?? null;
            pickPreset(preset);
            const params = new URLSearchParams(searchParams);
            if (preset) params.set(PRESET_PARAM, preset.name);
            else params.delete(PRESET_PARAM);
            setSearchParams(params, { replace: true });
          }}
          style={{ flex: "1 1 12rem" }}
        />
        <TextInput
          label="Name"
          value={form.name}
          placeholder={placeholderName}
          error={form.name ? nameError : null}
          onChange={(e) => setForm({ ...form, name: e.currentTarget.value })}
          style={{ flex: "1 1 10rem" }}
        />
        <Select
          label="Sandbox template"
          description="The pod or VM shape it runs on"
          searchable
          clearable
          data={templates}
          value={form.template || null}
          onChange={(template) => setForm({ ...form, template: template ?? "" })}
          placeholder={templates.length > 0 ? "Choose a template" : "Loading templates…"}
          style={{ flex: "1 1 14rem" }}
        />
        <MultiSelect
          label="Policy sets"
          description="What this sandbox may reach and do"
          hidePickedOptions
          data={policySets}
          value={form.policy_sets ?? []}
          onChange={(picked) => setForm({ ...form, policy_sets: picked })}
          style={{ flex: "1 1 18rem" }}
        />
        <Button
          onClick={() => void create()}
          disabled={
            nameError !== null ||
            !form.template ||
            Boolean(selectedPreset && (!thread.model || !modelOptions.some((option) => option.model === thread.model)))
          }
        >
          New sandbox
        </Button>
      </Group>
      <Textarea
        label="Bootstrap script"
        description="Runs once before this Sandbox's first session"
        autosize
        minRows={2}
        value={form.bootstrap}
        onChange={(event) => setForm({ ...form, bootstrap: event.currentTarget.value })}
      />
      <Stack gap="xs">
        <Text size="sm" fw={600}>
          Session launch defaults · optional
        </Text>
        <Text size="xs" c="dimmed">
          Used when this sandbox starts a runner session. A preset only fills these fields; each also works on its own.
        </Text>
        <Group align="flex-end">
          <Select
            label="Harness"
            allowDeselect={false}
            data={harnessOptions(modelCatalog)}
            value={thread.harness ?? null}
            onChange={(harness) =>
              setThread({ ...thread, harness: (harness ?? undefined) as SessionDefaults["harness"] })
            }
          />
          <Select
            label="Model"
            searchable
            allowDeselect={false}
            data={modelOptions.map((option) => ({ value: option.model, label: option.display_name }))}
            value={thread.model ?? null}
            onChange={(model) => {
              const efforts = modelOptions.find((option) => option.model === model)?.reasoning_efforts ?? [];
              setThread({
                ...thread,
                model,
                reasoning_effort: efforts.includes(thread.reasoning_effort ?? "") ? thread.reasoning_effort : undefined,
              });
            }}
            disabled={modelOptions.length === 0}
            placeholder={modelCatalog ? "No models available" : "Loading models…"}
            style={{ flex: "1 1 20rem" }}
          />
          {reasoningEfforts.length > 0 && (
            <Select
              label="Reasoning effort"
              data={reasoningEfforts}
              value={thread.reasoning_effort ?? null}
              onChange={(effort) => setThread({ ...thread, reasoning_effort: effort ?? undefined })}
            />
          )}
        </Group>
        <TextInput
          label="Working directory"
          value={thread.cwd ?? ""}
          onChange={(event) => setThread({ ...thread, cwd: event.currentTarget.value })}
        />
        <Disclosure summary="Thread setup script" keepMounted>
          <Textarea
            aria-label="Thread setup script"
            description="Runs once in each new Thread's working directory, before its harness starts"
            autosize
            minRows={2}
            value={thread.setup_script ?? ""}
            onChange={(event) => setThread({ ...thread, setup_script: event.currentTarget.value })}
          />
        </Disclosure>
        <Disclosure summary="Standing instructions" keepMounted>
          <Textarea
            aria-label="Standing instructions"
            description="Edits apply to future threads in this sandbox"
            autosize
            minRows={3}
            value={thread.instructions ?? ""}
            onChange={(event) => setThread({ ...thread, instructions: event.currentTarget.value })}
          />
        </Disclosure>
      </Stack>
      <Table>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Name</Table.Th>
            <Table.Th visibleFrom="sm">Status</Table.Th>
            <Table.Th visibleFrom="sm">Node</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((row) => {
            const ip = podIp(row);
            const node = `${row.pod?.node_name ?? "—"} ${ip ? `(${ip})` : ""}`;
            const state = <StateBadge row={row} />;
            return (
              <Table.Tr key={row.name}>
                <Table.Td>
                  <Button variant="subtle" px="xs" onClick={() => onOpen(row.name)}>
                    {row.name}
                  </Button>
                  {/* On a phone the other columns fold under the name, leaving room for the actions. */}
                  <Stack gap="xs" hiddenFrom="sm">
                    {state}
                    <Text size="xs" c="dimmed">
                      {node}
                    </Text>
                  </Stack>
                </Table.Td>
                <Table.Td visibleFrom="sm">{state}</Table.Td>
                <Table.Td visibleFrom="sm">{node}</Table.Td>
                <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}>
                  <Group gap="xs" wrap="nowrap" justify="flex-end">
                    <SuspendResume sandbox={row} onAct={(action) => void act(row.name, action)} />
                    <Menu position="bottom-end">
                      <Menu.Target>
                        <ActionIcon variant="subtle" aria-label={`More actions for ${row.name}`}>
                          <IconDotsVertical size={16} />
                        </ActionIcon>
                      </Menu.Target>
                      <Menu.Dropdown>
                        {/* The API refuses a running sandbox (inventory.py); suspend is one click left. */}
                        <Menu.Item color="red" disabled={!deletable(row)} onClick={() => setConfirmingDelete(row.name)}>
                          Delete
                        </Menu.Item>
                      </Menu.Dropdown>
                    </Menu>
                  </Group>
                </Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
      {live.snapshot === null && <Text c="dimmed">Waiting for the first update…</Text>}
    </Stack>
  );
}
