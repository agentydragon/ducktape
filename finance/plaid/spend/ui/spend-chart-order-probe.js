(() => {
  const events = [];
  const record = (kind, details) => {
    events.push({ sequence: events.length, kind, ...details });
  };

  const NativeResizeObserver = window.ResizeObserver;
  window.ResizeObserver = class extends NativeResizeObserver {
    constructor(callback) {
      super((entries, observer) => {
        record("resize-observer", {
          entries: entries.map((entry) => ({
            width: entry.contentRect.width,
            height: entry.contentRect.height,
          })),
        });
        callback(entries, observer);
      });
    }
  };

  const plugin = {
    id: "spend-chart-order-probe",
    beforeUpdate(chart, args) {
      record("before-update", {
        mode: args.mode ?? null,
        stack: new Error().stack ?? null,
        width: chart.width,
        height: chart.height,
        cssRect: rect(chart.canvas),
      });
    },
    afterDraw(chart) {
      const area = chart.chartArea;
      record("after-draw", {
        width: chart.width,
        height: chart.height,
        dpr: chart.currentDevicePixelRatio,
        minPadding: chart._minPadding,
        area: area && {
          left: area.left,
          right: area.right,
          top: area.top,
          bottom: area.bottom,
          width: area.width,
          height: area.height,
        },
        cssRect: rect(chart.canvas),
      });
    },
  };

  function rect(element) {
    const bounds = element.getBoundingClientRect();
    return { left: bounds.left, top: bounds.top, width: bounds.width, height: bounds.height };
  }

  window.__spendChartProbe = { events };
  window.__spendChartProbePlugins = [plugin];
})();
