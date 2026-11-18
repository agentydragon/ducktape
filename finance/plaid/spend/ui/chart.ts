import {
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  type ChartConfiguration,
  LinearScale,
  LineController,
  LineElement,
  PointElement,
  Tooltip,
} from "chart.js";

Chart.register(CategoryScale, LinearScale, LineController, LineElement, PointElement, Tooltip);
Chart.register(BarController, BarElement);

const interaction = { mode: "index", intersect: false } as const;
const options = { responsive: true, animation: false, maintainAspectRatio: false, interaction } as const;

type SpendChartType = "bar" | "line";
type ChartColorScheme = "light" | "dark";

export const chartPalette = {
  light: {
    series: ["#1769AA", "#8B3F7D", "#087E73", "#A75413", "#5548AB", "#B13236", "#596D1F", "#087A93", "#9D3470"],
    netWorth: "#087E73",
    leash: "#79610A",
    reference: "#62707D",
    emphasis: "#111827",
  },
  dark: {
    series: ["#74BAF1", "#EB9DD9", "#57D6C2", "#FFB77C", "#B3A6FC", "#FF9090", "#BCD46E", "#69CFE8", "#F889BA"],
    netWorth: "#57D6C2",
    leash: "#E5C756",
    reference: "#A0ABB8",
    emphasis: "#F3F4F6",
  },
};

export function seriesColor(scheme: ChartColorScheme, index: number): string {
  const hue = (index * 137.5) % 360;
  return chartPalette[scheme].series[index] ?? `hsl(${hue} 72% ${scheme === "dark" ? 70 : 35}%)`;
}

const standardAveragePositions: Readonly<Partial<Record<number, number>>> = { 7: 1, 30: 0, 90: 3, 365: 2 };

export function movingAverageColor(scheme: ChartColorScheme, days: number, index = 0): string {
  return seriesColor(scheme, standardAveragePositions[days] ?? index + 4);
}

export function mountChart(canvas: HTMLCanvasElement, config: ChartConfiguration<SpendChartType>) {
  const chart = new Chart<SpendChartType>(canvas, { ...config, options: { ...options, ...config.options } });
  return () => chart.destroy();
}
