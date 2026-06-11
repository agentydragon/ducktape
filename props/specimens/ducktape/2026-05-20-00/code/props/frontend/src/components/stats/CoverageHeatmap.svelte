<script lang="ts">
  import { onMount } from "svelte";
  import { Chart, Tooltip, LinearScale } from "chart.js";
  import { MatrixController, MatrixElement } from "chartjs-chart-matrix";
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

  let { definitions, examples, cells }: Props = $props();

  let canvas: HTMLCanvasElement;
  let chart: Chart | null = $state(null);

  const matrixData = $derived(
    cells.map((c) => ({
      x: c.example_idx,
      y: c.definition_idx,
      v: c.recall,
      best: c.is_best,
    }))
  );

  onMount(() => {
    chart = new Chart(canvas, {
      type: "matrix",
      data: {
        datasets: [
          {
            data: matrixData as any,
            backgroundColor(ctx: any) {
              const v = ctx.dataset.data[ctx.dataIndex];
              if (!v) return "rgba(243, 244, 246, 1)"; // gray-100
              if (v.best) {
                // Green intensity based on recall
                const alpha = 0.3 + v.v * 0.7;
                return `rgba(22, 163, 74, ${alpha})`;
              }
              // Light gray for evaluated-but-not-best
              return `rgba(209, 213, 219, ${0.3 + v.v * 0.5})`;
            },
            width: ({ chart }: any) => {
              const xScale = chart.scales.x;
              return Math.max(xScale.width / (examples.length + 1) - 1, 4);
            },
            height: ({ chart }: any) => {
              const yScale = chart.scales.y;
              return Math.max(yScale.height / (definitions.length + 1) - 1, 12);
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
              label(ctx: any) {
                const d = ctx.dataset.data[ctx.dataIndex];
                const def = definitions[d.y];
                const ex = examples[d.x];
                const slug = ex.snapshot_slug.length > 20 ? ex.snapshot_slug.slice(0, 20) + "…" : ex.snapshot_slug;
                return [
                  `Def: ${formatDigest(def.image_digest)}`,
                  `Example: ${slug}`,
                  `Recall: ${(d.v * 100).toFixed(1)}%`,
                  d.best ? "★ Best on this example" : "",
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
            max: examples.length - 0.5,
            ticks: { display: false },
            grid: { display: false },
            title: { display: true, text: `Examples (${examples.length})` },
          },
          y: {
            type: "linear",
            offset: true,
            min: -0.5,
            max: definitions.length - 0.5,
            ticks: {
              callback: (val: any) => {
                const def = definitions[Math.round(val)];
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

    return () => {
      chart?.destroy();
    };
  });

  $effect(() => {
    if (chart) {
      (chart.data.datasets[0].data as any) = matrixData;
      chart.update("none");
    }
  });
</script>

<div class="bg-white dark:bg-gray-900 rounded-lg border dark:border-gray-700 p-4">
  <h3 class="text-sm font-semibold mb-2">Definition Coverage Heatmap</h3>
  <div class="flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400 mb-2">
    <span class="flex items-center gap-1">
      <span class="inline-block w-3 h-3 rounded" style="background: rgba(22, 163, 74, 0.8)"></span> Best
    </span>
    <span class="flex items-center gap-1">
      <span class="inline-block w-3 h-3 rounded" style="background: rgba(209, 213, 219, 0.6)"></span> Evaluated
    </span>
    <span class="flex items-center gap-1">
      <span class="inline-block w-3 h-3 rounded" style="background: rgba(243, 244, 246, 1)"></span> Not evaluated
    </span>
  </div>
  <!-- Right side: definition labels -->
  <div class="flex gap-2">
    <div
      class="flex flex-col justify-around text-xs text-gray-600 dark:text-gray-400 font-mono"
      style="min-width: 100px;"
    >
      {#each definitions as def}
        <div class="flex items-center gap-1 truncate" title={def.image_digest}>
          {formatDigest(def.image_digest)} ({def.best_on_count})
        </div>
      {/each}
    </div>
    <div class="flex-1" style="height: {Math.max(definitions.length * 24, 120)}px;">
      <canvas bind:this={canvas}></canvas>
    </div>
  </div>
</div>
