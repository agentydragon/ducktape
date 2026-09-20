import { useEffect, useMemo, useRef } from "react";
import { Chart, BarController, BarElement, CategoryScale, LinearScale, Tooltip, Title } from "chart.js";
import { Paper, Text } from "@mantine/core";

Chart.register(BarController, BarElement, CategoryScale, LinearScale, Tooltip, Title);

interface Props {
  values: number[];
  title: string;
  numBuckets?: number;
  valueFormat?: (value: number) => string;
  color?: string;
}

export default function DistributionChart({
  values,
  title,
  numBuckets = 10,
  valueFormat = (value: number) => `${(value * 100).toFixed(1)}%`,
  color = "rgb(59, 130, 246)",
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const chartConfigRef = useRef({ buckets: { labels: [] as string[], counts: [] as number[] }, title, color });

  const buckets = useMemo(() => {
    if (values.length === 0) return { labels: [] as string[], counts: [] as number[] };
    const sorted = [...values].sort((a, b) => a - b);
    const min = sorted[0];
    const max = sorted[sorted.length - 1];
    if (min === max) return { labels: [valueFormat(min)], counts: [values.length] };

    const width = (max - min) / numBuckets;
    const labels: string[] = [];
    const counts: number[] = [];
    for (let index = 0; index < numBuckets; index++) {
      const low = min + index * width;
      const high = min + (index + 1) * width;
      labels.push(`[${valueFormat(low)}, ${valueFormat(high)})`);
      counts.push(
        values.filter((value) => value >= low && (index === numBuckets - 1 ? value <= high : value < high)).length
      );
    }
    return { labels, counts };
  }, [values, numBuckets, valueFormat]);

  const stats = useMemo(() => {
    if (values.length === 0) return null;
    const sorted = [...values].sort((a, b) => a - b);
    const n = sorted.length;
    const mean = sorted.reduce((sum, value) => sum + value, 0) / n;
    const median = n % 2 === 0 ? (sorted[n / 2 - 1] + sorted[n / 2]) / 2 : sorted[Math.floor(n / 2)];
    const p10 = sorted[Math.floor(n * 0.1)];
    const p90 = sorted[Math.floor(n * 0.9)];
    return { n, mean, median, p10, p90 };
  }, [values]);

  chartConfigRef.current = { buckets, title, color };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const initialConfig = chartConfigRef.current;

    const chart = new Chart(canvas, {
      type: "bar",
      data: {
        labels: initialConfig.buckets.labels,
        datasets: [{ data: initialConfig.buckets.counts, backgroundColor: initialConfig.color, borderWidth: 0 }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: { display: true, text: initialConfig.title, font: { size: 14 } },
          tooltip: { callbacks: { label: (context) => `${context.parsed.y} examples` } },
        },
        scales: {
          x: { grid: { display: false } },
          y: { beginAtZero: true, title: { display: true, text: "Count" } },
        },
        animation: false,
      },
    });
    chartRef.current = chart;

    return () => {
      chart.destroy();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (chart) {
      chart.data.labels = buckets.labels;
      chart.data.datasets[0].data = buckets.counts;
      chart.data.datasets[0].backgroundColor = color;
      chart.options.plugins!.title!.text = title;
      chart.update("none");
    }
  }, [buckets, color, title]);

  return (
    <Paper withBorder radius="md" p="md" className="bg-white dark:bg-gray-900 dark:border-gray-700">
      {stats && (
        <Text size="xs" c="dimmed" mb="xs">
          N={stats.n} · μ={valueFormat(stats.mean)} · median={valueFormat(stats.median)} · P10={valueFormat(stats.p10)}{" "}
          · P90={valueFormat(stats.p90)}
        </Text>
      )}
      <div style={{ height: 200 }}>
        <canvas ref={canvasRef} />
      </div>
    </Paper>
  );
}
