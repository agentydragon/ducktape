import { useEffect, useMemo, useRef } from "react";
import { Chart, Tooltip, LinearScale, type ChartDataset, type ScriptableContext, type TooltipItem } from "chart.js";
import { MatrixController, MatrixElement } from "chartjs-chart-matrix";
import { Group, Paper, Text } from "@mantine/core";
import { formatDigest } from "../../lib/formatters";

Chart.register(MatrixController, MatrixElement, Tooltip, LinearScale);

interface Definition {
  image_digest: string;
  best_on_count: number;
  evaluated_on_count: number;
}

interface Example {
  snapshot_slug: string;
  example_kind: string;
  files_hash: string | null;
  max_recall: number;
  tp_count: number;
}

interface Cell {
  definition_idx: number;
  example_idx: number;
  recall: number;
  is_best: boolean;
}

interface Props {
  definitions: Definition[];
  examples: Example[];
  cells: Cell[];
}

// Matrix points carry a custom best field beyond chartjs-chart-matrix's data type.
type MatrixPoint = { x: number; y: number; v: number; best: boolean };

export default function CoverageHeatmap({ definitions, examples, cells }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const definitionsRef = useRef(definitions);
  const examplesRef = useRef(examples);
  definitionsRef.current = definitions;
  examplesRef.current = examples;

  const matrixData = useMemo(
    () => cells.map((cell) => ({ x: cell.example_idx, y: cell.definition_idx, v: cell.recall, best: cell.is_best })),
    [cells]
  );
  const matrixDataRef = useRef(matrixData);
  matrixDataRef.current = matrixData;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const currentDefinitions = definitionsRef.current;
    const currentExamples = examplesRef.current;
    const chart = new Chart(canvas, {
      type: "matrix",
      data: {
        datasets: [
          {
            data: matrixDataRef.current as unknown as ChartDataset["data"],
            backgroundColor(context: ScriptableContext<"matrix">) {
              const point = context.dataset.data[context.dataIndex] as unknown as MatrixPoint;
              if (!point) return "rgba(243, 244, 246, 1)";
              if (point.best) {
                const alpha = 0.3 + point.v * 0.7;
                return `rgba(22, 163, 74, ${alpha})`;
              }
              return `rgba(209, 213, 219, ${0.3 + point.v * 0.5})`;
            },
            width: ({ chart: currentChart }: { chart: Chart }) => {
              const xScale = currentChart.scales.x;
              return Math.max(xScale.width / (examplesRef.current.length + 1) - 1, 4);
            },
            height: ({ chart: currentChart }: { chart: Chart }) => {
              const yScale = currentChart.scales.y;
              return Math.max(yScale.height / (definitionsRef.current.length + 1) - 1, 12);
            },
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          tooltip: {
            callbacks: {
              title: () => "",
              label(context: TooltipItem<"matrix">) {
                const point = context.dataset.data[context.dataIndex] as unknown as MatrixPoint;
                const def = definitionsRef.current[point.y];
                const example = examplesRef.current[point.x];
                const slug =
                  example.snapshot_slug.length > 20 ? example.snapshot_slug.slice(0, 20) + "…" : example.snapshot_slug;
                return [
                  `Def: ${formatDigest(def.image_digest)}`,
                  `Example: ${slug}`,
                  `Recall: ${(point.v * 100).toFixed(1)}%`,
                  point.best ? "★ Best on this example" : "",
                ].filter(Boolean);
              },
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            offset: true,
            min: -0.5,
            max: currentExamples.length - 0.5,
            ticks: { display: false },
            grid: { display: false },
            title: { display: true, text: `Examples (${currentExamples.length})` },
          },
          y: {
            type: "linear",
            offset: true,
            min: -0.5,
            max: currentDefinitions.length - 0.5,
            ticks: {
              callback: (value: number | string) => {
                const def = definitionsRef.current[Math.round(Number(value))];
                return def ? formatDigest(def.image_digest) : "";
              },
              autoSkip: false,
              font: { family: "monospace", size: 10 },
            },
            grid: { display: false },
            reverse: true,
          },
        },
        animation: false,
      },
    });
    chartRef.current = chart;

    return () => {
      chart.destroy();
      chartRef.current = null;
    };
  }, [definitions.length, examples.length]);

  useEffect(() => {
    const chart = chartRef.current;
    if (chart) {
      chart.data.datasets[0].data = matrixData as unknown as ChartDataset["data"];
      chart.update("none");
    }
  }, [matrixData]);

  return (
    <Paper withBorder radius="md" p="md" className="bg-white dark:bg-gray-900 dark:border-gray-700">
      <Text component="h3" size="sm" fw={600} mb="xs">
        Definition Coverage Heatmap
      </Text>
      <Group gap="md" className="text-xs text-gray-500 dark:text-gray-400 mb-2">
        <Group gap={4}>
          <span className="inline-block w-3 h-3 rounded" style={{ background: "rgba(22, 163, 74, 0.8)" }} />
          <Text size="xs">Best</Text>
        </Group>
        <Group gap={4}>
          <span className="inline-block w-3 h-3 rounded" style={{ background: "rgba(209, 213, 219, 0.6)" }} />
          <Text size="xs">Evaluated</Text>
        </Group>
        <Group gap={4}>
          <span className="inline-block w-3 h-3 rounded" style={{ background: "rgba(243, 244, 246, 1)" }} />
          <Text size="xs">Not evaluated</Text>
        </Group>
      </Group>
      <div className="flex gap-2">
        <div
          className="flex flex-col justify-around text-xs text-gray-600 dark:text-gray-400 font-mono"
          style={{ minWidth: 100 }}
        >
          {definitions.map((definition) => (
            <div
              key={definition.image_digest}
              className="flex items-center gap-1 truncate"
              title={definition.image_digest}
            >
              {formatDigest(definition.image_digest)} ({definition.best_on_count})
            </div>
          ))}
        </div>
        <div className="flex-1" style={{ height: Math.max(definitions.length * 24, 120) }}>
          <canvas ref={canvasRef} />
        </div>
      </div>
    </Paper>
  );
}
