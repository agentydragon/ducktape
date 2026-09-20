import { Alert, Anchor, Button, Group, Modal, NumberInput, Select, Stack, Tabs, Text } from "@mantine/core";
import { useEffect, useRef, useState, type JSX } from "react";

import {
  api,
  fetchDefinitions,
  fetchModelMetadata,
  triggerValidationRuns,
  type DefinitionInfo,
} from "../lib/api/client";
import { formatDigest } from "../lib/formatters";
import { resolve } from "../lib/router";
import { toast } from "../lib/toast";
import type { ExampleKind, RunModalPrefill, Split } from "../lib/types";

type RunMode = "validation" | "optimize" | "improve";

interface Props {
  open: boolean;
  onClose: () => void;
  prefill?: RunModalPrefill;
}

export default function RunTriggerModal({ open, onClose, prefill }: Props): JSX.Element {
  const [mode, setMode] = useState<RunMode>("validation");
  const [loading, setLoading] = useState(false);
  const [loadingDefinitions, setLoadingDefinitions] = useState(true);
  const [loadingModels, setLoadingModels] = useState(true);
  const [definitions, setDefinitions] = useState<DefinitionInfo[]>([]);
  const [modelIds, setModelIds] = useState<string[]>([]);
  const [resultMessage, setResultMessage] = useState<string | null>(null);
  const [resultRunId, setResultRunId] = useState<string | null>(null);
  const [budgetUsd, setBudgetUsd] = useState<number>(5);
  const [timeoutSeconds, setTimeoutSeconds] = useState<number>(3600);
  const [criticModel, setCriticModel] = useState("gpt-5.1-codex-mini");
  const [selectedDefinition, setSelectedDefinition] = useState("");
  const [selectedSplit, setSelectedSplit] = useState<Split>("valid");
  const [selectedKind, setSelectedKind] = useState<ExampleKind>("whole_snapshot");
  const [nSamples, setNSamples] = useState<number>(5);
  const [optTargetMetric, setOptTargetMetric] = useState("whole-repo");
  const [optOptimizerModel, setOptOptimizerModel] = useState("gpt-5.1");
  const [impNExamples, setImpNExamples] = useState<number>(10);
  const [impImprovementModel, setImpImprovementModel] = useState("gpt-5.1");
  const dataFetched = useRef(false);
  const prefillApplied = useRef(false);

  useEffect(() => {
    if (!open || dataFetched.current) return;
    dataFetched.current = true;
    void Promise.all([fetchDefinitions("critic"), fetchModelMetadata()])
      .then(([definitionResult, modelResult]) => {
        setDefinitions(definitionResult.definitions);
        setSelectedDefinition((current) => current || definitionResult.definitions[0]?.image_digest || "");
        setModelIds(modelResult.models.map((model) => model.model_id));
      })
      .catch((error: unknown) => {
        toast.error(error instanceof Error ? error.message : "Failed to load data");
      })
      .finally(() => {
        setLoadingDefinitions(false);
        setLoadingModels(false);
      });
  }, [open]);

  useEffect(() => {
    if (open && prefill && !prefillApplied.current) {
      prefillApplied.current = true;
      if (prefill.definitionId) setSelectedDefinition(prefill.definitionId);
      if (prefill.split) setSelectedSplit(prefill.split);
      if (prefill.kind) setSelectedKind(prefill.kind);
    } else if (!open) {
      prefillApplied.current = false;
    }
  }, [open, prefill]);

  function clearResult(): void {
    setResultMessage(null);
    setResultRunId(null);
  }

  async function handleValidation(): Promise<void> {
    if (!selectedDefinition) return;
    setLoading(true);
    clearResult();
    try {
      const result = await triggerValidationRuns({
        image_digest: selectedDefinition,
        split: selectedSplit,
        example_kind: selectedKind,
        n_samples: nSamples,
        critic_model: criticModel,
        budget_usd: budgetUsd,
      });
      const message = `${result.message} (job ${result.job_id.slice(0, 8)})`;
      setResultMessage(message);
      toast.success(message);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to trigger validation runs");
    } finally {
      setLoading(false);
    }
  }

  async function handleOptimize(): Promise<void> {
    setLoading(true);
    clearResult();
    try {
      const { data, error } = await api.POST("/api/runs/optimize", {
        body: {
          target_metric: optTargetMetric as "whole-repo" | "targeted",
          budget_usd: budgetUsd,
          optimizer_model: optOptimizerModel,
          critic_model: criticModel,
          timeout_seconds: timeoutSeconds,
        },
      });
      if (error) throw new Error((error as { detail?: string }).detail ?? "Failed to launch optimize agent");
      setResultRunId(data.agent_run_id);
      setResultMessage("Optimize agent launched");
      toast.success("Optimize agent launched");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to launch optimize agent");
    } finally {
      setLoading(false);
    }
  }

  async function handleImprove(): Promise<void> {
    setLoading(true);
    clearResult();
    try {
      const { data, error } = await api.POST("/api/runs/improve", {
        body: {
          n_examples: impNExamples,
          budget_usd: budgetUsd,
          improvement_model: impImprovementModel,
          critic_model: criticModel,
          timeout_seconds: timeoutSeconds,
        },
      });
      if (error) throw new Error((error as { detail?: string }).detail ?? "Failed to launch improve agent");
      setResultRunId(data.agent_run_id);
      const message = `Improve agent launched on ${data.n_examples_selected} examples from ${formatDigest(data.definition_id)}`;
      setResultMessage(message);
      toast.success(message);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to launch improve agent");
    } finally {
      setLoading(false);
    }
  }

  async function handleTrigger(): Promise<void> {
    if (mode === "validation") await handleValidation();
    else if (mode === "optimize") await handleOptimize();
    else await handleImprove();
  }

  const modelSelectData = modelIds.map((id) => ({ value: id, label: id }));
  const disabled = loading || loadingModels;

  return (
    <Modal opened={open} onClose={onClose} title="Launch Agent" centered size="md">
      <Stack gap="md">
        <Group grow align="start">
          <NumberInput
            label="Budget ($)"
            min={0.01}
            step={0.5}
            value={budgetUsd}
            onChange={(value) => typeof value === "number" && setBudgetUsd(value)}
            disabled={loading}
          />
          <NumberInput
            label="Timeout (s)"
            min={60}
            step={300}
            value={timeoutSeconds}
            onChange={(value) => typeof value === "number" && setTimeoutSeconds(value)}
            disabled={loading}
          />
        </Group>

        <Select
          label="Critic Model"
          data={modelSelectData}
          value={criticModel}
          onChange={(value) => value && setCriticModel(value)}
          disabled={disabled}
        />

        <Tabs
          value={mode}
          onChange={(value) => {
            if (!value) return;
            setMode(value as RunMode);
            clearResult();
          }}
        >
          <Tabs.List grow>
            <Tabs.Tab value="validation">Validation</Tabs.Tab>
            <Tabs.Tab value="optimize">Optimize</Tabs.Tab>
            <Tabs.Tab value="improve">Improve</Tabs.Tab>
          </Tabs.List>
          <Tabs.Panel value="validation" pt="md">
            {loadingDefinitions ? (
              <Text c="dimmed">Loading definitions…</Text>
            ) : (
              <Stack gap="sm">
                <Select
                  label="Critic Definition"
                  data={definitions.map((definition) => ({
                    value: definition.image_digest,
                    label: formatDigest(definition.image_digest),
                  }))}
                  value={selectedDefinition || null}
                  onChange={(value) => setSelectedDefinition(value ?? "")}
                  disabled={loading}
                />
                <Group grow align="start">
                  <Select
                    label="Split"
                    data={[
                      { value: "train", label: "Train" },
                      { value: "valid", label: "Validation" },
                    ]}
                    value={selectedSplit}
                    onChange={(value) => value && setSelectedSplit(value as Split)}
                    disabled={loading}
                  />
                  <Select
                    label="Example Kind"
                    data={[
                      { value: "whole_snapshot", label: "Whole Snapshot" },
                      { value: "file_set", label: "File Set" },
                    ]}
                    value={selectedKind}
                    onChange={(value) => value && setSelectedKind(value as ExampleKind)}
                    disabled={loading}
                  />
                </Group>
                <NumberInput
                  label="Samples (1–50)"
                  min={1}
                  max={50}
                  value={nSamples}
                  onChange={(value) => typeof value === "number" && setNSamples(value)}
                  disabled={loading}
                />
              </Stack>
            )}
          </Tabs.Panel>
          <Tabs.Panel value="optimize" pt="md">
            <Stack gap="sm">
              <Select
                label="Target Metric"
                data={[
                  { value: "whole-repo", label: "Whole Repo (full-snapshot validation only)" },
                  { value: "targeted", label: "Targeted (includes per-file validation)" },
                ]}
                value={optTargetMetric}
                onChange={(value) => value && setOptTargetMetric(value)}
                disabled={loading}
              />
              <Select
                label="Optimizer Model"
                data={modelSelectData}
                value={optOptimizerModel}
                onChange={(value) => value && setOptOptimizerModel(value)}
                disabled={disabled}
              />
            </Stack>
          </Tabs.Panel>
          <Tabs.Panel value="improve" pt="md">
            <Stack gap="sm">
              <Text size="sm" c="dimmed">
                Auto-selects the best definition (by validation LCB) and top Pareto training examples.
              </Text>
              <Select
                label="Improvement Model"
                data={modelSelectData}
                value={impImprovementModel}
                onChange={(value) => value && setImpImprovementModel(value)}
                disabled={disabled}
              />
              <NumberInput
                label="Examples"
                min={1}
                max={100}
                value={impNExamples}
                onChange={(value) => typeof value === "number" && setImpNExamples(value)}
                disabled={loading}
              />
            </Stack>
          </Tabs.Panel>
        </Tabs>

        {resultMessage && (
          <Alert color="green" variant="light">
            {resultMessage}
            {resultRunId && (
              <>
                {" — "}
                <Anchor href={resolve(`/runs/${resultRunId}`)} onClick={onClose} fw={600}>
                  view run
                </Anchor>
              </>
            )}
          </Alert>
        )}

        <Group justify="flex-end">
          <Button variant="default" onClick={onClose} disabled={loading}>
            {resultMessage ? "Close" : "Cancel"}
          </Button>
          <Button
            onClick={() => void handleTrigger()}
            loading={loading}
            disabled={mode === "validation" && (!selectedDefinition || budgetUsd <= 0)}
          >
            {mode === "validation" ? "Run" : mode === "optimize" ? "Optimize" : "Improve"}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
