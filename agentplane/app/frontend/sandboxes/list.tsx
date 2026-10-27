import {
  ActionIcon,
  Button,
  Checkbox,
  Group,
  Menu,
  MultiSelect,
  Select,
  Stack,
  Table,
  TagsInput,
  Text,
  Textarea,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import { IconDotsVertical } from "../icons";
import { type JSX, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router";

import {
  api,
  displayableError,
  type NewSandbox,
  type NewSandboxDefaults,
  type SandboxPresetView,
  type SandboxView,
} from "../client";
import { IdleSuspendField, IdleSuspendInput } from "./idle_suspend";
import { ConfirmDelete, deletable, deleting, SuspendResume } from "./lifecycle";
import { liveSandboxesUrl, liveSchemas, useLive, type SandboxesSnapshot } from "../live";
import { MarkGlyph } from "../mark_glyph";
import { placeholderSandboxName, sandboxNameError } from "./sandbox_name";
import { podIp, sandboxStatusDetail, sandboxSummary } from "../sandbox_status";
import { SANDBOX_STATUS_MARKS } from "../status_mark";
import { TopbarTitle } from "../topbar";
import { WakeOnSendCheckbox } from "./wake_on_send";

const EMPTY_FORM: NewSandbox = {
  name: "",
  template: "",
  policy_sets: [],
  inbox_tags: [],
  bootstrap: "",
  wake_on_send: false,
};
// The picked preset, in the URL like the sandbox page's tab, so a launch form can be linked to.
const PRESET_PARAM = "preset";

function StatusIcon({ row }: { row: SandboxView }): JSX.Element {
  const summary = sandboxSummary(row);
  return (
    <Tooltip label={sandboxStatusDetail(row)} multiline style={{ whiteSpace: "pre-line" }} withArrow>
      <span aria-label={summary.label} data-status={summary.kind}>
        <MarkGlyph mark={SANDBOX_STATUS_MARKS[summary.kind]} size="small" />
      </span>
    </Tooltip>
  );
}

export function SandboxList({ onOpen }: { onOpen: (name: string) => void }): JSX.Element {
  // The list is pushed; an action's own failure is what this holds.
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<NewSandbox>(EMPTY_FORM);
  // The app's starting wake and idle suspension values; a preset leaves them alone.
  const [defaults, setDefaults] = useState<NewSandboxDefaults>({ wake_on_send: false });
  const [presets, setPresets] = useState<SandboxPresetView[]>([]);
  const [selectedPreset, setSelectedPreset] = useState<string | null>(null);
  const [templates, setTemplates] = useState<string[]>([]);
  // The PolicySets a new Sandbox may name; a preset pre-fills the pick and the operator edits it.
  const [policySets, setPolicySets] = useState<string[]>([]);
  // The sandbox whose deletion is being confirmed, by name; deleting takes its volume with it.
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);
  const live = useLive<SandboxesSnapshot>(liveSandboxesUrl(), "Sandboxes", liveSchemas.sandboxes);
  const rows: SandboxView[] = useMemo(() => live.snapshot?.sandboxes ?? [], [live.snapshot?.sandboxes]);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedPreset = searchParams.get(PRESET_PARAM);
  // An empty Name field creates the Sandbox under this free name, derived from the preset's.
  const placeholderName = placeholderSandboxName(selectedPreset ?? "sandbox", new Set(rows.map((row) => row.name)));
  const nameError = sandboxNameError(form.name || placeholderName);

  /** Fill the Sandbox creation fields from a preset; session defaults remain app configuration. */
  function pickPreset(preset: SandboxPresetView | null): void {
    setSelectedPreset(preset?.name ?? null);
    if (!preset) {
      setForm((current) => ({
        ...current,
        template: "",
        policy_sets: [],
        inbox_tags: [],
        bootstrap: "",
      }));
      return;
    }
    setForm((current) => ({
      ...current,
      template: preset.template,
      policy_sets: preset.policy_sets,
      inbox_tags: preset.inbox_tags,
      bootstrap: preset.bootstrap,
    }));
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
      else {
        setPolicySets(sandboxForm.policy_sets);
        setDefaults(sandboxForm.new_sandbox_defaults);
        setForm((current) => ({ ...current, ...sandboxForm.new_sandbox_defaults }));
      }
      const { data: presetViews } = await api.GET("/presets");
      setPresets(presetViews ?? []);
      const { data: templateNames, error: templateFailure } = await api.GET("/sandboxes/templates");
      if (templateFailure) setError(displayableError(templateFailure));
      else setTemplates(templateNames);
    })();
  }, []);

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
      preset: selectedPreset ?? undefined,
    };
    const { data, error: failure } = await api.POST("/sandboxes", {
      body,
    });
    if (failure) setError(displayableError(failure));
    else {
      setForm({ ...EMPTY_FORM, ...defaults });
      setSelectedPreset(null);
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
          description="Fills the template, policy sets, inbox tags, and bootstrap below; session defaults stay in app configuration"
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
      </Group>
      <MultiSelect
        label="Policy sets"
        description="What this sandbox may reach and do"
        hidePickedOptions
        data={policySets}
        value={form.policy_sets ?? []}
        onChange={(picked) => setForm({ ...form, policy_sets: picked })}
        w="100%"
      />
      <TagsInput
        label="Inbox tags"
        description="Groups a send grant can name this sandbox by"
        value={form.inbox_tags ?? []}
        onChange={(tags) => setForm({ ...form, inbox_tags: tags })}
        maxLength={63}
        w="100%"
      />
      <Group align="flex-end" gap="sm">
        <Checkbox
          label="Wake on message"
          checked={form.wake_on_send ?? false}
          onChange={(event) => setForm({ ...form, wake_on_send: event.currentTarget.checked })}
        />
        <IdleSuspendField
          seconds={form.idle_suspend_after_seconds}
          onChange={(seconds) => setForm({ ...form, idle_suspend_after_seconds: seconds })}
        />
        <Button onClick={() => void create()} disabled={nameError !== null || !form.template}>
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
      <Table>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Status</Table.Th>
            <Table.Th>Name</Table.Th>
            <Table.Th visibleFrom="sm">Node</Table.Th>
            <Table.Th visibleFrom="sm">Wake on message</Table.Th>
            <Table.Th visibleFrom="sm">Suspend when idle (min)</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((row) => {
            const ip = podIp(row);
            const node = `${row.pod?.node_name ?? "—"} ${ip ? `(${ip})` : ""}`;
            return (
              <Table.Tr key={row.name}>
                <Table.Td style={{ width: "1%", whiteSpace: "nowrap" }}>
                  <StatusIcon row={row} />
                </Table.Td>
                <Table.Td>
                  <Button variant="subtle" px="xs" onClick={() => onOpen(row.name)}>
                    {row.name}
                  </Button>
                  {/* On a phone the other columns fold under the name, leaving room for the actions. */}
                  <Stack gap="xs" hiddenFrom="sm">
                    <Text size="xs" c="dimmed">
                      {node}
                    </Text>
                    <WakeOnSendCheckbox
                      sandbox={row}
                      disabled={deleting(row)}
                      label="Wake on message"
                      onError={setError}
                    />
                    <Group gap="xs" wrap="nowrap">
                      <Text size="sm">Suspend when idle (min)</Text>
                      <IdleSuspendInput sandbox={row} disabled={deleting(row)} onError={setError} />
                    </Group>
                  </Stack>
                </Table.Td>
                <Table.Td visibleFrom="sm">{node}</Table.Td>
                <Table.Td visibleFrom="sm">
                  <WakeOnSendCheckbox sandbox={row} disabled={deleting(row)} onError={setError} />
                </Table.Td>
                <Table.Td visibleFrom="sm">
                  <IdleSuspendInput sandbox={row} disabled={deleting(row)} onError={setError} />
                </Table.Td>
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
