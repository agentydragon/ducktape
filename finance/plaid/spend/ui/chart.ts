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

export function mountChart(canvas: HTMLCanvasElement, config: ChartConfiguration<SpendChartType>) {
  const chart = new Chart<SpendChartType>(canvas, { ...config, options: { ...options, ...config.options } });
  return () => chart.destroy();
}
