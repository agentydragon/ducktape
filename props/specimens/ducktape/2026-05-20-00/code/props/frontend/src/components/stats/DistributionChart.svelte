<script lang="ts">
  import { onMount } from "svelte";
  import { Chart, BarController, BarElement, CategoryScale, LinearScale, Tooltip, Title } from "chart.js";

  Chart.register(BarController, BarElement, CategoryScale, LinearScale, Tooltip, Title);

  interface Props {
    values: number[];
    title: string;
    numBuckets?: number;
    valueFormat?: (v: number) => string;
    color?: string;
  }

  let {
    values,
    title,
    numBuckets = 10,
    valueFormat = (v: number) => `${(v * 100).toFixed(1)}%`,
    color = "rgb(59, 130, 246)",
  }: Props = $props();

  let canvas: HTMLCanvasElement;
  let chart: Chart | null = $state(null);

  // Compute buckets from values
  const buckets = $derived.by(() => {
    if (values.length === 0) return { labels: [], counts: [] };
    const sorted = [...values].sort((a, b) => a - b);
    const min = sorted[0];
    const max = sorted[sorted.length - 1];
    if (min === max) return { labels: [valueFormat(min)], counts: [values.length] };

    const width = (max - min) / numBuckets;
    const labels: string[] = [];
    const counts: number[] = [];
    for (let i = 0; i < numBuckets; i++) {
      const low = min + i * width;
      const high = min + (i + 1) * width;
      labels.push(`[${valueFormat(low)}, ${valueFormat(high)})`);
      counts.push(values.filter((v) => v >= low && (i === numBuckets - 1 ? v <= high : v < high)).length);
    }
    return { labels, counts };
  });

  // Summary stats
  const stats = $derived.by(() => {
    if (values.length === 0) return null;
    const sorted = [...values].sort((a, b) => a - b);
    const n = sorted.length;
    const mean = sorted.reduce((a, b) => a + b, 0) / n;
    const median = n % 2 === 0 ? (sorted[n / 2 - 1] + sorted[n / 2]) / 2 : sorted[Math.floor(n / 2)];
    const p10 = sorted[Math.floor(n * 0.1)];
    const p90 = sorted[Math.floor(n * 0.9)];
    return { n, mean, median, p10, p90 };
  });

  onMount(() => {
    chart = new Chart(canvas, {
      type: "bar",
      data: {
        labels: buckets.labels,
        datasets: [
          {
            data: buckets.counts,
            backgroundColor: color,
            borderWidth: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: { display: true, text: title, font: { size: 14 } },
          tooltip: {
            callbacks: {
              label: (ctx) => `${ctx.parsed.y} examples`,
            },
          },
        },
        scales: {
          x: { grid: { display: false } },
          y: { beginAtZero: true, title: { display: true, text: "Count" } },
        },
        animation: false,
      },
    });

    return () => {
      chart?.destroy();
    };
  });

  // Update chart when data changes
  $effect(() => {
    if (chart) {
      chart.data.labels = buckets.labels;
      chart.data.datasets[0].data = buckets.counts;
      chart.options!.plugins!.title!.text = title;
      chart.update("none");
    }
  });
</script>

<div class="bg-white dark:bg-gray-900 rounded-lg border dark:border-gray-700 p-4">
  {#if stats}
    <div class="text-xs text-gray-500 dark:text-gray-400 mb-2">
      N={stats.n} · μ={valueFormat(stats.mean)} · median={valueFormat(stats.median)} · P10={valueFormat(stats.p10)} · P90={valueFormat(
        stats.p90
      )}
    </div>
  {/if}
  <div style="height: 200px;">
    <canvas bind:this={canvas}></canvas>
  </div>
</div>
