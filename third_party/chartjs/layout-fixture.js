import Chart from "chart.js/auto";

const labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const originalBars = [16, 7, 14, 10, 20, 9, 13];
const probe = {
  drawCount: 0,
  lastDrawnHeight: null,
  lastDrawnWidth: null,
  chart: null,
};

window.chartProbe = {
  get drawCount() {
    return probe.drawCount;
  },
  get lastDrawnHeight() {
    return probe.lastDrawnHeight;
  },
  get lastDrawnWidth() {
    return probe.lastDrawnWidth;
  },
  hasDrawnAfter({ previousCount, expectedWidth = null }) {
    return (
      probe.drawCount > previousCount &&
      probe.lastDrawnHeight === 250 &&
      (expectedWidth === null || probe.lastDrawnWidth === expectedWidth)
    );
  },
  snapshot() {
    const { chart } = probe;
    const rect = chart.canvas.getBoundingClientRect();
    const scaleSnapshot = (scale) => ({
      bounds: {
        left: scale.left,
        right: scale.right,
        top: scale.top,
        bottom: scale.bottom,
      },
      ticks: scale.ticks.map((tick, index) => ({
        label: tick.label,
        pixel: scale.getPixelForTick(index),
      })),
      title: scale.options.title?.text ?? null,
    });
    return {
      png: chart.canvas.toDataURL("image/png"),
      geometry: {
        chart: {
          width: chart.width,
          height: chart.height,
          devicePixelRatio: chart.currentDevicePixelRatio,
        },
        canvas: {
          bitmapWidth: chart.canvas.width,
          bitmapHeight: chart.canvas.height,
          rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
        },
        chartArea: { ...chart.chartArea },
        scales: {
          x: scaleSnapshot(chart.scales.x),
          y: scaleSnapshot(chart.scales.y),
        },
      },
    };
  },
  setHostWidth(width) {
    document.getElementById("host").style.width = `${width}px`;
  },
  setBarValue(index, value) {
    probe.chart.data.datasets[0].data[index] = value;
    probe.chart.update("none");
  },
  setLineBorderWidth(width) {
    probe.chart.data.datasets[1].targetBorderWidth = width;
    probe.chart.data.datasets[2].borderWidth = width;
    probe.chart.update("none");
  },
  setDatasetActive(active) {
    const meta = probe.chart.getDatasetMeta(1);
    if (active) {
      meta.controller.setHoverStyle(meta.dataset, meta.index, undefined);
      probe.chart.update("active");
    } else {
      meta.controller.removeHoverStyle(meta.dataset, meta.index, undefined);
      probe.chart.update("none");
    }
  },
  update(mode = "none") {
    probe.chart.update(mode);
  },
};

const hostWidth = Number(new URLSearchParams(window.location.search).get("width") ?? 292);
document.getElementById("host").style.width = `${hostWidth}px`;

probe.chart = new Chart(document.getElementById("chart"), {
  type: "bar",
  data: {
    labels,
    datasets: [
      {
        label: "Spend",
        data: [...originalBars],
        backgroundColor: "#4b83d1",
        borderWidth: 0,
      },
      {
        type: "line",
        label: "Daily target",
        data: [12, 12, 12, 12, 12, 12, 12],
        borderColor: "#e47922",
        borderDash: [5, 3],
        targetBorderWidth: 2,
        borderWidth: (context) => context.dataset.targetBorderWidth,
        hoverBorderWidth: 4,
        pointRadius: 0,
        pointHoverRadius: 0,
        fill: false,
      },
      {
        type: "line",
        label: "Upper target",
        data: [18, 18, 18, 18, 18, 18, 18],
        borderColor: "#8c64c7",
        borderWidth: 2,
        hoverBorderWidth: 4,
        pointRadius: 0,
        pointHoverRadius: 0,
        fill: false,
      },
    ],
  },
  options: {
    animation: false,
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: false } },
    scales: {
      x: {
        offset: true,
        ticks: { autoSkip: true, maxTicksLimit: 4, minRotation: 0, maxRotation: 0 },
      },
      y: { title: { display: true, text: "Spend ($)" } },
    },
  },
  plugins: [
    {
      id: "layoutFixtureProbe",
      afterDraw(chart) {
        probe.drawCount += 1;
        probe.lastDrawnWidth = chart.width;
        probe.lastDrawnHeight = chart.height;
      },
    },
  ],
});
